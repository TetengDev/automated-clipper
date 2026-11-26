"""The commentary `discovering` stage (CLP-144): Twitch scan, virality bar, score, filters,
trending streamers, storage and the quiet-day skip."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from clipper.commentary import discovery
from clipper.commentary.config import Channel, CommentaryConfig, CommentarySettings
from clipper.commentary.discovery import (
    DiscoveryError,
    DiscoveryStage,
    Scan,
    candidate_score,
    evaluate,
    passes_virality_bar,
    scan_twitch,
    velocity,
)
from clipper.config import AppConfig
from clipper.db import connect, migrate
from clipper.jobs.models import Job, JobStatus
from clipper.jobs.queue import enqueue
from clipper.jobs.stages import DEFAULT_STAGES, StageDef
from clipper.jobs.worker import Worker
from clipper.pipeline import build_commentary_stages
from clipper.pipeline.commentary import NOT_BUILT
from clipper.sources import twitch
from clipper.sources.twitch import ChannelInfo, Clip, Helix
from clipper.urls import SourceRef

NOW = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)
GTA = "1867279146"
OTHER_GAME = "33214"
KAI = "641972806"
SECRET = "SENTINEL-client-secret-7f3a"
TOKEN = "SENTINEL-access-token-91bc"
ACCOUNT = "random_gaming_yt"
DAY1 = SourceRef("commentary", f"{ACCOUNT}:2026-11-25")
DAY2 = SourceRef("commentary", f"{ACCOUNT}:2026-11-26")


def make_clip(
    clip_id: str,
    *,
    views: int = 12000,
    age_h: float = 2.0,
    broadcaster: str = "100",
    name: str = "Streamer100",
    game: str = GTA,
    language: str = "en",
    duration: float = 30.0,
) -> Clip:
    return Clip(
        id=clip_id,
        url=f"https://clips.twitch.tv/{clip_id}",
        broadcaster_id=broadcaster,
        broadcaster_name=name,
        game_id=game,
        language=language,
        title=f"title {clip_id}",
        view_count=views,
        created_at=NOW - timedelta(hours=age_h),
        duration_s=duration,
    )


def info(
    broadcaster: str, login: str | None = None, *, labels: Sequence[str] = (), game: str = GTA
) -> ChannelInfo:
    login = login or f"streamer{broadcaster}"
    return ChannelInfo(broadcaster, login, login.title(), game, frozenset(labels))


def make_channel(**extra: Any) -> Channel:
    data: dict[str, Any] = {
        "account": ACCOUNT,
        "source": "twitch_clips",
        "prompt": "commentary_gaming_v1",
        "games": [{"name": "Grand Theft Auto VI", "game_id": GTA}],
        "streamers": [{"name": "Kai Cenat", "twitch": "kaicenat", "user_id": KAI}],
        "exclude_streamers": [],
    }
    data.update(extra)
    return Channel.model_validate(data)


def make_scan(
    clips: Sequence[Clip],
    channels: Mapping[str, ChannelInfo] | None = None,
    *,
    top: Sequence[str] = (),
    pinned: Sequence[str] = (KAI,),
    games: Sequence[str] = (GTA,),
) -> Scan:
    if channels is None:
        channels = {c.broadcaster_id: info(c.broadcaster_id) for c in clips}
    return Scan(tuple(clips), dict(channels), frozenset(top), frozenset(pinned), frozenset(games))


def settings(**changes: Any) -> CommentarySettings:
    return CommentarySettings.model_validate(changes)


def reasons(result: discovery.Discovery) -> dict[str, str | None]:
    return {c.clip.id: c.skip_reason for c in result.candidates}


# --- Score and virality bar -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("views", "age_h", "expected"),
    [(2000, 2.0, 1000.0), (500, 0.5, 500.0), (500, 0.0, 500.0), (4800, 24.0, 200.0)],
)
def test_velocity_is_views_per_hour_with_age_at_least_one_hour(
    views: int, age_h: float, expected: float
) -> None:
    assert velocity(make_clip("c", views=views, age_h=age_h), NOW) == pytest.approx(expected)


@pytest.mark.parametrize(
    ("trending", "pinned", "expected"),
    [(False, False, 1000.0), (True, False, 1500.0), (False, True, 1500.0), (True, True, 2000.0)],
)
def test_score_is_velocity_times_one_plus_the_boosts(
    trending: bool, pinned: bool, expected: float
) -> None:
    clip = make_clip("c", views=2000, age_h=2.0)

    score = candidate_score(clip, NOW, trending=trending, pinned=pinned, settings=settings())

    assert score == pytest.approx(expected)


def test_score_uses_the_configured_boosts() -> None:
    clip = make_clip("c", views=2000, age_h=2.0)
    tuned = settings(trending_boost=0.25, pinned_boost=2.0)

    assert candidate_score(clip, NOW, trending=True, pinned=True, settings=tuned) == 3250.0


@pytest.mark.parametrize(
    ("views", "age_h", "passes"),
    [
        (10000, 30.0, True),  # the views arm: 333/h, below the rate
        (9999, 30.0, False),  # neither arm
        (800, 2.0, True),  # the rate arm: exactly 400/h
        (799, 2.0, False),  # 399.5/h, neither
        (300, 0.5, False),  # age counts as 1 h: 300/h
        (5000, 20.0, False),  # 250/h, 5000 views: neither
    ],
)
def test_virality_bar_needs_views_or_views_per_hour(views: int, age_h: float, passes: bool) -> None:
    clip = make_clip("c", views=views, age_h=age_h)

    assert passes_virality_bar(clip, NOW, settings()) is passes


def test_virality_bar_follows_the_settings() -> None:
    clip = make_clip("c", views=1500, age_h=10.0)  # 150/h

    assert passes_virality_bar(clip, NOW, settings(min_clip_views=1500)) is True
    assert passes_virality_bar(clip, NOW, settings(min_views_per_hour=150)) is True
    assert passes_virality_bar(clip, NOW, settings(min_views_per_hour=151)) is False


# --- Filters --------------------------------------------------------------------------------


def test_a_clip_below_the_bar_is_kept_as_a_row_with_its_reason() -> None:
    result = evaluate(
        make_scan([make_clip("low", views=500, age_h=20.0)]), make_channel(), settings(), NOW
    )

    (only,) = result.candidates
    assert only.skip_reason == "below the virality bar"
    assert only.score == pytest.approx(25.0)
    assert result.chosen == ()


def test_no_score_floor_by_default() -> None:
    clip = make_clip("slow", views=10000, age_h=23.0)  # passes on views; score 434.8

    result = evaluate(make_scan([clip]), make_channel(), settings(), NOW)

    assert reasons(result) == {"slow": None}


def test_score_floor_drops_clips_below_it() -> None:
    clips = [make_clip("under", views=2000, age_h=4.0), make_clip("at", views=2000, age_h=2.0)]

    result = evaluate(make_scan(clips), make_channel(), settings(min_score=1000), NOW)

    assert reasons(result) == {"at": None, "under": "score below min_score"}


@pytest.mark.parametrize(
    ("clip", "channel_info", "channel_extra", "tuned", "reason"),
    [
        (make_clip("c", duration=4.9), None, {}, {}, "shorter than 5 s"),
        (make_clip("c", game=OTHER_GAME), None, {}, {}, "not one of the channel's games"),
        (make_clip("c", language="de"), None, {}, {}, "language is not en"),
        (make_clip("c", language=""), None, {}, {}, "language is not en"),
        (make_clip("c"), None, {"exclude_streamers": ["streamer100"]}, {}, "excluded streamer"),
        (
            make_clip("c"),
            info("100", "loudguy"),
            {"exclude_streamers": ["loudguy"]},
            {},
            "excluded streamer",
        ),
        (
            make_clip("c"),
            info("100", labels=["SexualThemes"]),
            {},
            {},
            "channel labelled SexualThemes",
        ),
        (make_clip("c"), info("100", labels=["Gambling"]), {}, {}, "channel labelled Gambling"),
        (make_clip("c"), info("100", game="498566"), {}, {}, "gambling or excluded game"),
        (
            make_clip("c"),
            info("100", game="27471"),
            {},
            {"excluded_game_ids": ["27471"]},
            "gambling or excluded game",
        ),
    ],
)
def test_each_filter_skips_the_clip_with_its_reason(
    clip: Clip,
    channel_info: ChannelInfo | None,
    channel_extra: dict[str, Any],
    tuned: dict[str, Any],
    reason: str,
) -> None:
    channels = {clip.broadcaster_id: channel_info or info(clip.broadcaster_id)}

    result = evaluate(
        make_scan([clip], channels), make_channel(**channel_extra), settings(**tuned), NOW
    )

    assert reasons(result) == {"c": reason}


def test_english_variants_count_as_english() -> None:
    clips = [make_clip("upper", language="EN"), make_clip("gb", language="en-gb")]

    result = evaluate(make_scan(clips), make_channel(), settings(), NOW)

    assert reasons(result) == {"upper": None, "gb": None}


def test_other_labels_do_not_skip() -> None:
    clip = make_clip("c")
    channels = {"100": info("100", labels=["ProfanityVulgarity", "MatureGame"])}

    assert reasons(evaluate(make_scan([clip], channels), make_channel(), settings(), NOW)) == {
        "c": None
    }


def test_exclude_list_is_case_insensitive_on_login_and_display_name() -> None:
    clips = [
        make_clip("by_login", broadcaster="1"),
        make_clip("by_name", broadcaster="2", name="TheLoudOne"),
    ]
    channels = {"1": info("1", "SomeLogin"), "2": info("2", "otherlogin")}
    channel = make_channel(exclude_streamers=["somelogin", "theloudone"])

    result = evaluate(make_scan(clips, channels), channel, settings(), NOW)

    assert reasons(result) == {"by_login": "excluded streamer", "by_name": "excluded streamer"}


def test_a_clip_without_channel_information_is_skipped() -> None:
    result = evaluate(make_scan([make_clip("c")], {}), make_channel(), settings(), NOW)

    assert reasons(result) == {"c": "no channel information"}
    assert result.candidates[0].login == "streamer100"  # the display name, lower case


def test_a_clip_already_used_or_taken_is_skipped_with_the_given_reason() -> None:
    clips = [make_clip("used"), make_clip("fresh")]

    result = evaluate(
        make_scan(clips),
        make_channel(),
        settings(),
        NOW,
        blocked={"used": "already used in a commentary Short"},
    )

    assert reasons(result) == {"fresh": None, "used": "already used in a commentary Short"}


def test_a_pinned_streamers_clip_of_another_game_does_not_count() -> None:
    clips = [make_clip("kai_other", broadcaster=KAI, game=OTHER_GAME)]

    result = evaluate(
        make_scan(clips, {KAI: info(KAI, "kaicenat")}), make_channel(), settings(), NOW
    )

    assert reasons(result) == {"kai_other": "not one of the channel's games"}
    assert result.rankings == ()


# --- Order, top N ---------------------------------------------------------------------------


def test_pinned_clips_rank_above_every_other_clip() -> None:
    clips = [
        make_clip("big", views=90000, broadcaster="1"),
        make_clip("kai", views=12000, broadcaster=KAI),
        make_clip("mid", views=40000, broadcaster="2"),
    ]
    channels = {"1": info("1"), "2": info("2"), KAI: info(KAI, "kaicenat")}

    result = evaluate(make_scan(clips, channels), make_channel(), settings(), NOW)

    assert [c.clip.id for c in result.chosen] == ["kai", "big", "mid"]
    assert [c.pinned for c in result.chosen] == [True, False, False]


def test_a_pin_is_matched_by_login_when_its_user_id_is_unknown() -> None:
    clips = [make_clip("big", views=90000, broadcaster="1"), make_clip("kai", broadcaster="777")]
    channels = {"1": info("1"), "777": info("777", "kaicenat")}

    result = evaluate(make_scan(clips, channels, pinned=()), make_channel(), settings(), NOW)

    assert [c.clip.id for c in result.chosen] == ["kai", "big"]


def test_pinned_boost_and_trending_boost_order_within_each_group() -> None:
    clips = [
        make_clip("plain", views=2400, broadcaster="1"),  # 1200/h
        make_clip("trend", views=2000, broadcaster="2", game=GTA),  # 1000/h x 1.5
    ]
    channels = {"1": info("1"), "2": info("2")}
    channel = make_channel(
        games=[
            {"name": "Grand Theft Auto VI", "game_id": GTA},
            {"name": "Fortnite", "game_id": "1"},
        ]
    )
    clips[0] = replace(clips[0], game_id="1")

    scan = make_scan(clips, channels, top=[GTA], games=[GTA, "1"])

    result = evaluate(scan, channel, settings(), NOW)

    assert [(c.clip.id, c.trending, c.score) for c in result.chosen] == [
        ("trend", True, 1500.0),
        ("plain", False, 1200.0),
    ]


def test_equal_scores_put_the_newer_clip_first() -> None:
    clips = [
        make_clip("older", views=4000, age_h=4.0, broadcaster="1"),
        make_clip("newer", views=2000, age_h=2.0, broadcaster="2"),
    ]

    result = evaluate(make_scan(clips), make_channel(), settings(), NOW)

    assert [c.clip.id for c in result.chosen] == ["newer", "older"]


def test_only_the_top_candidates_per_day_go_on() -> None:
    clips = [make_clip(f"c{n}", views=10000 + n, broadcaster=str(n)) for n in range(1, 5)]

    result = evaluate(
        make_scan(clips), make_channel(), settings(candidates_per_day=2, stories_per_day=1), NOW
    )

    assert [c.clip.id for c in result.chosen] == ["c4", "c3"]
    assert reasons(result) == {
        "c4": None,
        "c3": None,
        "c2": "below today's top 2",
        "c1": "below today's top 2",
    }


# --- Trending streamers ---------------------------------------------------------------------


def test_streamers_rank_by_summed_velocity_and_keep_the_top_plus_every_pin() -> None:
    clips = [
        make_clip("a1", views=3000, age_h=1.0, broadcaster="1"),  # 3000/h
        make_clip("b1", views=2000, age_h=2.0, broadcaster="2"),  # 1000/h
        make_clip("b2", views=6000, age_h=2.0, broadcaster="2"),  # 3000/h -> b: 4000/h
        make_clip("c1", views=12000, age_h=6.0, broadcaster="3"),  # 2000/h
        make_clip("k1", views=11000, age_h=11.0, broadcaster=KAI),  # 1000/h, rank 4
        make_clip("x1", views=99000, age_h=1.0, broadcaster="9"),  # excluded
    ]
    channels = {
        **{b: info(b) for b in ("1", "2", "3")},
        KAI: info(KAI, "kaicenat"),
        "9": info("9", "noisy"),
    }
    channel = make_channel(exclude_streamers=["noisy"])

    result = evaluate(make_scan(clips, channels), channel, settings(max_streamers_scanned=2), NOW)

    ranked = [(r.rank, r.login, r.clip_count, r.view_count, r.pinned) for r in result.rankings]
    assert ranked == [
        (1, "streamer2", 2, 8000, False),
        (2, "streamer1", 1, 3000, False),
        (4, "kaicenat", 1, 11000, True),
    ]
    assert [r.views_per_hour for r in result.rankings] == [4000.0, 3000.0, 1000.0]
    assert reasons(result) == {
        "k1": None,
        "b2": None,
        "a1": None,
        "b1": None,
        "c1": "streamer not trending today",
        "x1": "excluded streamer",
    }


def test_ranking_counts_only_usable_clips() -> None:
    """Owner, 2026-11-26: only clips that pass the hard filters rank a streamer. The
    virality bar, the score floor and "already used" do not matter for the ranking."""
    clips = [
        make_clip("de1", views=90000, age_h=1.0, broadcaster="1", language="de"),
        make_clip("de2", views=80000, age_h=1.0, broadcaster="2", language="de"),
        make_clip("short", views=70000, age_h=1.0, broadcaster="5", duration=3.0),
        make_clip("en", views=12000, age_h=2.0, broadcaster="3"),  # 6000/h
        make_clip("low", views=500, age_h=20.0, broadcaster="4"),  # 25/h, below the bar
    ]
    channels = {b: info(b) for b in ("1", "2", "3", "4")}
    channels["5"] = info("5", labels=["MatureGame"])

    result = evaluate(
        make_scan(clips, channels), make_channel(), settings(max_streamers_scanned=2), NOW
    )

    assert [(r.rank, r.login) for r in result.rankings] == [
        (1, "streamer3"),
        (2, "streamer4"),
    ]
    assert reasons(result) == {
        "en": None,
        "de1": "language is not en",
        "de2": "language is not en",
        "short": "shorter than 5 s",
        "low": "below the virality bar",
    }


@pytest.mark.parametrize(
    ("clip", "channel_info"),
    [
        (make_clip("x", game=OTHER_GAME), None),
        (make_clip("x", language="fr"), None),
        (make_clip("x", duration=4.0), None),
        (make_clip("x"), info("100", labels=["SexualThemes"])),
        (make_clip("x"), info("100", labels=["Gambling"])),
        (make_clip("x"), info("100", game="498566")),
    ],
)
def test_a_clip_failing_a_hard_filter_ranks_nobody(
    clip: Clip, channel_info: ChannelInfo | None
) -> None:
    channels = {"100": channel_info or info("100")}

    result = evaluate(make_scan([clip], channels), make_channel(), settings(), NOW)

    assert result.rankings == ()


def test_used_and_below_bar_clips_still_rank_their_streamer() -> None:
    clips = [make_clip("used", broadcaster="1"), make_clip("low", views=10, broadcaster="2")]

    result = evaluate(
        make_scan(clips), make_channel(), settings(), NOW, blocked={"used": "already used"}
    )

    assert {r.login for r in result.rankings} == {"streamer1", "streamer2"}


# --- The Twitch scan ------------------------------------------------------------------------


@dataclass
class FakeTwitch:
    """A MockTransport handler standing in for id.twitch.tv and Helix."""

    by_game: dict[str, list[list[dict[str, object]]]] = field(default_factory=dict)
    by_broadcaster: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    top: list[str] = field(default_factory=list)
    channels: dict[str, dict[str, object]] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)
    games: dict[str, str] = field(default_factory=dict)
    fail: Callable[[httpx.Request], httpx.Response | None] | None = None
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail is not None:
            failed = self.fail(request)
            if failed is not None:
                return failed
        if request.url == twitch.TOKEN_URL:
            return httpx.Response(200, json={"access_token": TOKEN})
        params = request.url.params
        path = request.url.path
        if path == "/helix/clips":
            if "game_id" in params:
                pages = self.by_game.get(params["game_id"], [])
                page = int(params.get("after", "0"))
                rows = pages[page] if page < len(pages) else []
                more = {"cursor": str(page + 1)} if page + 1 < len(pages) else {}
                return httpx.Response(200, json={"data": rows, "pagination": more})
            rows = self.by_broadcaster.get(params["broadcaster_id"], [])
            return httpx.Response(200, json={"data": rows, "pagination": {}})
        if path == "/helix/games/top":
            return httpx.Response(200, json={"data": [{"id": g} for g in self.top]})
        if path == "/helix/channels":
            ids = params.get_list("broadcaster_id")
            return httpx.Response(
                200, json={"data": [self.channels[i] for i in ids if i in self.channels]}
            )
        if path == "/helix/users":
            logins = params.get_list("login")
            data = [{"login": x, "id": self.users[x]} for x in logins if x in self.users]
            return httpx.Response(200, json={"data": data})
        if path == "/helix/games":
            names = params.get_list("name")
            data = [{"name": n, "id": self.games[n]} for n in names if n in self.games]
            return httpx.Response(200, json={"data": data})
        raise AssertionError(f"unexpected request {request.url}")

    def helix(self) -> Helix:
        return Helix(httpx.Client(transport=httpx.MockTransport(self)), "cid", SECRET)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests if r.url != twitch.TOKEN_URL]


def clip_row(clip_id: str, **extra: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": clip_id,
        "url": f"https://clips.twitch.tv/{clip_id}",
        "broadcaster_id": "100",
        "broadcaster_name": "Streamer100",
        "game_id": GTA,
        "language": "en",
        "title": f"title {clip_id}",
        "view_count": 12000,
        "created_at": "2026-11-26T09:00:00Z",
        "duration": 30.0,
    }
    row.update(extra)
    return row


def channel_row(broadcaster: str, login: str, labels: Sequence[str] = ()) -> dict[str, object]:
    return {
        "broadcaster_id": broadcaster,
        "broadcaster_login": login,
        "broadcaster_name": login.title(),
        "game_id": GTA,
        "content_classification_labels": list(labels),
    }


def test_scan_reads_games_pins_top_games_and_channels() -> None:
    fake = FakeTwitch(
        by_game={GTA: [[clip_row("g1"), clip_row("both", broadcaster_id=KAI)], [clip_row("g2")]]},
        by_broadcaster={
            KAI: [clip_row("both", broadcaster_id=KAI), clip_row("k2", broadcaster_id=KAI)]
        },
        top=["509658", GTA],
        channels={"100": channel_row("100", "streamer100"), KAI: channel_row(KAI, "kaicenat")},
    )
    channel = make_channel(
        streamers=[
            {"name": "Kai Cenat", "twitch": "kaicenat", "user_id": KAI},
            {"name": "IShowSpeed", "youtube": {"handle": "@IShowSpeed"}},
        ]
    )

    scan = scan_twitch(fake.helix(), channel, NOW)

    assert [c.id for c in scan.clips] == ["g1", "both", "g2", "k2"]  # deduplicated
    assert scan.top_games == frozenset({"509658", GTA})
    assert scan.pinned_ids == frozenset({KAI})
    assert set(scan.channels) == {"100", KAI}
    clip_calls = [r for r in fake.requests if r.url.path == "/helix/clips"]
    game_calls = [r for r in clip_calls if "game_id" in r.url.params]
    pin_calls = [r for r in clip_calls if "broadcaster_id" in r.url.params]
    assert [r.url.params["first"] for r in game_calls] == ["100", "100"]
    assert [(r.url.params["broadcaster_id"], r.url.params["first"]) for r in pin_calls] == [
        (KAI, "20")
    ]
    for request in clip_calls:
        assert request.url.params["started_at"] == "2026-11-25T11:00:00Z"
        assert request.url.params["ended_at"] == "2026-11-26T11:00:00Z"
    assert fake.paths().count("/helix/games/top") == 1
    (channels_call,) = [r for r in fake.requests if r.url.path == "/helix/channels"]
    assert sorted(channels_call.url.params.get_list("broadcaster_id")) == sorted(["100", KAI])


def test_scan_stops_after_five_pages_per_game() -> None:
    pages = [[clip_row(f"p{n}")] for n in range(8)]
    fake = FakeTwitch(by_game={GTA: pages}, channels={"100": channel_row("100", "s")})

    scan = scan_twitch(fake.helix(), make_channel(streamers=[]), NOW)

    assert [c.id for c in scan.clips] == ["p0", "p1", "p2", "p3", "p4"]


def test_scan_looks_up_missing_game_and_user_ids() -> None:
    fake = FakeTwitch(
        by_game={GTA: [[clip_row("g1")]]},
        games={"Grand Theft Auto VI": GTA},
        users={"kaicenat": KAI},
        channels={"100": channel_row("100", "s")},
    )
    channel = make_channel(
        games=[{"name": "Grand Theft Auto VI"}],
        streamers=[{"name": "Kai Cenat", "twitch": "kaicenat"}],
    )

    scan = scan_twitch(fake.helix(), channel, NOW)

    assert [c.id for c in scan.clips] == ["g1"]
    assert scan.pinned_ids == frozenset({KAI})
    assert "/helix/games" in fake.paths()
    assert "/helix/users" in fake.paths()


def test_scan_fails_when_a_game_has_no_twitch_id() -> None:
    fake = FakeTwitch()
    channel = make_channel(games=[{"name": "Grand Theft Auto VI"}], streamers=[])

    with pytest.raises(DiscoveryError, match="Grand Theft Auto VI.*clipper resolve"):
        scan_twitch(fake.helix(), channel, NOW)


def test_a_pin_twitch_does_not_know_is_left_out_of_the_scan() -> None:
    fake = FakeTwitch(channels={})
    channel = make_channel(streamers=[{"name": "Gone", "twitch": "gonegone"}])

    scan = scan_twitch(fake.helix(), channel, NOW)

    assert scan.pinned_ids == frozenset()
    assert not [r for r in fake.requests if "broadcaster_id" in r.url.params]


# --- The stage, through the worker ----------------------------------------------------------


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    with closing(connect(path)) as conn:
        migrate(conn)
    return path


@pytest.fixture
def conn(db: Path) -> Iterator[sqlite3.Connection]:
    with closing(connect(db)) as c:
        yield c


def gta_day(fake: FakeTwitch) -> None:
    """A normal day: two viral GTA clips, one by Kai Cenat, and one that is not viral."""
    fake.by_game = {
        GTA: [
            [
                clip_row("viral", view_count=30000, broadcaster_id="100"),
                clip_row("flop", view_count=40, broadcaster_id="200", broadcaster_name="Flop"),
            ]
        ]
    }
    fake.by_broadcaster = {KAI: [clip_row("kai", broadcaster_id=KAI, broadcaster_name="KaiCenat")]}
    fake.top = [GTA]
    fake.channels = {
        "100": channel_row("100", "streamer100"),
        "200": channel_row("200", "flop"),
        KAI: {**channel_row(KAI, "kaicenat"), "broadcaster_name": "KaiCenat"},
    }


@dataclass
class Harness:
    db: Path
    fake: FakeTwitch
    clock: Clock
    worker: Worker
    alerts: list[str]
    channel: Channel

    def enqueue(self, ref: SourceRef = DAY2) -> Job:
        with closing(connect(self.db)) as c:
            job, _ = enqueue(c, ref, self.clock(), creator=ACCOUNT)
            c.execute("UPDATE jobs SET status = 'discovering' WHERE id = ?", (job.id,))
        return job

    def row(self, job_id: int) -> sqlite3.Row:
        with closing(connect(self.db)) as c:
            return c.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    def candidates(self) -> list[sqlite3.Row]:
        with closing(connect(self.db)) as c:
            return c.execute("SELECT * FROM story_candidates ORDER BY twitch_clip_id").fetchall()

    def rankings(self, job_id: int) -> list[sqlite3.Row]:
        with closing(connect(self.db)) as c:
            return c.execute(
                "SELECT * FROM streamer_rankings WHERE job_id = ? ORDER BY rank", (job_id,)
            ).fetchall()


@pytest.fixture
def harness(db: Path, tmp_path: Path) -> Iterator[Harness]:
    fake = FakeTwitch()
    gta_day(fake)
    clock = Clock(NOW)
    channel = make_channel()
    stage = DiscoveryStage(
        load=lambda ctx: CommentaryConfig(channels=[channel]),
        credentials=lambda: ("cid", SECRET),
        http=lambda: httpx.Client(transport=httpx.MockTransport(fake)),
    )
    alerts: list[str] = []
    c = connect(db)
    worker = Worker(
        conn=c,
        config=AppConfig(),
        worker_id="w1",
        stages=dict(DEFAULT_STAGES),
        media_root=tmp_path / "media",
        clock=clock,
        alert=alerts.append,
        commentary_stages={**DEFAULT_STAGES, JobStatus.DISCOVERING: StageDef(stage)},
    )
    yield Harness(db, fake, clock, worker, alerts, channel)
    c.close()


def test_a_normal_day_stores_every_clip_and_moves_on(harness: Harness) -> None:
    job = harness.enqueue()

    assert harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "fetching_sources"
    assert harness.alerts == []
    stored = {r["twitch_clip_id"]: r for r in harness.candidates()}
    assert set(stored) == {"viral", "flop", "kai"}
    assert stored["flop"]["skip_reason"] == "below the virality bar"
    assert stored["viral"]["skip_reason"] is None
    kai = stored["kai"]
    assert kai["skip_reason"] is None
    assert (kai["pinned"], kai["trending"]) == (1, 1)
    assert kai["broadcaster_login"] == "kaicenat"
    assert kai["broadcaster_name"] == "KaiCenat"
    assert kai["account_id"] == ACCOUNT
    assert kai["job_id"] == job.id
    assert kai["score"] == pytest.approx(12000 / 2 * 2.0)
    assert kai["clip_created_at"] == "2026-11-26T09:00:00Z"
    assert kai["url"] == "https://clips.twitch.tv/kai"
    assert (kai["view_count"], kai["duration_s"], kai["language"]) == (12000, 30.0, "en")
    ranks = [(r["rank"], r["broadcaster_login"], r["pinned"]) for r in harness.rankings(job.id)]
    assert ranks == [(1, "streamer100", 0), (2, "kaicenat", 1), (3, "flop", 0)]


def test_day_candidates_lists_the_chosen_clips_in_rank_order(harness: Harness) -> None:
    job = harness.enqueue()
    harness.worker.run_once()

    with closing(connect(harness.db)) as c:
        chosen = discovery.day_candidates(c, job.id)

    assert [r["twitch_clip_id"] for r in chosen] == ["kai", "viral"]


def test_a_quiet_day_is_skipped_without_an_alert(harness: Harness) -> None:
    harness.fake.by_game = {GTA: [[clip_row("flop", view_count=40)]]}
    harness.fake.by_broadcaster = {}
    job = harness.enqueue()

    assert harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "skipped"
    assert row["skip_reason"] == "no clip passed the virality bar today"
    assert row["last_error"] is None
    assert harness.alerts == []
    assert [(r["twitch_clip_id"], r["skip_reason"]) for r in harness.candidates()] == [
        ("flop", "below the virality bar")
    ]


def test_a_day_with_no_clips_at_all_is_skipped(harness: Harness) -> None:
    harness.fake.by_game = {}
    harness.fake.by_broadcaster = {}
    job = harness.enqueue()

    harness.worker.run_once()

    assert harness.row(job.id)["skip_reason"] == "no clip passed the virality bar today"
    assert harness.candidates() == []


def test_running_discovery_again_replaces_the_days_rows(harness: Harness) -> None:
    job = harness.enqueue()
    harness.worker.run_once()
    harness.fake.by_game = {GTA: [[clip_row("viral", view_count=31000, broadcaster_id="100")]]}
    harness.fake.by_broadcaster = {}
    with closing(connect(harness.db)) as c:
        c.execute("UPDATE jobs SET status = 'discovering' WHERE id = ?", (job.id,))

    harness.worker.run_once()

    stored = harness.candidates()
    assert [(r["twitch_clip_id"], r["view_count"]) for r in stored] == [("viral", 31000)]
    assert [r["broadcaster_login"] for r in harness.rankings(job.id)] == ["streamer100"]


def test_a_clip_seen_again_on_a_later_day_moves_to_that_day(harness: Harness) -> None:
    day1 = harness.enqueue(DAY1)
    harness.worker.run_once()
    with closing(connect(harness.db)) as c:
        c.execute("UPDATE jobs SET status = 'skipped' WHERE id = ?", (day1.id,))
        first_created = c.execute(
            "SELECT created_at FROM story_candidates WHERE twitch_clip_id = 'viral'"
        ).fetchone()[0]
    harness.clock.now = NOW + timedelta(hours=20)
    day2 = harness.enqueue(DAY2)

    harness.worker.run_once()

    stored = {r["twitch_clip_id"]: r for r in harness.candidates()}
    assert len(harness.candidates()) == 3  # no duplicates
    assert stored["viral"]["job_id"] == day2.id
    assert stored["viral"]["created_at"] == first_created
    assert stored["viral"]["updated_at"] == "2026-11-27T07:00:00Z"
    assert harness.row(day2.id)["status"] == "fetching_sources"


def test_a_clip_moving_to_a_later_day_starts_unfetched(harness: Harness) -> None:
    day1 = harness.enqueue(DAY1)
    harness.worker.run_once()
    with closing(connect(harness.db)) as c:
        c.execute("UPDATE jobs SET status = 'skipped' WHERE id = ?", (day1.id,))
        c.execute(
            "UPDATE story_candidates SET source_path = 'candidates/1/source.mp4',"
            " source_kind = 'vertical', loudness_peaks = '[]',"
            " transcript_path = 'candidates/1/transcript.json', drop_reason = 'fetch: gone'"
            " WHERE twitch_clip_id = 'viral'"
        )
    harness.clock.now = NOW + timedelta(hours=20)
    day2 = harness.enqueue(DAY2)

    harness.worker.run_once()

    viral = {r["twitch_clip_id"]: r for r in harness.candidates()}["viral"]
    assert viral["job_id"] == day2.id
    fields = ("source_path", "source_kind", "loudness_peaks", "transcript_path", "drop_reason")
    assert [viral[f] for f in fields] == [None] * 5


def test_a_used_clip_is_never_a_candidate_again(harness: Harness) -> None:
    day1 = harness.enqueue(DAY1)
    harness.worker.run_once()
    with closing(connect(harness.db)) as c:
        c.execute("UPDATE jobs SET status = 'clips_ready' WHERE id = ?", (day1.id,))
        c.execute(
            "INSERT INTO clips (id, job_id, rank, created_at, updated_at) VALUES (9, ?, 1, ?, ?)",
            (day1.id, "2026-11-26T00:00:00Z", "2026-11-26T00:00:00Z"),
        )
        c.execute("UPDATE story_candidates SET used_by_clip_id = 9 WHERE twitch_clip_id = 'kai'")
    day2 = harness.enqueue(DAY2)

    harness.worker.run_once()

    stored = {r["twitch_clip_id"]: r for r in harness.candidates()}
    assert stored["kai"]["job_id"] == day1.id  # untouched
    assert stored["kai"]["used_by_clip_id"] == 9
    assert stored["viral"]["job_id"] == day2.id
    with closing(connect(harness.db)) as c:
        chosen = [r["twitch_clip_id"] for r in discovery.day_candidates(c, day2.id)]
    assert chosen == ["viral"]


def test_a_clip_held_by_another_live_job_stays_with_it(harness: Harness) -> None:
    other = SourceRef("commentary", "other_channel:2026-11-26")
    with closing(connect(harness.db)) as c:
        held, _ = enqueue(c, other, NOW, creator="other_channel")
        c.execute("UPDATE jobs SET status = 'fetching_sources' WHERE id = ?", (held.id,))
        c.execute(
            "INSERT INTO story_candidates (job_id, account_id, twitch_clip_id, broadcaster_id,"
            " broadcaster_login, url, view_count, duration_s, clip_created_at, score,"
            " created_at, updated_at) VALUES (?, 'other_channel', 'viral', '100', 'streamer100',"
            " 'https://clips.twitch.tv/viral', 1, 30, ?, 1, ?, ?)",
            (held.id, "2026-11-26T09:00:00Z", "2026-11-26T09:00:00Z", "2026-11-26T09:00:00Z"),
        )
    job = harness.enqueue()

    harness.worker.run_once()

    stored = {r["twitch_clip_id"]: r for r in harness.candidates()}
    assert stored["viral"]["job_id"] == held.id
    assert stored["viral"]["account_id"] == "other_channel"
    with closing(connect(harness.db)) as c:
        chosen = [r["twitch_clip_id"] for r in discovery.day_candidates(c, job.id)]
    assert chosen == ["kai"]


@pytest.mark.parametrize(
    "failure",
    [
        lambda r: httpx.Response(503) if r.url.path == "/helix/clips" else None,
        lambda r: httpx.Response(429) if r.url.path == "/helix/games/top" else None,
        lambda r: (_ for _ in ()).throw(httpx.ConnectError("down")),
    ],
)
def test_twitch_errors_defer_the_job_with_backoff(
    harness: Harness, failure: Callable[[httpx.Request], httpx.Response | None]
) -> None:
    harness.fake.fail = failure
    job = harness.enqueue()

    assert harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "discovering"
    assert row["attempts"] == 0  # a deferral uses no attempt
    assert row["last_error"].startswith("deferred: Twitch ")
    first = datetime.fromisoformat(row["next_attempt_at"]) - NOW
    assert timedelta(seconds=48) <= first <= timedelta(seconds=72)  # 1 min, +-20 %
    assert harness.alerts == []
    assert harness.candidates() == []

    harness.clock.now = NOW + timedelta(minutes=10)
    harness.worker.run_once()

    later = datetime.fromisoformat(harness.row(job.id)["next_attempt_at"]) - harness.clock.now
    assert timedelta(minutes=8) <= later <= timedelta(minutes=12)  # backs off


def test_backoff_is_capped_at_an_hour(harness: Harness) -> None:
    harness.fake.fail = lambda r: httpx.Response(500)
    job = harness.enqueue()
    harness.worker.run_once()

    harness.clock.now = NOW + timedelta(hours=3)
    harness.worker.run_once()

    wait = datetime.fromisoformat(harness.row(job.id)["next_attempt_at"]) - harness.clock.now
    assert timedelta(minutes=48) <= wait <= timedelta(minutes=72)


def test_twitch_errors_for_six_hours_fail_the_day_with_one_alert(harness: Harness) -> None:
    harness.fake.fail = lambda r: httpx.Response(500)
    job = harness.enqueue()
    harness.worker.run_once()
    for _ in range(30):
        row = harness.row(job.id)
        if row["status"] != "discovering":
            break
        assert harness.alerts == []
        harness.clock.now = datetime.fromisoformat(row["next_attempt_at"])
        harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "failed"
    assert harness.clock.now - NOW >= timedelta(hours=6)
    assert "6 h of Twitch errors" in row["last_error"]
    assert len(harness.alerts) == 1


def test_a_success_after_errors_starts_the_next_outage_afresh(harness: Harness) -> None:
    harness.fake.fail = lambda r: httpx.Response(500)
    job = harness.enqueue()
    harness.worker.run_once()
    harness.fake.fail = None
    harness.clock.now = NOW + timedelta(minutes=5)
    harness.worker.run_once()
    assert harness.row(job.id)["status"] == "fetching_sources"
    marker_dir = harness.worker.media_root / job.ref.key
    assert not (marker_dir / discovery.TWITCH_ERRORS_MARKER).exists()


def test_rejected_credentials_fail_the_job_at_once(harness: Harness) -> None:
    harness.fake.fail = lambda r: httpx.Response(403) if r.url == twitch.TOKEN_URL else None
    job = harness.enqueue()

    harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "failed"
    assert "token request failed: HTTP 403" in row["last_error"]
    assert len(harness.alerts) == 1


def test_secret_and_token_never_reach_the_job_row_or_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    harness.fake.fail = lambda r: (
        httpx.Response(500, text=f"{SECRET} {r.headers.get('Authorization')}")
        if r.url.path == "/helix/clips"
        else None
    )
    job = harness.enqueue()

    harness.worker.run_once()

    row = harness.row(job.id)
    text = f"{row['last_error']} {caplog.text} {harness.alerts}"
    assert "HTTP 500" in text
    assert SECRET not in text
    assert TOKEN not in text


def test_a_missing_twitch_secret_fails_the_job(harness: Harness) -> None:
    stage = DiscoveryStage(load=lambda ctx: CommentaryConfig(channels=[harness.channel]))
    harness.worker.commentary_stages = {JobStatus.DISCOVERING: StageDef(stage)}
    job = harness.enqueue()

    harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "failed"
    assert "TWITCH_CLIENT_ID is not set" in row["last_error"]


def test_a_day_whose_channel_is_not_configured_fails_with_an_alert(harness: Harness) -> None:
    stage = DiscoveryStage(
        load=lambda ctx: CommentaryConfig(),
        credentials=lambda: ("cid", SECRET),
    )
    harness.worker.commentary_stages = {JobStatus.DISCOVERING: StageDef(stage)}
    job = harness.enqueue()

    harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "failed"
    assert f"no commentary channel for account {ACCOUNT}" in row["last_error"]
    assert len(harness.alerts) == 1


def test_a_game_id_looked_up_at_discovery_counts_for_the_filters(harness: Harness) -> None:
    channel = make_channel(games=[{"name": "Grand Theft Auto VI"}])
    harness.fake.games = {"Grand Theft Auto VI": GTA}
    harness.worker.commentary_stages = {
        JobStatus.DISCOVERING: StageDef(
            DiscoveryStage(
                load=lambda ctx: CommentaryConfig(channels=[channel]),
                credentials=lambda: ("cid", SECRET),
                http=lambda: httpx.Client(transport=httpx.MockTransport(harness.fake)),
            )
        )
    }
    job = harness.enqueue()

    harness.worker.run_once()

    assert harness.row(job.id)["status"] == "fetching_sources"
    with closing(connect(harness.db)) as c:
        chosen = [r["twitch_clip_id"] for r in discovery.day_candidates(c, job.id)]
    assert chosen == ["kai", "viral"]


def test_scan_carries_the_resolved_game_ids() -> None:
    fake = FakeTwitch(games={"Grand Theft Auto VI": GTA})
    channel = make_channel(games=[{"name": "Grand Theft Auto VI"}], streamers=[])

    assert scan_twitch(fake.helix(), channel, NOW).game_ids == frozenset({GTA})


def _plant_marker(harness: Harness, job: Job) -> Path:
    folder = harness.worker.media_root / job.ref.key
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / discovery.TWITCH_ERRORS_MARKER
    marker.write_text("2026-11-26T10:00:00+00:00\n2026-11-26T10:30:00+00:00")
    return marker


@pytest.mark.parametrize(
    "breakage",
    [
        "auth",
        "helix_4xx",
        "unknown_game",
        "config",
        "no_channel",
        "no_secret",
    ],
)
def test_every_permanent_failure_clears_the_twitch_errors_marker(
    harness: Harness, breakage: str
) -> None:
    from clipper.jobs.stages import PermanentError

    def broken_config(ctx: Any) -> CommentaryConfig:
        raise PermanentError("invalid commentary config")

    def no_secret() -> tuple[str, str]:
        raise PermanentError("TWITCH_CLIENT_ID is not set; add it to .env")

    channel = harness.channel
    load: Callable[[Any], CommentaryConfig] = lambda ctx: CommentaryConfig(channels=[channel])  # noqa: E731
    credentials: Callable[[], tuple[str, str]] = lambda: ("cid", SECRET)  # noqa: E731
    if breakage == "auth":
        harness.fake.fail = lambda r: httpx.Response(401) if r.url == twitch.TOKEN_URL else None
    elif breakage == "helix_4xx":
        harness.fake.fail = lambda r: httpx.Response(404) if r.url.path == "/helix/clips" else None
    elif breakage == "unknown_game":
        channel = make_channel(games=[{"name": "Grand Theft Auto VI"}])
    elif breakage == "config":
        load = broken_config
    elif breakage == "no_channel":
        load = lambda ctx: CommentaryConfig()  # noqa: E731
    else:
        credentials = no_secret
    harness.worker.commentary_stages = {
        JobStatus.DISCOVERING: StageDef(
            DiscoveryStage(
                load=load,
                credentials=credentials,
                http=lambda: httpx.Client(transport=httpx.MockTransport(harness.fake)),
            )
        )
    }
    job = harness.enqueue()
    marker = _plant_marker(harness, job)

    harness.worker.run_once()

    assert harness.row(job.id)["status"] == "failed"
    assert not marker.exists()


@pytest.mark.parametrize("status", [400, 403, 404, 410, 422])
def test_a_permanent_helix_error_fails_the_job_at_once(harness: Harness, status: int) -> None:
    harness.fake.fail = lambda r: httpx.Response(status) if r.url.path == "/helix/clips" else None
    job = harness.enqueue()

    harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "failed"
    assert f"clips request failed: HTTP {status}" in row["last_error"]
    assert len(harness.alerts) == 1


@pytest.mark.parametrize("status", [408, 429, 500, 502])
def test_a_transient_helix_error_still_defers(harness: Harness, status: int) -> None:
    harness.fake.fail = lambda r: httpx.Response(status) if r.url.path == "/helix/clips" else None
    job = harness.enqueue()

    harness.worker.run_once()

    assert harness.row(job.id)["status"] == "discovering"
    assert harness.alerts == []


def _other_live_job(harness: Harness, skip_reason: str | None) -> int:
    other = SourceRef("commentary", "other_channel:2026-11-26")
    with closing(connect(harness.db)) as c:
        held, _ = enqueue(c, other, NOW, creator="other_channel")
        c.execute("UPDATE jobs SET status = 'fetching_sources' WHERE id = ?", (held.id,))
        c.execute(
            "INSERT INTO story_candidates (job_id, account_id, twitch_clip_id, broadcaster_id,"
            " broadcaster_login, url, view_count, duration_s, clip_created_at, score,"
            " skip_reason, created_at, updated_at) VALUES (?, 'other_channel', 'viral', '100',"
            " 'streamer100', 'https://clips.twitch.tv/viral', 1, 30, ?, 1, ?, ?, ?)",
            (
                held.id,
                "2026-11-26T09:00:00Z",
                skip_reason,
                "2026-11-26T09:00:00Z",
                "2026-11-26T09:00:00Z",
            ),
        )
    return held.id


def test_a_clip_another_live_job_rejected_stays_usable(harness: Harness) -> None:
    _other_live_job(harness, "streamer not trending today")
    job = harness.enqueue()

    harness.worker.run_once()

    stored = {r["twitch_clip_id"]: r for r in harness.candidates()}
    assert stored["viral"]["job_id"] == job.id
    assert stored["viral"]["skip_reason"] is None
    with closing(connect(harness.db)) as c:
        chosen = [r["twitch_clip_id"] for r in discovery.day_candidates(c, job.id)]
    assert chosen == ["kai", "viral"]


def test_a_clip_another_job_takes_during_the_scan_is_not_taken_over(harness: Harness) -> None:
    job = harness.enqueue()
    real_scan = discovery.scan_twitch
    held: list[int] = []

    def race(helix: Helix, channel: Channel, now: datetime) -> Scan:
        scan = real_scan(helix, channel, now)
        held.append(_other_live_job(harness, None))  # lands between the scan and the write
        return scan

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(discovery, "scan_twitch", race)
        harness.worker.run_once()

    stored = {r["twitch_clip_id"]: r for r in harness.candidates()}
    assert stored["viral"]["job_id"] == held[0]
    with closing(connect(harness.db)) as c:
        chosen = [r["twitch_clip_id"] for r in discovery.day_candidates(c, job.id)]
    assert chosen == ["kai"]


def test_the_upsert_never_overwrites_a_live_jobs_candidate(harness: Harness) -> None:
    held = _other_live_job(harness, None)
    job = harness.enqueue()
    with closing(connect(harness.db)) as c:
        job_row = c.execute("SELECT * FROM jobs WHERE id = ?", (job.id,)).fetchone()
    scan = make_scan([make_clip("viral", views=30000)])
    # As if the blocked set had missed it: the SQL guard alone must keep the row.
    result = evaluate(scan, harness.channel, settings(), NOW)

    with closing(connect(harness.db)) as c:
        discovery.write_rows(c, Job.from_row(job_row), result, NOW)

    (row,) = harness.candidates()
    assert (row["job_id"], row["account_id"]) == (held, "other_channel")


def test_hold_lease_uses_the_time_of_the_write(harness: Harness) -> None:
    harness.enqueue()
    real_scan = discovery.scan_twitch
    leases: list[datetime] = []
    real_hold = discovery.hold_lease

    def slow(helix: Helix, channel: Channel, now: datetime) -> Scan:
        scan = real_scan(helix, channel, now)
        harness.clock.now = NOW + timedelta(minutes=30)  # the scan took half an hour
        return scan

    def spy(conn: sqlite3.Connection, job_id: int, worker_id: str, now: datetime) -> None:
        leases.append(now)
        real_hold(conn, job_id, worker_id, now)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(discovery, "scan_twitch", slow)
        mp.setattr(discovery, "hold_lease", spy)
        harness.worker.run_once()

    assert leases == [NOW + timedelta(minutes=30)]
    stored = {r["twitch_clip_id"]: r for r in harness.candidates()}
    assert stored["kai"]["score"] == pytest.approx(12000 / 2 * 2.0)  # scored at scan time


def test_a_game_without_an_id_fails_the_job(harness: Harness) -> None:
    channel = make_channel(games=[{"name": "Grand Theft Auto VI"}])
    harness.worker.commentary_stages = {
        JobStatus.DISCOVERING: StageDef(
            DiscoveryStage(
                load=lambda ctx: CommentaryConfig(channels=[channel]),
                credentials=lambda: ("cid", SECRET),
                http=lambda: httpx.Client(transport=httpx.MockTransport(harness.fake)),
            )
        )
    }
    job = harness.enqueue()

    harness.worker.run_once()

    row = harness.row(job.id)
    assert row["status"] == "failed"
    assert "clipper resolve" in row["last_error"]


def test_a_lost_lease_writes_nothing(harness: Harness) -> None:
    job = harness.enqueue()
    real_scan = discovery.scan_twitch

    def steal(helix: Helix, channel: Channel, now: datetime) -> Scan:
        scan = real_scan(helix, channel, now)
        with closing(connect(harness.db)) as c:
            c.execute("UPDATE jobs SET worker_id = 'w2' WHERE id = ?", (job.id,))
        return scan

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(discovery, "scan_twitch", steal)
        harness.worker.run_once()

    assert harness.candidates() == []
    assert harness.rankings(job.id) == []


# --- Wiring and the default loaders ---------------------------------------------------------


def test_discovering_is_built_and_registered() -> None:
    assert JobStatus.DISCOVERING not in NOT_BUILT
    assert isinstance(build_commentary_stages()[JobStatus.DISCOVERING].run, DiscoveryStage)


def test_the_default_loader_reads_commentary_yaml_next_to_clipper_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_commentary_config import ACCOUNTS_YAML, COMMENTARY_YAML, CREATORS_YAML

    folder = tmp_path / "cfg"
    folder.mkdir()
    (folder / "creators.yaml").write_text(CREATORS_YAML)
    (folder / "accounts.yaml").write_text(ACCOUNTS_YAML)
    (folder / "commentary.yaml").write_text(COMMENTARY_YAML)
    monkeypatch.setenv("CLIPPER_CONFIG", str(folder / "creators.yaml"))
    from clipper.config import load_config

    ctx = _context(tmp_path, load_config(folder / "creators.yaml"))

    loaded = discovery.load_commentary_config(ctx)

    assert [c.account for c in loaded.channels] == [ACCOUNT]


def test_the_default_loader_turns_a_bad_file_into_a_permanent_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipper.jobs.stages import PermanentError

    (tmp_path / "commentary.yaml").write_text("defaults: [\n")
    monkeypatch.setenv("CLIPPER_CONFIG", str(tmp_path / "creators.yaml"))

    with pytest.raises(PermanentError, match="invalid YAML"):
        discovery.load_commentary_config(_context(tmp_path, AppConfig()))


def test_the_default_credentials_come_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", SECRET)

    assert discovery.twitch_credentials() == ("cid", SECRET)


def test_the_default_http_client_has_a_timeout() -> None:
    with discovery.http_client() as client:
        assert client.timeout.read is not None


def _context(tmp_path: Path, config: AppConfig) -> Any:
    from clipper.jobs.stages import StageContext

    c = sqlite3.connect(":memory:")
    return StageContext(tmp_path, c, config)

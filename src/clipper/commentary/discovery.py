"""The commentary `discovering` stage (CLP-144): find the day's candidate Twitch clips.

For the channel of the day's job (`job.creator` is its account), discovery reads Twitch
Helix for clips made in the last 24 h: Get Clips for each of the channel's games (100 a
page, up to 5 pages) and for each pinned Twitch streamer (20), Get Top Games (20) for the
trending boost, and Get Channel Information for every streamer it found (login, current
game, content classification labels). A pinned streamer's clips count only when they are
of one of the channel's games.

Every scanned clip is stored in `story_candidates`, with `skip_reason` NULL when it goes
on to fetching and the reason otherwise. A clip is skipped, in this order, when it was
used in an earlier Short or is a chosen candidate of another live commentary job; is shorter
than 5 s; is not of the channel's games; is not in English; is by an excluded streamer;
has no channel information; comes from a channel labelled SexualThemes or Gambling, or
whose game is a gambling category or in `excluded_game_ids`; misses the virality bar
(`min_clip_views` views, or `min_views_per_hour`); scores below `min_score`; is by a
streamer outside the day's trending streamers; or falls below the day's top
`candidates_per_day`.

The score is the clip's view velocity, view_count / max(age_hours, 1), times
(1 + trending_boost when its game is in today's top games + pinned_boost when its
streamer is pinned). Pinned streamers' clips rank above all others (owner, settled
2026-11-24); then the score, then the newer clip. `day_candidates` reads the chosen
clips back in that order.

Trending streamers: the streamers of the scanned usable clips (those that pass the hard
filters: the channel's games, English, at least 5 s, no blocked label, no gambling or
excluded game, no excluded streamer), ranked by the sum of those clips' velocities. The
virality bar, the score floor and an earlier use do not count (owner, 2026-11-26). The top
`max_streamers_scanned`, plus every pinned streamer, are kept and stored in
`streamer_rankings`.

A day on which no clip is left is skipped ("no clip passed the virality bar today"),
which sends no alert. Twitch errors defer the job with backoff (no attempt used), doubling
from 1 minute up to 1 hour, until they have lasted TWITCH_GIVE_UP: then the day fails
with one alert (5xx, 408, 429 and network errors). Rejected credentials, any other Helix
4xx, a missing Twitch secret, an invalid config, a job whose channel is not in the config,
or a game with no Twitch id fail the job at once, with the alert; each also clears the
Twitch-errors marker, so a `clipper retry` starts afresh.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from clipper.accounts import accounts_path, load_accounts
from clipper.commentary.config import (
    Channel,
    CommentaryConfig,
    CommentarySettings,
    commentary_path,
    load_commentary,
)
from clipper.config import DEFAULT_CONFIG_PATH, ConfigError, MissingSecretError, Secrets
from clipper.db import transaction
from clipper.jobs.models import TERMINAL, Job, to_iso
from clipper.jobs.queue import BACKOFF_BASE, BACKOFF_CAP, hold_lease, jittered
from clipper.jobs.stages import Deferred, PermanentError, SkipJob, StageContext, StageDef
from clipper.pipeline.common import episode_since
from clipper.sources.twitch import (
    MAX_CLIP_PAGES,
    ChannelInfo,
    Clip,
    Helix,
    TwitchError,
    TwitchPermanentError,
)

logger = logging.getLogger(__name__)

SCAN_WINDOW = timedelta(hours=24)
PINNED_CLIPS = 20
"""Get Clips page size for a pinned streamer."""
MIN_CLIP_SECONDS = 5.0
GAMBLING_GAME_IDS = frozenset({"498566"})
"""Twitch gambling categories always excluded: Slots. `excluded_game_ids` adds more."""
BLOCKED_LABELS = ("SexualThemes", "Gambling")
"""Content classification labels whose channels are never used."""
HTTP_TIMEOUT = 30.0
TWITCH_GIVE_UP = timedelta(hours=6)
"""How long Twitch errors may last before the day's job fails with an alert."""
TWITCH_ERRORS_MARKER = "twitch_errors"
"""In the job's media dir: when the current run of Twitch errors began."""

NO_CANDIDATE = "no clip passed the virality bar today"
USED = "already used in a commentary Short"
TAKEN = "a candidate of another commentary job"
TOO_SHORT = "shorter than 5 s"
OTHER_GAME = "not one of the channel's games"
NOT_ENGLISH = "language is not en"
EXCLUDED = "excluded streamer"
NO_CHANNEL = "no channel information"
EXCLUDED_GAME = "gambling or excluded game"
BELOW_BAR = "below the virality bar"
BELOW_FLOOR = "score below min_score"
NOT_TRENDING = "streamer not trending today"


class DiscoveryError(Exception):
    """The channel's inputs cannot be scanned (a game with no Twitch id)."""


@dataclass(frozen=True)
class Scan:
    """What Twitch returned for the day."""

    clips: tuple[Clip, ...]
    channels: Mapping[str, ChannelInfo]
    top_games: frozenset[str]
    pinned_ids: frozenset[str]
    """User ids of the channel's pinned Twitch streamers that Twitch knows."""
    game_ids: frozenset[str]
    """Twitch ids of the channel's games: from the config, or looked up by name."""


@dataclass(frozen=True)
class Candidate:
    """A scanned clip, its score and why it was skipped (None: it goes on)."""

    clip: Clip
    login: str
    name: str
    trending: bool
    pinned: bool
    score: float
    skip_reason: str | None


@dataclass(frozen=True)
class StreamerRank:
    rank: int
    broadcaster_id: str
    login: str
    name: str
    clip_count: int
    view_count: int
    views_per_hour: float
    pinned: bool


@dataclass(frozen=True)
class Discovery:
    candidates: tuple[Candidate, ...]
    """Every scanned clip: the chosen ones first, in rank order."""
    rankings: tuple[StreamerRank, ...]
    blocked: frozenset[str] = frozenset()
    """Clip ids whose rows this job cannot take over (used, or another live job's): their
    rows are left as they are."""

    @property
    def chosen(self) -> tuple[Candidate, ...]:
        return tuple(c for c in self.candidates if c.skip_reason is None)


# --- Score, bar, filters ----------------------------------------------------------------------


def velocity(clip: Clip, now: datetime) -> float:
    """Views per hour since the clip was made, counting at least one hour."""
    age_hours = (now - clip.created_at).total_seconds() / 3600
    return clip.view_count / max(age_hours, 1.0)


def candidate_score(
    clip: Clip, now: datetime, *, trending: bool, pinned: bool, settings: CommentarySettings
) -> float:
    boost = (settings.trending_boost if trending else 0.0) + (
        settings.pinned_boost if pinned else 0.0
    )
    return velocity(clip, now) * (1 + boost)


def passes_virality_bar(clip: Clip, now: datetime, settings: CommentarySettings) -> bool:
    return (
        clip.view_count >= settings.min_clip_views
        or velocity(clip, now) >= settings.min_views_per_hour
    )


def _english(language: str) -> bool:
    return language.casefold().split("-")[0] == "en"


def _identity(clip: Clip, channels: Mapping[str, ChannelInfo]) -> tuple[str, str]:
    """(login, display name); without channel information the display name stands in."""
    info = channels.get(clip.broadcaster_id)
    if info is not None:
        return info.login.casefold(), info.name or clip.broadcaster_name
    fallback = clip.broadcaster_name or clip.broadcaster_id
    return fallback.casefold(), fallback


def _rank_streamers(
    clips: Sequence[Clip],
    channels: Mapping[str, ChannelInfo],
    is_pinned: Callable[[Clip, str], bool],
    now: datetime,
    keep: int,
) -> tuple[StreamerRank, ...]:
    """Rank the given clips' streamers by summed velocity; keep the top `keep` and every
    pinned streamer."""
    totals: dict[str, list[Clip]] = defaultdict(list)
    for clip in clips:
        totals[clip.broadcaster_id].append(clip)
    rows = []
    for broadcaster_id, theirs in totals.items():
        login, name = _identity(theirs[0], channels)
        rows.append(
            (
                sum(velocity(c, now) for c in theirs),
                sum(c.view_count for c in theirs),
                login,
                broadcaster_id,
                name,
                len(theirs),
                is_pinned(theirs[0], login),
            )
        )
    rows.sort(key=lambda r: (-r[0], -r[1], r[2], r[3]))
    return tuple(
        StreamerRank(rank, bid, login, name, count, views, vph, pinned)
        for rank, (vph, views, login, bid, name, count, pinned) in enumerate(rows, start=1)
        if rank <= keep or pinned
    )


def evaluate(
    scan: Scan,
    channel: Channel,
    settings: CommentarySettings,
    now: datetime,
    blocked: Mapping[str, str] | None = None,
) -> Discovery:
    """Score and filter the scanned clips, rank the streamers, choose the day's top clips.

    `blocked` maps clip ids to a reason they cannot be used (see `blocked_clips`)."""
    blocked = blocked or {}
    game_ids = scan.game_ids
    excluded = {e.casefold() for e in channel.exclude_streamers}
    excluded_games = GAMBLING_GAME_IDS | set(settings.excluded_game_ids)
    pinned_logins = {s.twitch for s in channel.streamers if s.twitch is not None}

    def is_pinned(clip: Clip, login: str) -> bool:
        return clip.broadcaster_id in scan.pinned_ids or login in pinned_logins

    def is_excluded(clip: Clip, login: str, name: str) -> bool:
        return bool({login, name.casefold(), clip.broadcaster_name.casefold()} & excluded)

    def unusable(clip: Clip, login: str, name: str) -> str | None:
        """The hard filters: why the clip could never be used, whatever its views."""
        info = scan.channels.get(clip.broadcaster_id)
        if clip.duration_s < MIN_CLIP_SECONDS:
            return TOO_SHORT
        if clip.game_id not in game_ids:
            return OTHER_GAME
        if not _english(clip.language):
            return NOT_ENGLISH
        if is_excluded(clip, login, name):
            return EXCLUDED
        if info is None:
            return NO_CHANNEL
        for label in BLOCKED_LABELS:
            if label in info.labels:
                return f"channel labelled {label}"
        if clip.game_id in excluded_games or info.game_id in excluded_games:
            return EXCLUDED_GAME
        return None

    # Owner, 2026-11-26: only usable clips rank a streamer. The virality bar, the score
    # floor and an earlier use do not change a streamer's ranking.
    usable = [c for c in scan.clips if unusable(c, *_identity(c, scan.channels)) is None]
    rankings = _rank_streamers(
        usable, scan.channels, is_pinned, now, settings.max_streamers_scanned
    )
    trending_streamers = {r.broadcaster_id for r in rankings}

    def reason(clip: Clip, login: str, name: str, score: float) -> str | None:
        if clip.id in blocked:
            return blocked[clip.id]
        hard = unusable(clip, login, name)
        if hard is not None:
            return hard
        if not passes_virality_bar(clip, now, settings):
            return BELOW_BAR
        if settings.min_score is not None and score < settings.min_score:
            return BELOW_FLOOR
        if clip.broadcaster_id not in trending_streamers:
            return NOT_TRENDING
        return None

    scored = []
    for clip in scan.clips:
        login, name = _identity(clip, scan.channels)
        pinned = is_pinned(clip, login)
        trending = clip.game_id in scan.top_games
        score = candidate_score(clip, now, trending=trending, pinned=pinned, settings=settings)
        scored.append(
            Candidate(clip, login, name, trending, pinned, score, reason(clip, login, name, score))
        )
    scored.sort(
        key=lambda c: (
            c.skip_reason is not None,
            not c.pinned,
            -c.score,
            -c.clip.created_at.timestamp(),
            c.clip.id,
        )
    )
    top = settings.candidates_per_day
    final = tuple(
        c
        if c.skip_reason is not None or n < top
        else replace(c, skip_reason=f"below today's top {top}")
        for n, c in enumerate(scored)
    )
    return Discovery(final, rankings, frozenset(blocked))


# --- Twitch --------------------------------------------------------------------------------


def scan_twitch(helix: Helix, channel: Channel, now: datetime) -> Scan:
    """Read the day's clips, top games and channel information from Twitch. Game and user
    ids not yet filled in by `clipper resolve` are looked up here."""
    since = now - SCAN_WINDOW
    missing_games = [g.name for g in channel.games if not g.game_id]
    found_games = helix.game_ids(missing_games) if missing_games else {}
    game_ids = []
    for game in channel.games:
        game_id = game.game_id or found_games.get(game.name)
        if game_id is None:
            raise DiscoveryError(
                f"Twitch has no game named {game.name!r}; fix the name or set its game_id"
                " with `clipper resolve`"
            )
        game_ids.append(game_id)
    pins = [s for s in channel.streamers if s.twitch is not None]
    missing_users = [s.twitch for s in pins if s.user_id is None and s.twitch is not None]
    found_users = helix.user_ids(missing_users) if missing_users else {}
    pinned_ids = [
        user_id
        for s in pins
        if (user_id := s.user_id or found_users.get(s.twitch or "")) is not None
    ]

    clips: dict[str, Clip] = {}
    for game_id in game_ids:
        for clip in helix.clips(
            game_id=game_id, started_at=since, ended_at=now, max_pages=MAX_CLIP_PAGES
        ):
            clips.setdefault(clip.id, clip)
    for user_id in pinned_ids:
        for clip in helix.clips(
            broadcaster_id=user_id, started_at=since, ended_at=now, first=PINNED_CLIPS, max_pages=1
        ):
            clips.setdefault(clip.id, clip)
    top_games = frozenset(helix.top_games())
    broadcasters = list(dict.fromkeys(c.broadcaster_id for c in clips.values()))
    channels = helix.channels(broadcasters)
    return Scan(
        tuple(clips.values()), channels, top_games, frozenset(pinned_ids), frozenset(game_ids)
    )


# --- Storage ---------------------------------------------------------------------------------


def blocked_clips(conn: sqlite3.Connection, job_id: int, clip_ids: Sequence[str]) -> dict[str, str]:
    """Clip ids that already have a row this job cannot take over: used in a Short, or a
    chosen candidate (no skip reason) of another commentary job that is still running. A
    row of a finished job, of this job, or one another job skipped is taken over. Call it
    inside the transaction that writes the rows (`record`), so no other job can take a
    clip in between."""
    terminal = [s.value for s in TERMINAL]
    blocked: dict[str, str] = {}
    for start in range(0, len(clip_ids), 500):
        batch = clip_ids[start : start + 500]
        marks = ", ".join("?" * len(batch))
        query = (
            "SELECT s.twitch_clip_id, s.job_id, s.used_by_clip_id, s.skip_reason, j.status"  # noqa: S608 - placeholders only
            " FROM story_candidates s LEFT JOIN jobs j ON j.id = s.job_id"
            f" WHERE s.twitch_clip_id IN ({marks})"
        )
        rows = conn.execute(query, batch).fetchall()
        for row in rows:
            if row["used_by_clip_id"] is not None:
                blocked[row["twitch_clip_id"]] = USED
            elif (
                row["job_id"] != job_id
                and row["status"] not in (*terminal, None)
                and row["skip_reason"] is None
            ):
                blocked[row["twitch_clip_id"]] = TAKEN
    return blocked


def record(
    conn: sqlite3.Connection,
    job: Job,
    scan: Scan,
    channel: Channel,
    settings: CommentarySettings,
    now: datetime,
    *,
    clock: Callable[[], datetime],
    worker_id: str | None = None,
) -> Discovery:
    """Evaluate the scan and replace the job's candidates and ranking with the result, in
    one transaction fenced on the claim. `now` is the scan's time (ages and scores);
    `clock` gives the time of the write (the lease check and the row stamps). The rows
    other jobs hold are read inside the same transaction, so no clip is taken twice."""
    with transaction(conn):
        written_at = clock()
        if worker_id is not None:
            hold_lease(conn, job.id, worker_id, written_at)
        blocked = blocked_clips(conn, job.id, [c.id for c in scan.clips])
        result = evaluate(scan, channel, settings, now, blocked)
        write_rows(conn, job, result, written_at)
    return result


_LIVE_JOB_IDS = (
    "SELECT id FROM jobs WHERE status NOT IN ("  # noqa: S608 - fixed status names
    + ", ".join(f"'{s.value}'" for s in sorted(TERMINAL))
    + ")"
)


def write_rows(conn: sqlite3.Connection, job: Job, result: Discovery, now: datetime) -> None:
    """Replace the job's rows with `result`; the caller holds the transaction. Rows of used
    clips and other live jobs' chosen candidates are never overwritten, even if they are
    missing from `result.blocked` (the upsert checks again)."""
    stamp = to_iso(now)
    conn.execute("DELETE FROM streamer_rankings WHERE job_id = ?", (job.id,))
    conn.execute(
        "DELETE FROM story_candidates WHERE job_id = ? AND used_by_clip_id IS NULL",
        (job.id,),
    )
    for c in result.candidates:
        if c.clip.id in result.blocked:
            continue
        clip = c.clip
        conn.execute(
            "INSERT INTO story_candidates (job_id, account_id, twitch_clip_id,"  # noqa: S608 - fixed text
            " broadcaster_id, broadcaster_login, broadcaster_name, game_id, title, url,"
            " language, view_count, duration_s, clip_created_at, trending, pinned, score,"
            " skip_reason, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (twitch_clip_id) DO UPDATE SET job_id = excluded.job_id,"
            " account_id = excluded.account_id, broadcaster_id = excluded.broadcaster_id,"
            " broadcaster_login = excluded.broadcaster_login,"
            " broadcaster_name = excluded.broadcaster_name, game_id = excluded.game_id,"
            " title = excluded.title, url = excluded.url, language = excluded.language,"
            " view_count = excluded.view_count, duration_s = excluded.duration_s,"
            " clip_created_at = excluded.clip_created_at, trending = excluded.trending,"
            " pinned = excluded.pinned, score = excluded.score,"
            " skip_reason = excluded.skip_reason, updated_at = excluded.updated_at"
            " WHERE story_candidates.used_by_clip_id IS NULL"
            " AND (story_candidates.job_id = excluded.job_id"
            " OR story_candidates.skip_reason IS NOT NULL"
            f" OR story_candidates.job_id NOT IN ({_LIVE_JOB_IDS}))",
            (
                job.id,
                job.creator,
                clip.id,
                clip.broadcaster_id,
                c.login,
                c.name,
                clip.game_id,
                clip.title,
                clip.url,
                clip.language,
                clip.view_count,
                clip.duration_s,
                to_iso(clip.created_at),
                int(c.trending),
                int(c.pinned),
                max(c.score, 0.0),
                c.skip_reason,
                stamp,
                stamp,
            ),
        )
    conn.executemany(
        "INSERT INTO streamer_rankings (job_id, rank, broadcaster_id, broadcaster_login,"
        " broadcaster_name, clip_count, view_count, views_per_hour, pinned, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                job.id,
                r.rank,
                r.broadcaster_id,
                r.login,
                r.name,
                r.clip_count,
                r.view_count,
                r.views_per_hour,
                int(r.pinned),
                stamp,
            )
            for r in result.rankings
        ],
    )


def day_candidates(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    """The day's chosen clips in rank order: pinned first, then score, then newer."""
    return conn.execute(
        "SELECT * FROM story_candidates WHERE job_id = ? AND skip_reason IS NULL"
        " AND used_by_clip_id IS NULL"
        " ORDER BY pinned DESC, score DESC, clip_created_at DESC, twitch_clip_id",
        (job_id,),
    ).fetchall()


# --- The stage --------------------------------------------------------------------------------


def load_commentary_config(ctx: StageContext) -> CommentaryConfig:
    """commentary.yaml next to the creators config (`CLIPPER_CONFIG`, as run-worker reads
    it; `CLIPPER_COMMENTARY` and `CLIPPER_ACCOUNTS` still apply). Read on every run, so a
    `clipper resolve` or a config edit counts from the next day without a restart."""
    config_path = Path(os.environ.get("CLIPPER_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        accounts = load_accounts(accounts_path(config_path), ctx.config)
        return load_commentary(commentary_path(config_path), accounts)
    except ConfigError as exc:
        raise PermanentError(str(exc)) from None


def twitch_credentials() -> tuple[str, str]:
    secrets = Secrets()
    try:
        return secrets.require("TWITCH_CLIENT_ID"), secrets.require("TWITCH_CLIENT_SECRET")
    except MissingSecretError as exc:
        raise PermanentError(str(exc)) from None


def http_client() -> httpx.Client:
    return httpx.Client(timeout=HTTP_TIMEOUT, follow_redirects=False)


def _twitch_deferred(ctx: StageContext, reason: str) -> Exception:
    """Deferred with backoff: the wait doubles with the length of the current run of Twitch
    errors (from BACKOFF_BASE up to BACKOFF_CAP, jittered); after TWITCH_GIVE_UP, a
    PermanentError."""
    now = ctx.clock()
    marker = ctx.media_dir / TWITCH_ERRORS_MARKER
    lasted = now - episode_since(marker, now, TWITCH_GIVE_UP)
    if lasted >= TWITCH_GIVE_UP:
        marker.unlink(missing_ok=True)
        hours = TWITCH_GIVE_UP.total_seconds() / 3600
        return PermanentError(f"{reason} ({hours:.0f} h of Twitch errors; giving up on today)")
    wait = jittered(min(max(lasted, BACKOFF_BASE), BACKOFF_CAP))
    return Deferred(now + wait, reason)


@dataclass(frozen=True)
class DiscoveryStage:
    """The stage, with its inputs as seams for tests."""

    load: Callable[[StageContext], CommentaryConfig] = load_commentary_config
    credentials: Callable[[], tuple[str, str]] = twitch_credentials
    http: Callable[[], httpx.Client] = field(default=http_client)

    def __call__(self, job: Job, ctx: StageContext) -> None:
        try:
            self._run(job, ctx)
        except PermanentError:
            # A failed day starts afresh on `clipper retry`, not inside the old run of errors.
            (ctx.media_dir / TWITCH_ERRORS_MARKER).unlink(missing_ok=True)
            raise

    def _run(self, job: Job, ctx: StageContext) -> None:
        config = self.load(ctx)
        channel = next((c for c in config.channels if c.account == job.creator), None)
        if channel is None:
            # The monitor makes jobs only for configured channels: the config changed or
            # the worker reads another file than the monitor (CLIPPER_CONFIG).
            raise PermanentError(f"no commentary channel for account {job.creator}")
        settings = config.settings_for(channel)
        client_id, client_secret = self.credentials()
        now = ctx.clock()
        try:
            with self.http() as http:
                scan = scan_twitch(Helix(http, client_id, client_secret), channel, now)
        except TwitchPermanentError as exc:
            raise PermanentError(str(exc)) from None
        except TwitchError as exc:
            raise _twitch_deferred(ctx, str(exc)) from None
        except DiscoveryError as exc:
            raise PermanentError(str(exc)) from None
        (ctx.media_dir / TWITCH_ERRORS_MARKER).unlink(missing_ok=True)
        result = record(
            ctx.conn,
            job,
            scan,
            channel,
            settings,
            now,
            clock=ctx.clock,
            worker_id=ctx.worker_id,
        )
        logger.info(
            "job %d: discovery scanned %d clip(s), %d streamer(s) ranked, %d candidate(s)",
            job.id,
            len(result.candidates),
            len(result.rankings),
            len(result.chosen),
        )
        if not result.chosen:
            raise SkipJob(NO_CANDIDATE)


STAGE = StageDef(DiscoveryStage())

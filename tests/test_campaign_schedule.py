"""Campaign pre-flight at scheduling (CLP-97): a brief violation holds the post with the
reason; a clean post is scheduled with the metadata the pre-flight checked."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from clipper.campaigns import Campaign
from clipper.cli import app
from clipper.edl import parse_edl
from clipper.posting import repo
from clipper.posting.models import OPEN_STATUSES, InvalidPostTransition, PostStatus
from clipper.posting.scheduler import publish_problem, recheck_held, schedule_for_clip
from clipper.posting.stage import PostingStage
from posting_support import FakeAccount, FakePublisher
from review_support import Env, login, make_env

NOW = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)  # review_support.WALL_START
TAGS = ["#giveaway", "#island", "#challenge"]


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


def campaign(**overrides: object) -> Campaign:
    base: dict[str, object] = {
        "id": "vyro_mrbeast",
        "program": "vyro",
        "creator": "MrBeast",
        "platforms": ["tiktok", "youtube"],
        "brief_verified": True,
        "min_duration_s": 20,
        "required_hashtags": ["#mrbeast", "#mrbeastpartner"],
        "forbidden": ["ai_content"],
    }
    base.update(overrides)
    return Campaign.model_validate(base)


def account(**overrides: object) -> FakeAccount:
    base: dict[str, object] = {
        "id": "tt_main",
        "windows": ["00:00-23:59"],
        "campaign": "vyro_mrbeast",
    }
    base.update(overrides)
    return FakeAccount(**base)  # type: ignore[arg-type]


def rendered_clip(
    env: Env,
    *,
    seconds: float = 30.0,
    ai: bool = False,
    sidecar: bool = True,
    status: str = "approved",
    hashtags: list[str] | None = None,
) -> int:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    video = env.media_root / rel
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"fake mp4")
    if sidecar:
        hook = {"text": "Wait for it", "ai_generated": ai}
        edl = parse_edl({"source": {"path": "s.mp4", "in_s": 0, "out_s": seconds}, "hook": hook})
        video.with_suffix(".json").write_text(json.dumps({"edl": edl.to_props()}))
    return env.clip(
        job,
        status=status,
        video_path=rel,
        start_s=100.0,
        end_s=100.0 + seconds,
        hashtags=hashtags if hashtags is not None else TAGS,
        caption="The craziest giveaway yet",
        title="He gave away a private island",
    )


def schedule(env: Env, clip_id: int, *accounts: FakeAccount, **campaigns: Campaign):
    found = campaigns or {"vyro_mrbeast": campaign()}
    return schedule_for_clip(
        env.conn, clip_id, accounts or [account()], NOW, campaigns=found, media_root=env.media_root
    )


def stored_metadata(env: Env, post_id: int) -> dict[str, object]:
    raw = env.conn.execute("SELECT metadata FROM posts WHERE id = ?", (post_id,)).fetchone()[0]
    return json.loads(raw)


# --- AC1: a brief violation holds the post and shows the reason ---------------------------


def test_a_clean_campaign_post_is_scheduled_with_the_campaign_tags(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env))

    assert post.status == PostStatus.SCHEDULED
    assert post.campaign_id == "vyro_mrbeast"
    assert post.error is None
    metadata = stored_metadata(env, post.id)
    assert metadata["hashtags"][-2:] == ["#mrbeast", "#mrbeastpartner"]
    assert str(metadata["caption"]).endswith("#mrbeast #mrbeastpartner")
    assert metadata["flags"] == {"is_aigc": False, "brand_content_toggle": False}


def test_too_short_clip_is_held_with_the_reason(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env, seconds=12))

    assert post.status == PostStatus.HELD
    assert post.error is not None and "under the brief's 20 s" in post.error


def test_missing_required_tag_is_held(env: Env) -> None:
    # A required tag the post cannot carry: Instagram caps hashtags at 5.
    tags = campaign(required_hashtags=[f"#tag{n}" for n in range(6)], platforms=["instagram"])
    ig = account(id="ig_main", platform="instagram")

    [post] = schedule(env, rendered_clip(env), ig, vyro_mrbeast=tags)

    assert post.status == PostStatus.HELD
    assert post.error is not None and "hashtags, at most 5" in post.error


def test_forbidden_ai_element_is_held(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env, ai=True))

    assert post.status == PostStatus.HELD
    assert post.error is not None and "AI-generated" in post.error


def test_clip_without_a_render_sidecar_is_held_under_an_ai_ban(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env, sidecar=False))

    assert post.status == PostStatus.HELD
    assert post.error is not None and "cannot confirm" in post.error


def test_account_with_an_unknown_campaign_is_held(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env), account(campaign="gone"))

    assert post.status == PostStatus.HELD
    assert post.error is not None and "unknown campaign 'gone'" in post.error


def test_campaign_only_applies_to_its_creator(env: Env) -> None:
    other = campaign(creator="Kai Cenat")
    clip_id = rendered_clip(env, seconds=5)  # too short for the brief, fine for TikTok

    [post] = schedule(env, clip_id, vyro_mrbeast=other)

    assert post.status == PostStatus.SCHEDULED
    assert post.campaign_id is None


def test_non_campaign_account_is_scheduled_with_its_platform_variant(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env, seconds=5), account(campaign=None))

    assert post.status == PostStatus.SCHEDULED
    metadata = stored_metadata(env, post.id)
    assert "#mrbeast" not in metadata["hashtags"]
    assert metadata["hashtags"] == TAGS


def test_held_post_does_not_take_a_slot(env: Env) -> None:
    acct = account(posts_per_day=1, min_gap_minutes=0)
    held = schedule(env, rendered_clip(env, seconds=12), acct)
    [ok] = schedule(env, rendered_clip(env), acct)

    assert held[0].status == PostStatus.HELD
    assert ok.status == PostStatus.SCHEDULED
    assert ok.scheduled_at.date() == NOW.date()


def test_held_post_is_never_published(env: Env) -> None:
    schedule(env, rendered_clip(env, seconds=12))
    publisher = FakePublisher()
    stage = PostingStage(
        env.conn,
        {"tiktok": publisher},
        "w1",
        media_root=env.media_root,
        clock=lambda: NOW + timedelta(days=30),
    )

    stage.run_once()

    assert publisher.calls == []


def test_held_is_an_open_status_that_can_only_be_cancelled_or_rechecked(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env, seconds=12))

    assert PostStatus.HELD in OPEN_STATUSES
    with pytest.raises(InvalidPostTransition):
        repo.reschedule(env.conn, post.id, NOW + timedelta(hours=1), NOW)
    with pytest.raises(InvalidPostTransition):
        repo.retry(env.conn, post.id, NOW)
    assert repo.cancel(env.conn, post.id, NOW).status == PostStatus.CANCELLED


def test_hold_is_audited_with_the_reason(env: Env) -> None:
    clip_id = rendered_clip(env, seconds=12)
    schedule(env, clip_id)

    [row] = [r for r in env.reviews(clip_id) if r["action"] == "post_held"]
    assert "under the brief's 20 s" in json.loads(row["payload"])["reason"]


# --- posting sends what the pre-flight checked ---------------------------------------------


def test_posting_stage_publishes_the_stored_metadata(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env))
    publisher = FakePublisher()
    stage = PostingStage(
        env.conn,
        {"tiktok": publisher},
        "w1",
        media_root=env.media_root,
        clock=lambda: post.scheduled_at + timedelta(minutes=1),
    )

    stage.run_once()

    [(_, _, metadata)] = publisher.calls
    assert metadata.hashtags[-2:] == ["#mrbeast", "#mrbeastpartner"]
    assert metadata.flags["is_aigc"] is False
    assert metadata.title == "He gave away a private island"


# --- recheck --------------------------------------------------------------------------------


def test_recheck_schedules_held_posts_that_now_pass(env: Env) -> None:
    [held] = schedule(env, rendered_clip(env), vyro_mrbeast=campaign(brief_verified=False))
    assert held.status == PostStatus.HELD

    result = recheck_held(
        env.conn,
        [account()],
        {"vyro_mrbeast": campaign()},
        NOW,
        media_root=env.media_root,
    )

    [released] = result.released
    assert released.id == held.id
    assert released.status == PostStatus.SCHEDULED
    assert released.error is None
    assert released.scheduled_at >= NOW
    assert result.still_held == []


def test_recheck_keeps_failing_posts_held_with_the_new_reason(env: Env) -> None:
    [held] = schedule(env, rendered_clip(env, seconds=12))

    result = recheck_held(
        env.conn,
        [account()],
        {"vyro_mrbeast": campaign(min_duration_s=15)},
        NOW,
        media_root=env.media_root,
    )

    [still] = result.still_held
    assert still.id == held.id and still.status == PostStatus.HELD
    assert still.error is not None and "under the brief's 15 s" in still.error


def test_recheck_leaves_posts_of_a_removed_account_held(env: Env) -> None:
    [held] = schedule(env, rendered_clip(env, seconds=12))

    result = recheck_held(env.conn, [], {}, NOW, media_root=env.media_root)

    assert result.released == []
    [still] = result.still_held
    assert still.id == held.id
    assert still.error == "account tt_main is no longer configured"


def test_recheck_keeps_a_post_held_when_the_account_no_longer_posts_the_creator(
    env: Env,
) -> None:
    schedule(env, rendered_clip(env), vyro_mrbeast=campaign(brief_verified=False))

    result = recheck_held(
        env.conn,
        [account(creators=["Kai Cenat"])],
        {"vyro_mrbeast": campaign()},
        NOW,
        media_root=env.media_root,
    )

    [still] = result.still_held
    assert still.error == "account tt_main no longer posts MrBeast"


def test_recheck_never_releases_a_sensitive_creator(env: Env) -> None:
    schedule(env, rendered_clip(env), vyro_mrbeast=campaign(brief_verified=False))

    result = recheck_held(
        env.conn,
        [account()],
        {"vyro_mrbeast": campaign()},
        NOW,
        media_root=env.media_root,
        is_sensitive=lambda creator: creator == "MrBeast",
    )

    [still] = result.still_held
    assert still.error is not None and "sensitive" in still.error


def test_recheck_without_a_free_slot_says_so(env: Env) -> None:
    schedule(env, rendered_clip(env), vyro_mrbeast=campaign(brief_verified=False))
    full = account(posts_per_day=1, min_gap_minutes=0)
    for _ in range(8):  # fill today and the next 7 days
        schedule(env, rendered_clip(env), full, vyro_mrbeast=campaign())

    result = recheck_held(
        env.conn, [full], {"vyro_mrbeast": campaign()}, NOW, media_root=env.media_root
    )

    [still] = result.still_held
    assert still.error is not None and "no free slot" in still.error


def test_recheck_releases_with_the_campaign_that_applies_now(env: Env) -> None:
    [held] = schedule(env, rendered_clip(env), vyro_mrbeast=campaign(brief_verified=False))

    result = recheck_held(env.conn, [account(campaign=None)], {}, NOW, media_root=env.media_root)

    [released] = result.released
    assert released.id == held.id and released.campaign_id is None
    assert "#mrbeast" not in stored_metadata(env, held.id)["hashtags"]


def test_recheck_uses_the_clips_current_version(env: Env) -> None:
    clip_id = rendered_clip(env, seconds=12)
    [held] = schedule(env, clip_id)
    # The reviewer re-cut the clip: version 2, long enough, approved again.
    env.conn.execute(
        "INSERT INTO clip_versions (clip_id, version, start_s, end_s, title, caption, hashtags,"
        " video_path, sensitive, created_at)"
        " SELECT clip_id, 2, start_s, start_s + 30, title, caption, hashtags, video_path,"
        " sensitive, created_at FROM clip_versions WHERE clip_id = ? AND version = 1",
        (clip_id,),
    )
    env.conn.execute("UPDATE clips SET current_version = 2 WHERE id = ?", (clip_id,))

    result = recheck_held(
        env.conn, [account()], {"vyro_mrbeast": campaign()}, NOW, media_root=env.media_root
    )

    [released] = result.released
    assert released.id == held.id and released.clip_version == 2


def test_violation_is_held_even_when_the_account_has_no_free_slot(env: Env) -> None:
    full = account(posts_per_day=1, min_gap_minutes=0)
    for _ in range(8):
        schedule(env, rendered_clip(env), full)

    [post] = schedule(env, rendered_clip(env, seconds=12), full)

    assert post.status == PostStatus.HELD
    assert post.error is not None and "under the brief's 20 s" in post.error


def test_forbidden_keyword_is_held_at_scheduling(env: Env) -> None:
    brief = campaign(forbidden_keywords=["giveaway"])

    [post] = schedule(env, rendered_clip(env), vyro_mrbeast=brief)

    assert post.status == PostStatus.HELD
    assert post.error is not None and "forbidden keyword 'giveaway'" in post.error


def test_held_post_keeps_its_file_reserved_on_the_platform(env: Env) -> None:
    # Intended: a held post can be released later, so the same file never goes to a
    # second account on the platform meanwhile (CLP-92's one-file rule).
    other = account(id="tt_other", campaign=None)

    posts = schedule(env, rendered_clip(env, seconds=12), account(), other)

    assert [(p.account_id, p.status) for p in posts] == [("tt_main", PostStatus.HELD)]


# --- publish-time pre-flight ----------------------------------------------------------------


def stage(env: Env, publisher: FakePublisher, accounts: list[FakeAccount], **briefs: Campaign):
    found = {a.id: a for a in accounts}

    def guard(post):  # type: ignore[no-untyped-def]
        return publish_problem(env.conn, post, found, briefs, NOW, media_root=env.media_root)

    return PostingStage(
        env.conn,
        {"tiktok": publisher},
        "w1",
        media_root=env.media_root,
        clock=lambda: NOW + timedelta(days=30),
        guard=guard,
    )


def test_publish_holds_a_post_whose_campaign_was_disabled_after_scheduling(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env))
    publisher = FakePublisher()

    stage(env, publisher, [account()], vyro_mrbeast=campaign(enabled=False)).run_once()

    assert publisher.calls == []
    found = repo.get_post(env.conn, post.id)
    assert found is not None and found.status == PostStatus.HELD
    assert found.error is not None and "disabled" in found.error


def test_publish_holds_a_post_rescheduled_past_the_campaign_end(env: Env) -> None:
    ends = NOW + timedelta(days=2)
    [post] = schedule(env, rendered_clip(env), vyro_mrbeast=campaign(ends_at=ends))
    repo.reschedule(env.conn, post.id, NOW + timedelta(days=5), NOW)
    publisher = FakePublisher()

    guard_now = NOW + timedelta(days=5)
    found_accounts = {"tt_main": account()}
    briefs = {"vyro_mrbeast": campaign(ends_at=ends)}
    PostingStage(
        env.conn,
        {"tiktok": publisher},
        "w1",
        media_root=env.media_root,
        clock=lambda: guard_now,
        guard=lambda p: publish_problem(
            env.conn, p, found_accounts, briefs, guard_now, media_root=env.media_root
        ),
    ).run_once()

    assert publisher.calls == []
    found = repo.get_post(env.conn, post.id)
    assert found is not None and found.status == PostStatus.HELD
    assert found.error is not None and "ended" in found.error


def test_publish_holds_a_post_whose_account_changed_campaign(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env), account(campaign=None))
    publisher = FakePublisher()

    stage(env, publisher, [account()], vyro_mrbeast=campaign()).run_once()

    assert publisher.calls == []
    found = repo.get_post(env.conn, post.id)
    assert found is not None and found.error is not None and "campaign changed" in found.error


def test_publish_passes_a_compliant_post(env: Env) -> None:
    schedule(env, rendered_clip(env))
    publisher = FakePublisher()

    stage(env, publisher, [account()], vyro_mrbeast=campaign()).run_once()

    assert len(publisher.calls) == 1


def test_a_failing_guard_holds_the_post(env: Env) -> None:
    [post] = schedule(env, rendered_clip(env))
    publisher = FakePublisher()

    def broken(_post):  # type: ignore[no-untyped-def]
        raise RuntimeError("db locked")

    PostingStage(
        env.conn,
        {"tiktok": publisher},
        "w1",
        media_root=env.media_root,
        clock=lambda: NOW + timedelta(days=30),
        guard=broken,
    ).run_once()

    assert publisher.calls == []
    found = repo.get_post(env.conn, post.id)
    assert found is not None and found.status == PostStatus.HELD
    assert found.error is not None and "publish pre-flight failed" in found.error


# --- review app -----------------------------------------------------------------------------


def logged_in(env: Env, **kwargs: object) -> TestClient:
    client = env.client(**kwargs)
    client.csrf = login(client)  # type: ignore[attr-defined]
    return client


def test_approve_holds_a_violating_post_and_shows_the_reason(env: Env) -> None:
    client = logged_in(env, accounts=[account()], campaigns={"vyro_mrbeast": campaign()})
    clip_id = rendered_clip(env, seconds=12, status="pending_review")

    response = client.post(
        f"/clip/{clip_id}/approve",
        data={"version": "1"},
        headers={"X-CSRF-Token": client.csrf, "HX-Request": "true"},  # type: ignore[attr-defined]
    )
    assert response.status_code == 200
    [post] = repo.list_posts(env.conn, clip_id=clip_id)
    assert post.status == PostStatus.HELD

    assert "1 post(s) held" in client.get("/queue").text
    page = client.get("/scheduled")
    assert page.status_code == 200
    assert 'id="held"' in page.text
    assert "under the brief&#39;s 20 s" in page.text or "under the brief's 20 s" in page.text


# --- CLI ------------------------------------------------------------------------------------

runner = CliRunner()

CREATORS = """\
creators:
  - name: MrBeast
    region: global
    content_type: challenge_stunt
    youtube:
      handle: "@MrBeast"
"""
ACCOUNTS = """\
accounts:
  - id: tt_main
    platform: tiktok
    handle: "@tt.main"
    market: US
    timezone: UTC
    creators: [MrBeast]
    posts_per_day: 3
    min_gap_minutes: 0
    windows: ["00:00-23:59"]
    campaign: vyro_mrbeast
"""
CAMPAIGNS = """\
campaigns:
  - id: vyro_mrbeast
    program: vyro
    creator: MrBeast
    platforms: [tiktok]
    brief_verified: {verified}
    min_duration_s: 20
    required_hashtags: ["#mrbeast"]
"""


def write_configs(tmp_path: Path, verified: bool) -> Path:
    creators = tmp_path / "creators.yaml"
    creators.write_text(CREATORS)
    (tmp_path / "accounts.yaml").write_text(ACCOUNTS)
    (tmp_path / "campaigns.yaml").write_text(CAMPAIGNS.format(verified=str(verified).lower()))
    return creators


def test_cli_recheck_releases_held_posts(env: Env, tmp_path: Path) -> None:
    [held] = schedule(env, rendered_clip(env), vyro_mrbeast=campaign(brief_verified=False))
    config = write_configs(tmp_path, verified=True)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "--db",
            str(env.db_path),
            "posts",
            "recheck",
            "--media-dir",
            str(env.media_root),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "1 released, 0 still held" in result.output
    found = repo.get_post(env.conn, held.id)
    assert found is not None and found.status == PostStatus.SCHEDULED


def test_check_config_summarises_campaigns(tmp_path: Path) -> None:
    config = write_configs(tmp_path, verified=False)

    result = runner.invoke(app, ["--config", str(config), "check-config"])

    assert result.exit_code == 0, result.output
    assert "ok: 1 campaigns" in result.output
    assert "vyro_mrbeast" in result.output
    assert "placeholder brief" in result.output


def test_check_config_fails_on_a_market_that_cannot_meet_the_audience(tmp_path: Path) -> None:
    config = write_configs(tmp_path, verified=True)
    (tmp_path / "campaigns.yaml").write_text(
        CAMPAIGNS.format(verified="true")
        + "    audience_rule:\n      markets: [GB]\n      min_share: 0.5\n"
    )

    result = runner.invoke(app, ["--config", str(config), "check-config"])

    assert result.exit_code == 1
    assert "tt_main" in result.output and "market US" in result.output

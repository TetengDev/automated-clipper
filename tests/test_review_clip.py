"""Clip detail page, media route (Range, traversal), approve/reject + audit (CLP-20)."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper import clips
from clipper.clips import ClipStatus, InvalidTransition
from clipper.review import routes
from clipper.review.app import STATIC_DIR
from review_support import BASE_URL, Env, generate_mp4, login, make_env

NOW = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


@pytest.fixture
def client(env: Env) -> TestClient:
    client = env.client()
    client.csrf = login(client)  # type: ignore[attr-defined]
    return client


@pytest.fixture(scope="module")
def mp4_bytes(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    return generate_mp4(tmp_path_factory.mktemp("mp4") / "clip.mp4").read_bytes()


@pytest.fixture
def video_clip(env: Env, mp4_bytes: bytes) -> int:
    target = env.media_root / "youtube_x" / "clip_1_v1.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(mp4_bytes)
    (target.parent / "thumb.jpg").write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    return env.clip(video_path="youtube_x/clip_1_v1.mp4", thumb_path="youtube_x/thumb.jpg")


def post(client: TestClient, url: str, htmx: bool = True, **data: object):
    headers = {"X-CSRF-Token": client.csrf}  # type: ignore[attr-defined]
    if htmx:
        headers["HX-Request"] = "true"
    return client.post(url, data=data, headers=headers)


# --- detail page ------------------------------------------------------------------------


def test_detail_shows_metadata_reason_source_and_versions(client: TestClient, env: Env) -> None:
    job = env.job("Kai Cenat", platform="twitch", item_id="987654321", title="Late stream")
    clip_id = env.clip(job, title="Chair falls", start_s=3725.0, end_s=3755.0, versions=2)

    response = client.get(f"/clip/{clip_id}")

    html = response.text
    assert response.status_code == 200
    assert "Chair falls" in html
    assert "Watch this" in html
    assert "#funny #viral" in html
    assert "Strong hook and payoff" in html
    assert 'href="https://www.twitch.tv/videos/987654321?t=1h2m5s"' in html
    assert "v1" in html and "v2" in html and "(current)" in html
    assert f'src="/media/clip/{clip_id}/2/video"' in html
    for reason in clips.REJECT_REASONS:
        assert f'<option value="{reason}">' in html
    assert '<select id="reason-tag" name="reason_tag" required>' in html
    assert '<script src="/static/review.js" defer></script>' in html


def test_sensitive_badge_on_detail_page_only_for_sensitive_clips(
    client: TestClient, env: Env
) -> None:
    flagged = env.clip(sensitive=True)
    plain = env.clip()

    assert 'class="badge sensitive"' in client.get(f"/clip/{flagged}").text
    assert 'class="badge sensitive"' not in client.get(f"/clip/{plain}").text
    assert "check for names, faces or identifiers of victims and minors" in (
        client.get(f"/clip/{flagged}").text
    )


def test_youtube_source_link_has_timestamp(client: TestClient, env: Env) -> None:
    clip_id = env.clip(env.job(item_id="dQw4w9WgXcQ"), start_s=125.9)

    html = client.get(f"/clip/{clip_id}").text

    assert 'href="https://www.youtube.com/watch?v=dQw4w9WgXcQ&amp;t=125s"' in html


def test_detail_has_neighbour_links(client: TestClient, env: Env) -> None:
    old = env.clip(created_at="2027-01-01T00:00:00Z")
    mid = env.clip(created_at="2027-01-02T00:00:00Z")
    new = env.clip(created_at="2027-01-03T00:00:00Z")

    html = client.get(f"/clip/{mid}").text

    assert f'id="next-clip" href="/clip/{old}"' in html
    assert f'id="prev-clip" href="/clip/{new}"' in html


def test_missing_clip_is_404(client: TestClient) -> None:
    assert client.get("/clip/999").status_code == 404
    assert client.get("/clip/abc").status_code == 422


def test_detail_requires_login(env: Env) -> None:
    clip_id = env.clip()

    response = env.client().get(f"/clip/{clip_id}")

    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next=%2Fclip%2F{clip_id}"


def test_keyboard_script_is_static_and_handles_shortcuts(env: Env) -> None:
    js = (STATIC_DIR / "review.js").read_text()
    response = env.client().get("/static/review.js")

    assert response.status_code == 200
    for key in ('"a"', '"r"', '"j"', '"k"'):
        assert f"case {key}" in js


# --- media ------------------------------------------------------------------------------


def test_video_served_with_range_support(
    client: TestClient, video_clip: int, mp4_bytes: bytes
) -> None:
    url = f"/media/clip/{video_clip}/1/video"
    full = client.get(url)
    partial = client.get(url, headers={"Range": "bytes=0-99"})
    tail = client.get(url, headers={"Range": "bytes=-10"})
    size = len(mp4_bytes)

    assert full.status_code == 200
    assert full.headers["content-type"] == "video/mp4"
    assert full.headers["accept-ranges"] == "bytes"
    assert full.content == mp4_bytes
    assert partial.status_code == 206
    assert partial.headers["content-range"] == f"bytes 0-99/{size}"
    assert partial.content == mp4_bytes[:100]
    assert tail.status_code == 206
    assert tail.headers["content-range"] == f"bytes {size - 10}-{size - 1}/{size}"
    assert tail.content == mp4_bytes[-10:]


def test_open_ended_and_unsatisfiable_ranges(
    client: TestClient, video_clip: int, mp4_bytes: bytes
) -> None:
    url = f"/media/clip/{video_clip}/1/video"
    size = len(mp4_bytes)

    open_ended = client.get(url, headers={"Range": f"bytes={size - 5}-"})
    beyond = client.get(url, headers={"Range": f"bytes={size + 10}-{size + 20}"})

    assert open_ended.status_code == 206
    assert open_ended.content == mp4_bytes[-5:]
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{size}"


def test_thumbnail_served(client: TestClient, video_clip: int) -> None:
    response = client.get(f"/media/clip/{video_clip}/1/thumb")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"


def test_media_requires_login(env: Env, video_clip: int) -> None:
    response = env.client().get(f"/media/clip/{video_clip}/1/video")

    assert response.status_code == 303


@pytest.mark.parametrize(
    "stored",
    ["../secret.mp4", "../../etc/passwd", "/etc/passwd", "youtube_x/../../secret.mp4", ""],
)
def test_db_paths_outside_media_root_are_not_served(
    client: TestClient, env: Env, tmp_path: Path, stored: str
) -> None:
    (tmp_path / "secret.mp4").write_bytes(b"secret")
    clip_id = env.clip(video_path=stored or None)

    response = client.get(f"/media/clip/{clip_id}/1/video")

    assert response.status_code == 404
    assert b"secret" not in response.content


def test_symlink_escaping_media_root_not_served(
    client: TestClient, env: Env, tmp_path: Path
) -> None:
    (tmp_path / "secret.mp4").write_bytes(b"secret")
    os.symlink(tmp_path / "secret.mp4", env.media_root / "link.mp4")
    clip_id = env.clip(video_path="link.mp4")

    assert client.get(f"/media/clip/{clip_id}/1/video").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/media/clip/1/1/..%2F..%2Fetc%2Fpasswd",
        "/media/clip/1/1/../../../etc/passwd",
        "/media/clip/1/1/state.db",
        "/media/clip/1/9/video",
        "/media/clip/999/1/video",
        "/media/clip/1/1/%2e%2e",
    ],
)
def test_url_traversal_and_unknown_media_rejected(
    client: TestClient, video_clip: int, path: str
) -> None:
    response = client.get(path)

    assert response.status_code in (404, 422)
    assert b"SQLite" not in response.content


def test_wrong_media_types_not_served(client: TestClient, env: Env) -> None:
    (env.media_root / "notes.txt").write_text("hello")
    clip_id = env.clip(video_path="notes.txt", thumb_path="notes.txt")

    assert client.get(f"/media/clip/{clip_id}/1/video").status_code == 404
    assert client.get(f"/media/clip/{clip_id}/1/thumb").status_code == 404


def test_resolve_media_path_rejects_nul(env: Env) -> None:
    assert clips.resolve_media_path(env.media_root, "a\x00b.mp4") is None
    assert clips.resolve_media_path(env.media_root, env.media_root.name) is None


# --- approve / reject -------------------------------------------------------------------


def test_reject_requires_reason(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    missing = post(client, f"/clip/{clip_id}/reject")
    invalid = post(client, f"/clip/{clip_id}/reject", reason_tag="meh")

    assert missing.status_code == 422
    assert "Pick a reject reason" in missing.text
    assert invalid.status_code == 422
    assert env.status(clip_id) == "pending_review"
    assert env.reviews(clip_id) == []


def test_reject_writes_audit_and_moves_to_next_pending(client: TestClient, env: Env) -> None:
    older = env.clip(created_at="2027-01-01T00:00:00Z")
    clip_id = env.clip(created_at="2027-01-02T00:00:00Z")

    response = post(client, f"/clip/{clip_id}/reject", reason_tag="bad_cut", version=1)

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/clip/{older}"
    assert env.status(clip_id) == "rejected"
    [row] = env.reviews(clip_id)
    assert (row["action"], row["reason_tag"], row["version"]) == ("reject", "bad_cut", 1)
    assert row["created_at"] == "2027-01-15T08:00:00Z"


def test_double_reject_is_safe(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    first = post(client, f"/clip/{clip_id}/reject", reason_tag="boring")
    second = post(client, f"/clip/{clip_id}/reject", reason_tag="boring")

    assert first.status_code == second.status_code == 200
    assert len(env.reviews(clip_id)) == 1


def test_non_htmx_actions_redirect_to_queue_when_nothing_left(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    response = post(client, f"/clip/{clip_id}/reject", htmx=False, reason_tag="other")

    assert response.status_code == 303
    assert response.headers["location"] == "/queue"
    assert "rejected (other)" in client.get("/queue").text


def test_approve_writes_audit_exports_and_moves_on(
    client: TestClient, env: Env, video_clip: int
) -> None:
    newer = env.clip(created_at="2027-01-20T00:00:00Z")

    response = post(client, f"/clip/{video_clip}/approve", version=1)

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/clip/{newer}"
    assert env.status(video_clip) == "exported"
    actions = [r["action"] for r in env.reviews(video_clip)]
    assert actions == ["approve", "export"]
    assert all(r["created_at"] == "2027-01-15T08:00:00Z" for r in env.reviews(video_clip))
    assert "approved; exporting" in client.get("/queue").text


def test_double_approve_exports_once(client: TestClient, env: Env, video_clip: int) -> None:
    post(client, f"/clip/{video_clip}/approve")
    again = post(client, f"/clip/{video_clip}/approve")

    assert again.status_code == 200
    assert [r["action"] for r in env.reviews(video_clip)] == ["approve", "export"]
    assert len(list((env.output_dir / "ready").rglob("*.mp4"))) == 1


def test_approve_with_failed_export_stays_approved(client: TestClient, env: Env) -> None:
    clip_id = env.clip(video_path="missing.mp4")

    response = post(client, f"/clip/{clip_id}/approve")

    assert response.status_code == 200
    assert env.status(clip_id) == "approved"
    assert [r["action"] for r in env.reviews(clip_id)] == ["approve", "export_failed"]
    page = client.get(f"/clip/{clip_id}").text
    assert "Last export failed (ExportError)" in page
    assert "rendered video" not in page  # detail only in logs
    assert "Retry export" in page


def test_invalid_transitions_rejected(client: TestClient, env: Env) -> None:
    rejected = env.clip(status="rejected")
    exported = env.clip(status="exported")

    approve_rejected = post(client, f"/clip/{rejected}/approve")
    reject_exported = post(client, f"/clip/{exported}/reject", reason_tag="boring")

    assert approve_rejected.status_code == 409
    assert "Cannot approve: clip is rejected" in approve_rejected.text
    assert reject_exported.status_code == 409
    assert env.status(rejected) == "rejected"
    assert env.status(exported) == "exported"
    assert env.reviews(rejected) == [] and env.reviews(exported) == []


def test_stale_version_rejected(client: TestClient, env: Env) -> None:
    clip_id = env.clip(versions=2)

    response = post(client, f"/clip/{clip_id}/reject", reason_tag="boring", version=1)

    assert response.status_code == 409
    assert env.status(clip_id) == "pending_review"


# --- CLP-87: sensitive clips need a face check before approval ----------------------------

FACE_NOTE = "Check faces of minors and victims; the model cannot see the video."
CONFIRM_BOX = 'type="checkbox" name="confirm_sensitive" value="1"'


def test_face_check_note_and_confirm_box_only_on_sensitive_clips(
    client: TestClient, env: Env
) -> None:
    flagged = client.get(f"/clip/{env.clip(sensitive=True)}").text
    plain = client.get(f"/clip/{env.clip()}").text

    assert FACE_NOTE in flagged and CONFIRM_BOX in flagged
    assert FACE_NOTE not in plain and CONFIRM_BOX not in plain


@pytest.mark.parametrize("htmx", [True, False])
@pytest.mark.parametrize("confirm", [None, "", "0", "on"])
def test_approving_a_sensitive_clip_without_confirmation_is_refused(
    client: TestClient, env: Env, htmx: bool, confirm: str | None
) -> None:
    clip_id = env.clip(sensitive=True)
    data = {} if confirm is None else {"confirm_sensitive": confirm}

    response = post(client, f"/clip/{clip_id}/approve", htmx=htmx, version=1, **data)

    assert response.status_code == 422
    assert 'class="error" role="alert"' in response.text
    assert "Confirm you checked faces of minors and victims" in response.text
    assert env.status(clip_id) == "pending_review"
    assert env.reviews(clip_id) == []


@pytest.mark.parametrize("status", ["pending_review", "needs_attention"])
def test_approving_a_sensitive_clip_with_confirmation_works(
    client: TestClient, env: Env, status: str
) -> None:
    clip_id = env.clip(sensitive=True, status=status, video_path="missing.mp4")

    response = post(client, f"/clip/{clip_id}/approve", version=1, confirm_sensitive="1")

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/queue"
    assert env.status(clip_id) == "approved"
    assert [r["action"] for r in env.reviews(clip_id)][0] == "approve"


def test_retrying_the_export_of_an_approved_sensitive_clip_needs_no_new_confirmation(
    client: TestClient, env: Env
) -> None:
    clip_id = env.clip(sensitive=True, video_path="missing.mp4")
    post(client, f"/clip/{clip_id}/approve", confirm_sensitive="1")

    page = client.get(f"/clip/{clip_id}").text
    response = post(client, f"/clip/{clip_id}/approve")

    assert "Retry export" in page and CONFIRM_BOX not in page
    assert response.status_code == 200
    assert [r["action"] for r in env.reviews(clip_id)].count("export_failed") == 2


def test_plain_clip_approves_without_confirmation(client: TestClient, env: Env) -> None:
    clip_id = env.clip(video_path="missing.mp4")

    assert post(client, f"/clip/{clip_id}/approve", version=1).status_code == 200
    assert env.status(clip_id) == "approved"


def test_sensitive_confirmation_still_needs_csrf(client: TestClient, env: Env) -> None:
    clip_id = env.clip(sensitive=True)

    response = client.post(
        f"/clip/{clip_id}/approve", data={"confirm_sensitive": "1", "csrf_token": "nope"}
    )

    assert response.status_code == 403
    assert env.status(clip_id) == "pending_review"


@pytest.mark.parametrize("creator", ["Raffy Tulfo in Action", "manual", "Raffy Tulfo"])
def test_always_sensitive_creator_needs_confirmation_without_the_flag(
    client: TestClient, env: Env, creator: str
) -> None:
    """Review M2: no backfill, so a public_service clip stored before the fix
    (sensitive=0), or one whose creator is not in the config, still needs the check."""
    clip_id = env.clip(env.job(creator), video_path="missing.mp4")

    page = client.get(f"/clip/{clip_id}").text
    refused = post(client, f"/clip/{clip_id}/approve", version=1)
    confirmed = post(client, f"/clip/{clip_id}/approve", version=1, confirm_sensitive="1")

    assert FACE_NOTE in page and CONFIRM_BOX in page and 'class="badge sensitive"' in page
    assert refused.status_code == 422
    assert confirmed.status_code == 200
    assert env.status(clip_id) == "approved"


def test_review_app_without_a_config_fails_closed(env: Env) -> None:
    client = env.client(config=None)
    client.csrf = login(client)  # type: ignore[attr-defined]
    clip_id = env.clip()  # MrBeast, unknown without a config

    assert post(client, f"/clip/{clip_id}/approve", version=1).status_code == 422


def test_stale_version_is_409_before_the_sensitive_check(client: TestClient, env: Env) -> None:
    """Review L2: a stale page gets "reload", not "tick the box" for a clip it never saw."""
    clip_id = env.clip(sensitive=True, versions=2)

    response = post(client, f"/clip/{clip_id}/approve", version=1)

    assert response.status_code == 409
    assert "Cannot approve: clip is pending_review (version 2)." in response.text
    assert env.status(clip_id) == "pending_review"


def test_confirmed_sensitive_approve_is_audited(client: TestClient, env: Env) -> None:
    """Review L1: the review row records that the faces check was confirmed."""
    flagged = env.clip(sensitive=True, video_path="missing.mp4")
    plain = env.clip(video_path="missing.mp4")

    post(client, f"/clip/{flagged}/approve", version=1, confirm_sensitive="1")
    post(client, f"/clip/{plain}/approve", version=1, confirm_sensitive="1")

    assert env.reviews(flagged)[0]["action"] == "approve"
    assert env.reviews(flagged)[0]["payload"] == '{"confirmed_sensitive": true}'
    assert env.reviews(plain)[0]["payload"] is None


def test_versionless_approve_is_pinned_to_the_checked_version(
    client: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review: a version that appears between the check and the approve is not approved."""
    clip_id = env.clip(sensitive=True, versions=2)
    current = clips.get_clip(env.conn, clip_id)
    assert current is not None
    v1 = replace(current, current_version=1, current=current.versions[-1])  # newest first
    monkeypatch.setattr(clips, "get_clip", lambda _conn, _id: v1)

    response = post(client, f"/clip/{clip_id}/approve", confirm_sensitive="1")

    assert response.status_code == 409
    assert env.status(clip_id) == "pending_review"
    assert env.reviews(clip_id) == []


def test_actions_on_missing_clip_404(client: TestClient) -> None:
    assert post(client, "/clip/999/approve").status_code == 404
    assert post(client, "/clip/999/reject", reason_tag="boring").status_code == 404


def test_actions_require_csrf(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    no_token = client.post(f"/clip/{clip_id}/approve")
    bad_token = client.post(
        f"/clip/{clip_id}/reject", data={"reason_tag": "boring", "csrf_token": "nope"}
    )

    assert no_token.status_code == 403
    assert bad_token.status_code == 403
    assert env.status(clip_id) == "pending_review"


def test_actions_require_login(env: Env) -> None:
    clip_id = env.clip()
    client = env.client()

    response = client.post(f"/clip/{clip_id}/approve", headers={"HX-Request": "true"})

    assert response.status_code in (401, 403)
    assert env.status(clip_id) == "pending_review"


def test_form_post_with_csrf_field_works(env: Env) -> None:
    clip_id = env.clip()
    client = TestClient(env.app(), base_url=BASE_URL, follow_redirects=False)
    token = login(client)

    response = client.post(
        f"/clip/{clip_id}/reject", data={"reason_tag": "duplicate", "csrf_token": token}
    )

    assert response.status_code == 303
    assert env.status(clip_id) == "rejected"


# --- state machine unit tests -----------------------------------------------------------


def test_transition_table() -> None:
    assert clips.can_transition("pending_review", ClipStatus.APPROVED)
    assert clips.can_transition("pending_review", ClipStatus.REJECTED)
    assert clips.can_transition("approved", ClipStatus.EXPORTED)
    assert not clips.can_transition("rejected", ClipStatus.APPROVED)
    assert not clips.can_transition("approved", ClipStatus.REJECTED)
    assert not clips.can_transition("nonsense", ClipStatus.APPROVED)


def test_repo_transitions(env: Env) -> None:
    clip_id = env.clip()

    first = clips.approve(env.conn, clip_id, NOW)
    repeat = clips.approve(env.conn, clip_id, NOW)

    assert first.changed and not repeat.changed
    with pytest.raises(InvalidTransition):
        clips.reject(env.conn, clip_id, "boring", NOW)
    with pytest.raises(ValueError, match="reason"):
        clips.reject(env.conn, clip_id, "meh", NOW)
    with pytest.raises(LookupError):
        clips.approve(env.conn, 999, NOW)


def test_hashtag_parsing() -> None:
    assert clips.parse_hashtags('["a", "#b", " ", 3]') == ("#a", "#b", "#3")
    assert clips.parse_hashtags("not json") == ()
    assert clips.parse_hashtags('{"a": 1}') == ()
    assert clips.parse_hashtags(None) == ()


def test_next_pending_wraps_to_newest(env: Env) -> None:
    newest = env.clip(created_at="2027-01-05T00:00:00Z")
    oldest = env.clip(created_at="2027-01-01T00:00:00Z")

    assert clips.next_pending_after(env.conn, oldest) == newest
    assert clips.neighbour_pending(env.conn, 999, older=True) is None
    assert clips.get_clip(env.conn, 999) is None


# --- background export, retry, bounds ---------------------------------------------------


def test_approve_skips_export_already_running(
    client: TestClient, env: Env, video_clip: int
) -> None:
    locks = client.app.state.export_locks  # type: ignore[attr-defined]
    assert locks.try_acquire(video_clip)

    response = post(client, f"/clip/{video_clip}/approve")

    assert response.status_code == 200
    assert env.status(video_clip) == "approved"
    assert [r["action"] for r in env.reviews(video_clip)] == ["approve"]
    locks.release(video_clip)


def test_approve_while_export_running_says_in_progress(
    client: TestClient, env: Env, video_clip: int
) -> None:
    clips.approve(env.conn, video_clip, NOW)
    locks = client.app.state.export_locks  # type: ignore[attr-defined]
    assert locks.try_acquire(video_clip)

    post(client, f"/clip/{video_clip}/approve")

    assert "Export already in progress" in client.get("/queue").text
    assert env.status(video_clip) == "approved"
    locks.release(video_clip)


def test_background_export_sweeps_stale_staging(
    client: TestClient, env: Env, video_clip: int
) -> None:
    stale = env.output_dir / ".staging" / "stale.abc"
    stale.mkdir(parents=True)
    os.utime(stale, (1_000, 1_000))

    post(client, f"/clip/{video_clip}/approve")

    assert not stale.exists()
    assert env.status(video_clip) == "exported"


def test_retry_export_button_schedules_export(
    client: TestClient, env: Env, mp4_bytes: bytes
) -> None:
    clip_id = env.clip(video_path="late.mp4")
    post(client, f"/clip/{clip_id}/approve")
    assert env.status(clip_id) == "approved"
    (env.media_root / "late.mp4").write_bytes(mp4_bytes)

    response = post(client, f"/clip/{clip_id}/approve")

    assert response.status_code == 200
    assert env.status(clip_id) == "exported"
    assert [r["action"] for r in env.reviews(clip_id)] == ["approve", "export_failed", "export"]
    page = client.get("/queue").text
    assert "Clip 1 approved; exporting." in page  # both flashes survive
    assert "Retrying export of clip 1." in page


def test_background_export_crash_is_logged_and_lock_released(
    env: Env, video_clip: int, caplog: pytest.LogCaptureFixture
) -> None:
    def crash(_args: list[str]) -> None:
        raise RuntimeError("boom")

    client = env.client(export_runner=crash, rclone_remote="gdrive:x")
    client.csrf = login(client)  # type: ignore[attr-defined]

    post(client, f"/clip/{video_clip}/approve")

    assert env.status(video_clip) == "approved"
    assert env.reviews(video_clip)[-1]["action"] == "export_failed"
    assert "RuntimeError: boom" in caplog.text
    assert client.app.state.export_locks.try_acquire(video_clip)  # type: ignore[attr-defined]


def test_background_export_escaping_error_is_logged_and_lock_released(
    client: TestClient,
    env: Env,
    video_clip: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(routes, "export_clip", broken)

    post(client, f"/clip/{video_clip}/approve")

    assert env.status(video_clip) == "approved"
    assert "background export of clip" in caplog.text
    assert client.app.state.export_locks.try_acquire(video_clip)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "path",
    [
        f"/clip/{2**63}",
        "/clip/0",
        f"/media/clip/{2**63}/1/video",
        f"/media/clip/1/{2**64}/video",
        "/media/clip/1/0/video",
    ],
)
def test_out_of_range_ids_are_422(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 422


def test_out_of_range_action_ids_are_422(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    assert post(client, f"/clip/{2**63}/approve").status_code == 422
    assert (
        post(client, f"/clip/{clip_id}/reject", reason_tag="boring", version=2**63).status_code
        == 422
    )
    assert env.status(clip_id) == "pending_review"


def test_clip_with_invalid_source_id_renders_without_link(client: TestClient, env: Env) -> None:
    clip_id = env.clip(env.job(item_id="bad id!"))

    response = client.get(f"/clip/{clip_id}")

    assert response.status_code == 200
    assert 'id="source-link"' not in response.text
    assert "no valid source link" in response.text


def test_detail_shows_the_rendered_duration(client: TestClient, env: Env) -> None:
    """Jump cuts: a 45 s source range rendered to 38 s shows 38 s; the source link
    still starts at the source position."""
    clip_id = env.clip(start_s=125.0, end_s=170.0, duration_s=38.0)
    html = client.get(f"/clip/{clip_id}").text
    assert "38s" in html and "45s" not in html


# --- quality gates (CLP-74) -------------------------------------------------------------


def test_needs_attention_clip_lists_its_reasons(client: TestClient, env: Env) -> None:
    clip_id = env.clip(status="needs_attention", gate_reasons=["word_crosses_join", "loudness"])

    html = client.get(f"/clip/{clip_id}").text

    assert 'id="gate-reasons"' in html
    assert "A word is cut at a jump-cut join" in html
    assert "Loudness is off target" in html
    assert 'id="approve-btn"' in html and 'id="reject-btn"' in html


def test_gate_error_is_shown_on_a_pending_clip(client: TestClient, env: Env) -> None:
    clip_id = env.clip(gate_reasons=["gate_error"])
    html = client.get(f"/clip/{clip_id}").text
    assert "A quality check could not run" in html


def test_passing_clip_shows_no_gate_reasons(client: TestClient, env: Env) -> None:
    clip_id = env.clip(gate_reasons=[])
    assert 'id="gate-reasons"' not in client.get(f"/clip/{clip_id}").text


def test_unknown_stored_reason_is_shown_as_is(client: TestClient, env: Env) -> None:
    clip_id = env.clip(status="needs_attention", gate_reasons=["<b>new_check</b>"])
    html = client.get(f"/clip/{clip_id}").text
    assert "&lt;b&gt;new_check&lt;/b&gt;" in html and "<b>new_check</b>" not in html


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_needs_attention_clip_can_be_decided(
    client: TestClient, env: Env, video_clip: int, action: str
) -> None:
    env.conn.execute("UPDATE clips SET status = 'needs_attention' WHERE id = ?", (video_clip,))
    post(client, f"/clip/{video_clip}/{action}", version=1, reason_tag="bad_cut")
    assert env.status(video_clip) == ("exported" if action == "approve" else "rejected")

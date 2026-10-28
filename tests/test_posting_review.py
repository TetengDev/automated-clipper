"""Review app side of CLP-92: approving schedules posts, the Scheduled page, cancel and
reschedule, and the clip page's Posts section."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper.posting import repo
from clipper.posting.models import Post, PostStatus
from clipper.review import routes
from posting_support import FakeAccount
from review_support import Env, login, make_env

NOW = datetime(2027, 1, 15, 8, 0, tzinfo=UTC)  # review_support.WALL_START


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


ACCOUNTS = (
    FakeAccount(id="tt_main", windows=["00:00-23:59"]),
    FakeAccount(id="yt_main", platform="youtube", windows=["00:00-23:59"]),
)


def logged_in(env: Env, **kwargs: object) -> TestClient:
    client = env.client(**kwargs)
    client.csrf = login(client)  # type: ignore[attr-defined]
    return client


@pytest.fixture
def client(env: Env) -> TestClient:
    return logged_in(env, accounts=ACCOUNTS)


def post(client: TestClient, url: str, htmx: bool = True, **data: object):
    headers = {"X-CSRF-Token": client.csrf}  # type: ignore[attr-defined]
    if htmx:
        headers["HX-Request"] = "true"
    return client.post(url, data=data, headers=headers)


def video_clip(env: Env, **kwargs: object) -> int:
    job = env.job()
    rel = f"youtube_{job}/clip_1_v1.mp4"
    target = env.media_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"fake mp4")
    return env.clip(job, video_path=rel, **kwargs)  # type: ignore[arg-type]


def scheduled_post(env: Env, *, at: datetime = NOW + timedelta(hours=2)) -> Post:
    clip_id = video_clip(env, status="approved")
    return repo.insert_post(
        env.conn,
        clip_id=clip_id,
        clip_version=1,
        account_id="tt_main",
        platform="tiktok",
        scheduled_at=at,
        video_path=f"clip-{clip_id}.mp4",
        now=NOW,
    )


def posts_for(env: Env, clip_id: int) -> list[Post]:
    return repo.list_posts(env.conn, clip_id=clip_id)


# --- approve hook ------------------------------------------------------------------------


def test_approve_schedules_one_post_per_account(client: TestClient, env: Env) -> None:
    clip_id = video_clip(env)

    response = post(client, f"/clip/{clip_id}/approve", version=1)

    assert response.status_code == 200
    created = posts_for(env, clip_id)
    assert sorted(p.account_id for p in created) == ["tt_main", "yt_main"]
    assert all(p.status == PostStatus.SCHEDULED for p in created)
    assert all(p.scheduled_at >= NOW for p in created)
    assert "Scheduled 2 post(s)" in client.get("/queue").text


def test_approve_without_accounts_schedules_nothing(env: Env) -> None:
    client = logged_in(env)
    clip_id = video_clip(env)

    response = post(client, f"/clip/{clip_id}/approve", version=1)

    assert response.status_code == 200
    assert posts_for(env, clip_id) == []


def test_approving_a_sensitive_clip_schedules_nothing(client: TestClient, env: Env) -> None:
    clip_id = video_clip(env, sensitive=True)

    response = post(client, f"/clip/{clip_id}/approve", version=1, confirm_sensitive="1")

    assert response.status_code == 200
    assert env.status(clip_id) in ("approved", "exported")
    assert posts_for(env, clip_id) == []


def test_approving_a_clip_of_an_always_sensitive_creator_schedules_nothing(env: Env) -> None:
    job = env.job("Raffy Tulfo in Action")
    clip_id = env.clip(job, video_path="a.mp4")
    accounts = (FakeAccount(creators=["Raffy Tulfo in Action"]),)
    client = logged_in(env, accounts=accounts)

    response = post(client, f"/clip/{clip_id}/approve", version=1, confirm_sensitive="1")

    assert response.status_code == 200
    assert posts_for(env, clip_id) == []


def test_scheduling_error_never_breaks_the_approval(
    client: TestClient, env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_: object, **__: object) -> list[Post]:
        raise RuntimeError("scheduler exploded")

    monkeypatch.setattr(routes, "schedule_for_clip", boom)
    clip_id = video_clip(env)

    response = post(client, f"/clip/{clip_id}/approve", version=1)

    assert response.status_code == 200
    assert env.status(clip_id) in ("approved", "exported")
    assert "scheduler exploded" in caplog.text
    assert posts_for(env, clip_id) == []


def test_retrying_the_export_does_not_duplicate_posts(client: TestClient, env: Env) -> None:
    clip_id = video_clip(env)
    post(client, f"/clip/{clip_id}/approve", version=1)

    post(client, f"/clip/{clip_id}/approve", version=1)

    assert len(posts_for(env, clip_id)) == 2


# --- scheduled page ----------------------------------------------------------------------


def test_scheduled_page_lists_upcoming_and_failed_posts(client: TestClient, env: Env) -> None:
    upcoming = scheduled_post(env)
    failed = scheduled_post(env)
    env.conn.execute(
        "UPDATE posts SET status = 'post_failed', error = 'quota exceeded' WHERE id = ?",
        (failed.id,),
    )

    page = client.get("/scheduled")

    assert page.status_code == 200
    html = page.text
    assert f'id="post-{upcoming.id}"' in html
    assert f'id="post-{failed.id}"' in html
    assert "quota exceeded" in html
    assert f'action="/posts/{upcoming.id}/cancel"' in html
    assert f'action="/posts/{upcoming.id}/reschedule"' in html
    assert 'href="/scheduled"' in html  # nav link


def test_scheduled_page_shows_an_empty_state(client: TestClient) -> None:
    assert "No upcoming posts" in client.get("/scheduled").text


def test_scheduled_page_needs_a_login(env: Env) -> None:
    response = env.client().get("/scheduled")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_scheduled_page_only_links_https_post_urls(client: TestClient, env: Env) -> None:
    good = scheduled_post(env)
    bad = scheduled_post(env)
    env.conn.execute(
        "UPDATE posts SET status = 'posted', url = 'https://tiktok.test/v/1' WHERE id = ?",
        (good.id,),
    )
    env.conn.execute(
        "UPDATE posts SET status = 'posted', url = 'javascript:alert(1)' WHERE id = ?",
        (bad.id,),
    )

    html = client.get("/scheduled").text

    assert 'href="https://tiktok.test/v/1"' in html
    assert "javascript:alert" not in html


# --- cancel and reschedule ----------------------------------------------------------------


def test_cancel_needs_the_csrf_token(client: TestClient, env: Env) -> None:
    target = scheduled_post(env)

    response = client.post(f"/posts/{target.id}/cancel")

    assert response.status_code == 403
    assert repo.get_post(env.conn, target.id).status == PostStatus.SCHEDULED  # type: ignore[union-attr]


def test_cancel_needs_a_login(env: Env) -> None:
    target = scheduled_post(env)

    response = env.client().post(f"/posts/{target.id}/cancel")

    assert response.status_code in (303, 403)
    assert repo.get_post(env.conn, target.id).status == PostStatus.SCHEDULED  # type: ignore[union-attr]


def test_cancel_cancels_and_audits(client: TestClient, env: Env) -> None:
    target = scheduled_post(env)

    response = post(client, f"/posts/{target.id}/cancel")

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == "/scheduled"
    assert repo.get_post(env.conn, target.id).status == PostStatus.CANCELLED  # type: ignore[union-attr]
    assert env.reviews(target.clip_id)[-1]["action"] == "post_cancelled"


def test_cancel_without_htmx_redirects_to_a_safe_next(client: TestClient, env: Env) -> None:
    target = scheduled_post(env)

    response = post(
        client, f"/posts/{target.id}/cancel", htmx=False, next=f"/clip/{target.clip_id}"
    )
    evil = scheduled_post(env)
    other = post(client, f"/posts/{evil.id}/cancel", htmx=False, next="https://evil.test/")

    assert response.status_code == 303
    assert response.headers["location"] == f"/clip/{target.clip_id}"
    assert other.headers["location"] == "/"


def test_cancel_a_posted_post_is_a_conflict(client: TestClient, env: Env) -> None:
    target = scheduled_post(env)
    env.conn.execute("UPDATE posts SET status = 'posted' WHERE id = ?", (target.id,))

    response = post(client, f"/posts/{target.id}/cancel")

    assert response.status_code == 409
    assert "posted" in response.text


def test_unknown_post_is_404(client: TestClient) -> None:
    assert post(client, "/posts/999/cancel").status_code == 404
    assert post(client, "/posts/0/cancel").status_code == 422


def test_reschedule_moves_the_post(client: TestClient, env: Env) -> None:
    target = scheduled_post(env)

    response = post(client, f"/posts/{target.id}/reschedule", scheduled_at="2027-01-16T09:30")

    assert response.status_code == 200
    moved = repo.get_post(env.conn, target.id)
    assert moved is not None
    assert moved.scheduled_at == datetime(2027, 1, 16, 9, 30, tzinfo=UTC)
    assert env.reviews(target.clip_id)[-1]["action"] == "post_rescheduled"


def test_reschedule_requeues_a_failed_post(client: TestClient, env: Env) -> None:
    target = scheduled_post(env)
    env.conn.execute(
        "UPDATE posts SET status = 'post_failed', attempts = 3 WHERE id = ?", (target.id,)
    )

    post(client, f"/posts/{target.id}/reschedule", scheduled_at="2027-01-16T09:30")

    moved = repo.get_post(env.conn, target.id)
    assert moved is not None
    assert moved.status == PostStatus.SCHEDULED and moved.attempts == 0


@pytest.mark.parametrize("value", ["", "tomorrow", "2027-01-14T09:00", "2029-01-01T00:00"])
def test_reschedule_rejects_bad_or_past_or_far_times(
    client: TestClient, env: Env, value: str
) -> None:
    target = scheduled_post(env)

    response = post(client, f"/posts/{target.id}/reschedule", scheduled_at=value)

    assert response.status_code == 422
    assert repo.get_post(env.conn, target.id).scheduled_at == target.scheduled_at  # type: ignore[union-attr]


# --- clip page ---------------------------------------------------------------------------


def test_clip_page_loads_its_posts_section(client: TestClient, env: Env) -> None:
    target = scheduled_post(env)

    page = client.get(f"/clip/{target.clip_id}").text
    section = client.get(f"/clip/{target.clip_id}/posts")

    assert f'hx-get="/clip/{target.clip_id}/posts"' in page
    assert section.status_code == 200
    assert f'id="post-{target.id}"' in section.text
    assert "tt_main" in section.text
    assert "<html" not in section.text  # a partial


def test_clip_posts_section_says_when_there_are_none(client: TestClient, env: Env) -> None:
    clip_id = video_clip(env)

    assert "Not scheduled" in client.get(f"/clip/{clip_id}/posts").text


def test_clip_posts_section_for_unknown_clip_is_404(client: TestClient) -> None:
    assert client.get("/clip/999/posts").status_code == 404

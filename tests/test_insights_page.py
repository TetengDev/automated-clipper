"""The insights pages of the review app (CLP-154)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper.insights import repo
from clipper.insights.repo import PUBLIC, ChannelMetrics
from insights_support import ACCOUNT, HAND_UPLOADS, T0, short, upload
from posting_support import FakeAccount
from review_support import Env, login, make_env

CHANNEL = FakeAccount(
    id=ACCOUNT,
    platform="youtube",
    content="commentary",
    creators=[],
    timezone="America/New_York",
    windows=["00:00-23:59"],
)
CLIPS = FakeAccount(id="yt_main", platform="youtube", windows=["00:00-23:59"])
TODAY = date(2027, 1, 15)  # review_support's wall clock: 2027-01-15T08:00Z, 03:00 in New York
SETTLED = T0 - timedelta(days=5)
"""Public long enough to count in the tag breakdown (FINDINGS_MIN_AGE_DAYS = 3)."""
FRESH = datetime(2027, 1, 14, 20, 0, tzinfo=UTC)
"""Public for 12 hours at the wall clock: in the Shorts table, not in the tag breakdown."""


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


@pytest.fixture
def client(env: Env) -> TestClient:
    c = env.client(accounts=(CHANNEL, CLIPS))
    login(c)
    return c


def seed(env: Env) -> None:
    for day, subs, views in ((TODAY - timedelta(days=7), 300, 860_000), (TODAY, 370, 1_000_000)):
        repo.upsert_channel_metrics(
            env.conn,
            ChannelMetrics(
                ACCOUNT,
                day,
                PUBLIC,
                subscribers=subs,
                total_views=50_000,
                shorts_views_90d=views,
                public_uploads_90d=5,
            ),
            T0,
        )
    post = short(env.conn, title="Big win", scheduled_at=SETTLED)
    seeded = [
        (post.external_id or "", "Big win", 400, "question", post.id),
        (HAND_UPLOADS[0], "Mid", 200, "question", None),  # hand uploads
        (HAND_UPLOADS[1], "Low", 100, "list", None),
    ]
    for vid, title, n, hook, post_id in seeded:
        video = upload(env.conn, vid, title=title, published_at=SETTLED, post_id=post_id)
        repo.add_video_metrics(env.conn, video, PUBLIC, repo.PostMetrics(views=n), T0)
        repo.set_tags(env.conn, {"hook_type": hook}, video_id=vid)


def tags_section(html: str) -> str:
    return html.split("<h2>By tag</h2>", 1)[1]


def test_the_page_shows_the_tracker_trend_shorts_and_tags(client: TestClient, env: Env) -> None:
    seed(env)

    page = client.get(f"/insights/{ACCOUNT}")

    assert page.status_code == 200
    html = page.text
    assert "Tier 1: fan funding" in html and "Tier 2: ad revenue" in html
    assert "370 of 500" in html
    assert "about 13 days at the last 7 days&#39; pace" in html
    assert "estimate" in html
    assert 'id="trend"' in html and "2027-01-08" in html
    assert "Big win" in html and "2.0x" in html and "0.5x" in html
    assert "hand upload" in html and "pipeline" in html
    assert 'href="https://www.youtube.com/shorts/2BpbZneO_To"' in html
    assert 'id="tags"' in html and "question" in html and "hook_type" in html
    assert 'id="reauth"' not in html and 'id="no-token"' not in html


def test_every_short_links_to_youtube_by_video_id(client: TestClient, env: Env) -> None:
    seed(env)

    html = client.get(f"/insights/{ACCOUNT}").text

    for vid in (*HAND_UPLOADS[:2], "vid"):
        assert f'href="https://www.youtube.com/shorts/{vid}' in html


def test_fresh_shorts_are_in_the_table_but_not_the_tags(client: TestClient, env: Env) -> None:
    seed(env)
    video = upload(env.conn, HAND_UPLOADS[2], title="Just out", published_at=FRESH)
    repo.add_video_metrics(env.conn, video, PUBLIC, repo.PostMetrics(views=50), T0)
    repo.set_tags(env.conn, {"hook_type": "reaction"}, video_id=HAND_UPLOADS[2])

    html = client.get(f"/insights/{ACCOUNT}").text

    assert "Just out" in html and "0.3x" in html  # 50 over the median of 50/100/200/400
    tags = tags_section(html)
    assert "question" in tags and "reaction" not in tags
    assert "public for 3 days or more" in tags


def test_the_page_says_which_metrics_need_reauthorisation(client: TestClient, env: Env) -> None:
    repo.set_analytics_state(
        env.conn, ACCOUNT, repo.AnalyticsState.NEEDS_REAUTH, "re-authorise", T0
    )

    html = client.get(f"/insights/{ACCOUNT}").text

    assert 'id="reauth"' in html and 'id="no-token"' not in html
    assert "yt-analytics.readonly" in html
    assert "engaged views" in html and "retention" in html


def test_the_page_says_when_no_refresh_token_is_set(client: TestClient, env: Env) -> None:
    repo.set_analytics_state(env.conn, ACCOUNT, repo.AnalyticsState.NO_TOKEN, "not set", T0)

    html = client.get(f"/insights/{ACCOUNT}").text

    assert 'id="no-token"' in html and 'id="reauth"' not in html
    assert "CLIPPER_REFRESH_TOKEN_RANDOM_GAMING_YT" in html
    assert "engaged views" in html and "retention" in html


def test_an_empty_channel_renders(client: TestClient) -> None:
    html = client.get(f"/insights/{ACCOUNT}").text

    assert "no data yet" in html and "No Shorts with numbers yet" in html
    assert "<progress" not in html  # no data is not a 0% bar


def test_unknown_and_clips_accounts_are_404(client: TestClient) -> None:
    assert client.get("/insights/nobody_yt").status_code == 404
    assert client.get("/insights/yt_main").status_code == 404
    assert client.get("/insights/Bad-Id").status_code == 422


def test_a_disabled_channel_is_404(env: Env) -> None:
    disabled = replace(CHANNEL, enabled=False)
    c = env.client(accounts=(disabled,))
    login(c)

    assert c.get(f"/insights/{ACCOUNT}").status_code == 404
    assert f'href="/insights/{ACCOUNT}"' not in c.get("/insights").text


def test_the_index_lists_commentary_channels_and_the_nav_links_it(client: TestClient) -> None:
    html = client.get("/insights").text

    assert f'href="/insights/{ACCOUNT}"' in html
    assert "yt_main" not in html
    assert 'href="/insights"' in client.get("/queue").text


def test_insights_need_a_login(env: Env) -> None:
    anonymous = env.client(accounts=(CHANNEL,))

    response = anonymous.get(f"/insights/{ACCOUNT}")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")

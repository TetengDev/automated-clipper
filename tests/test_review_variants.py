"""Per-platform title and caption on the clip page, editable before approval (CLP-96)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from review_support import Env, login, make_env

PLATFORMS = ("tiktok", "instagram", "youtube", "facebook")
STORED = {
    p: {"title": f"{p} title", "caption": f"{p} caption", "hashtags": ["#kai", "#chair", "#fall"]}
    for p in PLATFORMS
}


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


def post(client: TestClient, clip_id: int, htmx: bool = True, **data: object):
    headers = {"X-CSRF-Token": client.csrf}  # type: ignore[attr-defined]
    if htmx:
        headers["HX-Request"] = "true"
    return client.post(f"/clip/{clip_id}/variants", data=data, headers=headers)


def store(env: Env, clip_id: int, variants: object, version: int = 1) -> None:
    env.conn.execute(
        "UPDATE clip_versions SET variants = ? WHERE clip_id = ? AND version = ?",
        (json.dumps(variants), clip_id, version),
    )


def stored(env: Env, clip_id: int, version: int = 1) -> dict[str, dict[str, object]]:
    row = env.conn.execute(
        "SELECT variants FROM clip_versions WHERE clip_id = ? AND version = ?", (clip_id, version)
    ).fetchone()
    return json.loads(row[0])


# --- the page -------------------------------------------------------------------------------


def test_detail_shows_each_platforms_title_caption_and_hashtags(
    client: TestClient, env: Env
) -> None:
    clip_id = env.clip()
    store(env, clip_id, STORED)

    html = client.get(f"/clip/{clip_id}").text

    assert 'id="clip-variants"' in html
    assert f'action="/clip/{clip_id}/variants"' in html
    assert html.index('id="clip-variants"') < html.index('id="approve-btn"')
    for p in PLATFORMS:
        assert f'name="{p}_title"' in html and f'value="{p} title"' in html
        assert f'name="{p}_caption"' in html and f"{p} caption</textarea>" in html
    assert "#kai #chair #fall" in html
    assert 'name="csrf_token"' in html


def test_clip_from_before_variants_shows_derived_text(client: TestClient, env: Env) -> None:
    clip_id = env.clip(title="Chair falls", caption="Kai falls off", hashtags=["#kai", "#viral"])

    html = client.get(f"/clip/{clip_id}").text

    assert html.count('value="Chair falls"') == 4
    assert html.count("Kai falls off</textarea>") == 4


def test_variant_text_is_html_escaped(client: TestClient, env: Env) -> None:
    clip_id = env.clip()
    evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
    store(env, clip_id, {p: {"title": evil, "caption": evil, "hashtags": []} for p in PLATFORMS})

    html = client.get(f"/clip/{clip_id}").text

    assert "<script>alert(1)" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_no_edit_form_once_the_clip_is_approved(client: TestClient, env: Env) -> None:
    clip_id = env.clip(status="approved")
    store(env, clip_id, STORED)

    html = client.get(f"/clip/{clip_id}").text

    assert "tiktok title" in html
    assert f'action="/clip/{clip_id}/variants"' not in html


# --- the edit route ---------------------------------------------------------------------------


def test_edit_saves_title_and_caption_and_is_audited(client: TestClient, env: Env) -> None:
    clip_id = env.clip()
    store(env, clip_id, STORED)

    response = post(
        client,
        clip_id,
        version=1,
        tiktok_title="Kai Cenat chair collapse",
        tiktok_caption="Kai Cenat's chair gives out mid-stream\nHe did not see it coming",
        youtube_title="Kai Cenat chair collapse on stream",
    )

    assert response.status_code == 200
    assert response.headers["HX-Redirect"] == f"/clip/{clip_id}"
    saved = stored(env, clip_id)
    assert saved["tiktok"] == {
        "title": "Kai Cenat chair collapse",
        "caption": "Kai Cenat's chair gives out mid-stream\nHe did not see it coming",
        "hashtags": ["#kai", "#chair", "#fall"],
    }
    assert saved["youtube"]["title"] == "Kai Cenat chair collapse on stream"
    assert saved["youtube"]["caption"] == "youtube caption"
    assert saved["instagram"] == STORED["instagram"]
    (review,) = env.reviews(clip_id)
    assert (review["action"], review["version"]) == ("variants_edited", 1)
    assert json.loads(review["payload"]) == {"platforms": ["tiktok", "youtube"]}
    assert env.status(clip_id) == "pending_review"


def test_edit_without_htmx_redirects_back_to_the_clip(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    response = post(client, clip_id, htmx=False, version=1, facebook_title="New title")

    assert response.status_code == 303
    assert response.headers["location"] == f"/clip/{clip_id}"
    assert stored(env, clip_id)["facebook"]["title"] == "New title"


def test_edit_writes_only_the_current_version(client: TestClient, env: Env) -> None:
    clip_id = env.clip(versions=2)
    store(env, clip_id, STORED, version=1)
    store(env, clip_id, STORED, version=2)

    post(client, clip_id, version=2, instagram_caption="Fresh")

    assert stored(env, clip_id, 2)["instagram"]["caption"] == "Fresh"
    assert stored(env, clip_id, 1) == STORED


def test_stale_version_is_409_and_changes_nothing(client: TestClient, env: Env) -> None:
    clip_id = env.clip(versions=2)
    store(env, clip_id, STORED, version=2)

    response = post(client, clip_id, version=1, tiktok_title="Too late")

    assert response.status_code == 409
    assert "version 2" in response.text
    assert stored(env, clip_id, 2) == STORED
    assert env.reviews(clip_id) == []


@pytest.mark.parametrize("status", ["approved", "exported", "rejected"])
def test_edit_after_review_is_409(client: TestClient, env: Env, status: str) -> None:
    clip_id = env.clip(status=status)
    store(env, clip_id, STORED)

    response = post(client, clip_id, version=1, tiktok_title="Changed")

    assert response.status_code == 409
    assert stored(env, clip_id) == STORED


def test_over_limit_text_is_422_and_changes_nothing(client: TestClient, env: Env) -> None:
    clip_id = env.clip()
    store(env, clip_id, STORED)

    too_long_title = post(client, clip_id, version=1, youtube_title="y" * 101)
    too_long_caption = post(client, clip_id, version=1, instagram_caption="c" * 2201)

    assert too_long_title.status_code == 422
    assert "youtube" in too_long_title.text.lower() and "title" in too_long_title.text
    assert too_long_caption.status_code == 422
    assert "instagram" in too_long_caption.text.lower()
    assert stored(env, clip_id) == STORED
    assert env.reviews(clip_id) == []


def test_youtube_caption_may_be_longer_than_the_others(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    response = post(client, clip_id, version=1, youtube_caption="d " * 2400)

    assert response.status_code == 200
    assert len(stored(env, clip_id)["youtube"]["caption"]) <= 5000


def test_control_characters_are_stripped_from_edits(client: TestClient, env: Env) -> None:
    clip_id = env.clip()

    post(client, clip_id, version=1, tiktok_title="Kai‮\x07 falls")

    assert stored(env, clip_id)["tiktok"]["title"] == "Kai falls"


def test_unknown_clip_is_404(client: TestClient) -> None:
    assert post(client, 999, version=1, tiktok_title="x").status_code == 404


def test_version_is_required(client: TestClient, env: Env) -> None:
    clip_id = env.clip()
    assert post(client, clip_id, tiktok_title="x").status_code == 422


def test_edit_requires_csrf(client: TestClient, env: Env) -> None:
    clip_id = env.clip()
    store(env, clip_id, STORED)

    no_token = client.post(f"/clip/{clip_id}/variants", data={"version": 1, "tiktok_title": "x"})
    bad_token = client.post(
        f"/clip/{clip_id}/variants",
        data={"version": 1, "tiktok_title": "x", "csrf_token": "nope"},
    )

    assert no_token.status_code == 403 and bad_token.status_code == 403
    assert stored(env, clip_id) == STORED


def test_edit_requires_login(env: Env) -> None:
    clip_id = env.clip()
    store(env, clip_id, STORED)

    response = env.client().post(
        f"/clip/{clip_id}/variants",
        data={"version": 1, "tiktok_title": "x"},
        headers={"HX-Request": "true"},
    )

    assert response.status_code in (401, 403)
    assert stored(env, clip_id) == STORED

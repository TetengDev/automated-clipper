"""Review queue page: default view, filters, sort, counts, empty state, paging (CLP-19)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clipper import clips
from clipper.clips import QueueFilter
from review_support import Env, login, make_env


@pytest.fixture
def env(tmp_path: Path) -> Iterator[Env]:
    environment = make_env(tmp_path)
    yield environment
    environment.conn.close()


@pytest.fixture
def client(env: Env) -> TestClient:
    client = env.client()
    login(client)
    return client


def card_ids(html: str) -> list[int]:
    return [int(m) for m in re.findall(r'data-clip-id="(\d+)"', html)]


@pytest.fixture
def seeded(env: Env) -> dict[str, int]:
    beast = env.job("MrBeast", title="Beast source")
    kai = env.job("Kai Cenat", platform="twitch", item_id="123456789", title="Kai stream")
    return {
        "old_pending": env.clip(beast, created_at="2027-01-01T00:00:00Z", score=9.0),
        "new_pending": env.clip(beast, created_at="2027-01-05T00:00:00Z", score=5.0),
        "kai_pending": env.clip(kai, created_at="2027-01-03T00:00:00Z", score=7.0),
        "kai_rejected": env.clip(kai, status="rejected", created_at="2027-01-04T00:00:00Z"),
        "beast_exported": env.clip(beast, status="exported", created_at="2027-01-02T00:00:00Z"),
    }


def test_queue_requires_login(env: Env) -> None:
    response = env.client().get("/queue")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_default_view_lists_only_pending_newest_first(
    client: TestClient, seeded: dict[str, int]
) -> None:
    response = client.get("/queue")

    assert response.status_code == 200
    assert card_ids(response.text) == [
        seeded["new_pending"],
        seeded["kai_pending"],
        seeded["old_pending"],
    ]


def test_root_renders_the_queue(client: TestClient, seeded: dict[str, int]) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert "Review queue" in response.text
    assert len(card_ids(response.text)) == 3


def test_card_shows_metadata_and_thumbnail(client: TestClient, env: Env) -> None:
    job = env.job("MrBeast", title="The Source Title")
    clip_id = env.clip(job, title="Huge jump", score=8.25, thumb_path="t.jpg")

    html = client.get("/queue").text

    assert "Huge jump" in html
    assert "MrBeast" in html
    assert "30s" in html
    assert "8.2" in html
    assert "The Source Title" in html
    assert f'src="/media/clip/{clip_id}/1/thumb"' in html


def test_creator_filter(client: TestClient, seeded: dict[str, int]) -> None:
    response = client.get("/queue", params={"creator": "Kai Cenat"})

    assert card_ids(response.text) == [seeded["kai_pending"]]
    assert '<option value="Kai Cenat" selected>' in response.text


def test_status_filter_and_all(client: TestClient, seeded: dict[str, int]) -> None:
    rejected = client.get("/queue", params={"status": "rejected"})
    everything = client.get("/queue", params={"status": "all"})

    assert card_ids(rejected.text) == [seeded["kai_rejected"]]
    assert len(card_ids(everything.text)) == 5


def test_combined_filters_reflected_in_links(client: TestClient, seeded: dict[str, int]) -> None:
    response = client.get("/queue", params={"creator": "Kai Cenat", "status": "all"})

    assert sorted(card_ids(response.text)) == sorted(
        [seeded["kai_pending"], seeded["kai_rejected"]]
    )
    # Status tabs keep the creator filter in their URL query.
    assert "/queue?status=rejected&amp;sort=newest&amp;creator=Kai+Cenat" in response.text


def test_min_score_filter_and_score_sort(client: TestClient, seeded: dict[str, int]) -> None:
    filtered = client.get("/queue", params={"min_score": "6.5"})
    by_score = client.get("/queue", params={"sort": "score"})

    assert sorted(card_ids(filtered.text)) == sorted([seeded["old_pending"], seeded["kai_pending"]])
    assert card_ids(by_score.text) == [
        seeded["old_pending"],
        seeded["kai_pending"],
        seeded["new_pending"],
    ]


def test_empty_min_score_is_ignored(client: TestClient, seeded: dict[str, int]) -> None:
    response = client.get("/queue", params={"min_score": ""})

    assert response.status_code == 200
    assert len(card_ids(response.text)) == 3


@pytest.mark.parametrize(
    "params", [{"status": "bogus"}, {"min_score": "abc"}, {"min_score": "nan"}, {"page": "0"}]
)
def test_invalid_filters_rejected(client: TestClient, params: dict[str, str]) -> None:
    assert client.get("/queue", params=params).status_code == 422


def test_counts_per_status(client: TestClient, seeded: dict[str, int]) -> None:
    html = client.get("/queue").text

    assert re.search(
        r'data-status="pending_review"[^>]*>pending review <span class="count">3<', html
    )
    assert re.search(r'data-status="rejected"[^>]*>rejected <span class="count">1<', html)
    assert re.search(r'data-status="exported"[^>]*>exported <span class="count">1<', html)
    assert 'id="queue-total">3<' in html


def test_empty_state(client: TestClient) -> None:
    html = client.get("/queue").text

    assert "No clips waiting for review." in html
    assert card_ids(html) == []


def test_paging_with_htmx_load_more(client: TestClient, env: Env) -> None:
    job = env.job()
    ids = [
        env.clip(job, created_at=f"2027-01-{day:02d}T00:00:00Z")
        for day in range(1, clips.PAGE_SIZE + 3)
    ]

    first = client.get("/queue")
    more = client.get(
        "/queue", params={"page": 2, "status": "pending_review"}, headers={"HX-Request": "true"}
    )

    assert len(card_ids(first.text)) == clips.PAGE_SIZE
    assert 'hx-get="/queue?status=pending_review&amp;sort=newest&amp;page=2"' in first.text
    assert "<html" not in more.text
    assert card_ids(more.text) == [ids[1], ids[0]]
    assert "Load more" not in more.text


def test_count_and_list_helpers(env: Env, seeded: dict[str, int]) -> None:
    flt = QueueFilter(status=None, creator="MrBeast")

    assert clips.count_queue(env.conn, flt) == 3
    assert clips.list_creators(env.conn) == ["Kai Cenat", "MrBeast"]
    assert clips.status_counts(env.conn, flt) == {"pending_review": 2, "exported": 1}


@pytest.mark.parametrize(
    ("params", "text"),
    [
        ({"status": "rejected"}, "No rejected clips."),
        ({"status": "all"}, "No clips yet."),
        ({"creator": "Nobody"}, "Nothing matches the current creator / score filters."),
    ],
)
def test_empty_state_reflects_filters(
    client: TestClient, params: dict[str, str], text: str
) -> None:
    html = client.get("/queue", params=params).text

    assert text in html
    assert card_ids(html) == []


def test_card_shows_the_rendered_duration(client: TestClient, env: Env) -> None:
    """Jump cuts: a 45 s source range rendered to 38 s shows 38 s."""
    env.clip(start_s=125.0, end_s=170.0, duration_s=38.0)
    html = client.get("/queue").text
    assert "· 38s ·" in html and "45s" not in html


def test_sensitive_badge_only_on_sensitive_cards(client: TestClient, env: Env) -> None:
    job = env.job("Kai Cenat", title="Case")
    flagged = env.clip(job, title="Flagged one", sensitive=True, created_at="2027-01-02T00:00:00Z")
    env.clip(job, title="Plain one", created_at="2027-01-01T00:00:00Z")

    html = client.get("/queue").text

    cards = html.split('class="clip-card"')[1:]
    assert len(cards) == 2
    assert f'data-clip-id="{flagged}"' in cards[0] and 'class="badge sensitive"' in cards[0]
    assert 'class="badge sensitive"' not in cards[1]
    assert "check for names, faces or identifiers of victims and minors" in cards[0]


def test_sensitive_cards_carry_the_face_check_note(client: TestClient, env: Env) -> None:
    """CLP-87: visible text, not only the badge's hover title (no hover on a phone)."""
    note = "Check faces of minors and victims; the model cannot see the video."
    job = env.job("Kai Cenat", title="Case")
    env.clip(job, title="Flagged one", sensitive=True, created_at="2027-01-02T00:00:00Z")
    env.clip(job, title="Plain one", created_at="2027-01-01T00:00:00Z")

    cards = client.get("/queue").text.split('class="clip-card"')[1:]

    assert f'<span class="sensitive-note">{note}</span>' in cards[0]
    assert note not in cards[1]


@pytest.mark.parametrize("creator", ["Raffy Tulfo in Action", "manual"])
def test_always_sensitive_creator_cards_show_badge_and_note_without_the_flag(
    client: TestClient, env: Env, creator: str
) -> None:
    """CLP-87 review M2: clips stored before the fix (sensitive=0) of a public_service
    creator, or of a creator not in the config, still get the faces check."""
    env.clip(env.job(creator, title="Case"), title="Old clip")

    (card,) = client.get("/queue").text.split('class="clip-card"')[1:]

    assert 'class="badge sensitive"' in card
    assert "Check faces of minors and victims; the model cannot see the video." in card


def test_needs_attention_has_its_own_tab(client: TestClient, env: Env) -> None:
    env.clip(status="needs_attention", title="Flagged", gate_reasons=["loudness"])
    env.clip(title="Plain")
    pending = client.get("/queue").text
    assert "Flagged" not in pending and "Plain" in pending
    flagged = client.get("/queue?status=needs_attention").text
    assert "Flagged" in flagged and "Plain" not in flagged

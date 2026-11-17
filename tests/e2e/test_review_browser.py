"""The review app in a real browser (CLP-61): login, queue counts, keyboard review, the
HTMX error swaps, campaign checks, form limits and video seeking."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

import pytest
from e2e_support import LiveApp
from playwright.sync_api import BrowserContext, Page, Playwright, Response, expect

from clipper.campaigns import Campaign
from clipper.review.app import _INVALID_FORM, MAX_FORM_FIELDS
from clipper.review.routes import CHECKS_UNCONFIRMED
from posting_support import FakeAccount

pytestmark = pytest.mark.e2e

StartApp = Callable[..., LiveApp]
CREATORS = ("MrBeast", "Kai Cenat", "Raffy Tulfo in Action")


def card_hrefs(page: Page) -> list[str]:
    """The queue cards' links, in page order: `/clip/<id>?<the queue view>`."""
    cards = page.locator(".clip-card")
    return cards.evaluate_all("cards => cards.map(card => card.getAttribute('href'))")


def clip_id_of(href: str) -> int:
    return int(href.partition("?")[0].removeprefix("/clip/"))


def audit(conn: sqlite3.Connection, clip_id: int, action: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM clip_reviews WHERE clip_id = ? AND action = ?", (clip_id, action)
    ).fetchall()


def test_login_refuses_a_wrong_password_then_lands_on_the_queue_and_logs_out(
    live_app: StartApp, page: Page
) -> None:
    app = live_app()
    page.goto(app("/queue"))
    expect(page).to_have_url(app("/login?next=%2Fqueue"))

    page.get_by_label("Password").fill("not-the-password")
    page.get_by_role("button", name="Sign in").click()
    expect(page.get_by_role("alert")).to_have_text("Incorrect password.")
    expect(page.get_by_role("heading", name="Sign in")).to_be_visible()

    page.get_by_label("Password").fill(app.password)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(app("/queue"))
    expect(page.get_by_role("heading", name="Review queue")).to_be_visible()

    page.get_by_role("button", name="Sign out").click()
    expect(page).to_have_url(app("/login"))
    expect(page.get_by_role("status")).to_have_text("Signed out.")
    page.goto(app("/queue"))
    expect(page).to_have_url(app("/login?next=%2Fqueue"))


def test_queue_counts_match_the_db_for_each_creator_and_status(
    live_app: StartApp, page: Page
) -> None:
    app = live_app()
    seeded = {
        "MrBeast": ["pending_review"] * 3 + ["approved", "rejected", "rejected"],
        "Kai Cenat": ["pending_review", "pending_review", "exported", "needs_attention"],
        "Raffy Tulfo in Action": ["pending_review", "needs_attention", "archived"],
    }
    for creator, statuses in seeded.items():
        job = app.env.job(creator)
        for status in statuses:
            app.clip(job, status=status)
    app.login(page)

    for creator in ("", *CREATORS):
        page.goto(app("/queue"))
        page.locator("select[name=creator]").select_option(creator)
        page.locator("select[name=status]").select_option("all")
        page.get_by_role("button", name="Apply").click()
        expect(page.locator(".tabs a[aria-current=page]")).to_contain_text("all")
        counts = dict(
            app.env.conn.execute(
                "SELECT c.status, COUNT(*) FROM clips c JOIN jobs j ON j.id = c.job_id"
                " WHERE ? = '' OR j.creator = ? GROUP BY c.status",
                (creator, creator),
            ).fetchall()
        )
        tabs = page.locator(".tabs")
        expect(tabs.locator("a").first.locator(".count")).to_have_text(str(sum(counts.values())))
        expect(page.locator(".clip-card")).to_have_count(sum(counts.values()))
        for status in ("pending_review", "needs_attention", "approved", "exported", "rejected"):
            tab = tabs.locator(f"a[data-status={status}]")
            expect(tab.locator(".count")).to_have_text(str(counts.get(status, 0)))
        for status, n in counts.items():
            tabs.locator(f"a[data-status={status}]").click()
            expect(page.locator(f".tabs a[data-status={status}]")).to_have_attribute(
                "aria-current", "page"
            )
            expect(page.locator("#queue-total")).to_have_text(str(n))
            cards = page.locator(".clip-card")
            expect(cards).to_have_count(n)
            if creator:
                expect(cards.locator(".meta > span").first).to_contain_text(creator)


def test_a_approves_and_moves_to_the_next_clip(live_app: StartApp, page: Page) -> None:
    app = live_app()
    job = app.env.job("MrBeast")
    for day in (1, 2, 3):
        app.clip(job, created_at=f"2027-01-0{day}T00:00:00Z")
    app.login(page)
    first, second, _ = card_hrefs(page)
    page.locator(".clip-card").first.click()
    expect(page).to_have_url(app(first))

    page.keyboard.press("a")

    expect(page).to_have_url(app(second))
    clip_id = clip_id_of(first)
    expect(page.get_by_role("status").first).to_contain_text(f"Clip {clip_id} approved")
    assert app.env.status(clip_id) in ("approved", "exported")  # the export runs after
    assert len(audit(app.env.conn, clip_id, "approve")) == 1


def test_r_then_a_reason_then_r_rejects(live_app: StartApp, page: Page) -> None:
    app = live_app()
    job = app.env.job("MrBeast")
    older = app.clip(job, created_at="2027-01-01T00:00:00Z")
    newer = app.clip(job, created_at="2027-01-02T00:00:00Z")
    app.login(page, f"/clip/{newer}")
    reason = page.get_by_label("Reject reason")

    page.keyboard.press("r")
    expect(reason).to_be_focused()
    reason.select_option("duplicate")
    expect(reason).to_be_focused()
    page.keyboard.press("r")

    expect(page).to_have_url(app(f"/clip/{older}"))
    expect(page.get_by_role("status").first).to_contain_text(f"Clip {newer} rejected (duplicate)")
    assert app.env.status(newer) == "rejected"
    assert [r["reason_tag"] for r in audit(app.env.conn, newer, "reject")] == ["duplicate"]


def test_j_and_k_walk_the_filtered_view_in_queue_order(live_app: StartApp, page: Page) -> None:
    app = live_app()
    beast, kai = app.env.job("MrBeast"), app.env.job("Kai Cenat")
    app.clip(beast, score=9.0, created_at="2027-01-01T00:00:00Z")
    app.clip(kai, score=8.5, created_at="2027-01-02T00:00:00Z")  # another creator
    app.clip(beast, score=8.0, created_at="2027-01-03T00:00:00Z")  # three tied scores:
    app.clip(beast, score=8.0, created_at="2027-01-03T00:00:00Z")  # same time too, so
    app.clip(beast, score=8.0, created_at="2027-01-04T00:00:00Z")  # the id breaks the tie
    app.clip(beast, score=9.5, status="rejected")  # outside the status filter
    app.clip(beast, score=None, created_at="2027-01-05T00:00:00Z")  # no score sorts last
    app.clip(beast, score=6.0, created_at="2027-01-06T00:00:00Z")
    expected = [
        row[0]
        for row in app.env.conn.execute(
            "SELECT c.id FROM clips c JOIN jobs j ON j.id = c.job_id"
            " JOIN clip_versions v ON v.clip_id = c.id AND v.version = c.current_version"
            " WHERE j.creator = 'MrBeast' AND c.status = 'pending_review'"
            " ORDER BY v.score IS NULL, v.score DESC, c.created_at DESC, c.id DESC"
        )
    ]
    app.login(page)
    page.locator("select[name=creator]").select_option("MrBeast")
    page.locator("select[name=sort]").select_option("score")
    page.get_by_role("button", name="Apply").click()
    expect(page.locator(".clip-card")).to_have_count(len(expected))
    hrefs = card_hrefs(page)
    assert [clip_id_of(h) for h in hrefs] == expected
    page.locator(".clip-card").first.click()
    expect(page).to_have_url(app(hrefs[0]))

    for href in hrefs[1:]:
        page.keyboard.press("j")
        expect(page).to_have_url(app(href))
    expect(page.locator("#next-clip")).to_have_count(0)
    for href in reversed(hrefs[:-1]):
        page.keyboard.press("k")
        expect(page).to_have_url(app(href))
    expect(page.locator("#prev-clip")).to_have_count(0)


def test_campaign_checks_must_be_ticked_before_approving(live_app: StartApp, page: Page) -> None:
    brief = Campaign.model_validate(
        {
            "id": "vyro_mrbeast",
            "program": "vyro",
            "creator": "MrBeast",
            "platforms": ["tiktok"],
            "forbidden": ["logos", "negative_framing"],
        }
    )
    app = live_app(
        accounts=[FakeAccount(campaign="vyro_mrbeast")], campaigns={"vyro_mrbeast": brief}
    )
    job = app.env.job("MrBeast")
    older = app.clip(job, created_at="2027-01-01T00:00:00Z")
    newer = app.clip(job, created_at="2027-01-02T00:00:00Z")
    app.login(page, f"/clip/{newer}")
    checks = page.locator("#reviewer-checks")
    expect(checks).to_contain_text("vyro_mrbeast")
    expect(checks).to_contain_text("No logos, No negative framing")

    page.get_by_role("button", name="Approve (a)").click()  # the box is not ticked
    expect(page.locator("#action-error")).to_have_text(CHECKS_UNCONFIRMED)
    expect(page).to_have_url(app(f"/clip/{newer}"))
    assert app.env.status(newer) == "pending_review"

    box = page.get_by_label("I checked these in the video")
    page.keyboard.press("a")
    expect(box).to_be_focused()
    page.keyboard.press("Space")
    expect(box).to_be_checked()
    page.keyboard.press("a")

    expect(page).to_have_url(app(f"/clip/{older}"))
    (row,) = audit(app.env.conn, newer, "approve")
    assert json.loads(row["payload"]) == {
        "confirmed_reviewer_checks": {"vyro_mrbeast": ["logos", "negative_framing"]}
    }


def test_an_invalid_htmx_form_shows_the_fixed_error(live_app: StartApp, page: Page) -> None:
    app = live_app()
    clip_id = app.clip()
    app.login(page, f"/clip/{clip_id}")
    page.locator("form:has(#approve-btn) input[name=version]").evaluate(
        "input => { input.value = 'not-a-number'; }"
    )

    page.get_by_role("button", name="Approve (a)").click()

    expect(page.locator("#action-error")).to_have_text(_INVALID_FORM)
    expect(page.locator("#action-error")).not_to_contain_text("not-a-number")
    assert app.env.status(clip_id) == "pending_review"


def test_too_many_form_fields_and_multipart_are_refused(live_app: StartApp, page: Page) -> None:
    app = live_app()
    clip_id = app.clip()
    app.login(page, f"/clip/{clip_id}")
    token = json.loads(page.locator("body").get_attribute("hx-headers") or "{}")["X-CSRF-Token"]
    url = app(f"/clip/{clip_id}/approve")
    fields = "&".join(f"f{n}=x" for n in range(MAX_FORM_FIELDS + 1))

    too_many = page.request.post(
        url,
        data=f"csrf_token={token}&version=1&{fields}",
        headers={"Content-Type": "application/x-www-form-urlencoded", "HX-Request": "true"},
    )
    multipart = page.request.post(url, multipart={"csrf_token": token, "version": "1"})

    assert too_many.status == 413
    assert "Not saved: the form has too many fields." in too_many.text()
    assert multipart.status == 415
    assert app.env.status(clip_id) == "pending_review"


def test_the_video_seeks_on_a_phone(
    live_app: StartApp,
    new_context: Callable[..., BrowserContext],
    playwright: Playwright,
    watch_page: Callable[[Page], Page],
) -> None:
    iphone = dict(playwright.devices["iPhone 13"])
    del iphone["default_browser_type"]  # emulate the phone in Chromium
    page = watch_page(new_context(**iphone).new_page())
    app = live_app()
    clip_id = app.clip()
    video_statuses: list[int] = []

    def on_response(response: Response) -> None:
        if response.url.endswith(f"/media/clip/{clip_id}/1/video"):
            video_statuses.append(response.status)

    page.on("response", on_response)
    app.login(page, f"/clip/{clip_id}")
    size = app.video.stat().st_size

    ranged = page.request.get(
        app(f"/media/clip/{clip_id}/1/video"), headers={"Range": "bytes=100-199"}
    )
    assert ranged.status == 206
    assert ranged.headers["content-range"] == f"bytes 100-199/{size}"
    assert len(ranged.body()) == 100

    video = page.locator("video.player").element_handle()
    page.wait_for_function("video => video.readyState >= 1 && video.seekable.length > 0", arg=video)
    target = page.evaluate("video => video.duration * 2 / 3", video)
    page.evaluate("([video, at]) => { video.currentTime = at; }", [video, target])
    page.wait_for_function(
        "([video, at]) => !video.seeking && Math.abs(video.currentTime - at) < 0.2",
        arg=[video, target],
    )
    assert page.evaluate("video => video.error", video) is None
    assert 206 in video_statuses  # the browser itself fetched the video by range

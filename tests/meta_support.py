"""Shared helpers for the Meta publisher tests (CLP-94): recorded Graph API responses, fake
credentials and a fake post. No test talks to Meta: every request goes to respx."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from clipper.posting.models import Post, PostMetadata, PostStatus
from clipper.publish.meta import GRAPH_URL, GraphClient, TokenGuard, load_credentials

FIXTURES = Path(__file__).parent / "fixtures" / "meta"
T0 = datetime(2027, 1, 15, 1, 0, tzinfo=UTC)

FB_TOKEN = "EAAB-fake-page-token-FB-0001"
IG_TOKEN = "EAAB-fake-page-token-IG-0002"
PAGE_ID = "1111111111"
IG_ID = "17841400000000001"
ENV = {
    "CLIPPER_ACCESS_TOKEN_FB_MAIN": FB_TOKEN,
    "CLIPPER_META_ID_FB_MAIN": PAGE_ID,
    "CLIPPER_ACCESS_TOKEN_IG_MAIN": IG_TOKEN,
    "CLIPPER_META_ID_IG_MAIN": IG_ID,
}


def recorded(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def graph(path: str) -> str:
    return f"{GRAPH_URL}/{path}"


def reply(name: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=recorded(name))


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Alerts:
    messages: list[str] = field(default_factory=list)

    def __call__(self, message: str) -> None:
        self.messages.append(message)


def credentials(env: dict[str, str] | None = None):  # noqa: ANN201 - a partial
    return partial(load_credentials, env=ENV if env is None else env)


def guard(http: httpx.Client, clock: Clock, alerts: Alerts) -> TokenGuard:
    return TokenGuard(GraphClient(http), clock=clock, alert=alerts)


def make_post(
    *,
    platform: str = "facebook",
    account: str = "fb_main",
    at: datetime = T0 + timedelta(minutes=30),
    attempts: int = 0,
    key: str = "idem-0001",
) -> Post:
    return Post(
        id=7,
        clip_id=3,
        clip_version=1,
        account_id=account,
        platform=platform,
        status=PostStatus.UPLOADING,
        scheduled_at=at,
        attempts=attempts,
        idempotency_key=key,
        video_path="youtube_1/clip_1_v1.mp4",
    )


def metadata(
    *,
    caption: str = "He did not see it coming\n\n#funny #fail #chair",
    hashtags: list[str] | None = None,
    flags: dict[str, bool] | None = None,
) -> PostMetadata:
    return PostMetadata(
        title="Chair falls",
        caption=caption,
        hashtags=["#funny", "#fail", "#chair"] if hashtags is None else hashtags,
        flags={"is_ai_generated": False, "paid_partnership": False} if flags is None else flags,
    )


def video(tmp_path: Path, size: int = 2048) -> Path:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00" * size)
    return path

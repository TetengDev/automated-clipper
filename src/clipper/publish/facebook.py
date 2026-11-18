"""Facebook Page Reels publisher (CLP-94).

The Reels Publishing API, three calls with the Page access token:

1. `POST /{page-id}/video_reels upload_phase=start` -> `video_id`, `upload_url`.
2. `POST https://rupload.facebook.com/video-upload/{version}/{video-id}` with the bytes
   (`offset: 0`, `file_size`). The file goes straight to Meta; nothing is served publicly.
3. `POST /{page-id}/video_reels upload_phase=finish video_state=SCHEDULED
   scheduled_publish_time=<slot>` with the description (caption plus hashtags), title and
   `is_ai_generated`. Facebook itself publishes at the slot.

Facebook accepts a scheduled time 10 minutes to 29 days ahead, so the posting stage
claims a Facebook post's first attempt `LEAD_TIME` before its slot. When the slot is less
than `SCHEDULE_MIN_AHEAD` away at the finish call (a late worker, a retry), the Reel is
published at once instead. The post is recorded `posted` when Facebook accepts the Reel,
which for a scheduled Reel is up to `LEAD_TIME` before it goes live.

Clips longer than 90 s never get here: the scheduler skips them for Facebook (CLP-94).
The Reels API has no plain paid-partnership toggle (branded content needs a sponsor Page),
so a post with `paid_partnership` set is refused rather than published undisclosed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from clipper.jobs.models import Clock, utcnow
from clipper.posting.models import Post, PostMetadata, PublishRejected, PublishResult
from clipper.publish.meta import (
    GRAPH_VERSION,
    UPLOAD_HOST,
    Credentials,
    GraphClient,
    MetaCredentials,
    MetaError,
    MetaPublisher,
    TokenGuard,
    form_bool,
    graph_id,
    load_credentials,
    safe_link,
)

LEAD_TIME = timedelta(minutes=25)
"""How early the stage claims a first attempt, so the Reel can be scheduled natively."""
SCHEDULE_MIN_AHEAD = timedelta(minutes=11)
"""Facebook's minimum is 10 minutes; one more for clock skew."""
SCHEDULE_MAX_AHEAD = timedelta(days=29)
AI_FLAG = "is_ai_generated"
PAID_FLAG = "paid_partnership"
_PROGRESS_MAX = 500

logger = logging.getLogger(__name__)


@dataclass
class _Progress:
    account_id: str
    video_id: str
    finish_sent: bool = False


class FacebookPublisher(MetaPublisher):
    platform = "facebook"
    lead_time = LEAD_TIME

    def __init__(
        self,
        client: GraphClient,
        guard: TokenGuard,
        *,
        credentials: Credentials = load_credentials,
        clock: Clock = utcnow,
        campaigns: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(client, guard, credentials=credentials, clock=clock, campaigns=campaigns)
        self._progress: dict[str, _Progress] = {}

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        if metadata.flags.get(PAID_FLAG):
            raise PublishRejected(
                "facebook: the paid partnership label cannot be set through the Reels API;"
                " post this campaign clip by hand"
            )
        self._refuse_campaign(post.account_id)
        deadline = self._deadline()
        creds = self._creds(post.account_id)
        try:
            return self._upload(post, creds, video_path, metadata, deadline)
        except MetaError as exc:
            self._raise(creds, exc)
            raise  # unreachable: _raise always raises

    def _upload(
        self,
        post: Post,
        creds: MetaCredentials,
        video_path: Path,
        metadata: PostMetadata,
        deadline: datetime,
    ) -> PublishResult:
        reels = f"{creds.object_id}/video_reels"
        start = self.client.post(reels, creds.token, data={"upload_phase": "start"})
        video_id = graph_id(start.get("video_id"), "video id")
        upload_url = start.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url:
            upload_url = f"https://{UPLOAD_HOST}/video-upload/{GRAPH_VERSION}/{video_id}"
        progress = _Progress(post.account_id, video_id)
        self._remember(post.idempotency_key, progress)
        self._in_time(deadline, "the upload")
        uploaded = self.client.upload(upload_url, creds.token, video_path)
        if uploaded.get("success") is not True:
            raise MetaError("facebook: the video upload was not accepted")
        self._in_time(deadline, "the finish call")
        finish = {
            "upload_phase": "finish",
            "video_id": video_id,
            **self._timing(post),
            "description": metadata.caption,
            **({"title": metadata.title} if metadata.title else {}),
            "is_ai_generated": form_bool(bool(metadata.flags.get(AI_FLAG))),
        }
        progress.finish_sent = True
        done = self.client.post(reels, creds.token, data=finish)
        if done.get("success") is not True:
            raise MetaError("facebook: the Reel was not accepted")
        self._progress.pop(post.idempotency_key, None)
        logger.info("post %d: Reel %s accepted (%s)", post.id, video_id, finish["video_state"])
        return PublishResult(external_id=video_id, url=f"https://www.facebook.com/reel/{video_id}")

    def _timing(self, post: Post) -> dict[str, str]:
        ahead = post.scheduled_at - self.clock()
        if ahead < SCHEDULE_MIN_AHEAD:
            return {"video_state": "PUBLISHED"}
        if ahead > SCHEDULE_MAX_AHEAD:
            raise PublishRejected("facebook: a Reel can be scheduled at most 29 days ahead")
        return {
            "video_state": "SCHEDULED",
            "scheduled_publish_time": str(int(post.scheduled_at.timestamp())),
        }

    def _remember(self, key: str, progress: _Progress) -> None:
        self._progress.pop(key, None)
        self._progress[key] = progress
        while len(self._progress) > _PROGRESS_MAX:
            self._progress.pop(next(iter(self._progress)))

    def lookup(self, idempotency_key: str) -> PublishResult | None:
        """Whether an earlier attempt of this worker got the Reel accepted after all (its
        finish reply was lost). Only attempts made by this process are known; after a
        restart this says not found and the stage handles the post as usual.

        Once the finish call was sent, only a Reel Facebook reports as failed (`status`
        `error`) is "not found". One that is neither scheduled nor published yet may still
        be processing, so this raises and the stage hands the post to a human rather
        than uploading it a second time."""
        progress = self._progress.get(idempotency_key)
        if progress is None or not progress.finish_sent:
            return None
        creds = self._creds(progress.account_id)
        fields = "id,published,scheduled_publish_time,permalink_url,status"
        try:
            info = self.client.get(progress.video_id, creds.token, params={"fields": fields})
        except MetaError as exc:
            self._raise(creds, exc)
            raise  # unreachable
        if not (info.get("published") is True or info.get("scheduled_publish_time")):
            status = info.get("status") if isinstance(info.get("status"), dict) else {}
            if status.get("video_status") == "error":
                self._progress.pop(idempotency_key, None)
                return None
            raise MetaError(
                f"facebook: Reel {progress.video_id} was finished but is neither scheduled"
                " nor published yet; check the Page before requeueing"
            )
        self._progress.pop(idempotency_key, None)
        return PublishResult(
            external_id=progress.video_id,
            url=_permalink(info.get("permalink_url"))
            or f"https://www.facebook.com/reel/{progress.video_id}",
        )


_HOSTS = frozenset({"www.facebook.com", "facebook.com"})


def _permalink(value: object) -> str | None:
    """Facebook's permalink (often a path) as an https www.facebook.com URL, or None."""
    if isinstance(value, str) and value.startswith("/") and not value.startswith("//"):
        value = f"https://www.facebook.com{value}"
    return safe_link(value, _HOSTS)

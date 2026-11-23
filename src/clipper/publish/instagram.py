"""Instagram Reels publisher (CLP-94): the container flow, with a resumable upload.

Instagram has no native scheduling: the posting stage calls this at the slot.

1. `GET /{ig-user-id}/content_publishing_limit`: defer when the account is within
   `QUOTA_RESERVE` of its 24-hour API publishing limit (50 posts).
2. `POST /{ig-user-id}/media media_type=REELS upload_type=resumable` with the caption
   (hashtag line included), `share_to_feed`, `is_ai_generated` and `is_paid_partnership`
   -> container `id` and an upload `uri` on rupload.facebook.com.
3. `POST <uri>` with the bytes (`offset: 0`, `file_size`). The resumable upload means no
   `video_url`: the clip is never served from a public URL, and the media directory stays
   private (the alternative, a public or signed `video_url`, was rejected; see
   docs/DEPLOYMENT.md, Meta publishing).
4. `GET /{container-id}?fields=status_code` every `POLL_EVERY` seconds until `FINISHED`;
   `ERROR` or `EXPIRED` fails the attempt, and after `MAX_POLLS` checks it times out,
   well within the stage's 30-minute STALE_UPLOAD.
5. `POST /{ig-user-id}/media_publish creation_id=<container>` -> the media id; then the
   permalink for the post's URL.

The caption is refused before any call when it has more than 5 hashtags (Instagram's cap
since December 2025) or is longer than 2,200 characters. CLP-96's Instagram variant keeps
within both, so this only guards against a bad edit.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from clipper.jobs.models import Clock, from_iso, to_iso, utcnow
from clipper.posting.models import (
    Post,
    PostMetadata,
    PublishDeferred,
    PublishRejected,
    PublishResult,
)
from clipper.publish.meta import (
    DEFER_RATE,
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
from clipper.redact import redact

CAPTION_MAX = 2200
HASHTAGS_MAX = 5
POLL_EVERY = 10.0
MAX_POLLS = 60
"""60 checks 10 s apart: a container gets 10 minutes to finish processing."""
QUOTA_RESERVE = 2
"""Publishing stops this many posts short of the 24-hour limit (room for manual posts)."""
AI_FLAG = "is_ai_generated"
PAID_FLAG = "paid_partnership"
_HASHTAG = re.compile(r"(?<![\w#])#\w+")
_PROGRESS_MAX = 500
_STATUS_MAX = 300

logger = logging.getLogger(__name__)


RECHECK_AFTER = timedelta(minutes=5)
"""A container still FINISHED after media_publish was sent is checked again this late."""
MATCH_SLACK = timedelta(minutes=2)
"""Clock skew allowed when matching a recent media's timestamp to the attempt."""
_HOSTS = frozenset({"www.instagram.com", "instagram.com"})


@dataclass
class _Progress:
    account_id: str
    container_id: str
    caption_sha256: str
    """The caption's digest: a published container is matched to a recent media by it."""
    started_at: datetime
    publish_sent: bool = False
    rechecked: bool = False
    media_id: str | None = None
    """What media_publish answered, once it did: the post is live."""

    def ref(self, **changes: object) -> dict[str, object]:
        """What is kept on the post (`posts.platform_ref`, CLP-122), with `changes`
        applied. Not the account: the post's own account is the only one whose token
        checks the container."""
        state = replace(self, **changes)  # type: ignore[arg-type]
        return {
            "container_id": state.container_id,
            "caption_sha256": state.caption_sha256,
            "started_at": to_iso(state.started_at),
            "publish_sent": state.publish_sent,
            "rechecked": state.rechecked,
            "media_id": state.media_id,
        }


def _digest(caption: str) -> str:
    return hashlib.sha256(caption.encode("utf-8")).hexdigest()


def check_caption(metadata: PostMetadata) -> None:
    """Raise PublishRejected when Instagram would refuse (or cut) the caption."""
    count = max(len(metadata.hashtags), len(_HASHTAG.findall(metadata.caption)))
    if count > HASHTAGS_MAX:
        raise PublishRejected(
            f"instagram: {count} hashtags, at most {HASHTAGS_MAX} hashtags allowed"
        )
    if len(metadata.caption) > CAPTION_MAX:
        raise PublishRejected(f"instagram: the caption is longer than {CAPTION_MAX} characters")


class InstagramPublisher(MetaPublisher):
    platform = "instagram"

    def __init__(
        self,
        client: GraphClient,
        guard: TokenGuard,
        *,
        credentials: Credentials = load_credentials,
        clock: Clock = utcnow,
        sleep: Callable[[float], None] = time.sleep,
        poll_every: float = POLL_EVERY,
        max_polls: int = MAX_POLLS,
        campaigns: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(client, guard, credentials=credentials, clock=clock, campaigns=campaigns)
        self.sleep = sleep
        self.poll_every = poll_every
        self.max_polls = max(1, max_polls)
        self._progress: dict[str, _Progress] = {}

    def publish(self, post: Post, video_path: Path, metadata: PostMetadata) -> PublishResult:
        check_caption(metadata)
        self._refuse_campaign(post.account_id)
        deadline = self._deadline()
        creds = self._creds(post.account_id)
        try:
            return self._publish(post, creds, video_path, metadata, deadline)
        except MetaError as exc:
            self._raise(creds, exc)
            raise  # unreachable: _raise always raises

    def _publish(
        self,
        post: Post,
        creds: MetaCredentials,
        video_path: Path,
        metadata: PostMetadata,
        deadline: datetime,
    ) -> PublishResult:
        self._check_quota(creds)
        ig = creds.object_id
        started = self.clock()
        container = self.client.post(
            f"{ig}/media",
            creds.token,
            data={
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": metadata.caption,
                "share_to_feed": "true",
                "is_ai_generated": form_bool(bool(metadata.flags.get(AI_FLAG))),
                "is_paid_partnership": form_bool(bool(metadata.flags.get(PAID_FLAG))),
            },
        )
        container_id = graph_id(container.get("id"), "container id")
        uri = container.get("uri")
        if not isinstance(uri, str) or not uri:
            uri = f"https://{UPLOAD_HOST}/ig-api-upload/{GRAPH_VERSION}/{container_id}"
        progress = _Progress(post.account_id, container_id, _digest(metadata.caption), started)
        self._remember(post.idempotency_key, progress)
        self._save_ref(post.idempotency_key, progress.ref())
        self._in_time(deadline, "the upload")
        uploaded = self.client.upload(uri, creds.token, video_path)
        if uploaded.get("success") is not True:
            raise MetaError("instagram: the video upload was not accepted")
        self._wait(creds, container_id, deadline)
        self._in_time(deadline, "media_publish")
        self._save_ref(post.idempotency_key, progress.ref(publish_sent=True))
        progress.publish_sent = True
        published = self.client.post(
            f"{ig}/media_publish", creds.token, data={"creation_id": container_id}
        )
        media_id = graph_id(published.get("id"), "media id")
        # Live now: the id only spares a later lookup the caption match, so a failed
        # write is logged, never raised.
        progress.media_id = media_id
        self._note_ref(post.idempotency_key, progress.ref())
        self._progress.pop(post.idempotency_key, None)
        logger.info("post %d: Reel %s published", post.id, media_id)
        return PublishResult(external_id=media_id, url=self._permalink(creds, media_id))

    def _check_quota(self, creds: MetaCredentials) -> None:
        try:
            body = self.client.get(
                f"{creds.object_id}/content_publishing_limit",
                creds.token,
                params={"fields": "config,quota_usage"},
            )
        except MetaError as exc:
            if exc.token_problem or exc.rate_limited:
                raise
            logger.warning(
                "account %s: publishing limit check failed (%s); publishing anyway",
                creds.account_id,
                exc,
            )
            return
        rows = body.get("data")
        row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else {}
        config = row.get("config") if isinstance(row.get("config"), dict) else {}
        used, total = row.get("quota_usage"), config.get("quota_total")
        if not (isinstance(used, int) and isinstance(total, int)):
            return
        if used >= total - QUOTA_RESERVE:
            raise PublishDeferred(
                f"instagram: publishing limit nearly reached for account {creds.account_id}"
                f" ({used}/{total} in 24 h)",
                until=self.clock() + DEFER_RATE,
            )

    def _status(self, creds: MetaCredentials, container_id: str) -> tuple[str, str]:
        body = self.client.get(container_id, creds.token, params={"fields": "status_code,status"})
        code = body.get("status_code")
        text = body.get("status")
        return (code if isinstance(code, str) else ""), (text if isinstance(text, str) else "")

    def _wait(self, creds: MetaCredentials, container_id: str, deadline: datetime) -> None:
        code = ""
        for attempt in range(self.max_polls):
            if attempt:
                self.sleep(self.poll_every)
                if self.clock() >= deadline:
                    break
            code, text = self._status(creds, container_id)
            if code == "FINISHED":
                return
            if code in ("ERROR", "EXPIRED", "PUBLISHED"):
                detail = redact(text)[:_STATUS_MAX]
                raise MetaError(f"instagram: container {container_id} is {code}: {detail}")
        raise MetaError(
            f"instagram: container {container_id} still {code or 'unknown'} when checking stopped",
            transient=True,
        )

    def _permalink(self, creds: MetaCredentials, media_id: str) -> str | None:
        try:
            body = self.client.get(media_id, creds.token, params={"fields": "permalink"})
        except MetaError as exc:
            logger.warning("media %s: permalink unavailable (%s)", media_id, exc)
            return None
        return safe_link(body.get("permalink"), _HOSTS)

    def _remember(self, key: str, progress: _Progress) -> None:
        self._progress.pop(key, None)
        self._progress[key] = progress
        while len(self._progress) > _PROGRESS_MAX:
            self._progress.pop(next(iter(self._progress)))

    def _current(self, key: str) -> _Progress | None:
        """The latest attempt's progress. The post's stored one wins (another worker may
        have started a newer upload since); this process's memory of the same container
        is kept, with the stored recheck and media id merged in, so a recheck is never
        repeated and a known media id is used."""
        stored = self._stored(key)
        known = self._progress.get(key)
        if stored is None:
            return known
        if known is not None and known.container_id == stored.container_id:
            known.rechecked = known.rechecked or stored.rechecked
            known.media_id = known.media_id or stored.media_id
            return known
        self._remember(key, stored)
        return stored

    def _stored(self, key: str) -> _Progress | None:
        """The progress kept on the post (CLP-122), or None."""
        loaded = self._load_ref(key)
        if loaded is None:
            return None
        account_id, ref = loaded
        sent, digest, stamp = (
            ref.get("publish_sent"),
            ref.get("caption_sha256"),
            ref.get("started_at"),
        )
        rechecked, media = ref.get("rechecked", False), ref.get("media_id")
        try:
            started = from_iso(stamp) if isinstance(stamp, str) else None
        except ValueError:
            started = None
        if not (
            isinstance(sent, bool)
            and isinstance(rechecked, bool)
            and isinstance(digest, str)
            and started is not None
        ):
            raise MetaError("instagram: the stored upload reference is unreadable")
        return _Progress(
            account_id,
            graph_id(ref.get("container_id"), "stored container id"),
            digest,
            started,
            publish_sent=sent,
            rechecked=rechecked,
            media_id=None if media is None else graph_id(media, "stored media id"),
        )

    def _confirmed(self, creds: MetaCredentials, progress: _Progress) -> PublishResult | None:
        """The stored media id, once a GET shows that media: it has this attempt's caption,
        its `owner`, when given (Graph leaves it out for media the querying user did not
        create), is this account, and it was posted since the attempt began. None when it
        cannot be confirmed (the container and caption match decide then). Token and
        rate-limit errors raise as usual."""
        media_id = progress.media_id
        if media_id is None:
            return None
        fields = "id,caption,owner,permalink,timestamp"
        try:
            body = self.client.get(media_id, creds.token, params={"fields": fields})
        except MetaError as exc:
            if exc.token_problem or exc.account_problem or exc.rate_limited:
                raise
            logger.warning("media %s: not confirmed (%s); matching by caption", media_id, exc)
            return None
        owner = body.get("owner")
        owner_id = owner.get("id") if isinstance(owner, dict) else None
        posted = _timestamp(body.get("timestamp"))
        caption = body.get("caption")
        if (
            str(body.get("id")) != media_id
            or not isinstance(caption, str)
            or _digest(caption) != progress.caption_sha256
            or (owner_id is not None and str(owner_id) != creds.object_id)
            or posted is None
            or posted < progress.started_at - MATCH_SLACK
        ):
            logger.warning("media %s: not confirmed; matching by caption", media_id)
            return None
        return PublishResult(external_id=media_id, url=safe_link(body.get("permalink"), _HOSTS))

    def lookup(self, idempotency_key: str) -> PublishResult | None:
        """Whether an earlier attempt published the Reel after all (its media_publish
        reply was lost, or the worker died). The container is checked by its id: the one
        stored on the post (`use_platform_refs`, CLP-122), else this process's memory, so
        a restart does not lose it. When media_publish answered before the worker died,
        its media id is stored too and returned once a GET confirms it. Otherwise, as the
        Graph API links no container to the media it became, the media id comes from
        matching the caption and time.

        A container still FINISHED may have a media_publish in flight (or one that never
        reached Meta): the first time, this defers the post by `RECHECK_AFTER` (the recheck
        is stored, so a restart does not repeat it); still FINISHED after that, it was never
        published: not found, so the stage publishes again (with a new container). Any
        other state than PUBLISHED is not found. A PUBLISHED container is matched to one
        recent media with the same caption posted since the attempt began; none, or more
        than one, raises: it is live, and the stage hands the post to a human instead of
        posting it twice."""
        progress = self._current(idempotency_key)
        if progress is None or not progress.publish_sent:
            return None
        creds = self._creds(progress.account_id)
        try:
            if progress.media_id is not None:
                confirmed = self._confirmed(creds, progress)
                if confirmed is not None:
                    self._progress.pop(idempotency_key, None)
                    return confirmed
            code, _ = self._status(creds, progress.container_id)
            if code == "FINISHED" and not progress.rechecked:
                progress.rechecked = True
                self._note_ref(idempotency_key, progress.ref())
                raise PublishDeferred(
                    f"instagram: container {progress.container_id} not published yet;"
                    " media_publish may still be in flight or may not have reached Meta;"
                    " checking again before publishing",
                    until=self.clock() + RECHECK_AFTER,
                )
            if code != "PUBLISHED":
                self._progress.pop(idempotency_key, None)
                return None
            recent = self.client.get(
                f"{creds.object_id}/media",
                creds.token,
                params={"fields": "id,caption,permalink,timestamp", "limit": "25"},
            )
        except MetaError as exc:
            self._raise(creds, exc)
            raise  # unreachable
        items = recent.get("data") if isinstance(recent.get("data"), list) else []
        since = progress.started_at - MATCH_SLACK
        matches = [
            item
            for item in items
            if isinstance(item, dict)
            and isinstance(caption := item.get("caption"), str)
            and _digest(caption) == progress.caption_sha256
            and (posted := _timestamp(item.get("timestamp"))) is not None
            and posted >= since
        ]
        if len(matches) != 1:
            raise MetaError(
                f"instagram: container {progress.container_id} was published but"
                f" {len(matches)} recent posts match it; check the account"
            )
        self._progress.pop(idempotency_key, None)
        return PublishResult(
            external_id=graph_id(matches[0].get("id"), "media id"),
            url=safe_link(matches[0].get("permalink"), _HOSTS),
        )


def _timestamp(value: object) -> datetime | None:
    """A Graph timestamp (`2027-01-15T01:01:00+0000`) as an aware datetime, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None

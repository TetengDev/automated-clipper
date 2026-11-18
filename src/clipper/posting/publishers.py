"""Which platform publishers the worker registers, from the accounts config (CLP-93).

A platform gets a publisher only when at least one enabled account posts to it; due posts
on any other platform stay `scheduled` (the posting stage logs that once).
"""

from __future__ import annotations

from clipper.accounts import AccountsConfig
from clipper.config import Secrets
from clipper.posting.models import Publisher
from clipper.posting.youtube import OAuthClient, YouTubePublisher
from clipper.publish.meta import meta_publishers


def _enabled(accounts: AccountsConfig, platform: str) -> tuple[str, ...]:
    return tuple(a.id for a in accounts.accounts if a.platform == platform and a.enabled)


def build_publishers(accounts: AccountsConfig, secrets: Secrets) -> dict[str, Publisher]:
    publishers: dict[str, Publisher] = {}
    youtube = _enabled(accounts, "youtube")
    if youtube:
        client_id = secrets.youtube_oauth_client_id
        publishers["youtube"] = YouTubePublisher(
            account_ids=youtube,
            client=OAuthClient(
                client_id=client_id.get_secret_value() if client_id else "",
                client_secret=secrets.youtube_oauth_client_secret,
            ),
        )
    # Facebook Page Reels and Instagram Reels (CLP-94); their tokens are read per
    # account at publish time, from the same CLP-91 secrets.
    publishers.update(meta_publishers(accounts.accounts))
    return publishers

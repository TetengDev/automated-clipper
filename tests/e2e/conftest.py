"""Browser tests of the review app (CLP-61): the real app under uvicorn, driven by Chromium.

Each test gets its own server on a free port, with a fresh SQLite DB, a password and session
secret generated for the run, and an ffmpeg-made fixture video. Run with
`uv run pytest tests/e2e -m e2e` (the default run leaves the `e2e` tests out).
"""

from __future__ import annotations

import secrets
import shutil
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from playwright.sync_api import ConsoleMessage, Page

from e2e_support import VIDEO_NAME, LiveApp, Server, make_video
from review_support import make_env


@pytest.fixture(scope="session")
def fixture_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_video(tmp_path_factory.mktemp("video") / VIDEO_NAME, seconds=3)


@pytest.fixture
def live_app(tmp_path: Path, fixture_video: Path) -> Iterator[Callable[..., LiveApp]]:
    """Start the review app; keyword arguments go to `create_app` (e.g. accounts)."""
    servers: list[Server] = []

    def start(**app_kwargs: Any) -> LiveApp:
        env = make_env(tmp_path / f"app{len(servers)}")
        shutil.copyfile(fixture_video, env.media_root / VIDEO_NAME)
        password = secrets.token_urlsafe(16)  # test-only, never a real credential
        app = env.app(
            password_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=4)).decode(),
            session_secret=secrets.token_urlsafe(48),
            secure_cookies=False,  # plain http on 127.0.0.1
            wall_clock=time.time,
            **app_kwargs,
        )
        servers.append(Server.start(app))
        return LiveApp(url=servers[-1].url, env=env, password=password)

    try:
        yield start
    finally:
        for server in servers:
            server.stop()


@pytest.fixture(autouse=True)
def watch_page(page: Page) -> Iterator[Callable[[Page], Page]]:
    """Fail the test if a watched page broke the CSP (no inline script or style) or a script
    threw. The `page` fixture is watched; a test watches any other page it opens."""
    problems: list[str] = []

    def on_console(message: ConsoleMessage) -> None:
        if "Content Security Policy" in message.text:
            problems.append(message.text)

    def watch(target: Page) -> Page:
        target.on("console", on_console)
        target.on("pageerror", lambda error: problems.append(f"page error: {error}"))
        return target

    watch(page)
    yield watch
    assert problems == []

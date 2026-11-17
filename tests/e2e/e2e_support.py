"""Shared helpers for the browser tests (CLP-61): the review app served by uvicorn on a free
port, and the fixture video."""

from __future__ import annotations

import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from playwright.sync_api import Page, expect

from review_support import Env

VIDEO_NAME = "fixture.mp4"
SERVER_START_TIMEOUT_S = 10.0


@dataclass
class LiveApp:
    """A running review app: its URL, the seeding helpers on its DB, and its password."""

    url: str
    env: Env
    password: str

    def __call__(self, path: str) -> str:
        return f"{self.url}{path}"

    @property
    def video(self) -> Path:
        return self.env.media_root / VIDEO_NAME

    def clip(self, *args: Any, **kwargs: Any) -> int:
        """A clip with the fixture video (see `Env.clip`)."""
        kwargs.setdefault("video_path", VIDEO_NAME)
        return self.env.clip(*args, **kwargs)

    def login(self, page: Page, path: str = "/queue") -> None:
        page.goto(self(path))
        page.get_by_label("Password").fill(self.password)
        page.get_by_role("button", name="Sign in").click()
        expect(page).to_have_url(self(path))


@dataclass
class Server:
    """uvicorn serving an app on a socket the OS gave a free port."""

    server: uvicorn.Server
    thread: threading.Thread
    sock: socket.socket

    @property
    def url(self) -> str:
        host, port = self.sock.getsockname()
        return f"http://{host}:{port}"

    @classmethod
    def start(cls, app: Any) -> Server:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        running = cls(server, thread, sock)
        thread.start()
        deadline = time.monotonic() + SERVER_START_TIMEOUT_S
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                running.stop()
                raise RuntimeError("the review app did not start")
            time.sleep(0.01)
        return running

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=SERVER_START_TIMEOUT_S)
        self.sock.close()


def make_video(path: Path, seconds: int) -> Path:
    """A test-pattern video, VP9 in MP4 with a keyframe every second so it can seek. VP9,
    not H.264: Playwright's Chromium builds have no proprietary codecs on every platform."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:  # the e2e job installs it; a skip would hide a run that tests nothing
        pytest.fail("ffmpeg is required for the e2e tests")
    subprocess.run(  # noqa: S603
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=duration={seconds}:size=64x112:rate=10",
            "-pix_fmt",
            "yuv420p",
            "-c:v",
            "libvpx-vp9",
            "-g",
            "10",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
    )
    return path

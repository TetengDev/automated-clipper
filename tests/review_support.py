"""Shared helpers for the review page tests: app factory, login, DB seeding, test media."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import bcrypt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from clipper.db import connect, migrate
from clipper.review.app import create_app

PASSWORD = "correct horse battery staple"
SECRET = "s" * 40
BASE_URL = "https://testserver"
WALL_START = 1_800_000_000.0  # 2027-01-15T08:00:00Z
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()

_YT_IDS = iter(f"vid{n:08d}" for n in range(10**6))


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf_token field missing"
    return match.group(1)


def login(client: TestClient) -> str:
    """Log in and return the session's CSRF token."""
    token = csrf_from(client.get("/login").text)
    response = client.post("/login", data={"password": PASSWORD, "csrf_token": token})
    assert response.status_code == 303
    page = client.get("/queue")
    assert page.status_code == 200
    return csrf_from(page.text)


def generate_mp4(path: Path, seconds: int = 1) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    path.parent.mkdir(parents=True, exist_ok=True)
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
            "libx264",
            "-movflags",
            "+faststart",
            str(path),
        ],
        check=True,
    )
    return path


@dataclass
class RecordingRunner:
    """Stands in for subprocess: records argv, optionally fails the first `fail` calls."""

    fail: int = 0
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, args: list[str]) -> None:
        self.calls.append(args)
        if len(self.calls) <= self.fail:
            raise subprocess.CalledProcessError(1, args, stderr="rclone: quota exceeded")


@dataclass
class Env:
    db_path: Path
    media_root: Path
    output_dir: Path
    conn: sqlite3.Connection
    runner: RecordingRunner = field(default_factory=RecordingRunner)
    wall: list[float] = field(default_factory=lambda: [WALL_START])

    def app(self, **kwargs: Any) -> FastAPI:
        params: dict[str, Any] = {
            "password_hash": PASSWORD_HASH,
            "session_secret": SECRET,
            "db_path": self.db_path,
            "media_root": self.media_root,
            "output_dir": self.output_dir,
            "export_runner": self.runner,
            "wall_clock": lambda: self.wall[0],
        }
        params.update(kwargs)
        return create_app(**params)

    def client(self, **kwargs: Any) -> TestClient:
        return TestClient(self.app(**kwargs), base_url=BASE_URL, follow_redirects=False)

    def job(
        self,
        creator: str = "MrBeast",
        *,
        platform: str = "youtube",
        item_id: str | None = None,
        title: str | None = "Source video",
    ) -> int:
        item = item_id or next(_YT_IDS)
        url = (
            f"https://www.youtube.com/watch?v={item}"
            if platform == "youtube"
            else f"https://www.twitch.tv/videos/{item}"
        )
        cur = self.conn.execute(
            "INSERT INTO jobs (platform, item_id, creator, source_url, title, status,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'clips_ready', ?, ?)",
            (platform, item, creator, url, title, "2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z"),
        )
        return int(cur.lastrowid or 0)

    def clip(
        self,
        job_id: int | None = None,
        *,
        status: str = "pending_review",
        created_at: str = "2027-01-10T00:00:00Z",
        title: str | None = "Big moment",
        score: float | None = 7.5,
        start_s: float = 125.0,
        end_s: float = 155.0,
        video_path: str | None = None,
        thumb_path: str | None = None,
        caption: str | None = "Watch this",
        hashtags: list[str] | None = None,
        versions: int = 1,
    ) -> int:
        job = job_id if job_id is not None else self.job()
        rank = self.conn.execute(
            "SELECT COALESCE(MAX(rank), 0) + 1 FROM clips WHERE job_id = ?", (job,)
        ).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO clips (job_id, rank, status, current_version, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (job, rank, status, versions, created_at, created_at),
        )
        clip_id = int(cur.lastrowid or 0)
        for n in range(1, versions + 1):
            self.conn.execute(
                "INSERT INTO clip_versions (clip_id, version, start_s, end_s, title, caption,"
                " hashtags, score, reason, video_path, thumb_path, prompt_version, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    clip_id,
                    n,
                    start_s,
                    end_s,
                    title,
                    caption,
                    json.dumps(hashtags if hashtags is not None else ["funny", "#viral"]),
                    score,
                    "Strong hook and payoff",
                    video_path,
                    thumb_path,
                    "select-v3",
                    created_at,
                ),
            )
        return clip_id

    def status(self, clip_id: int) -> str:
        return self.conn.execute("SELECT status FROM clips WHERE id = ?", (clip_id,)).fetchone()[0]

    def reviews(self, clip_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM clip_reviews WHERE clip_id = ? ORDER BY id", (clip_id,)
        ).fetchall()


def make_env(tmp_path: Path) -> Env:
    db_path = tmp_path / "state.db"
    conn = connect(db_path)
    migrate(conn)
    media_root = tmp_path / "media"
    media_root.mkdir()
    return Env(db_path=db_path, media_root=media_root, output_dir=tmp_path / "output", conn=conn)

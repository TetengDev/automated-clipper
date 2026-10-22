"""Thin yt-dlp wrapper: argument lists only (never a shell), `--` before every URL.

Runs the venv's yt-dlp as `python -m yt_dlp` so the pinned version is used. All process
execution goes through a `Runner` so tests can substitute it (no network in tests).

Error mapping (from yt-dlp's stderr):
- private / removed / members-only / age-restricted / requested format missing
  -> `Unavailable` (a `PermanentError`: retrying cannot fix it)
- "Sign in to confirm you're not a bot" / HTTP 429 -> `DownloadBlocked` (retryable; a
  distinct type so alerting can tell "YouTube is blocking us" apart from flakiness)
- anything else -> `YtDlpError` (retryable) carrying a redacted tail of stderr.

Callers pass only URLs built by `clipper.urls.canonical_url`, never raw user input.

Known exposure (accepted): the proxy URL, credentials included, is a yt-dlp argument, so
it is visible in the process list inside the container. It never reaches logs or the DB
(see `proxy_secrets`).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Protocol
from urllib.parse import quote, unquote, urlsplit

from clipper.jobs.stages import PermanentError
from clipper.redact import MASK, redact

logger = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 120.0
DOWNLOAD_TIMEOUT_S = 90 * 60.0
STDERR_TAIL_CHARS = 500
REDACT_WINDOW_CHARS = 4096
"""Only this much text (the end) is ever passed to `redact`: bounded work per error."""
COOKIES_ENV = "CLIPPER_YTDLP_COOKIES"
PROXY_ENV = "CLIPPER_YTDLP_PROXY"
AUDIO_MAX_FILESIZE = "4G"
SECTION_MAX_FILESIZE = "2G"
STDOUT_LIMIT_BYTES = 32 * 1024 * 1024
"""yt-dlp's probe JSON is often several MB; anything bigger is cut (and fails to parse)."""
STDERR_LIMIT_BYTES = 64 * 1024
LINE_LIMIT_BYTES = 64 * 1024
"""`on_stderr_line` gets at most this much per call; longer lines arrive in pieces."""
PUMP_JOIN_TIMEOUT_S = 5.0
"""How long to wait for output after the child exits before killing its process group
(a grandchild that inherited the pipes would otherwise keep us waiting forever)."""
"""Only the last 64 KB of stderr are kept; stream lines with `on_stderr_line` for more."""
CHILD_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "TMPDIR", "XDG_CACHE_HOME",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
)  # fmt: skip
"""Children (yt-dlp, ffmpeg, ffprobe) never see API keys or other secrets from our env."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class Runner(Protocol):
    """`runner(args, timeout_s)`: run `args` (no shell), capture output, raise
    `CommandTimeout` if it runs longer than `timeout_s`. `on_stderr_line`, when given, is
    called with every stderr line as it arrives (used for unbounded ffmpeg logs)."""

    def __call__(
        self,
        args: Sequence[str],
        timeout_s: float,
        *,
        on_stderr_line: Callable[[str], None] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> RunResult: ...


class CommandTimeout(Exception):
    """A subprocess exceeded its timeout and was killed (retryable)."""


class YtDlpError(RuntimeError):
    """yt-dlp failed in a way that may succeed later (retryable). Message is redacted."""


class DownloadBlocked(YtDlpError):
    """The platform is refusing us (bot check / rate limit). Retryable; alert on it."""


class Unavailable(PermanentError):
    """The video cannot be fetched by anyone without special access; do not retry."""


def child_env() -> dict[str, str]:
    return {k: os.environ[k] for k in CHILD_ENV_ALLOWLIST if k in os.environ}


class _Tail:
    """Keeps (at least) the last `limit` bytes written to it, as a deque of chunks so a
    large buffer is never shifted byte by byte. Thread-safe; once `close()`d it ignores
    further writes (an abandoned reader may still be running)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.chunks: deque[bytes] = deque()
        self.size = 0
        self.closed = False
        self._lock = threading.Lock()

    def add(self, data: bytes) -> bool:
        """Append `data`; False once closed (the reader should stop)."""
        with self._lock:
            if self.closed:
                return False
            self.chunks.append(data)
            self.size += len(data)
            while self.chunks and self.size - len(self.chunks[0]) >= self.limit:
                self.size -= len(self.chunks.popleft())
            return True

    def close(self) -> None:
        with self._lock:
            self.closed = True

    def text(self) -> str:
        with self._lock:
            data = b"".join(self.chunks)
        return data[-self.limit :].decode("utf-8", errors="replace")


def _pump(stream: IO[bytes], tail: _Tail, on_line: Callable[[str], None] | None) -> None:
    with stream:
        if on_line is None:
            for chunk in iter(lambda: stream.read(65536), b""):
                if not tail.add(chunk):
                    return
            return
        broken = False
        while raw := stream.readline(LINE_LIMIT_BYTES):
            if not tail.add(raw):
                return  # abandoned by run_command: no more callbacks
            if broken:
                continue
            try:
                on_line(raw.decode("utf-8", errors="replace"))
            except Exception:
                # Keep draining: a stalled pipe would block the child.
                logger.exception("stderr line handler failed; ignoring further lines")
                broken = True


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # the child leads its own session/group
    except (ProcessLookupError, PermissionError):
        proc.kill()
    proc.wait()


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _finish_pumps(
    proc: subprocess.Popen[bytes], pumps: Sequence[threading.Thread], tails: Sequence[_Tail]
) -> None:
    """Wait for the output readers; if something still holds the pipes after the child is
    gone, kill the rest of its process group and give the readers a last chance, all
    within one deadline. A reader still blocked after that is a daemon thread and is
    abandoned (closing a pipe under a blocked reader is not safe); its tail is closed so it
    can neither change the result nor call `on_stderr_line` again."""
    deadline = time.monotonic() + 2 * PUMP_JOIN_TIMEOUT_S
    for pump in pumps:
        pump.join(max(0.0, deadline - PUMP_JOIN_TIMEOUT_S - time.monotonic()))
    # The leader is reaped by now, so its pid could in principle be reused. A pid is not
    # handed out while a process group with that id exists, so probing first means we
    # only signal when members of our group are still alive. Remaining risk: the group
    # empties and the id is reused as a new group id between probe and kill (tiny window).
    if any(p.is_alive() for p in pumps) and _group_exists(proc.pid):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    for pump in pumps:
        pump.join(max(0.0, deadline - time.monotonic()))
    for tail in tails:
        tail.close()
    if any(p.is_alive() for p in pumps):
        logger.warning("%s: output pipe still held open; not waiting", Path(proc.args[0]).name)


def run_command(
    args: Sequence[str],
    timeout_s: float,
    *,
    on_stderr_line: Callable[[str], None] | None = None,
    env: Mapping[str, str] | None = None,
) -> RunResult:
    """Default `Runner`: argument list (no shell), no stdin, allowlisted env (plus `env`),
    own process group (killed as a whole on timeout), bounded stdout/stderr."""
    proc = subprocess.Popen(  # noqa: S603 - argument list, never a shell
        list(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**child_env(), **(env or {})},
        start_new_session=True,
    )
    out, err = _Tail(STDOUT_LIMIT_BYTES), _Tail(STDERR_LIMIT_BYTES)
    assert proc.stdout is not None and proc.stderr is not None
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, out, None), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, err, on_stderr_line), daemon=True),
    ]
    for pump in pumps:
        pump.start()
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        _finish_pumps(proc, pumps, (out, err))
        raise CommandTimeout(f"{Path(args[0]).name} timed out after {timeout_s:.0f}s") from None
    except BaseException:
        _kill_group(proc)
        raise
    _finish_pumps(proc, pumps, (out, err))
    return RunResult(proc.returncode, out.text(), err.text())


# Order matters: "Sign in to confirm your age" is permanent, the bot check is not.
# The rate-limit check (_RATE_LIMITED) runs before these.
_PERMANENT: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (r"sign in to confirm your age|age[- ]restricted|inappropriate for some users", "age"),
        (r"private video|video is private", "private"),
        (r"members[- ]only|join this channel to get access", "members-only"),
        (
            r"video has been removed|video unavailable|video is unavailable|"
            r"account .*has been terminated|no longer available|video .*does not exist",
            "removed or unavailable",
        ),
        (r"requested format is not available", "requested format not available"),
    )
)
# YouTube's rate-limit page says "Video unavailable ... try again later": not permanent.
_RATE_LIMITED = re.compile(r"try again later", re.IGNORECASE)
_BLOCKED = re.compile(
    r"sign in to confirm|not a bot|http error 429|too many requests", re.IGNORECASE
)


def _tail(stderr: str, secrets: Sequence[str] = ()) -> str:
    lines = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("ERROR:")]
    text = " | ".join(errors or lines[-3:])
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    text = redact(text[-REDACT_WINDOW_CHARS:])
    return text[-STDERR_TAIL_CHARS:] if len(text) > STDERR_TAIL_CHARS else text


def classify_error(stderr: str, *, secrets: Sequence[str] = ()) -> Exception:
    """Map a failed yt-dlp run's stderr to the exception to raise (not raised here)."""
    tail = _tail(stderr, secrets)
    if _RATE_LIMITED.search(stderr):
        return DownloadBlocked(f"blocked by platform (rate limited): {tail}")
    for pattern, reason in _PERMANENT:
        if pattern.search(stderr):
            return Unavailable(f"video not downloadable ({reason}): {tail}")
    if _BLOCKED.search(stderr):
        return DownloadBlocked(f"blocked by platform (bot check / rate limit): {tail}")
    return YtDlpError(f"yt-dlp failed: {tail or 'no error output'}")


def proxy_secrets(proxy: str | None) -> list[str]:
    """The proxy URL and its password in raw, percent-decoded and percent-encoded forms."""
    if not proxy:
        return []
    secrets = [proxy]
    try:
        password = urlsplit(proxy).password
    except ValueError:
        password = None
    if password:
        decoded = unquote(password)
        secrets += [password, decoded, quote(decoded, safe="")]
    return sorted(set(secrets), key=len, reverse=True)


def _no_cap(timeout_s: float) -> float:
    return timeout_s


@dataclass(frozen=True)
class YtDlp:
    runner: Runner = run_command
    cookies: Path | None = None
    proxy: str | None = None
    probe_timeout_s: float = PROBE_TIMEOUT_S
    download_timeout_s: float = DOWNLOAD_TIMEOUT_S
    cap_timeout: Callable[[float], float] = _no_cap
    """Shrinks each call's timeout to what is left of the stage budget (`Deadline.timeout`)."""

    def base_args(self) -> list[str]:
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-progress",
            "--no-warnings",
            "--socket-timeout",
            "30",
        ]
        if self.cookies is not None:
            args += ["--cookies", str(self.cookies)]
        if self.proxy:
            args += ["--proxy", self.proxy]
        return args

    def _run(self, extra: Sequence[str], url: str, timeout_s: float) -> RunResult:
        args = [*self.base_args(), *extra, "--", url]
        secrets = proxy_secrets(self.proxy)
        try:
            result = self.runner(args, self.cap_timeout(timeout_s))
        except CommandTimeout as exc:
            raise YtDlpError(str(exc)) from None
        if result.returncode != 0:
            raise classify_error(result.stderr, secrets=secrets)
        return result

    def probe(self, url: str) -> dict[str, Any]:
        """Metadata only (`--dump-single-json --skip-download`); nothing is downloaded."""
        result = self._run(["--dump-single-json", "--skip-download"], url, self.probe_timeout_s)
        try:
            info = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise YtDlpError("yt-dlp returned invalid JSON") from None
        if not isinstance(info, dict):
            raise YtDlpError("yt-dlp returned unexpected JSON")
        return info

    @classmethod
    def from_env(
        cls,
        runner: Runner,
        env: Mapping[str, str] | None = None,
        *,
        cap_timeout: Callable[[float], float] = _no_cap,
    ) -> YtDlp:
        """Optional CLIPPER_YTDLP_COOKIES (path to a cookies.txt) and CLIPPER_YTDLP_PROXY."""
        env = os.environ if env is None else env
        cookies_raw = env.get(COOKIES_ENV, "").strip()
        cookies = Path(cookies_raw) if cookies_raw else None
        if cookies is not None and not cookies.is_file():
            raise YtDlpError(f"{COOKIES_ENV} points to a missing file")
        proxy = env.get(PROXY_ENV, "").strip() or None
        return cls(runner=runner, cookies=cookies, proxy=proxy, cap_timeout=cap_timeout)

    def download_audio(
        self, url: str, out_dir: Path, stem: str, *, info_json: Path | None = None
    ) -> Path:
        """Download the best audio-only format (`-f bestaudio`, never video) into
        `out_dir/<stem>.<ext>` and return that path.

        `info_json` (from a previous `probe`) skips a second metadata request. `stem` must
        be a fixed, safe name chosen by the caller.
        """
        extra = [
            "-f", "bestaudio", "--max-filesize", AUDIO_MAX_FILESIZE, "-o", _template(out_dir, stem),
        ]  # fmt: skip
        if info_json is not None:
            extra += ["--load-info-json", str(info_json)]
        self._run(extra, url, self.download_timeout_s)
        return _downloaded(out_dir, stem)

    def download_section(
        self,
        url: str,
        out_dir: Path,
        stem: str,
        section: tuple[float, float],
        *,
        format: str,
        timeout_s: float | None = None,
    ) -> Path:
        """Download only `section` (start_s, end_s) of the video in `format`, merged to mp4
        and cut at keyframes, into `out_dir/<stem>.<ext>`; return that path. No resume:
        a retry starts the section over."""
        start, end = section
        extra = [
            "--no-continue",
            "--no-part",
            "-f",
            format,
            "--merge-output-format",
            "mp4",
            "--download-sections",
            f"*{start:.3f}-{end:.3f}",
            "--force-keyframes-at-cuts",
            "--max-filesize",
            SECTION_MAX_FILESIZE,
            "-o",
            _template(out_dir, stem),
        ]
        self._run(extra, url, self.download_timeout_s if timeout_s is None else timeout_s)
        return _downloaded(out_dir, stem)


def _template(out_dir: Path, stem: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", stem) or stem.startswith("-"):
        raise ValueError(f"unsafe file stem: {stem!r}")
    # yt-dlp treats '%' in -o as a template field; escape the directory part.
    return str(out_dir.resolve() / stem).replace("%", "%%") + ".%(ext)s"


def _downloaded(out_dir: Path, stem: str) -> Path:
    found = [
        p
        for p in out_dir.glob(f"{stem}.*")
        if p.is_file() and p.suffix not in {".part", ".ytdl", ".tmp"}
    ]
    if len(found) != 1:
        raise YtDlpError(f"expected one downloaded file, found {len(found)}")
    return found[0]

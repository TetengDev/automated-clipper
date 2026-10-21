"""Render an EDL to a finished vertical clip: pre-cut -> Remotion -> loudnorm -> thumbnail.

Every external tool runs through `runner` (default `run_process`): an argument list, never a
shell, in its own session/process group so a timeout kills the whole tree (Remotion spawns
Chrome, a Rust compositor and ffmpeg), with a minimal environment (no API keys) and
captured stderr. Outputs land in `out_dir`, renamed into place only after every step
succeeded:

    clip.mp4         H.264 + AAC, 1080x1920 @ 30 fps, -14 LUFS, +faststart
    clip.jpg         thumbnail (mid-hook frame)
    clip.json        sidecar: EDL, file names, loudness measurements, timings

`edl.source.path` is resolved under `media_root` (and must stay inside it). The segment
[in_s, out_s] is first cut out with ffmpeg into a private work dir (frame-accurate, CFR
30 fps, at most 1080 px wide) written straight into `work/public/`, Remotion's public dir;
props, raw render and temp files live outside it. (A symlink would be nicer, but Remotion's
static server refuses symlinks with a 404.) Remotion copies served media into its temp dir
before decoding, so it must never see a multi-GB VOD, and decoding a small 1080-wide
segment keeps a 30 s clip well inside the render budget. Children get
TMPDIR=`work/tmp`, so the final rmtree of the work dir removes Remotion's bundle and frame
caches too.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from clipper.edl import EDL, FPS, HEIGHT, WIDTH, parse_edl

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess[str]]

COMPOSITION_ID = "Clip"
SEGMENT_NAME = "segment.mp4"
SEGMENT_MAX_WIDTH = 1080
SEGMENT_CRF = "18"
REMOTION_CLI = Path("node_modules/@remotion/cli/remotion-cli.js")
# Input hardening for every ffmpeg read: local files only, container demuxers we expect.
FFMPEG_INPUT_GUARD = (
    "-protocol_whitelist",
    "file",
    "-format_whitelist",
    "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm",
)
# Variables passed through to children (besides PATH/HOME/TMPDIR/locale set explicitly).
_PASSTHROUGH_ENV = ("PATH", "HOME")
TARGET_LUFS = -14.0
TARGET_TRUE_PEAK = -1.5
TARGET_LRA = 11.0
AUDIO_BITRATE = "192k"
AUDIO_SAMPLE_RATE = "48000"
DEFAULT_RENDER_TIMEOUT_S = 900.0
DEFAULT_FFMPEG_TIMEOUT_S = 300.0
STDERR_TAIL_CHARS = 4000
# Below this the input is effectively silent; loudnorm cannot (and should not) boost it.
SILENCE_LUFS = -70.0
_LOUDNORM_JSON = re.compile(r"\{[^{}]*\"input_i\"[^{}]*\}", re.DOTALL)
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")


class RenderError(Exception):
    """A render step failed. `stderr_tail` holds the end of the tool's stderr."""

    def __init__(self, message: str, stderr_tail: str = "") -> None:
        self.stderr_tail = stderr_tail
        detail = f"\n--- stderr (tail) ---\n{stderr_tail}" if stderr_tail else ""
        super().__init__(f"{message}{detail}")


@dataclass(frozen=True)
class RenderResult:
    video_path: Path
    thumbnail_path: Path
    sidecar_path: Path
    duration_s: float
    render_seconds: float


def default_render_dir() -> Path:
    """`render/` package location: $CLIPPER_RENDER_DIR, else the repo checkout's render/."""
    env = os.environ.get("CLIPPER_RENDER_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "render"


def _tail(text: str | bytes | None, limit: int = STDERR_TAIL_CHARS) -> str:
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    return text[-limit:].strip()


def _kill_group(proc: subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def run_process(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float,
    popen: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
) -> subprocess.CompletedProcess[str]:
    """Run argv (no shell) in a new session; on timeout SIGKILL the whole process group.

    Raises subprocess.TimeoutExpired (with the captured stderr) after killing the group.
    """
    proc = popen(
        list(argv),
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        stdout, stderr = proc.communicate()
        raise subprocess.TimeoutExpired(list(argv), timeout, output=stdout, stderr=stderr) from None
    except BaseException:  # KeyboardInterrupt etc.: never leave Chrome/ffmpeg behind
        _kill_group(proc)
        proc.communicate()
        raise
    # The leader exited; sweep anything it left running in its group (Chrome helpers etc.).
    # Residual risk: once every member is gone the pgid could in theory be reused by an
    # unrelated new group before we signal it. The window is microseconds and the probe
    # below skips the kill when the group no longer exists, which is the common case.
    try:
        os.killpg(proc.pid, 0)
    except (ProcessLookupError, PermissionError):
        pass
    else:
        _kill_group(proc)
    return subprocess.CompletedProcess(list(argv), proc.returncode, stdout, stderr)


def _raise_system_exit(signum: int, frame: object) -> None:
    raise SystemExit(128 + signum)


@contextlib.contextmanager
def _sigterm_as_exit() -> Iterator[None]:
    """While rendering, turn SIGTERM (docker stop) into SystemExit so run_process kills the
    child process group and the work dir is cleaned up. Restores the previous handler."""
    try:
        previous = signal.signal(signal.SIGTERM, _raise_system_exit)
    except ValueError:  # not the main thread: leave signal handling to the owner
        yield
        return
    try:
        yield
    finally:
        # None means the old handler wasn't installed from Python; fall back to default.
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


def child_env(tmp_dir: Path) -> dict[str, str]:
    """Minimal environment for render subprocesses: no secrets, private TMPDIR."""
    env = {key: os.environ[key] for key in _PASSTHROUGH_ENV if key in os.environ}
    env.setdefault("PATH", os.defpath)
    env.update(
        {
            "TMPDIR": str(tmp_dir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "NO_COLOR": "1",
        }
    )
    return env


def _run(
    runner: Runner,
    argv: Sequence[str],
    *,
    step: str,
    timeout_s: float,
    env: Mapping[str, str],
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    log.info("render step %s: %s", step, " ".join(argv))
    try:
        proc = runner(list(argv), cwd=cwd, env=dict(env), timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        tail = _tail(exc.stderr)
        log.error("render step %s timed out after %ss\n%s", step, timeout_s, tail)
        raise RenderError(f"{step} timed out after {timeout_s:g}s", tail) from exc
    except OSError as exc:
        raise RenderError(f"{step} could not start: {exc}") from exc
    tail = _tail(proc.stderr)
    if proc.returncode != 0:
        log.error("render step %s failed (exit %s)\n%s", step, proc.returncode, tail)
        raise RenderError(f"{step} failed with exit code {proc.returncode}", tail)
    if tail:
        log.debug("render step %s stderr:\n%s", step, tail)
    return proc


def precut_argv(ffmpeg: str, src: Path, dst: Path, in_s: float, duration_s: float) -> list[str]:
    """Frame-accurate cut (input seek + re-encode) to CFR 30 fps, <= 1080 px wide."""
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-y",
        "-ss",
        f"{in_s:.3f}",
        *FFMPEG_INPUT_GUARD,
        "-i",
        str(src),
        "-t",
        f"{duration_s:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf",
        f"scale='min({SEGMENT_MAX_WIDTH},iw)':-2,fps={FPS}",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        SEGMENT_CRF,
        "-g",
        str(FPS),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        AUDIO_BITRATE,
        "-ar",
        AUDIO_SAMPLE_RATE,
        "-f",
        "mp4",
        str(dst),
    ]


def segment_props(edl: EDL) -> dict[str, Any]:
    """Composition props for the pre-cut segment: source starts at 0, words shifted to match.

    Words that end before the cut or start after it are dropped; boundary words are clamped.
    The result is re-validated, so Remotion never receives props pydantic would reject.
    """
    in_s, duration = edl.source.in_s, edl.duration_s
    words = []
    for word in edl.words:
        start, end = word.start_s - in_s, word.end_s - in_s
        if end <= 0 or start >= duration:
            continue
        start = max(0.0, start)
        words.append({"text": word.text, "start_s": start, "end_s": max(start, end)})
    props = edl.to_props()
    props["source"] = {"path": SEGMENT_NAME, "in_s": 0.0, "out_s": duration}
    props["words"] = words
    return parse_edl(props).to_props()


def _resolve_source(edl: EDL, media_root: Path) -> Path:
    root = media_root.resolve()
    source = (root / edl.source.path).resolve()
    if not source.is_relative_to(root):
        raise RenderError(f"source path escapes media root: {edl.source.path}")
    if not source.is_file():
        raise RenderError(f"source media not found: {source}")
    return source


def remotion_argv(
    *,
    node: str,
    cli: Path,
    entry: str,
    output: Path,
    props_file: Path,
    public_dir: Path,
    concurrency: int | None,
    timeout_s: float,
) -> list[str]:
    # Invoke the CLI script with node directly (no npx/sh layers between us and the
    # process group we may have to kill).
    argv = [
        node,
        str(cli),
        "render",
        entry,
        COMPOSITION_ID,
        str(output),
        f"--props={props_file}",
        f"--public-dir={public_dir}",
        "--codec=h264",
        "--audio-codec=aac",
        "--enforce-audio-track",
        # Webpack's cache would live in render/node_modules/.cache, which is read-only in
        # the container; bundling from scratch takes ~3 s.
        "--bundle-cache=false",
        # Per-frame delayRender timeout (ms); generous, the overall timeout is ours.
        f"--timeout={int(min(timeout_s, 120) * 1000)}",
        "--log=info",
    ]
    if concurrency is not None:
        argv.append(f"--concurrency={concurrency}")
    return argv


def loudnorm_measure_argv(ffmpeg: str, src: Path) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        *FFMPEG_INPUT_GUARD,
        "-i",
        str(src),
        "-map",
        "0:a:0",
        "-af",
        f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TRUE_PEAK:g}:LRA={TARGET_LRA:g}:print_format=json",
        "-f",
        "null",
        "-",
    ]


def parse_loudnorm(stderr: str) -> dict[str, float]:
    """Parse the JSON block ffmpeg's loudnorm filter prints (first pass)."""
    # Values may be "-inf"/"inf" for silent input; callers must handle non-finite floats.
    matches = _LOUDNORM_JSON.findall(stderr or "")
    if not matches:
        raise RenderError("could not parse loudnorm measurement", _tail(stderr))
    try:
        raw = json.loads(matches[-1])
    except json.JSONDecodeError as exc:
        raise RenderError("could not parse loudnorm measurement", _tail(stderr)) from exc
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    try:
        return {key: float(raw[key]) for key in keys}
    except (KeyError, ValueError) as exc:
        raise RenderError(f"unexpected loudnorm output: {raw}") from exc


def finalize_argv(
    ffmpeg: str, src: Path, dst: Path, measured: dict[str, float] | None
) -> list[str]:
    """Copy video, loudness-normalize audio (2nd loudnorm pass), move moov to the front."""
    argv = [ffmpeg, "-hide_banner", "-nostats", "-y", *FFMPEG_INPUT_GUARD, "-i", str(src)]
    argv += ["-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy"]
    if measured is not None:
        argv += [
            "-af",
            f"loudnorm=I={TARGET_LUFS:g}:TP={TARGET_TRUE_PEAK:g}:LRA={TARGET_LRA:g}"
            f":measured_I={measured['input_i']:.2f}"
            f":measured_TP={measured['input_tp']:.2f}"
            f":measured_LRA={measured['input_lra']:.2f}"
            f":measured_thresh={measured['input_thresh']:.2f}"
            f":offset={measured['target_offset']:.2f}"
            ":linear=true:print_format=summary",
        ]
    argv += ["-c:a", "aac", "-b:a", AUDIO_BITRATE, "-ar", AUDIO_SAMPLE_RATE]
    argv += ["-movflags", "+faststart", "-f", "mp4", str(dst)]
    return argv


def thumbnail_argv(ffmpeg: str, src: Path, dst: Path, at_s: float) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-nostats",
        "-y",
        "-ss",
        f"{at_s:.3f}",
        *FFMPEG_INPUT_GUARD,
        "-i",
        str(src),
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-f",
        "image2",
        str(dst),
    ]


def thumbnail_time(edl: EDL) -> float:
    """Middle of the hook card (it's the most clickable frame), else ~1 s in."""
    if edl.hook is not None:
        return edl.hook.duration_s / 2
    return min(1.0, edl.duration_s / 2)


def _write_json(path: Path, data: Any) -> None:
    """Strict JSON (NaN/Infinity raise ValueError instead of producing invalid JSON)."""
    path.write_text(
        json.dumps(data, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )


def _finite_or_none(values: Mapping[str, float]) -> dict[str, float | None]:
    return {key: (value if math.isfinite(value) else None) for key, value in values.items()}


def _publish(moves: Sequence[tuple[Path, Path]]) -> None:
    """Rename staged files into place in order (sidecar last: it implies a complete set).
    If a rename fails, unlink the ones already published, then re-raise."""
    done: list[Path] = []
    try:
        for staged, final in moves:
            done.append(final)  # before replace: a rename that half-happened is undone too
            os.replace(staged, final)
    except BaseException:
        for final in done:
            with contextlib.suppress(OSError):
                final.unlink(missing_ok=True)
        raise


def _prepare_work_dir(work_dir: Path) -> None:
    """Fresh, private (0700) work dir. A symlink at that path is removed, never followed."""
    try:
        if work_dir.is_symlink() or work_dir.is_file():
            work_dir.unlink()
        elif work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(mode=0o700)
        (work_dir / "public").mkdir(mode=0o700)
        (work_dir / "tmp").mkdir(mode=0o700)
    except OSError as exc:
        raise RenderError(f"cannot prepare work dir {work_dir}: {exc}") from exc


def render_clip(
    edl: EDL | dict[str, Any],
    media_root: Path,
    out_dir: Path,
    *,
    runner: Runner = run_process,
    timeout_s: float = DEFAULT_RENDER_TIMEOUT_S,
    ffmpeg_timeout_s: float = DEFAULT_FFMPEG_TIMEOUT_S,
    render_dir: Path | None = None,
    basename: str = "clip",
    concurrency: int | None = None,
    node: str = "node",
    ffmpeg: str = "ffmpeg",
) -> RenderResult:
    """Render `edl` to `out_dir/<basename>.{mp4,jpg,json}`.

    Use a distinct basename per clip version (e.g. ``basename=f"v{version}"``) so a
    re-render never overwrites the files an earlier clip_versions row points at.

    Validates the EDL (pydantic) before any subprocess runs; the Remotion side validates
    the props again (ajv) before rendering a frame. Outputs are staged in a private work dir
    and renamed into `out_dir` only after every step succeeded; if a rename fails, the
    files already moved are removed again (best effort). Raises RenderError on any failure.
    """
    if not _BASENAME.fullmatch(basename):
        raise ValueError(f"invalid basename: {basename!r}")
    if not isinstance(edl, EDL):
        try:
            edl = parse_edl(edl)
        except ValidationError as exc:
            raise RenderError(f"invalid EDL: {exc}") from exc
    source = _resolve_source(edl, media_root)
    render_dir = (render_dir or default_render_dir()).resolve()
    entry = "src/index.ts"
    if not (render_dir / entry).is_file():
        raise RenderError(f"Remotion entry point not found: {render_dir / entry}")
    cli = render_dir / REMOTION_CLI
    if not cli.is_file():
        raise RenderError(f"Remotion CLI not found: {cli} (run `npm ci` in {render_dir})")

    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RenderError(f"cannot create output dir {out_dir}: {exc}") from exc
    out_dir = out_dir.resolve()
    work_dir = out_dir / f".{basename}.work"
    props_file = work_dir / "props.json"
    segment = work_dir / "public" / SEGMENT_NAME
    raw_video = work_dir / "raw.mp4"
    staged_video = work_dir / "final.mp4"
    staged_thumb = work_dir / "thumb.jpg"
    staged_sidecar = work_dir / "sidecar.json"
    video = out_dir / f"{basename}.mp4"
    thumb = out_dir / f"{basename}.jpg"
    sidecar = out_dir / f"{basename}.json"

    with _sigterm_as_exit():
        started = time.monotonic()
        _prepare_work_dir(work_dir)
        env = child_env(work_dir / "tmp")
        try:
            _run(
                runner,
                precut_argv(ffmpeg, source, segment, edl.source.in_s, edl.duration_s),
                step="pre-cut",
                timeout_s=ffmpeg_timeout_s,
                env=env,
            )
            _write_json(props_file, segment_props(edl))
            _run(
                runner,
                remotion_argv(
                    node=node,
                    cli=cli,
                    entry=entry,
                    output=raw_video,
                    props_file=props_file,
                    public_dir=work_dir / "public",
                    concurrency=concurrency,
                    timeout_s=timeout_s,
                ),
                step="remotion render",
                timeout_s=timeout_s,
                env=env,
                cwd=render_dir,
            )
            remotion_seconds = time.monotonic() - started  # includes the pre-cut

            measure = _run(
                runner,
                loudnorm_measure_argv(ffmpeg, raw_video),
                step="loudnorm measure",
                timeout_s=ffmpeg_timeout_s,
                env=env,
            )
            measured = parse_loudnorm(measure.stderr)
            silent = not math.isfinite(measured["input_i"]) or measured["input_i"] < SILENCE_LUFS
            if silent:
                log.warning(
                    "clip audio is silent (%.1f LUFS); skipping loudnorm", measured["input_i"]
                )
            _run(
                runner,
                finalize_argv(ffmpeg, raw_video, staged_video, None if silent else measured),
                step="loudnorm + faststart",
                timeout_s=ffmpeg_timeout_s,
                env=env,
            )
            _run(
                runner,
                thumbnail_argv(ffmpeg, staged_video, staged_thumb, thumbnail_time(edl)),
                step="thumbnail",
                timeout_s=ffmpeg_timeout_s,
                env=env,
            )

            render_seconds = time.monotonic() - started
            _write_json(
                staged_sidecar,
                {
                    "sidecar_version": 1,
                    "rendered_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "composition": COMPOSITION_ID,
                    "width": WIDTH,
                    "height": HEIGHT,
                    "fps": FPS,
                    "duration_s": edl.duration_s,
                    "duration_in_frames": edl.duration_in_frames,
                    "files": {"video": video.name, "thumbnail": thumb.name},
                    "loudness": {
                        "target_lufs": TARGET_LUFS,
                        "target_true_peak": TARGET_TRUE_PEAK,
                        "measured": _finite_or_none(measured),
                        "normalized": not silent,
                    },
                    "timings": {
                        "remotion_seconds": round(remotion_seconds, 2),
                        "total_seconds": round(render_seconds, 2),
                    },
                    "edl": edl.to_props(),
                },
            )
            _publish([(staged_thumb, thumb), (staged_video, video), (staged_sidecar, sidecar)])
        except (OSError, ValueError) as exc:
            raise RenderError(f"render I/O error: {exc}") from exc
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

        log.info("rendered %s in %.1fs", video, render_seconds)
        return RenderResult(
            video_path=video,
            thumbnail_path=thumb,
            sidecar_path=sidecar,
            duration_s=edl.duration_s,
            render_seconds=render_seconds,
        )

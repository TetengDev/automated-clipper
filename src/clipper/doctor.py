"""`clipper doctor`: preflight checks for the external toolchain (CLP-128).

Every check reports a status (`ok` / `warn` / `fail`) and a detail with the version and
resolved path. Details never contain environment values or child output beyond a parsed
version: a failed run is reported by its exit code only.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import sysconfig
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Literal

from yt_dlp.extractor.youtube.jsc._builtin.vendor._info import HASHES as EJS_HASHES
from yt_dlp.extractor.youtube.jsc._builtin.vendor._info import VERSION as EJS_VENDORED
from yt_dlp.utils import version_tuple
from yt_dlp.utils._jsruntime import DenoJsRuntime

from clipper.pipeline.ytdlp import CommandTimeout, RunResult, run_command

Status = Literal["ok", "warn", "fail"]
Runner = Callable[..., RunResult]
"""`run_command` or a fake: `(args, timeout_s, *, stdin_path=None)`."""
Which = Callable[[str], str | None]
Lookup = Callable[[str], str | None]
"""Installed distribution version by name, None when missing."""
VERSION_TIMEOUT_S = 30.0
SOLVER_TIMEOUT_S = 60.0
FILTERS = ("loudnorm", "ebur128", "silencedetect", "scale", "crop")
"""ffmpeg filters the pipeline uses (loudness, silence detection, reframing)."""
UV_SYNC_HINT = "run uv sync (yt-dlp[default])"
RENDER_DIR_ENV = "CLIPPER_RENDER_DIR"
DENO_RUN_ARGS = (
    "run", "--ext=js", "--no-code-cache", "--no-prompt", "--no-remote", "--no-lock",
    "--node-modules-dir=none", "--no-config", "--no-npm", "--cached-only", "-",
)  # fmt: skip
"""yt-dlp's exact Deno invocation when the challenge scripts come from yt-dlp-ejs."""
_VERSION_CHARS = r"([0-9A-Za-z.+~_-]{1,64})(?!\S)"
"""A printable version token: no control characters or escapes, bounded length."""
_FFMPEG_VERSION = re.compile(rf"^\S+ version {_VERSION_CHARS}")
_DENO_VERSION = re.compile(rf"^deno {_VERSION_CHARS}")
_NODE_VERSION = re.compile(rf"^v{_VERSION_CHARS}")


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


class _RunFailed(Exception):
    """A child could not start, timed out or exited non-zero; the message is safe to print."""


def parse_version(output: str) -> str | None:
    """`8.1.2` from an ffmpeg / ffprobe `-version` banner, else None."""
    return _match(_FFMPEG_VERSION, output)


def _match(pattern: re.Pattern[str], output: str) -> str | None:
    match = pattern.match(output)
    return match.group(1) if match else None


def package_version(name: str) -> str | None:
    """Installed distribution version, or None when it is not installed."""
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _run(
    runner: Runner, args: list[str], timeout_s: float, stdin_path: Path | None = None
) -> RunResult:
    label = " ".join(args[:2])
    try:
        kwargs = {} if stdin_path is None else {"stdin_path": stdin_path}
        result = runner(args, timeout_s, **kwargs)
    except CommandTimeout:
        raise _RunFailed(f"{label} timed out") from None
    except OSError:
        raise _RunFailed(f"{label} could not start") from None
    if result.returncode != 0:
        raise _RunFailed(f"{label} exited {result.returncode}")
    return result


def _ffmpeg_tool(name: str, runner: Runner, which: Which) -> Check:
    path = which(name)
    if path is None:
        return Check(name, "fail", "not found on PATH")
    try:
        result = _run(runner, [path, "-version"], VERSION_TIMEOUT_S)
    except _RunFailed as exc:
        return Check(name, "fail", str(exc))
    return Check(name, "ok", f"{parse_version(result.stdout) or 'unknown'} {path}")


def _filters(runner: Runner, which: Which) -> Check:
    name = "ffmpeg filters"
    path = which("ffmpeg")
    if path is None:
        return Check(name, "fail", "ffmpeg not found")
    try:
        result = _run(runner, [path, "-hide_banner", "-filters"], VERSION_TIMEOUT_S)
    except _RunFailed as exc:
        return Check(name, "fail", str(exc))
    # Filter lines are "<flags> <name> <in->out> <description>".
    found = {fields[1] for fields in map(str.split, result.stdout.splitlines()) if len(fields) > 1}
    missing = [f for f in FILTERS if f not in found]
    if missing:
        return Check(name, "fail", f"missing: {', '.join(missing)}")
    return Check(name, "ok", ", ".join(FILTERS))


def _ytdlp(version: Lookup) -> Check:
    # This module imports yt_dlp, so it is installed; only the version is reported.
    return Check("yt-dlp", "ok", version("yt-dlp") or "unknown")


@dataclass(frozen=True)
class _Solver:
    """yt-dlp-ejs as yt-dlp's `_pypackage_source` reads it: module version and scripts."""

    version: str
    lib: str
    core: str


def _ejs(solver: _Solver | None) -> Check:
    """yt-dlp (ejs.py `_get_script`) ignores package scripts whose major.minor differs from
    its vendored version or whose sha3-512 differs from its vendored hashes, and falls back
    to scripts that cannot solve without npm."""
    if solver is None:
        return Check("yt-dlp-ejs", "fail", f"not installed or unreadable; {UV_SYNC_HINT}")
    detail = f"{solver.version} (yt-dlp expects {EJS_VENDORED})"
    vendored = version_tuple(EJS_VENDORED, lenient=True)[:2]
    if version_tuple(solver.version, lenient=True)[:2] != vendored:
        return Check("yt-dlp-ejs", "fail", f"{detail}; {UV_SYNC_HINT}")
    expected = (EJS_HASHES["yt.solver.lib.min.js"], EJS_HASHES["yt.solver.core.min.js"])
    scripts = (solver.lib, solver.core)
    if tuple(hashlib.sha3_512(code.encode()).hexdigest() for code in scripts) != expected:
        mismatch = "solver scripts do not match yt-dlp's hashes"
        return Check("yt-dlp-ejs", "fail", f"{detail}: {mismatch}; {UV_SYNC_HINT}")
    return Check("yt-dlp-ejs", "ok", detail)


def _find_deno(which: Which, scripts_dir: Path) -> str | None:
    """yt-dlp's order: the Python scripts dir (the venv's bin/), then PATH."""
    venv = scripts_dir / "deno"
    if os.access(venv, os.X_OK) and not venv.is_dir():
        return str(venv)
    return which("deno")


def _load_solver() -> _Solver | None:
    """yt-dlp-ejs's version and (lib, core) scripts; None when missing or unreadable."""
    try:
        import yt_dlp_ejs
        import yt_dlp_ejs.yt.solver as solver

        return _Solver(str(yt_dlp_ejs.version), solver.lib(), solver.core())
    except (ImportError, OSError, ValueError):
        return None


def _solver_probe(solver: _Solver) -> str:
    """The solver as yt-dlp feeds it to Deno (ejs.py `_construct_stdin`), ending in a probe
    that prints `function` only when the core script defined `jsc`."""
    return (
        f"{solver.lib}\nObject.assign(globalThis, lib);\n{solver.core}\nconsole.log(typeof jsc);\n"
    )


def _deno(
    runner: Runner, which: Which, platform: str, scripts_dir: Path, verified: _Solver | None
) -> Check:
    """`verified` is None unless the yt-dlp-ejs check passed: Deno is never fed scripts
    that yt-dlp itself would refuse."""
    hint = "run uv sync" if platform == "darwin" else "rebuild the image (uv sync --locked)"
    path = _find_deno(which, scripts_dir)
    if path is None:
        return Check("deno", "fail", f"not found; {hint}")
    try:
        found = _match(_DENO_VERSION, _run(runner, [path, "--version"], VERSION_TIMEOUT_S).stdout)
        found = found or "unknown"
        minimum = DenoJsRuntime.MIN_SUPPORTED_VERSION
        if version_tuple(found, lenient=True) < minimum:
            needed = ".".join(map(str, minimum))
            return Check("deno", "fail", f"{found} {path} (yt-dlp needs >= {needed}); {hint}")
        if verified is None:
            skipped = "solver skipped, yt-dlp-ejs failed verification"
            return Check("deno", "fail", f"{found} {path}: {skipped}; {hint}")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                script = Path(tmp) / "solver.js"  # run_command takes stdin from a file only
                script.write_text(_solver_probe(verified), "utf-8")
                result = _run(runner, [path, *DENO_RUN_ARGS], SOLVER_TIMEOUT_S, script)
        except (OSError, ValueError):  # no exception text: it names TMPDIR paths
            return Check("deno", "fail", f"cannot write the solver script to a temp dir; {hint}")
    except _RunFailed as exc:
        return Check("deno", "fail", f"{exc}; {hint}")
    if result.stderr:  # yt-dlp fails a solve on any Deno stderr
        return Check("deno", "fail", f"{found} {path}: solver wrote to stderr; {hint}")
    if result.stdout.strip() != "function":
        return Check("deno", "fail", f"{found} {path}: solver did not load; {hint}")
    return Check("deno", "ok", f"{found} {path} (solver ok)")


def _node(runner: Runner, which: Which, env: Mapping[str, str]) -> Check:
    """Only renders (Remotion) need node: a failure when CLIPPER_RENDER_DIR says renders run."""
    severity: Status = "fail" if env.get(RENDER_DIR_ENV) else "warn"
    path = which("node")
    if path is None:
        return Check("node", severity, "not found (needed for renders)")
    try:
        result = _run(runner, [path, "--version"], VERSION_TIMEOUT_S)
    except _RunFailed as exc:
        return Check("node", severity, str(exc))
    return Check("node", "ok", f"{_match(_NODE_VERSION, result.stdout) or 'unknown'} {path}")


def run_checks(
    *,
    runner: Runner = run_command,
    which: Which = shutil.which,
    version: Lookup = package_version,
    env: Mapping[str, str] = os.environ,
    platform: str = sys.platform,
    scripts_dir: Path | None = None,
) -> list[Check]:
    """All checks, in order. `scripts_dir` defaults to this Python's scripts dir."""
    scripts = Path(sysconfig.get_path("scripts")) if scripts_dir is None else scripts_dir
    solver = _load_solver()
    ejs = _ejs(solver)
    verified = solver if ejs.status == "ok" else None
    return [
        _ffmpeg_tool("ffmpeg", runner, which),
        _ffmpeg_tool("ffprobe", runner, which),
        _filters(runner, which),
        _ytdlp(version),
        ejs,
        _deno(runner, which, platform, scripts, verified),
        _node(runner, which, env),
    ]


def format_check(check: Check) -> str:
    """One output line. Paths (from PATH) and versions (from dist-info) are not ours, so
    every unprintable character (control, ESC, bidi override) becomes `?`."""
    line = f"{check.status:<4} {check.name:<15} {check.detail}"
    return "".join(ch if ch.isprintable() else "?" for ch in line)


def summary(checks: list[Check]) -> str:
    counts = {status: sum(c.status == status for c in checks) for status in ("ok", "warn", "fail")}
    return f"doctor: {counts['ok']} ok, {counts['warn']} warnings, {counts['fail']} failed"

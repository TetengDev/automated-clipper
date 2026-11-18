import shutil
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from yt_dlp.extractor.youtube.jsc._builtin.vendor._info import VERSION as EJS_VENDORED

from clipper.doctor import (
    Check,
    format_check,
    package_version,
    parse_version,
    run_checks,
    summary,
)
from clipper.pipeline.ytdlp import CommandTimeout, RunResult

FFMPEG_5 = "ffmpeg version 5.1.9-0+deb12u1 Copyright (c) 2000-2025 the FFmpeg developers"
FFMPEG_8 = "ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers"
FFPROBE_8 = "ffprobe version 8.1.2 Copyright (c) 2007-2026 the FFmpeg developers"


def filters_listing(names: Sequence[str]) -> str:
    header = "Filters:\n  T.. = Timeline support\n  | = Source or sink filter\n  ------\n"
    return header + "".join(f" TSC {n:<16} A->A       Some filter.\n" for n in names)


DENO_BANNER = "deno 2.9.8 (stable, release, x86_64-unknown-linux-gnu)\nv8 15.0\ntypescript 6.0.3\n"
DENO_FLAGS = [
    "run", "--ext=js", "--no-code-cache", "--no-prompt", "--no-remote", "--no-lock",
    "--node-modules-dir=none", "--no-config", "--no-npm", "--cached-only", "-",
]  # fmt: skip
LINUX_HINT = "rebuild the image (uv sync --locked)"
NO_SCRIPTS = Path(__file__).parent / "no-such-scripts-dir"
FILTERS = ["acompressor", "crop", "ebur128", "loudnorm", "scale", "silencedetect"]


def ok(stdout: str = "") -> RunResult:
    return RunResult(0, stdout, "")


class FakeRunner:
    """Answers by (binary name, first argument); records every call."""

    def __init__(self, responses: Mapping[tuple[str, str], RunResult | Exception]) -> None:
        self.responses = dict(responses)
        self.calls: list[list[str]] = []
        self.stdin: dict[str, str] = {}

    def __call__(
        self, args: Sequence[str], timeout_s: float, *, stdin_path: Path | None = None
    ) -> RunResult:
        assert timeout_s > 0
        self.calls.append(list(args))
        name = Path(args[0]).name
        if stdin_path is not None:
            self.stdin[name] = stdin_path.read_text("utf-8")
        result = self.responses[(name, args[1])]
        if isinstance(result, Exception):
            raise result
        return result


def healthy() -> dict[tuple[str, str], RunResult | Exception]:
    return {
        ("ffmpeg", "-version"): ok(FFMPEG_8 + "\nbuilt with clang\n"),
        ("ffprobe", "-version"): ok(FFPROBE_8 + "\n"),
        ("ffmpeg", "-hide_banner"): ok(filters_listing(FILTERS)),
        ("deno", "--version"): ok(DENO_BANNER),
        ("deno", "run"): ok("function\n"),
        ("node", "--version"): ok("v22.22.0\n"),
    }


WHICH = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe", "deno": "/usr/bin/deno"}
WHICH["node"] = "/usr/local/bin/node"
VERSIONS = {"yt-dlp": "2026.10.21"}
EJS_UNUSABLE = "not installed or unreadable; run uv sync (yt-dlp[default])"


def deno_skipped(hint: str = "rebuild the image (uv sync --locked)") -> str:
    return f"2.9.8 /usr/bin/deno: solver skipped, yt-dlp-ejs failed verification; {hint}"


def checks(
    responses: Mapping[tuple[str, str], RunResult | Exception] | None = None,
    *,
    which: Mapping[str, str] | None = None,
    versions: Mapping[str, str] | None = None,
    platform: str = "linux",
    scripts_dir: Path = NO_SCRIPTS,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, Check], FakeRunner]:
    runner = FakeRunner(healthy() if responses is None else responses)
    found = WHICH if which is None else which
    installed = VERSIONS if versions is None else versions
    result = run_checks(
        runner=runner,
        which=found.get,
        version=installed.get,
        platform=platform,
        scripts_dir=scripts_dir,
        env={} if env is None else env,
    )
    return {c.name: c for c in result}, runner


@pytest.mark.parametrize(
    ("banner", "expected"),
    [(FFMPEG_5, "5.1.9-0+deb12u1"), (FFMPEG_8, "8.1.2"), (FFPROBE_8, "8.1.2")],
)
def test_parse_version_reads_ffmpeg_and_ffprobe_banners(banner: str, expected: str) -> None:
    assert parse_version(banner + "\nconfiguration: --x\n") == expected


def test_parse_version_unknown_output_is_none() -> None:
    assert parse_version("garbage") is None


def test_ffmpeg_and_ffprobe_ok_show_version_and_path() -> None:
    result, _ = checks()

    assert result["ffmpeg"].status == "ok"
    assert result["ffmpeg"].detail == "8.1.2 /usr/bin/ffmpeg"
    assert result["ffprobe"].status == "ok"
    assert result["ffprobe"].detail == "8.1.2 /usr/bin/ffprobe"


def test_missing_ffmpeg_fails_without_running_it() -> None:
    result, runner = checks(which={"ffprobe": "/usr/bin/ffprobe"})

    assert result["ffmpeg"].status == "fail"
    assert result["ffmpeg"].detail == "not found on PATH"
    assert not [c for c in runner.calls if Path(c[0]).name == "ffmpeg"]


def test_ffprobe_failed_run_reports_exit_code_only() -> None:
    responses = healthy()
    responses[("ffprobe", "-version")] = RunResult(3, "", "secret-ish stderr")

    result, _ = checks(responses)

    assert result["ffprobe"].status == "fail"
    assert result["ffprobe"].detail == "/usr/bin/ffprobe -version exited 3"


def test_ffmpeg_filters_all_present_is_ok() -> None:
    result, runner = checks()

    assert result["ffmpeg filters"].status == "ok"
    assert result["ffmpeg filters"].detail == "loudnorm, ebur128, silencedetect, scale, crop"
    assert ["/usr/bin/ffmpeg", "-hide_banner", "-filters"] in runner.calls


def test_ffmpeg_filters_missing_one_is_named() -> None:
    responses = healthy()
    listing = [n for n in FILTERS if n != "ebur128"] + ["ebur128_extra"]
    responses[("ffmpeg", "-hide_banner")] = ok(filters_listing(listing))

    result, _ = checks(responses)

    assert result["ffmpeg filters"].status == "fail"
    assert result["ffmpeg filters"].detail == "missing: ebur128"


def test_ffmpeg_filters_without_ffmpeg_fail() -> None:
    result, _ = checks(which={"ffprobe": "/usr/bin/ffprobe"})

    assert result["ffmpeg filters"].status == "fail"
    assert result["ffmpeg filters"].detail == "ffmpeg not found"


def test_ffmpeg_filters_timeout_fails() -> None:
    responses = healthy()
    responses[("ffmpeg", "-hide_banner")] = CommandTimeout("ffmpeg timed out after 30s")

    result, _ = checks(responses)

    assert result["ffmpeg filters"].status == "fail"
    assert result["ffmpeg filters"].detail == "/usr/bin/ffmpeg -hide_banner timed out"


def test_ytdlp_version_is_printed() -> None:
    result, _ = checks()

    assert result["yt-dlp"] == Check("yt-dlp", "ok", "2026.10.21")


def test_ejs_matching_vendored_major_minor_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp_ejs

    major, minor, _ = EJS_VENDORED.split(".")
    monkeypatch.setattr(yt_dlp_ejs, "version", f"{major}.{minor}.99")

    result, _ = checks()

    assert result["yt-dlp-ejs"] == Check(
        "yt-dlp-ejs", "ok", f"{major}.{minor}.99 (yt-dlp expects {EJS_VENDORED})"
    )


def test_ejs_missing_fails_with_uv_sync_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yt_dlp_ejs", None)  # import fails

    result, _ = checks()

    assert result["yt-dlp-ejs"] == Check("yt-dlp-ejs", "fail", EJS_UNUSABLE)


def test_ejs_version_is_the_module_attribute_ytdlp_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """yt-dlp's `_pypackage_source` reads `yt_dlp_ejs.version`, not the dist-info."""
    import yt_dlp_ejs

    major, minor, _ = EJS_VENDORED.split(".")
    stale = f"{major}.{int(minor) + 1}.0"
    monkeypatch.setattr(yt_dlp_ejs, "version", stale)

    result, _ = checks(versions={"yt-dlp": "2026.10.21", "yt-dlp-ejs": EJS_VENDORED})

    assert result["yt-dlp-ejs"] == Check(
        "yt-dlp-ejs",
        "fail",
        f"{stale} (yt-dlp expects {EJS_VENDORED}); run uv sync (yt-dlp[default])",
    )


def with_response(
    key: tuple[str, str], value: RunResult | Exception
) -> dict[tuple[str, str], RunResult | Exception]:
    responses = healthy()
    responses[key] = value
    return responses


def test_deno_ok_runs_the_solver_with_ytdlp_flags_and_scripts() -> None:
    import yt_dlp_ejs.yt.solver as solver

    result, runner = checks()

    assert result["deno"] == Check("deno", "ok", "2.9.8 /usr/bin/deno (solver ok)")
    assert ["/usr/bin/deno", *DENO_FLAGS] in runner.calls
    script = runner.stdin["deno"]
    lib_at = script.index(solver.lib())
    assign_at = script.index("Object.assign(globalThis, lib);")
    core_at = script.index(solver.core())
    assert lib_at < assign_at < core_at
    assert script.rstrip().endswith("console.log(typeof jsc);")


def test_deno_in_venv_scripts_dir_wins_over_path(tmp_path: Path) -> None:
    venv_deno = tmp_path / "deno"
    venv_deno.write_text("")
    venv_deno.chmod(0o755)

    result, runner = checks(scripts_dir=tmp_path)

    assert result["deno"].detail == f"2.9.8 {venv_deno} (solver ok)"
    assert [str(venv_deno), "--version"] in runner.calls
    assert not [c for c in runner.calls if c[0] == "/usr/bin/deno"]


def test_non_executable_deno_in_scripts_dir_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "deno").write_text("")

    result, _ = checks(scripts_dir=tmp_path)

    assert result["deno"].detail == "2.9.8 /usr/bin/deno (solver ok)"


@pytest.mark.parametrize(("platform", "hint"), [("darwin", "run uv sync"), ("linux", LINUX_HINT)])
def test_deno_missing_fails_with_platform_hint(platform: str, hint: str) -> None:
    which = {k: v for k, v in WHICH.items() if k != "deno"}

    result, runner = checks(which=which, platform=platform)

    assert result["deno"] == Check("deno", "fail", f"not found; {hint}")
    assert not [c for c in runner.calls if Path(c[0]).name == "deno"]


def test_deno_too_old_fails_before_the_solver_run() -> None:
    result, runner = checks(with_response(("deno", "--version"), ok("deno 2.2.12 (stable)\n")))

    assert result["deno"] == Check(
        "deno", "fail", f"2.2.12 /usr/bin/deno (yt-dlp needs >= 2.3.0); {LINUX_HINT}"
    )
    assert ["/usr/bin/deno", *DENO_FLAGS] not in runner.calls


def test_deno_version_run_failure_fails() -> None:
    result, _ = checks(with_response(("deno", "--version"), RunResult(1, "", "boom")))

    assert result["deno"] == Check(
        "deno", "fail", f"/usr/bin/deno --version exited 1; {LINUX_HINT}"
    )


def test_deno_solver_stderr_fails_even_with_right_output() -> None:
    noisy = RunResult(0, "function\n", "Warning: cache dir is read-only")

    result, _ = checks(with_response(("deno", "run"), noisy), platform="darwin")

    assert result["deno"] == Check(
        "deno", "fail", "2.9.8 /usr/bin/deno: solver wrote to stderr; run uv sync"
    )


def test_deno_solver_wrong_output_fails() -> None:
    result, _ = checks(with_response(("deno", "run"), ok("undefined\n")))

    assert result["deno"] == Check(
        "deno", "fail", f"2.9.8 /usr/bin/deno: solver did not load; {LINUX_HINT}"
    )


def test_deno_solver_nonzero_exit_fails_with_exit_code_only() -> None:
    result, _ = checks(with_response(("deno", "run"), RunResult(1, "", "error: hunter2")))

    assert result["deno"] == Check("deno", "fail", f"/usr/bin/deno run exited 1; {LINUX_HINT}")


def test_deno_solver_timeout_fails() -> None:
    result, _ = checks(with_response(("deno", "run"), CommandTimeout("deno timed out after 60s")))

    assert result["deno"] == Check("deno", "fail", f"/usr/bin/deno run timed out; {LINUX_HINT}")


def test_deno_solver_that_cannot_start_fails() -> None:
    result, _ = checks(with_response(("deno", "run"), PermissionError(13, "denied")))

    assert result["deno"] == Check(
        "deno", "fail", f"/usr/bin/deno run could not start; {LINUX_HINT}"
    )


NO_NODE = {k: v for k, v in WHICH.items() if k != "node"}
RENDERS = {"CLIPPER_RENDER_DIR": "/app/render"}


def test_node_found_is_ok_with_version_and_path() -> None:
    result, _ = checks(env=RENDERS)

    assert result["node"] == Check("node", "ok", "22.22.0 /usr/local/bin/node")


def test_node_missing_only_warns_without_render_dir() -> None:
    result, _ = checks(which=NO_NODE)

    assert result["node"] == Check("node", "warn", "not found (needed for renders)")


def test_node_missing_fails_when_renders_are_expected() -> None:
    result, _ = checks(which=NO_NODE, env=RENDERS)

    assert result["node"] == Check("node", "fail", "not found (needed for renders)")


@pytest.mark.parametrize(("env", "status"), [({}, "warn"), (RENDERS, "fail")])
def test_node_failed_run_uses_the_render_severity(env: dict, status: str) -> None:
    result, _ = checks(with_response(("node", "--version"), RunResult(9, "", "x")), env=env)

    assert result["node"] == Check("node", status, "/usr/local/bin/node --version exited 9")


def test_deno_without_solver_scripts_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "yt_dlp_ejs.yt.solver", None)  # import fails

    result, runner = checks()

    assert result["deno"] == Check("deno", "fail", deno_skipped())
    assert ["/usr/bin/deno", *DENO_FLAGS] not in runner.calls


def test_format_check_aligns_status_name_and_detail() -> None:
    line = format_check(Check("ffmpeg", "ok", "8.1.2 /usr/bin/ffmpeg"))

    assert line == "ok   ffmpeg          8.1.2 /usr/bin/ffmpeg"


def test_summary_counts_each_status() -> None:
    result = [
        Check("a", "ok", ""),
        Check("b", "ok", ""),
        Check("c", "warn", ""),
        Check("d", "fail", ""),
    ]

    assert summary(result) == "doctor: 2 ok, 1 warnings, 1 failed"


def test_output_never_contains_env_values_or_child_stderr() -> None:
    env = {
        "CLIPPER_YTDLP_PROXY": "http://u:hunter2@p:1",
        "GROQ_API_KEY": "gsk_x",
        "CLIPPER_RENDER_DIR": "/app/render",
    }
    leaky = RunResult(1, "", "proxy http://u:hunter2@p:1 failed, key gsk_x")
    responses = {key: leaky for key in healthy()}
    responses[("deno", "run")] = RunResult(0, "function\n", "hunter2 gsk_x")

    for which in (WHICH, NO_NODE):
        result, _ = checks(responses, which=which, env=env)
        output = "\n".join([*map(format_check, result.values()), summary(list(result.values()))])
        assert "hunter2" not in output
        assert "gsk_x" not in output
        assert "/app/render" not in output
    timeouts = {key: CommandTimeout("hunter2 timed out") for key in healthy()}
    result, _ = checks(timeouts, env=env)
    assert all("hunter2" not in format_check(c) for c in result.values())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")
def test_real_ffmpeg_ffprobe_and_filters_are_ok() -> None:
    result = {c.name: c for c in run_checks()}

    for name in ("ffmpeg", "ffprobe", "ffmpeg filters"):
        assert result[name].status == "ok", result[name]


def test_real_deno_runs_the_solver() -> None:
    """Deno ships in the venv (yt-dlp[pin-deno]), so this needs no skip."""
    result = {c.name: c for c in run_checks()}

    assert result["deno"].status == "ok", result["deno"]
    assert result["deno"].detail.endswith("(solver ok)")


def test_package_version_is_none_when_not_installed() -> None:
    assert package_version("clipper-no-such-distribution") is None
    assert package_version("yt-dlp")


@pytest.mark.parametrize("script", ["lib", "core"])
def test_ejs_scripts_not_matching_ytdlp_hashes_fail(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    import yt_dlp_ejs.yt.solver as solver

    monkeypatch.setattr(solver, script, lambda: "/* tampered */")

    result, _ = checks()

    assert result["yt-dlp-ejs"] == Check(
        "yt-dlp-ejs",
        "fail",
        f"{EJS_VENDORED} (yt-dlp expects {EJS_VENDORED}): solver scripts do not match"
        f" yt-dlp's hashes; run uv sync (yt-dlp[default])",
    )


def test_ejs_matching_hashes_is_ok_and_prints_no_hash() -> None:
    from yt_dlp.extractor.youtube.jsc._builtin.vendor._info import HASHES

    result, _ = checks()

    assert result["yt-dlp-ejs"].status == "ok"
    assert all(h[:16] not in result["yt-dlp-ejs"].detail for h in HASHES.values())


def test_undecodable_solver_scripts_fail_ejs_and_deno(monkeypatch: pytest.MonkeyPatch) -> None:
    import yt_dlp_ejs.yt.solver as solver

    def broken() -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(solver, "core", broken)

    result, runner = checks()

    assert result["yt-dlp-ejs"] == Check("yt-dlp-ejs", "fail", EJS_UNUSABLE)
    assert result["deno"] == Check("deno", "fail", deno_skipped())
    assert ["/usr/bin/deno", *DENO_FLAGS] not in runner.calls


def test_unwritable_temp_dir_fails_deno_without_crashing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "missing"))

    result, _ = checks()

    assert result["deno"] == Check(
        "deno", "fail", f"cannot write the solver script to a temp dir; {LINUX_HINT}"
    )
    assert result["node"].status == "ok"  # later checks still ran


def test_unparseable_deno_banner_fails() -> None:
    result, runner = checks(with_response(("deno", "--version"), ok("Deno, probably\n")))

    assert result["deno"] == Check(
        "deno", "fail", f"unknown /usr/bin/deno (yt-dlp needs >= 2.3.0); {LINUX_HINT}"
    )
    assert ["/usr/bin/deno", *DENO_FLAGS] not in runner.calls


ESCAPE = "\x1b]0;pwned\x07\x1b[31m"


@pytest.mark.parametrize(
    ("key", "banner", "name"),
    [
        (("ffmpeg", "-version"), f"ffmpeg version 8.1.2{ESCAPE} Copyright", "ffmpeg"),
        (("ffprobe", "-version"), f"ffprobe version {'9' * 65} Copyright", "ffprobe"),
        (("node", "--version"), f"v22.1.0{ESCAPE}\n", "node"),
    ],
)
def test_version_tokens_with_control_chars_or_huge_print_unknown(
    key: tuple[str, str], banner: str, name: str
) -> None:
    result, _ = checks(with_response(key, ok(banner)))

    line = format_check(result[name])
    assert result[name].detail.startswith("unknown ")
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in line)


def test_deno_banner_with_control_chars_prints_no_control_chars() -> None:
    result, _ = checks(with_response(("deno", "--version"), ok(f"deno 2.9.8{ESCAPE}\n")))

    line = format_check(result["deno"])
    assert "unknown" in line
    assert not any(ord(ch) < 32 or ord(ch) == 127 for ch in line)


@pytest.mark.parametrize("script", ["lib", "core"])
def test_deno_never_runs_scripts_that_failed_ejs_verification(
    monkeypatch: pytest.MonkeyPatch, script: str
) -> None:
    import yt_dlp_ejs.yt.solver as solver

    monkeypatch.setattr(solver, script, lambda: "/* tampered */")

    result, runner = checks(platform="darwin")

    assert result["yt-dlp-ejs"].status == "fail"
    assert result["deno"] == Check("deno", "fail", deno_skipped("run uv sync"))
    assert ["/usr/bin/deno", "--version"] in runner.calls
    assert "deno" not in runner.stdin
    assert ["/usr/bin/deno", *DENO_FLAGS] not in runner.calls


def test_format_check_replaces_unprintable_characters() -> None:
    check = Check("deno", "ok", "2.9.8 /opt/\x1b]0;pwned\x07bin\u202e/deno\n(solver ok)")

    assert format_check(check) == "ok   deno            2.9.8 /opt/?]0;pwned?bin?/deno?(solver ok)"

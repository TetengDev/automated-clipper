import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from clipper import render
from clipper.edl import parse_edl
from clipper.render import InvalidClipError, RenderError, render_clip

LOUDNORM_STDERR = """\
[Parsed_loudnorm_0 @ 0x1] 
{
\t"input_i" : "-23.10",
\t"input_tp" : "-6.02",
\t"input_lra" : "1.20",
\t"input_thresh" : "-33.10",
\t"output_i" : "-14.02",
\t"output_tp" : "-1.50",
\t"output_lra" : "1.10",
\t"output_thresh" : "-24.02",
\t"normalization_type" : "dynamic",
\t"target_offset" : "0.02"
}
"""

EDL_DATA: dict[str, Any] = {
    "source": {"path": "media/source.mp4", "in_s": 1.0, "out_s": 6.0},
    "hook": {"text": "Watch this", "style": "question", "duration_s": 2.0},
    "words": [{"text": "hi", "start_s": 1.2, "end_s": 1.5}],
}


class FakeRunner:
    """Records calls; creates the files each step would produce."""

    def __init__(
        self,
        *,
        fail_step: str | None = None,
        loudnorm_stderr: str = LOUDNORM_STDERR,
        raise_exc: BaseException | None = None,
        fail_stderr: str = "boom: something broke\nlast line",
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.props: dict[str, Any] | None = None
        self.fail_step = fail_step
        self.loudnorm_stderr = loudnorm_stderr
        self.raise_exc = raise_exc
        self.fail_stderr = fail_stderr
        self.public_listing: list[str] = []

    @staticmethod
    def step(argv: list[str]) -> str:
        if any(a.endswith("remotion-cli.js") for a in argv):
            return "remotion"
        if "-t" in argv:
            return "precut"
        if "null" in argv:
            return "measure"
        if "image2" in argv:
            return "thumb"
        return "finalize"

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        assert set(kwargs) == {"cwd", "env", "timeout"}
        self.calls.append({"argv": argv, **kwargs})
        step = self.step(argv)
        if self.raise_exc is not None and step == self.fail_step:
            raise self.raise_exc
        if step == self.fail_step:
            return subprocess.CompletedProcess(argv, 1, "", self.fail_stderr)
        stderr = ""
        if step == "remotion":
            props_arg = next(a for a in argv if a.startswith("--props="))
            self.props = json.loads(Path(props_arg.removeprefix("--props=")).read_text())
            public = Path(next(a for a in argv if a.startswith("--public-dir=")).split("=", 1)[1])
            self.public_listing = sorted(p.name for p in public.iterdir())

            Path(argv[5]).write_bytes(b"raw")
        elif step == "measure":
            stderr = self.loudnorm_stderr
        else:
            Path(argv[-1]).write_bytes(b"out")
        return subprocess.CompletedProcess(argv, 0, "", stderr)


@pytest.fixture
def media_root(tmp_path: Path) -> Path:
    root = tmp_path / "media_root"
    (root / "media").mkdir(parents=True)
    (root / "media" / "source.mp4").write_bytes(b"src")
    return root


@pytest.fixture
def render_dir(tmp_path: Path) -> Path:
    d = tmp_path / "render"
    (d / "src").mkdir(parents=True)
    (d / "src" / "index.ts").write_text("// entry")
    cli = d / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// cli")
    return d


def _render(media_root: Path, render_dir: Path, tmp_path: Path, runner: FakeRunner, **kw: Any):
    return render_clip(
        EDL_DATA, media_root, tmp_path / "out", runner=runner, render_dir=render_dir, **kw
    )


def test_happy_path(
    media_root: Path, render_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("SESSION_SECRET", "x" * 40)
    runner = FakeRunner()
    result = _render(media_root, render_dir, tmp_path, runner, timeout_s=123, concurrency=4)
    out = (tmp_path / "out").resolve()

    assert [FakeRunner.step(c["argv"]) for c in runner.calls] == [
        "precut",
        "remotion",
        "measure",
        "finalize",
        "thumb",
    ]
    work = out / ".clip.work"
    for call in runner.calls:
        assert call["timeout"] > 0
        env = call["env"]
        assert set(env) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "NO_COLOR"}
        assert env["TMPDIR"] == str(work / "tmp")
        assert "sk-secret" not in json.dumps(env)

    guard = ["-protocol_whitelist", "file", "-format_whitelist"]
    for call in runner.calls:
        argv = call["argv"]
        if argv[0] == "ffmpeg":
            i = argv.index("-i")
            assert argv[i - 4 : i - 1] == guard, argv
    precut = runner.calls[0]["argv"]
    assert precut[:4] == ["ffmpeg", "-hide_banner", "-nostats", "-y"]
    assert precut[precut.index("-ss") + 1] == "1.000"
    assert precut.index("-ss") < precut.index("-i")  # input seek (fast), re-encode (exact)
    assert precut[precut.index("-i") + 1] == str((media_root / "media" / "source.mp4").resolve())
    assert precut[precut.index("-t") + 1] == "5.000"
    assert precut[precut.index("-vf") + 1] == "scale='min(1080,iw)':-2,fps=30"
    assert precut[precut.index("-crf") + 1] == "18"
    assert precut[-1] == str(work / "public" / "segment.mp4")

    remotion = runner.calls[1]
    assert remotion["cwd"] == render_dir.resolve()
    assert remotion["timeout"] == 123
    assert remotion["argv"] == [
        "node",
        str(render_dir.resolve() / "node_modules/@remotion/cli/remotion-cli.js"),
        "render",
        "src/index.ts",
        "Clip",
        str(work / "raw.mp4"),
        f"--props={work / 'props.json'}",
        f"--public-dir={work / 'public'}",
        "--codec=h264",
        "--audio-codec=aac",
        "--enforce-audio-track",
        "--bundle-cache=false",
        "--timeout=120000",
        "--log=info",
        "--concurrency=4",
    ]
    assert runner.props == render.segment_props(parse_edl(EDL_DATA))
    assert runner.public_listing == ["segment.mp4"]  # props/raw/tmp stay out of public
    assert runner.props["source"] == {"path": "segment.mp4", "in_s": 0.0, "out_s": 5.0}

    measure = runner.calls[2]["argv"]
    assert measure[:2] == ["ffmpeg", "-hide_banner"]
    assert "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json" in measure

    finalize = runner.calls[3]["argv"]
    af = finalize[finalize.index("-af") + 1]
    assert af.startswith("loudnorm=I=-14:TP=-1.5:LRA=11:")
    for part in (
        "measured_I=-23.10",
        "measured_TP=-6.02",
        "measured_LRA=1.20",
        "measured_thresh=-33.10",
        "offset=0.02",
        "linear=true",
    ):
        assert part in af
    assert finalize[finalize.index("-movflags") + 1] == "+faststart"
    assert finalize[finalize.index("-c:v") + 1] == "copy"
    assert finalize[finalize.index("-c:a") + 1] == "aac"
    assert finalize[finalize.index("-ar") + 1] == "48000"

    thumb = runner.calls[4]["argv"]
    assert thumb[thumb.index("-ss") + 1] == "1.000"  # middle of a 2 s hook
    assert thumb[thumb.index("-i") + 1] == str(work / "final.mp4")  # staged, not published
    assert thumb[-1] == str(work / "thumb.jpg")

    assert result.video_path == out / "clip.mp4"
    assert result.thumbnail_path == out / "clip.jpg"
    assert result.sidecar_path == out / "clip.json"
    assert result.duration_s == pytest.approx(5.0)
    assert result.video_path.read_bytes() == b"out"
    # temp files cleaned up
    assert sorted(p.name for p in out.iterdir()) == ["clip.jpg", "clip.json", "clip.mp4"]

    sidecar = json.loads(
        result.sidecar_path.read_text(), parse_constant=lambda c: pytest.fail(f"non-JSON {c}")
    )
    assert sidecar["edl"] == parse_edl(EDL_DATA).to_props()
    assert sidecar["files"] == {"video": "clip.mp4", "thumbnail": "clip.jpg"}
    assert (sidecar["width"], sidecar["height"], sidecar["fps"]) == (1080, 1920, 30)
    assert sidecar["duration_in_frames"] == 150
    assert sidecar["loudness"]["measured"]["input_i"] == -23.1
    assert sidecar["loudness"]["normalized"] is True


def test_remotion_failure_raises_with_stderr_tail(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    runner = FakeRunner(fail_step="remotion")
    with pytest.raises(RenderError) as excinfo:
        _render(media_root, render_dir, tmp_path, runner)
    assert "remotion render failed with exit code 1" in str(excinfo.value)
    assert excinfo.value.stderr_tail.endswith("last line")
    assert len(runner.calls) == 2
    assert not (tmp_path / "out" / ".clip.work").exists()
    assert not (tmp_path / "out" / "clip.mp4").exists()
    assert not (tmp_path / "out" / "clip.json").exists()
    assert not isinstance(excinfo.value, InvalidClipError)  # may be transient: retry


def test_remotion_rejecting_props_is_invalid_clip(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    stderr = (
        "Error rendering composition\n"
        "EdlValidationError  EdlValidationError: invalid clip props: /hook/text: must match"
        " pattern; /words: bad\n"
        "    at parseEdl (validate.ts:64)"
    )
    runner = FakeRunner(fail_step="remotion", fail_stderr=stderr)
    with pytest.raises(InvalidClipError) as excinfo:
        _render(media_root, render_dir, tmp_path, runner)
    assert str(excinfo.value).splitlines()[0] == (
        "Remotion rejected the clip props: /hook/text: must match pattern; /words: bad"
    )


def test_props_text_without_the_error_class_is_not_a_rejection(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    runner = FakeRunner(fail_step="remotion", fail_stderr="echo: invalid clip props: x")
    with pytest.raises(RenderError) as excinfo:
        _render(media_root, render_dir, tmp_path, runner)
    assert not isinstance(excinfo.value, InvalidClipError)


def test_timeout(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    exc = subprocess.TimeoutExpired(["node"], 5, stderr=b"partial progress")
    runner = FakeRunner(fail_step="remotion", raise_exc=exc)
    with pytest.raises(RenderError, match="timed out after 5s") as excinfo:
        _render(media_root, render_dir, tmp_path, runner, timeout_s=5)
    assert excinfo.value.stderr_tail == "partial progress"


def test_missing_binary(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    runner = FakeRunner(fail_step="remotion", raise_exc=FileNotFoundError("node"))
    with pytest.raises(RenderError, match="could not start"):
        _render(media_root, render_dir, tmp_path, runner)


def test_ffmpeg_failure_cleans_temp_files(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    runner = FakeRunner(fail_step="finalize")
    with pytest.raises(RenderError, match="loudnorm \\+ faststart failed"):
        _render(media_root, render_dir, tmp_path, runner)
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == []


def test_silent_audio_skips_loudnorm(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    silent = LOUDNORM_STDERR.replace('"-23.10"', '"-inf"')
    runner = FakeRunner(loudnorm_stderr=silent)
    result = _render(media_root, render_dir, tmp_path, runner)
    finalize = runner.calls[3]["argv"]
    assert "-af" not in finalize
    assert "+faststart" in finalize
    text = result.sidecar_path.read_text()
    sidecar = json.loads(text, parse_constant=lambda c: pytest.fail(f"non-JSON {c}"))
    assert sidecar["loudness"]["normalized"] is False
    assert sidecar["loudness"]["measured"]["input_i"] is None


def test_unparseable_loudnorm(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    runner = FakeRunner(loudnorm_stderr="no json here")
    with pytest.raises(RenderError, match="could not parse loudnorm"):
        _render(media_root, render_dir, tmp_path, runner)


def test_invalid_edl_rejected_before_any_subprocess(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    runner = FakeRunner()
    bad = {**EDL_DATA, "layout": "split_screen"}
    with pytest.raises(InvalidClipError, match="invalid EDL"):
        render_clip(bad, media_root, tmp_path / "out", runner=runner, render_dir=render_dir)
    assert runner.calls == []


def test_missing_source(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    (media_root / "media" / "source.mp4").unlink()
    runner = FakeRunner()
    with pytest.raises(RenderError, match="source media not found") as excinfo:
        _render(media_root, render_dir, tmp_path, runner)
    assert runner.calls == []
    assert not isinstance(excinfo.value, InvalidClipError)  # volume may be back on retry


def test_symlink_escape_rejected(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    outside = tmp_path / "secret.mp4"
    outside.write_bytes(b"x")
    (media_root / "media" / "source.mp4").unlink()
    (media_root / "media" / "source.mp4").symlink_to(outside)
    runner = FakeRunner()
    with pytest.raises(InvalidClipError, match="escapes media root"):
        _render(media_root, render_dir, tmp_path, runner)


def test_missing_entry_point(media_root: Path, tmp_path: Path) -> None:
    runner = FakeRunner()
    with pytest.raises(RenderError, match="entry point not found"):
        render_clip(
            EDL_DATA, media_root, tmp_path / "out", runner=runner, render_dir=tmp_path / "nope"
        )


@pytest.mark.parametrize("basename", ["", "../x", "-rf", "a/b"])
def test_bad_basename(media_root: Path, render_dir: Path, tmp_path: Path, basename: str) -> None:
    with pytest.raises(ValueError, match="invalid basename"):
        _render(media_root, render_dir, tmp_path, FakeRunner(), basename=basename)


def test_accepts_model_and_custom_basename(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    runner = FakeRunner()
    result = render_clip(
        parse_edl({**EDL_DATA, "hook": None}),
        media_root,
        tmp_path / "out",
        runner=runner,
        render_dir=render_dir,
        basename="kai_v2",
    )
    assert result.video_path.name == "kai_v2.mp4"
    thumb = runner.calls[4]["argv"]
    assert thumb[thumb.index("-ss") + 1] == "1.000"  # no hook: ~1 s in


def test_default_render_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLIPPER_RENDER_DIR", str(tmp_path))
    assert render.default_render_dir() == tmp_path
    monkeypatch.delenv("CLIPPER_RENDER_DIR")
    assert render.default_render_dir() == Path(render.__file__).resolve().parents[2] / "render"


def test_parse_loudnorm_bad_values() -> None:
    with pytest.raises(RenderError, match="unexpected loudnorm output"):
        render.parse_loudnorm('{"input_i": "x", "input_tp": "1"}')


def test_thumbnail_time_short_clip() -> None:
    edl = parse_edl({"source": {"path": "a.mp4", "in_s": 0, "out_s": 1.2}})
    assert render.thumbnail_time(edl) == pytest.approx(0.6)


def test_precut_failure(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    runner = FakeRunner(fail_step="precut")
    with pytest.raises(RenderError, match="pre-cut failed"):
        _render(media_root, render_dir, tmp_path, runner)
    assert len(runner.calls) == 1


def test_segment_props_shift_and_clip_words() -> None:
    edl = parse_edl(
        {
            "source": {"path": "a.mp4", "in_s": 10.0, "out_s": 15.0},
            "hook": {"text": "x", "duration_s": 3},
            "words": [
                {"text": "before", "start_s": 8.0, "end_s": 9.5},
                {"text": "edge", "start_s": 9.8, "end_s": 10.4},
                {"text": "in", "start_s": 12.0, "end_s": 12.5},
                {"text": "tail", "start_s": 14.9, "end_s": 15.6},
                {"text": "after", "start_s": 15.0, "end_s": 16.0},
            ],
            "progress_bar": False,
        }
    )
    props = render.segment_props(edl)
    assert props["source"] == {"path": "segment.mp4", "in_s": 0.0, "out_s": 5.0}
    assert [(w["text"], round(w["start_s"], 3), round(w["end_s"], 3)) for w in props["words"]] == [
        ("edge", 0.0, 0.4),
        ("in", 2.0, 2.5),
        ("tail", 4.9, 5.6),
    ]
    assert props["hook"] == edl.to_props()["hook"]
    assert props["progress_bar"] is False


def test_segment_props_shift_segments_and_drop_cut_records() -> None:
    """EDL v2: one pre-cut of the whole span (no pre-cut per segment: lip-sync drift),
    segments shifted with it; the cut records stay in the sidecar, not in the props."""
    edl = parse_edl(
        {
            "source": {"path": "a.mp4", "in_s": 10.0, "out_s": 16.0},
            "segments": [{"in_s": 10.0, "out_s": 12.0}, {"in_s": 13.2, "out_s": 16.0}],
            "cuts": [
                {"in_s": 12.0, "out_s": 13.2, "kind": "silence", "confidence": 1.0,
                 "reason": "pause", "applied": True},
                {"in_s": 9.0, "out_s": 11.0, "kind": "retake", "confidence": 0.4,
                 "reason": "phrase", "applied": False},
            ],
            "words": [{"text": "in", "start_s": 13.5, "end_s": 13.9}],
        }
    )  # fmt: skip
    props = render.segment_props(edl)
    assert props["source"] == {"path": "segment.mp4", "in_s": 0.0, "out_s": 6.0}
    assert props["segments"] == [
        {"in_s": 0.0, "out_s": pytest.approx(2.0)},
        {"in_s": pytest.approx(3.2), "out_s": pytest.approx(6.0)},
    ]
    assert props["cuts"] == []
    assert [(w["text"], round(w["start_s"], 3)) for w in props["words"]] == [("in", 3.5)]


def test_precut_covers_the_whole_span_of_segments(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    data = {
        **EDL_DATA,
        "source": {"path": "media/source.mp4", "in_s": 1.0, "out_s": 6.0},
        "segments": [{"in_s": 1.0, "out_s": 3.0}, {"in_s": 4.0, "out_s": 6.0}],
        "hook": None,
    }
    runner = FakeRunner()
    result = render_clip(data, media_root, tmp_path / "out", runner=runner, render_dir=render_dir)
    precut = runner.calls[0]["argv"]
    assert precut[precut.index("-t") + 1] == "5.000"  # the span, not the 4 s output
    assert result.duration_s == pytest.approx(4.0)
    sidecar = json.loads(result.sidecar_path.read_text())
    assert sidecar["duration_in_frames"] == 120
    assert sidecar["edl"]["segments"] == data["segments"]


def test_thumbnail_failure_publishes_nothing(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "clip.mp4").write_bytes(b"previous")  # an earlier good render stays untouched
    runner = FakeRunner(fail_step="thumb")
    with pytest.raises(RenderError, match="thumbnail failed"):
        _render(media_root, render_dir, tmp_path, runner)
    assert sorted(p.name for p in out.iterdir()) == ["clip.mp4"]
    assert (out / "clip.mp4").read_bytes() == b"previous"


def test_work_dir_symlink_is_replaced_not_followed(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep.txt").write_text("keep")
    (out / ".clip.work").symlink_to(victim)
    _render(media_root, render_dir, tmp_path, FakeRunner())
    assert (victim / "keep.txt").read_text() == "keep"
    assert not (out / ".clip.work").exists()


def test_work_dir_is_private(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    modes: list[int] = []

    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        work = Path(kw["env"]["TMPDIR"]).parent
        modes.append(work.stat().st_mode & 0o777)
        raise FileNotFoundError("stop")

    with pytest.raises(RenderError):
        _render(media_root, render_dir, tmp_path, runner)  # type: ignore[arg-type]
    assert modes == [0o700]


def test_unwritable_out_dir(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(RenderError, match="cannot create output dir"):
        render_clip(
            EDL_DATA, media_root, blocker / "out", runner=FakeRunner(), render_dir=render_dir
        )


def test_missing_remotion_cli(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    (render_dir / "node_modules/@remotion/cli/remotion-cli.js").unlink()
    with pytest.raises(RenderError, match="npm ci"):
        _render(media_root, render_dir, tmp_path, FakeRunner())


def test_versions_get_their_own_files(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    v1 = _render(media_root, render_dir, tmp_path, FakeRunner(), basename="v1")
    v2 = _render(media_root, render_dir, tmp_path, FakeRunner(), basename="v2")
    assert {p.name for p in (tmp_path / "out").iterdir()} == {
        "v1.mp4", "v1.jpg", "v1.json", "v2.mp4", "v2.jpg", "v2.json",
    }  # fmt: skip
    assert v1.video_path != v2.video_path


def test_child_env_is_minimal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("HOME", "/home/x")
    env = render.child_env(tmp_path)
    assert env["TMPDIR"] == str(tmp_path)
    assert env["HOME"] == "/home/x"
    assert env["LC_ALL"] == "C.UTF-8"
    assert "ANTHROPIC_API_KEY" not in env
    monkeypatch.delenv("PATH")
    assert render.child_env(tmp_path)["PATH"] == os.defpath


class FakePopen:
    """Stand-in for subprocess.Popen: first communicate() may time out."""

    instances: list["FakePopen"] = []

    def __init__(self, argv: list[str], *, hang: bool = False, rc: int = 0, **kwargs: Any):
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 4242
        self.hang = hang
        self.returncode = rc
        self.communicate_calls: list[float | None] = []
        FakePopen.instances.append(self)

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communicate_calls.append(timeout)
        if self.hang and len(self.communicate_calls) == 1:
            raise subprocess.TimeoutExpired(self.argv, timeout or 0)
        return "out", "err tail"


def test_run_process_success(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(render.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    FakePopen.instances.clear()
    proc = render.run_process(
        ["x", "y"], cwd=Path("/w"), env={"PATH": "/bin"}, timeout=3, popen=FakePopen
    )
    (p,) = FakePopen.instances
    assert p.kwargs["start_new_session"] is True
    assert p.kwargs["env"] == {"PATH": "/bin"}
    assert p.kwargs["cwd"] == Path("/w")
    assert "shell" not in p.kwargs
    assert (proc.returncode, proc.stdout, proc.stderr) == (0, "out", "err tail")
    # Leftover group members (e.g. a daemonized Chrome helper) are swept after exit:
    # probe with signal 0 first, then SIGKILL.
    assert killed == [(4242, 0), (4242, signal.SIGKILL)]


def test_run_process_kills_group_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(render.os, "killpg", lambda pid, sig: killed.append((pid, sig)))

    def popen(argv: list[str], **kw: Any) -> FakePopen:
        return FakePopen(argv, hang=True, **kw)

    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        render.run_process(["node", "cli.js"], timeout=7, popen=popen)
    assert killed == [(4242, signal.SIGKILL)]
    assert excinfo.value.stderr == "err tail"
    assert excinfo.value.timeout == 7


def test_run_process_kills_group_on_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    killed: list[int] = []
    monkeypatch.setattr(render.os, "killpg", lambda pid, sig: killed.append(pid))

    class Interrupted(FakePopen):
        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.communicate_calls.append(timeout)
            if len(self.communicate_calls) == 1:
                raise KeyboardInterrupt
            return "", ""

    with pytest.raises(KeyboardInterrupt):
        render.run_process(["x"], timeout=1, popen=Interrupted)
    assert killed == [4242]


def test_run_process_real_timeout_kills_children(tmp_path: Path) -> None:
    """A grandchild in the same group dies with its parent (no orphans)."""
    pidfile = tmp_path / "pid"
    script = f"sleep 30 & echo $! > {pidfile}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        render.run_process(["/bin/sh", "-c", script], timeout=1)
    child = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("grandchild still alive after timeout")


def test_sigterm_handler_scoped_to_render(
    media_root: Path, render_dir: Path, tmp_path: Path
) -> None:
    sentinel = signal.getsignal(signal.SIGTERM)
    seen: list[Any] = []

    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        handler = signal.getsignal(signal.SIGTERM)
        seen.append(handler)
        assert callable(handler)
        with pytest.raises(SystemExit):
            handler(signal.SIGTERM, None)
        raise FileNotFoundError("stop")

    with pytest.raises(RenderError):
        _render(media_root, render_dir, tmp_path, runner)  # type: ignore[arg-type]
    assert seen and seen[0] is not sentinel
    assert signal.getsignal(signal.SIGTERM) is sentinel


def test_sigterm_mid_render_cleans_up(media_root: Path, render_dir: Path, tmp_path: Path) -> None:
    def runner(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # type: ignore[operator]
        raise AssertionError("unreachable")

    with pytest.raises(SystemExit):
        _render(media_root, render_dir, tmp_path, runner)  # type: ignore[arg-type]
    assert list((tmp_path / "out").iterdir()) == []


def test_parse_loudnorm_invalid_json() -> None:
    with pytest.raises(RenderError, match="could not parse loudnorm"):
        render.parse_loudnorm('{ "input_i" : nope }')


def test_props_write_value_error_becomes_render_error(
    media_root: Path, render_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(render, "segment_props", lambda edl: {"x": float("nan")})
    with pytest.raises(RenderError, match="render I/O error"):
        _render(media_root, render_dir, tmp_path, FakeRunner())
    assert list((tmp_path / "out").iterdir()) == []


def test_partial_publish_is_rolled_back(
    media_root: Path, render_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_replace = os.replace
    calls: list[str] = []

    def flaky_replace(src: Any, dst: Any) -> None:
        calls.append(Path(dst).name)
        if Path(dst).suffix == ".mp4":
            raise OSError("disk full")
        real_replace(src, dst)

    monkeypatch.setattr(render.os, "replace", flaky_replace)
    with pytest.raises(RenderError, match="disk full"):
        _render(media_root, render_dir, tmp_path, FakeRunner())
    assert calls == ["clip.jpg", "clip.mp4"]
    assert list((tmp_path / "out").iterdir()) == []  # the published thumbnail was removed


def test_run_process_skips_sweep_when_group_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[int] = []

    def killpg(pid: int, sig: int) -> None:
        sent.append(sig)
        raise ProcessLookupError

    monkeypatch.setattr(render.os, "killpg", killpg)
    render.run_process(["x"], timeout=1, popen=FakePopen)
    assert sent == [0]  # probe only; no SIGKILL to a group that no longer exists


def test_sigterm_restores_default_when_previous_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    def fake_signal(signum: int, handler: Any) -> Any:
        calls.append(handler)
        return None  # handler was installed outside Python

    monkeypatch.setattr(render.signal, "signal", fake_signal)
    with render._sigterm_as_exit():
        pass
    assert calls == [render._raise_system_exit, signal.SIG_DFL]


def test_publish_rollback_covers_partial_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replace that fails after creating the target still gets rolled back."""
    staged = [tmp_path / f"s{i}" for i in range(3)]
    finals = [tmp_path / f"f{i}" for i in range(3)]
    for f in staged:
        f.write_text("x")
    real_replace = os.replace

    def replace(src: Any, dst: Any) -> None:
        real_replace(src, dst)
        if Path(dst) == finals[1]:
            raise OSError("fsync failed after rename")

    monkeypatch.setattr(render.os, "replace", replace)
    with pytest.raises(OSError, match="fsync"):
        render._publish(list(zip(staged, finals, strict=True)))
    assert not finals[0].exists()
    assert not finals[1].exists()
    assert not finals[2].exists()

import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import pytest
from yt_dlp import YoutubeDL

from clipper.jobs.stages import PermanentError
from clipper.pipeline import ytdlp
from clipper.pipeline.ytdlp import (
    CommandTimeout,
    DownloadBlocked,
    RunResult,
    Unavailable,
    YtDlp,
    YtDlpError,
    classify_error,
    run_command,
)

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class Recorder:
    def __init__(self, result: RunResult | Exception, on_call=None) -> None:
        self.result = result
        self.on_call = on_call
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, args: Sequence[str], timeout_s: float) -> RunResult:
        self.calls.append((list(args), timeout_s))
        if self.on_call:
            self.on_call(list(args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def ok(stdout: str = "") -> RunResult:
    return RunResult(0, stdout, "")


def fail(stderr: str) -> RunResult:
    return RunResult(1, "", stderr)


def test_probe_uses_module_invocation_and_double_dash_before_url() -> None:
    runner = Recorder(ok(json.dumps({"id": "x", "title": "t"})))
    info = YtDlp(runner=runner).probe(URL)
    assert info["title"] == "t"
    args, timeout = runner.calls[0]
    assert args[:3] == [sys.executable, "-m", "yt_dlp"]
    assert args[-2:] == ["--", URL]
    assert "--skip-download" in args and "--dump-single-json" in args
    assert "--ignore-config" in args and "--no-playlist" in args
    assert timeout > 0


def test_download_requests_audio_only_and_returns_file(tmp_path: Path) -> None:
    def fake_download(args: list[str]) -> None:
        template = args[args.index("-o") + 1]
        Path(template.replace("%(ext)s", "webm")).write_bytes(b"audio")
        (tmp_path / ".src.webm.part").write_bytes(b"")  # partial leftovers are ignored

    runner = Recorder(ok(), on_call=fake_download)
    info = tmp_path / "info.json"
    path = YtDlp(runner=runner).download_audio(URL, tmp_path, ".src", info_json=info)
    assert path == tmp_path / ".src.webm"
    args, _ = runner.calls[0]
    # Audio-only when there is one, else a small muxed format with audio (ffmpeg drops the
    # video): --max-filesize only bounds each HLS fragment, not a whole muxed download.
    selector = "bestaudio/best[height<=480][acodec!=none]/worst[acodec!=none]"
    assert args[args.index("-f") + 1] == selector == ytdlp.AUDIO_FORMAT
    assert args[args.index("--load-info-json") + 1] == str(info)
    assert args[-2:] == ["--", URL]
    assert args.index("--") > args.index("-o")


def test_download_escapes_percent_in_directory(tmp_path: Path) -> None:
    odd = tmp_path / "100%dir"
    odd.mkdir()
    runner = Recorder(ok())
    with pytest.raises(YtDlpError, match="found 0"):
        YtDlp(runner=runner).download_audio(URL, odd, "src")
    template = runner.calls[0][0][runner.calls[0][0].index("-o") + 1]
    assert "100%%dir" in template and template.endswith("src.%(ext)s")


@pytest.mark.parametrize("stem", ["../x", "-rf", "a/b", ""])
def test_download_rejects_unsafe_stem(tmp_path: Path, stem: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        YtDlp(runner=Recorder(ok())).download_audio(URL, tmp_path, stem)


def test_cookies_and_proxy_are_passed_and_proxy_is_redacted(tmp_path: Path) -> None:
    proxy = "http://user:hunter2@proxy.example:8080"
    cookies = tmp_path / "cookies.txt"
    runner = Recorder(fail(f"ERROR: Unable to connect to proxy {proxy}"))
    with pytest.raises(YtDlpError) as info:
        YtDlp(runner=runner, cookies=cookies, proxy=proxy).probe(URL)
    args = runner.calls[0][0]
    assert args[args.index("--cookies") + 1] == str(cookies)
    assert args[args.index("--proxy") + 1] == proxy
    assert args[-2:] == ["--", URL]
    assert "hunter2" not in str(info.value)
    assert "REDACTED" in str(info.value)


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", Unavailable),
        ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader",
         Unavailable),
        ("ERROR: [youtube] x: Join this channel to get access to members-only content",
         Unavailable),
        ("ERROR: [youtube] x: Sign in to confirm your age. This video may be inappropriate",
         Unavailable),
        ("ERROR: [youtube] x: Requested format is not available", Unavailable),
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot. Use --cookies", DownloadBlocked),
        ("ERROR: unable to download webpage: HTTP Error 429: Too Many Requests", DownloadBlocked),
        ("ERROR: [youtube] x: Video unavailable. This content isn't available, try again later.",
         DownloadBlocked),
        ("ERROR: unable to download: Connection reset by peer", YtDlpError),
    ],
)  # fmt: skip
def test_error_mapping(stderr: str, expected: type[Exception]) -> None:
    error = classify_error(stderr)
    assert type(error) is expected


def test_permanent_vs_retryable_types() -> None:
    assert issubclass(Unavailable, PermanentError)
    assert issubclass(DownloadBlocked, YtDlpError)
    assert not issubclass(DownloadBlocked, PermanentError)


def test_error_message_is_redacted_tail() -> None:
    noise = "\n".join(f"[debug] line {i}" for i in range(50))
    stderr = f"{noise}\nERROR: fetch https://x.example/v?sig=SECRETSIG&a=1 failed"
    message = str(classify_error(stderr))
    assert "SECRETSIG" not in message
    assert "line 3" not in message
    assert len(message) < 700


def test_error_without_output() -> None:
    assert "no error output" in str(classify_error(""))


def test_nonzero_exit_raises_mapped_error() -> None:
    runner = Recorder(fail("ERROR: Sign in to confirm you're not a bot"))
    with pytest.raises(DownloadBlocked):
        YtDlp(runner=runner).probe(URL)


def test_timeout_is_retryable() -> None:
    runner = Recorder(CommandTimeout("python timed out after 1s"))
    with pytest.raises(YtDlpError, match="timed out"):
        YtDlp(runner=runner).probe(URL)


@pytest.mark.parametrize("stdout", ["not json", "[1, 2]"])
def test_bad_probe_output(stdout: str) -> None:
    with pytest.raises(YtDlpError):
        YtDlp(runner=Recorder(ok(stdout))).probe(URL)


def test_run_command_captures_output() -> None:
    code = "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"
    result = run_command([sys.executable, "-c", code], 30)
    assert result == RunResult(3, "out\n", "err\n")


def test_run_command_timeout() -> None:
    with pytest.raises(CommandTimeout, match="timed out"):
        run_command([sys.executable, "-c", "import time; time.sleep(5)"], 0.2)


# --- review hardening ------------------------------------------------------------------------


def test_run_command_passes_only_allowlisted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_should_not_leak_123")
    monkeypatch.setenv("TZ", "UTC")
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    env = json.loads(run_command([sys.executable, "-c", code], 30).stdout)
    assert "ANTHROPIC_API_KEY" not in env and "GROQ_API_KEY" not in env
    assert env.get("TZ") == "UTC"
    assert set(env) - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"} <= set(ytdlp.CHILD_ENV_ALLOWLIST)


def test_run_command_adds_extra_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    code = "import json, os; print(json.dumps(dict(os.environ)))"
    out = run_command([sys.executable, "-c", code], 30, env={"MALLOC_ARENA_MAX": "2"}).stdout
    env = json.loads(out)
    assert env["MALLOC_ARENA_MAX"] == "2" and "ANTHROPIC_API_KEY" not in env


def test_run_command_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-alive"
    grandchild = f"import time; time.sleep(1.5); open({str(marker)!r}, 'w').write('x')"
    code = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(30)"
    )
    with pytest.raises(CommandTimeout):
        run_command([sys.executable, "-c", code], 0.5)
    time.sleep(2.0)
    assert not marker.exists(), "grandchild survived the timeout"


def test_run_command_bounds_stderr_and_streams_lines() -> None:
    code = "import sys\nfor i in range(20000): print(f'line {i:05d} ' + 'x' * 20, file=sys.stderr)"
    lines: list[str] = []
    result = run_command([sys.executable, "-c", code], 30, on_stderr_line=lines.append)
    assert len(lines) == 20000 and lines[-1].startswith("line 19999")
    assert len(result.stderr.encode()) <= ytdlp.STDERR_LIMIT_BYTES
    assert result.stderr.rstrip().endswith("line 19999 " + "x" * 20)


def test_proxy_password_is_masked_in_all_encodings() -> None:
    proxy = "http://user:p%40ss%2Fw0rd@proxy.test:8080"
    stderr = (
        "ERROR: Unable to connect via http://user:p%40ss%2Fw0rd@proxy.test:8080 "
        "(password p@ss/w0rd, encoded p%40ss%2Fw0rd)"
    )
    runner = Recorder(fail(stderr))
    with pytest.raises(YtDlpError) as exc:
        YtDlp(runner=runner, proxy=proxy).probe(URL)
    text = str(exc.value)
    assert "p@ss/w0rd" not in text and "p%40ss%2Fw0rd" not in text
    assert ytdlp.proxy_secrets(None) == []


@pytest.mark.parametrize(
    ("stderr", "permanent"),
    [
        ("ERROR: [youtube] x: This video does not exist.", True),
        ("ERROR: [twitch:vod] 123: Video 123 does not exist", True),
        ("ERROR: unable to open: the requested file does not exist on CDN", False),
    ],
)
def test_does_not_exist_is_permanent_only_for_the_video(stderr: str, permanent: bool) -> None:
    assert isinstance(classify_error(stderr), Unavailable) is permanent


def test_cap_timeout_shrinks_each_call() -> None:
    runner = Recorder(ok(json.dumps({"id": "x"})))
    YtDlp(runner=runner, cap_timeout=lambda t: min(t, 7.0)).probe(URL)
    assert runner.calls[0][1] == 7.0


# --- second review pass ----------------------------------------------------------------------


def test_run_command_returns_when_a_grandchild_holds_the_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ytdlp, "PUMP_JOIN_TIMEOUT_S", 0.5)
    # The child exits at once; its grandchild inherits stdout/stderr and sleeps for 30 s.
    code = (
        "import subprocess, sys;"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']);"
        "print('parent done')"
    )
    start = time.monotonic()
    result = run_command([sys.executable, "-c", code], 30)
    assert time.monotonic() - start < 5.0
    assert result.returncode == 0 and result.stdout == "parent done\n"


def test_run_command_survives_a_raising_line_callback(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[str] = []

    def explode(line: str) -> None:
        calls.append(line)
        raise ValueError("bad handler")

    code = "import sys\nfor i in range(5000): print('x' * 50, file=sys.stderr)"
    result = run_command([sys.executable, "-c", code], 30, on_stderr_line=explode)
    assert result.returncode == 0
    assert len(calls) == 1  # handler disabled after its first failure, pipe still drained
    assert result.stderr.endswith("x" * 50 + "\n")
    assert "stderr line handler failed" in caplog.text


def test_run_command_caps_line_length() -> None:
    lines: list[str] = []
    code = "import sys; sys.stderr.write('y' * 200000 + '\\n')"
    run_command([sys.executable, "-c", code], 30, on_stderr_line=lines.append)
    assert max(len(line) for line in lines) <= ytdlp.LINE_LIMIT_BYTES
    assert sum(len(line) for line in lines) == 200001


def test_tail_keeps_the_last_bytes_without_shifting() -> None:
    tail = ytdlp._Tail(10)
    for chunk in (b"abcdef", b"ghij", b"klmnop"):
        tail.add(chunk)
    assert tail.text() == "ghijklmnop"
    assert len(tail.chunks) == 2


# --- final review pass -----------------------------------------------------------------------


def test_tail_is_safe_while_a_pump_is_still_adding() -> None:
    import threading

    tail = ytdlp._Tail(1024)
    stop = threading.Event()

    def writer() -> None:
        while not stop.is_set():
            tail.add(b"z" * 7)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for _ in range(2000):
            assert len(tail.text()) <= 1024
    finally:
        stop.set()
        thread.join()
    tail.close()
    before = tail.text()
    assert tail.add(b"late") is False
    assert tail.text() == before


def test_abandoned_pump_stops_appending_and_calling_back() -> None:
    import os
    import threading

    read_fd, write_fd = os.pipe()
    tail = ytdlp._Tail(1024)
    calls: list[str] = []
    with os.fdopen(read_fd, "rb") as stream:
        pump = threading.Thread(target=ytdlp._pump, args=(stream, tail, calls.append))
        pump.start()
        os.write(write_fd, b"first\n")
        deadline = time.monotonic() + 5
        while not calls and time.monotonic() < deadline:
            time.sleep(0.01)
        tail.close()  # what _finish_pumps does when it gives up on a reader
        os.write(write_fd, b"second\nthird\n")
        os.close(write_fd)
        pump.join(5)
    assert not pump.is_alive()
    assert calls == ["first\n"]
    assert tail.text() == "first\n"


def test_group_probe_is_false_once_the_group_is_gone() -> None:
    import subprocess

    proc = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)  # noqa: S603
    proc.wait()
    assert ytdlp._group_exists(proc.pid) is False


def test_run_command_feeds_stdin_from_a_file_and_runs_in_cwd(tmp_path: Path) -> None:
    (tmp_path / "in.txt").write_text("hello from stdin")
    work = tmp_path / "work"
    work.mkdir()
    code = "import os, sys; print(sys.stdin.read()); print(os.getcwd())"
    out = run_command(
        [sys.executable, "-c", code], 30, stdin_path=tmp_path / "in.txt", cwd=work
    ).stdout.splitlines()
    assert out[0] == "hello from stdin"
    assert Path(out[1]).resolve() == work.resolve()


def test_run_command_cancel_kills_the_child_promptly(tmp_path: Path) -> None:
    import threading

    pid_file = tmp_path / "pid"
    code = f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()

    with pytest.raises(ytdlp.CommandCancelled):
        run_command([sys.executable, "-c", code], 120, cancel=cancel)

    assert time.monotonic() - started < 5
    assert ytdlp._group_exists(int(pid_file.read_text())) is False


def test_ytdlp_hands_its_cancel_event_to_the_runner() -> None:
    import threading

    seen: list[object] = []

    def runner(args, timeout_s, **kwargs):
        seen.append(kwargs.get("cancel"))
        return ok(json.dumps({"id": "x"}))

    cancel = threading.Event()
    YtDlp(runner=runner, cancel=cancel).probe(URL)
    YtDlp(runner=Recorder(ok(json.dumps({"id": "x"})))).probe(URL)  # no cancel: not passed

    assert seen == [cancel]


def test_from_env_passes_cancel() -> None:
    import threading

    cancel = threading.Event()
    assert YtDlp.from_env(run_command, {}, cancel=cancel).cancel is cancel


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: [youtube] x: Requested format is not available. Use --list-formats",
        "ERROR: [youtube] x: No video formats found!; please report this issue",
    ],
)
def test_format_errors_share_one_reason(stderr: str) -> None:
    # Both mean yt-dlp found nothing it could fetch: usually a player change, not the video.
    error = classify_error(stderr)

    assert isinstance(error, Unavailable) and error.reason == ytdlp.NO_FORMAT


def test_probe_selects_like_the_clip_download(tmp_path: Path) -> None:
    # The monitor and the worker probe with the clip download's selector, which implies the
    # audio download's (see test_probe_selector_implies_the_audio_selector): a video the
    # probe accepts is one both downloads can fetch, and one they cannot is a format error
    # up front, before any transcription or LLM spend.
    probe = Recorder(ok(json.dumps({"id": "x"})))
    YtDlp(runner=probe).probe(URL)
    download = Recorder(ok())
    with pytest.raises(YtDlpError, match="found 0"):
        YtDlp(runner=download).download_audio(URL, tmp_path, "src")

    probe_args, download_args = probe.calls[0][0], download.calls[0][0]
    assert probe_args[probe_args.index("-f") + 1] == ytdlp.CLIP_FORMAT
    assert download_args[download_args.index("-f") + 1] == ytdlp.AUDIO_FORMAT
    assert ytdlp.CLIP_FORMAT == "bv*[height<=1080]+ba/b[height<=1080][acodec!=none]"


def _hls(height: int, tbr: float) -> dict[str, object]:
    return {
        "format_id": f"hls-{height}", "url": f"https://media.invalid/{height}.m3u8",
        "protocol": "m3u8_native", "ext": "mp4", "width": height * 16 // 9,
        "height": height, "tbr": tbr, "vcodec": "avc1.4d401f", "acodec": "mp4a.40.2",
    }  # fmt: skip


def _video_only(height: int) -> dict[str, object]:
    return {
        "format_id": f"v-{height}", "url": f"https://media.invalid/v{height}", "ext": "mp4",
        "height": height, "tbr": height * 4.0, "vcodec": "avc1.640028", "acodec": "none",
    }  # fmt: skip


AUDIO_ONLY = {
    "format_id": "140", "url": "https://media.invalid/a", "ext": "m4a", "tbr": 129.0,
    "vcodec": "none", "acodec": "mp4a.40.2",
}  # fmt: skip
MUXED_HLS = [_hls(h, t) for h, t in [(1080, 5e3), (144, 100), (720, 3e3), (480, 1200), (360, 700)]]


def _select(formats: list[dict[str, object]], selector: str = ytdlp.AUDIO_FORMAT) -> str:
    """Real yt-dlp format selection over an offline info dict: nothing is fetched."""
    info = {
        "id": "x", "title": "t", "extractor": "generic", "extractor_key": "Generic",
        "webpage_url": "https://media.invalid/watch", "formats": formats,
    }  # fmt: skip
    params = {"format": selector, "simulate": True, "quiet": True, "no_warnings": True}
    with YoutubeDL(params) as ydl:
        return str(ydl.process_ie_result(info, download=False)["format_id"])


FIXTURES = {
    "dash and muxed": [*MUXED_HLS, AUDIO_ONLY, _video_only(1080)],
    "dash only": [AUDIO_ONLY, _video_only(1080), _video_only(720)],
    "muxed hls only": MUXED_HLS,
    "muxed above 480p and video-only": [_hls(1080, 5000), _hls(720, 3000), _video_only(360)],
    "muxed above 1080p only": [_hls(1440, 9000), _hls(2160, 16000)],
    "audio only": [AUDIO_ONLY],
    "video only": [_video_only(1080), _video_only(720)],
    "nothing": [],
}


@pytest.mark.parametrize("name", FIXTURES)
def test_probe_selector_implies_the_audio_selector(name: str) -> None:
    # A video the probe accepts must be one the audio download accepts too: either `ba`
    # exists, or a muxed format with audio does, which the audio fallbacks take.
    formats = FIXTURES[name]
    try:
        _select(formats, ytdlp.CLIP_FORMAT)
    except Exception as exc:  # noqa: BLE001 - only a format error may come out
        error = classify_error(f"ERROR: {exc}")
        assert isinstance(error, Unavailable) and error.reason == ytdlp.NO_FORMAT
        return
    assert _select(formats)  # raises if the audio selector found nothing


@pytest.mark.parametrize("name", ["audio only", "video only", "muxed above 1080p only"])
def test_probe_selector_rejects_what_the_clip_download_cannot_use(name: str) -> None:
    # Audio but no video (a partial breakage): the job would otherwise pay for ASR and the
    # LLM and only then fail in fetch_clips. Video but no audio: the clips would be silent.
    with pytest.raises(Exception, match="Requested format is not available") as info:
        _select(FIXTURES[name], ytdlp.CLIP_FORMAT)

    error = classify_error(f"ERROR: {info.value}")
    assert isinstance(error, Unavailable) and error.reason == ytdlp.NO_FORMAT


def test_probe_selector_merges_without_downloading() -> None:
    # Selection with `+` is resolved in the info dict; nothing is merged under simulate.
    assert _select(FIXTURES["dash only"], ytdlp.CLIP_FORMAT) == "v-1080+140"


@pytest.mark.parametrize(
    ("formats", "chosen"),
    [
        ([*MUXED_HLS, AUDIO_ONLY, _video_only(1080)], "140"),  # audio-only always wins
        (MUXED_HLS, "hls-480"),  # muxed-only HLS: 480p at most, never the 1080p
        ([_hls(1080, 5000), _hls(720, 3000), _video_only(360)], "hls-720"),  # smallest w/ audio
    ],
)
def test_audio_selector_caps_a_muxed_only_fallback(
    formats: list[dict[str, object]], chosen: str
) -> None:
    assert _select(formats) == chosen


def test_audio_selector_never_picks_a_format_without_audio() -> None:
    # Plain `best` falls back to a video-only format here, and ffmpeg -vn then fails.
    with pytest.raises(Exception, match="Requested format is not available") as info:
        _select([_video_only(1080), _video_only(720)])

    error = classify_error(f"ERROR: {info.value}")
    assert isinstance(error, Unavailable) and error.reason == ytdlp.NO_FORMAT

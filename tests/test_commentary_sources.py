"""Commentary `fetching_sources` and `transcribing` (CLP-145).

The yt-dlp runner is faked: probes answer with a Twitch clip's formats as the
`twitch:clips` extractor reports them (no codec fields, portrait formats with
`quality: -2`), downloads copy a small real mp4 into place, and every other command
(ffmpeg, ffprobe) runs for real. The transcriber is faked at the `Transcriber` boundary,
so the clip pipeline's own `transcribe` code (chunking, stitching, transcript.json) runs.
No network, no model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from clipper.config import AppConfig
from clipper.db import bundled_migrations, connect, migrate
from clipper.jobs.models import Job, JobStatus, to_iso
from clipper.jobs.queue import MAX_ATTEMPTS, JobConflict
from clipper.jobs.stages import PermanentError, SkipJob, StageContext
from clipper.pipeline import build_commentary_stages, commentary
from clipper.pipeline import commentary_sources as cs
from clipper.pipeline.common import Deadline, MediaError, StageTimeout
from clipper.pipeline.transcribe import ChunkTranscript, FasterWhisperTranscriber, Segment, Word
from clipper.pipeline.ytdlp import RunResult, YtDlpError, run_command
from clipper.urls import SourceRef

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)

T0 = datetime(2026, 11, 26, 11, 0, tzinfo=UTC)
NOW = to_iso(T0)
DAY = SourceRef("commentary", "random_gaming_yt:2026-11-26")
OTHER_DAY = SourceRef("commentary", "random_gaming_yt:2026-11-25")

LANDSCAPE = ("1080", "720", "480", "360")
PORTRAIT = ("portrait-360", "portrait-480", "portrait-720", "portrait-900")
GONE = "ERROR: [twitch:clips] Gone: This clip is no longer available"
FLAKY = "ERROR: [twitch:clips] Flaky: Unable to download JSON metadata: connection reset"
NO_QUALITIES = (
    "ERROR: [twitch:clips] Empty: list index out of range; please report this issue on "
    " https://github.com/yt-dlp/yt-dlp/issues?q= , filling out the appropriate issue"
    " template. Confirm you are on the latest version using  yt-dlp -U"
)


# --- fixtures ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sample_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """3 s of video with a quiet tone that gets loud from 2.0 s to 2.4 s."""
    out = tmp_path_factory.mktemp("clip") / "sample.mp4"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x114:rate=10:duration=3",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.05*sin(2*PI*440*t)*(1+15*between(t\\,2\\,2.4)):d=3:s=16000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    return out


@pytest.fixture(scope="session")
def sample_60s_mp4(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """60 s (a Twitch clip's longest) with a quiet tone that gets loud from 1.0 s to 1.4 s:
    its ebur128 log is longer than the 64 KB of stderr a runner keeps."""
    out = tmp_path_factory.mktemp("clip60") / "sample.mp4"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:size=32x56:rate=5:duration=60",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc=0.05*sin(2*PI*440*t)*(1+15*between(t\\,1\\,1.4)):d=60:s=16000",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(out),
        ],  # fmt: skip
        check=True,
        capture_output=True,
    )
    return out


def _formats(ids: Sequence[str]) -> list[dict[str, Any]]:
    """Formats as yt-dlp's twitch:clips extractor reports them: no codec fields."""
    out = []
    for format_id in ids:
        portrait = format_id.startswith("portrait")
        digits = re.match(r"(?:portrait-?)?(\d*)", format_id)[1]  # type: ignore[index]
        entry: dict[str, Any] = {
            "format_id": format_id,
            "url": f"https://production.assets.clips.twitchcdn.net/{format_id}.mp4?sig=s",
            "ext": "mp4",
            "protocol": "https",
            "aspect_ratio": 0.56 if portrait else 1.78,
        }
        if digits:
            entry["height"] = int(digits)
        if portrait:
            entry["quality"] = -2
        out.append(entry)
    return out


class FakeYtDlp:
    """The process runner: yt-dlp is answered from `clips` (slug -> spec), the rest runs."""

    def __init__(self, sample: Path) -> None:
        self.sample = sample
        self.clips: dict[str, dict[str, Any]] = {}
        self.calls: list[list[str]] = []

    def clip(
        self, slug: str, formats: Sequence[str] = (*LANDSCAPE, *PORTRAIT), **spec: Any
    ) -> None:
        self.clips[slug] = {"formats": list(formats), **spec}

    def downloads(self) -> list[tuple[str, str]]:
        """(slug, format id) of every download call."""
        return [
            (args[-1].rsplit("/", 1)[-1], args[args.index("-f") + 1])
            for args in self.calls
            if "-o" in args
        ]

    def __call__(self, args: Sequence[str], timeout_s: float, **kwargs: Any) -> RunResult:
        args = list(args)
        if args[:3] != [sys.executable, "-m", "yt_dlp"]:
            return run_command(args, timeout_s, **kwargs)
        self.calls.append(args)
        assert args[-2] == "--"
        slug = args[-1].removeprefix("https://clips.twitch.tv/")
        spec = self.clips[slug]
        if "error" in spec:
            return RunResult(1, "", spec["error"])
        if "-o" not in args:
            assert "--dump-single-json" in args and "--skip-download" in args
            info = {"id": "1", "display_id": slug, "formats": _formats(spec["formats"])}
            return RunResult(0, spec.get("probe_stdout", json.dumps(info)), "")
        if "download_error" in spec:
            return RunResult(1, "", spec["download_error"])
        if spec.get("no_file"):
            return RunResult(0, "", "")
        format_id = args[args.index("-f") + 1]
        assert format_id in spec["formats"]
        info_json = Path(args[args.index("--load-info-json") + 1])
        assert info_json.is_file()
        template = args[args.index("-o") + 1]
        shutil.copy(self.sample, template.replace("%(ext)s", "mp4").replace("%%", "%"))
        return RunResult(0, "", "")


class FakeTranscriber:
    """A local-model look-alike; `fail` maps a candidate id to the exception to raise."""

    provider = "local"
    model = "fake-whisper"

    def __init__(self) -> None:
        self.fail: dict[int, Exception] = {}
        self.calls: list[int] = []

    def transcribe(
        self, audio: Path, *, language: str | None, timeout_s: float | None = None
    ) -> ChunkTranscript:
        candidate = int(audio.parent.parent.name)  # candidates/<id>/.transcribe-*/chunk
        self.calls.append(candidate)
        if candidate in self.fail:
            raise self.fail[candidate]
        words = (Word("no", 0.1, 0.3), Word("way", 0.4, 0.8))
        return ChunkTranscript("en", words, (Segment(0.0, 1.0, "no way"),))

    def cost_usd(self, chunk_seconds: Sequence[float]) -> float:
        return 0.0

    def billed_seconds(self, chunk_seconds: Sequence[float]) -> float:
        return float(sum(chunk_seconds))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    for job_id, ref in ((1, DAY), (2, OTHER_DAY)):
        c.execute(
            "INSERT INTO jobs (id, platform, item_id, creator, source_url, status, created_at,"
            " updated_at) VALUES (?, ?, ?, 'random_gaming_yt', '', 'fetching_sources', ?, ?)",
            (job_id, ref.platform, ref.item_id, NOW, NOW),
        )
    yield c
    c.close()


@pytest.fixture
def media(tmp_path: Path) -> Path:
    path = tmp_path / "media" / DAY.key
    path.mkdir(parents=True)
    return path


@pytest.fixture
def ytdlp(sample_mp4: Path) -> FakeYtDlp:
    return FakeYtDlp(sample_mp4)


def _ctx(conn: sqlite3.Connection, media: Path, worker_id: str | None = None) -> StageContext:
    return StageContext(media, conn, AppConfig(), lambda: T0, worker_id=worker_id)


def _job(status: JobStatus = JobStatus.FETCHING_SOURCES, attempts: int = 1) -> Job:
    return Job(
        id=1,
        ref=DAY,
        creator="random_gaming_yt",
        source_url="",
        status=status,
        is_stream=False,
        attempts=attempts,
    )


def _candidate(
    conn: sqlite3.Connection,
    slug: str,
    score: float,
    *,
    job_id: int = 1,
    pinned: bool = False,
    skip_reason: str | None = None,
    created_at: str = "2026-11-26T05:00:00Z",
) -> int:
    cur = conn.execute(
        "INSERT INTO story_candidates (job_id, account_id, twitch_clip_id, broadcaster_id,"
        " broadcaster_login, url, view_count, duration_s, clip_created_at, pinned, score,"
        " skip_reason, created_at, updated_at)"
        " VALUES (?, 'random_gaming_yt', ?, '123', 'kaicenat', ?, 20000, 30.0, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            slug,
            f"https://clips.twitch.tv/{slug}",
            created_at,
            int(pinned),
            score,
            skip_reason,
            NOW,
            NOW,
        ),
    )
    return int(cur.lastrowid or 0)


def _row(conn: sqlite3.Connection, candidate_id: int) -> sqlite3.Row:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM story_candidates WHERE id = ?", (candidate_id,)).fetchone()
    conn.row_factory = None
    return row


def _fetch(
    conn: sqlite3.Connection,
    media: Path,
    ytdlp: FakeYtDlp,
    *,
    alerts: list[str] | None = None,
    per_day: int = 12,
    attempts: int = 1,
) -> None:
    cs.fetch_sources(
        _job(attempts=attempts),
        _ctx(conn, media),
        runner=ytdlp,
        env={},
        alert=(alerts if alerts is not None else []).append,
        candidates_per_day=per_day,
    )


def _transcribe(
    conn: sqlite3.Connection,
    media: Path,
    transcriber: FakeTranscriber,
    *,
    alerts: list[str] | None = None,
    attempts: int = 1,
    per_day: int = 12,
) -> None:
    cs.transcribe_sources(
        _job(JobStatus.TRANSCRIBING, attempts=attempts),
        _ctx(conn, media),
        transcriber=transcriber,
        alert=(alerts if alerts is not None else []).append,
        candidates_per_day=per_day,
    )


# --- migration 0015 ---------------------------------------------------------------------


def test_migration_0015_is_bundled_and_adds_the_candidate_media_columns(
    conn: sqlite3.Connection,
) -> None:
    assert (15, "commentary_candidate_media") in [(m.version, m.name) for m in bundled_migrations()]
    columns = {r[1]: r for r in conn.execute("PRAGMA table_info(story_candidates)")}
    for name in ("source_path", "source_kind", "loudness_peaks", "transcript_path", "drop_reason"):
        assert name in columns
        assert columns[name][3] == 0  # nullable: discovery writes the row without them


def test_source_kind_is_vertical_or_landscape(conn: sqlite3.Connection) -> None:
    cid = _candidate(conn, "Clip", 10.0)
    for kind in ("vertical", "landscape", None):
        conn.execute("UPDATE story_candidates SET source_kind = ? WHERE id = ?", (kind, cid))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE story_candidates SET source_kind = 'square' WHERE id = ?", (cid,))


def test_loudness_peaks_must_be_json(conn: sqlite3.Connection) -> None:
    cid = _candidate(conn, "Clip", 10.0)
    conn.execute(
        "UPDATE story_candidates SET loudness_peaks = ? WHERE id = ?",
        ('[{"t_s": 1.2, "lufs": -14.0}]', cid),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE story_candidates SET loudness_peaks = 'peaks' WHERE id = ?", (cid,))


# --- registration -----------------------------------------------------------------------


def test_fetching_and_transcribing_are_real_stages_now() -> None:
    stages = build_commentary_stages()

    assert JobStatus.FETCHING_SOURCES not in commentary.NOT_BUILT
    assert JobStatus.TRANSCRIBING not in commentary.NOT_BUILT
    assert JobStatus.DISCOVERING not in commentary.NOT_BUILT  # CLP-144 shipped too
    assert stages[JobStatus.FETCHING_SOURCES] is cs.FETCH_STAGE
    assert stages[JobStatus.TRANSCRIBING] is cs.TRANSCRIBE_STAGE


# --- format choice ----------------------------------------------------------------------


def test_the_highest_portrait_format_is_chosen() -> None:
    assert cs.choose_format(_formats((*LANDSCAPE, *PORTRAIT))) == ("portrait-900", "vertical")
    assert cs.choose_format(_formats(("1080", "portrait-480", "portrait-360"))) == (
        "portrait-480",
        "vertical",
    )


def test_the_landscape_format_only_without_a_portrait_one() -> None:
    assert cs.choose_format(_formats(LANDSCAPE)) == ("1080", "landscape")


def test_a_portrait_format_without_a_height_is_still_vertical() -> None:
    assert cs.choose_format(_formats(("1080", "portrait"))) == ("portrait", "vertical")
    assert cs.choose_format(_formats(("portrait", "portrait-480"))) == ("portrait-480", "vertical")


def test_a_renamed_duplicate_portrait_format_is_still_vertical() -> None:
    """yt-dlp renames duplicate format ids to `<id>-<n>`."""
    formats = _formats(("1080", "portrait-720-0", "portrait-720-1", "portrait-480"))
    assert cs.choose_format(formats) == ("portrait-720-0", "vertical")
    assert cs.choose_format(_formats(("1080-0", "720"))) == ("1080-0", "landscape")


def test_no_usable_format_is_none() -> None:
    assert cs.choose_format([]) is None
    assert cs.choose_format([{"format_id": "1080", "height": 1080}]) is None  # no url


@pytest.mark.parametrize(
    "ids",
    [
        (*LANDSCAPE, *PORTRAIT),
        LANDSCAPE,
        ("portrait-480",),
        ("1080", "portrait"),  # the extractor's id for a portrait format without a quality
        ("720", "portrait", "portrait-720"),
        ("1080", "portrait-720-0", "portrait-480"),
    ],
)
def test_yt_dlps_own_selector_agrees_with_the_choice(ids: tuple[str, ...]) -> None:
    """The probe's `-f` runs through yt-dlp's real format selector (offline): it accepts
    Twitch clips' codec-less formats and picks what `choose_format` picks."""
    from yt_dlp import YoutubeDL

    info = {
        "id": "1",
        "title": "t",
        "formats": _formats(ids),
        "extractor": "twitch:clips",
        "extractor_key": "TwitchClips",
        "webpage_url": "https://clips.twitch.tv/Clip",
    }
    with YoutubeDL({"format": cs.CLIP_SELECTOR, "quiet": True, "simulate": True}) as ydl:
        chosen = ydl.process_video_result(info, download=False)

    expected = cs.choose_format(_formats(ids))
    assert expected is not None
    assert chosen["format_id"] == expected[0]


# --- fetching_sources -------------------------------------------------------------------


def test_every_selected_candidate_is_fetched_with_audio_and_peaks(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    ids = [_candidate(conn, slug, score) for slug, score in (("A", 30.0), ("B", 20.0))]
    ytdlp.clip("A")
    ytdlp.clip("B", LANDSCAPE)

    _fetch(conn, media, ytdlp)

    a, b = (_row(conn, i) for i in ids)
    assert (a["source_kind"], b["source_kind"]) == ("vertical", "landscape")
    assert sorted(ytdlp.downloads()) == [("A", "portrait-900"), ("B", "1080")]
    for row in (a, b):
        assert row["source_path"] == f"candidates/{row['id']}/source.mp4"
        assert row["drop_reason"] is None
        assert (media / row["source_path"]).is_file()
        assert (media / "candidates" / str(row["id"]) / "audio_16k.flac").is_file()
        peaks = json.loads(row["loudness_peaks"])
        assert peaks and all(set(p) == {"t_s", "lufs"} for p in peaks)
        assert not list((media / "candidates" / str(row["id"])).glob(".info*"))


def test_loudness_peaks_find_the_loud_moment(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    cid = _candidate(conn, "A", 30.0)
    ytdlp.clip("A")

    _fetch(conn, media, ytdlp)

    peaks = json.loads(_row(conn, cid)["loudness_peaks"])
    assert len(peaks) <= cs.PEAK_COUNT
    loudest = max(peaks, key=lambda p: p["lufs"])
    assert 1.9 <= loudest["t_s"] <= 2.9  # the burst at 2.0-2.4 s (400 ms window)


def test_a_hook_peak_in_the_first_second_of_a_60_s_clip_is_found(
    conn: sqlite3.Connection, media: Path, sample_60s_mp4: Path
) -> None:
    """The real `run_command` keeps only the last 64 KB of stderr; a 60 s clip's ebur128
    log is longer, so the samples are collected line by line."""
    ytdlp = FakeYtDlp(sample_60s_mp4)  # ffmpeg runs through the real run_command
    cid = _candidate(conn, "A", 30.0)
    ytdlp.clip("A")

    _fetch(conn, media, ytdlp)

    peaks = json.loads(_row(conn, cid)["loudness_peaks"])
    loudest = max(peaks, key=lambda p: p["lufs"])
    assert 0.9 <= loudest["t_s"] <= 1.9
    assert peaks[0]["t_s"] < 2.0


def test_the_url_is_built_from_the_clip_id_not_the_stored_url(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    cid = _candidate(conn, "A", 30.0)
    conn.execute(
        "UPDATE story_candidates SET url = 'https://evil.example/--exec' WHERE id = ?", (cid,)
    )
    ytdlp.clip("A")

    _fetch(conn, media, ytdlp)

    assert {args[-1] for args in ytdlp.calls} == {"https://clips.twitch.tv/A"}


def test_a_clip_id_that_is_not_a_twitch_slug_is_dropped_without_a_download(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    bad = _candidate(conn, "../etc", 30.0)
    good = _candidate(conn, "Good", 20.0)
    ytdlp.clip("Good")

    _fetch(conn, media, ytdlp)

    assert _row(conn, bad)["drop_reason"] == "fetch: not a Twitch clip id"
    assert _row(conn, good)["source_path"] is not None
    assert [args[-1] for args in ytdlp.calls if "-o" in args] == ["https://clips.twitch.tv/Good"]


def test_only_this_jobs_top_candidates_are_fetched(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """Pinned first (spec, settled 2026-11-24), then score, then the newer clip; filtered
    rows and other days' rows are never touched."""
    pinned = _candidate(conn, "Pinned", 5.0, pinned=True)
    top = _candidate(conn, "Top", 50.0)
    newer = _candidate(conn, "Newer", 40.0, created_at="2026-11-26T06:00:00Z")
    older = _candidate(conn, "Older", 40.0, created_at="2026-11-26T04:00:00Z")
    filtered = _candidate(conn, "Filtered", 99.0, skip_reason="language is not en")
    other_day = _candidate(conn, "OtherDay", 99.0, job_id=2)
    for slug in ("Pinned", "Top", "Newer", "Older", "Filtered", "OtherDay"):
        ytdlp.clip(slug)

    _fetch(conn, media, ytdlp, per_day=3)

    assert sorted(s for s, _ in ytdlp.downloads()) == ["Newer", "Pinned", "Top"]
    for cid in (pinned, top, newer):
        assert _row(conn, cid)["source_path"] is not None
    for cid in (older, filtered, other_day):
        row = _row(conn, cid)
        assert (row["source_path"], row["drop_reason"], row["updated_at"]) == (None, None, NOW)
    assert _row(conn, filtered)["skip_reason"] == "language is not en"


def test_an_unavailable_candidate_is_dropped_and_the_rest_go_on(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    gone = _candidate(conn, "Gone", 30.0)
    ok = _candidate(conn, "Ok", 20.0)
    ytdlp.clip("Gone", error=GONE)
    ytdlp.clip("Ok")
    alerts: list[str] = []

    _fetch(conn, media, ytdlp, alerts=alerts)

    reason = _row(conn, gone)["drop_reason"]
    assert reason.startswith("fetch: video not downloadable (removed or unavailable)")
    assert _row(conn, gone)["source_path"] is None
    assert _row(conn, ok)["source_path"] is not None
    assert alerts == []


def test_a_transient_error_retries_the_stage_then_drops_on_the_last_attempt(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """A flaky clip ranked first does not hold up the ones after it: they are fetched in
    the same run, and the stage is retried for the flaky one afterwards."""
    flaky = _candidate(conn, "Flaky", 30.0)
    ok1 = _candidate(conn, "Ok1", 20.0)
    ok2 = _candidate(conn, "Ok2", 10.0)
    ytdlp.clip("Flaky", download_error=FLAKY)
    ytdlp.clip("Ok1")
    ytdlp.clip("Ok2")

    with pytest.raises(YtDlpError):
        _fetch(conn, media, ytdlp, attempts=1)
    assert _row(conn, flaky)["drop_reason"] is None
    assert _row(conn, ok1)["source_path"] is not None
    assert _row(conn, ok2)["source_path"] is not None

    with pytest.raises(YtDlpError):
        _fetch(conn, media, ytdlp, attempts=MAX_ATTEMPTS - 1)  # not the last one yet
    assert _row(conn, flaky)["drop_reason"] is None

    _fetch(conn, media, ytdlp, attempts=MAX_ATTEMPTS)

    assert _row(conn, flaky)["drop_reason"].startswith("fetch: yt-dlp failed")
    assert [s for s, _ in ytdlp.downloads()].count("Ok1") == 1  # not fetched again


def test_every_candidate_is_tried_before_the_first_transient_error_is_raised(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    for slug, score in (("Flaky1", 30.0), ("Flaky2", 20.0)):
        _candidate(conn, slug, score)
        ytdlp.clip(slug, download_error=FLAKY.replace("Flaky", slug))

    with pytest.raises(YtDlpError, match="Flaky1"):
        _fetch(conn, media, ytdlp)

    assert {s for s, _ in ytdlp.downloads()} == {"Flaky1", "Flaky2"}


def test_the_stage_budget_running_out_never_drops_a_candidate(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time budget is the stage's, not the candidate's: even on the last attempt the
    stage fails (the worker records it) and no healthy candidate is dropped for it."""
    ids = [_candidate(conn, slug, score) for slug, score in (("A", 30.0), ("B", 20.0))]
    for slug in ("A", "B"):
        ytdlp.clip(slug)
    monkeypatch.setattr(cs, "Deadline", lambda: Deadline(budget_s=0.5))  # already spent

    with pytest.raises(StageTimeout):
        _fetch(conn, media, ytdlp, attempts=MAX_ATTEMPTS)

    assert [_row(conn, cid)["drop_reason"] for cid in ids] == [None, None]


def test_a_clip_without_qualities_is_dropped_at_once(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """yt-dlp's Twitch clips extractor fails with an IndexError on a clip that has no
    qualities; yt-dlp reports it as a generic error, which is not worth a retry here."""
    cid = _candidate(conn, "Empty", 30.0)
    other = _candidate(conn, "Ok", 20.0)
    ytdlp.clip("Empty", error=NO_QUALITIES)
    ytdlp.clip("Ok")

    _fetch(conn, media, ytdlp, attempts=1)

    assert _row(conn, cid)["drop_reason"] == "fetch: the Twitch clip has no formats"
    assert _row(conn, other)["source_path"] is not None


def test_another_extractor_index_error_is_still_retried(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    _candidate(conn, "Odd", 30.0)
    ytdlp.clip("Odd", error="ERROR: [generic] Odd: list index out of range")

    with pytest.raises(YtDlpError):
        _fetch(conn, media, ytdlp, attempts=1)


def test_a_probe_without_formats_is_dropped(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    cid = _candidate(conn, "Empty", 30.0)
    _candidate(conn, "Ok", 20.0)
    ytdlp.clip("Empty", formats=())
    ytdlp.clip("Ok")

    _fetch(conn, media, ytdlp)

    assert _row(conn, cid)["drop_reason"] == "fetch: no downloadable format"


def test_all_candidates_failing_skips_the_day_with_one_alert(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    for slug, score in (("Gone1", 30.0), ("Gone2", 20.0)):
        _candidate(conn, slug, score)
        ytdlp.clip(slug, error=GONE.replace("Gone", slug))
    alerts: list[str] = []

    with pytest.raises(SkipJob) as info:
        _fetch(conn, media, ytdlp, alerts=alerts)

    assert str(info.value).startswith("no candidate could be fetched (2 dropped; first: fetch:")
    assert len(alerts) == 1
    assert alerts[0].startswith(
        "No commentary Short today (job 1, commentary_random_gaming_yt_2026-11-26):"
    )
    assert alerts[0].endswith("Run `clipper retry 1` to try again.")
    assert "no candidate could be fetched" in alerts[0]


def test_the_alert_is_sent_once_when_a_crashed_run_is_claimed_again(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """The alert goes out before the worker records the skip; a worker that dies in
    between leaves the day to be claimed again (a later attempt), which must not alert
    twice."""
    _candidate(conn, "Gone", 30.0)
    ytdlp.clip("Gone", error=GONE)
    alerts: list[str] = []
    with pytest.raises(SkipJob):
        _fetch(conn, media, ytdlp, alerts=alerts, attempts=1)
    probes = len(ytdlp.calls)

    with pytest.raises(SkipJob, match="no candidate could be fetched"):
        _fetch(conn, media, ytdlp, alerts=alerts, attempts=2)

    assert len(alerts) == 1
    assert len(ytdlp.calls) == probes  # the dropped clip is not tried again either


def test_a_retried_day_that_fails_again_alerts_again(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """`clipper retry` brings the day back to the stage at its first attempt."""
    _candidate(conn, "Gone", 30.0)
    ytdlp.clip("Gone", error=GONE)
    alerts: list[str] = []
    for _ in range(2):
        with pytest.raises(SkipJob):
            _fetch(conn, media, ytdlp, alerts=alerts, attempts=1)

    assert len(alerts) == 2


def test_an_earlier_runs_alert_does_not_silence_a_new_run(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """A retried day whose discovery found a new candidate (so not every one is dropped)
    and that fails again still alerts."""
    _candidate(conn, "Gone", 30.0)
    ytdlp.clip("Gone", error=GONE)
    alerts: list[str] = []
    with pytest.raises(SkipJob):
        _fetch(conn, media, ytdlp, alerts=alerts, attempts=1)
    _candidate(conn, "New", 20.0)
    ytdlp.clip("New", error=GONE.replace("Gone", "New"))

    with pytest.raises(SkipJob):
        _fetch(conn, media, ytdlp, alerts=alerts, attempts=1)

    assert len(alerts) == 2


def test_an_alert_that_cannot_be_sent_still_skips_the_day(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    _candidate(conn, "Gone", 30.0)
    ytdlp.clip("Gone", error=GONE)

    def broken(text: str) -> None:
        raise RuntimeError("discord is down")

    with pytest.raises(SkipJob):
        cs.fetch_sources(
            _job(), _ctx(conn, media), runner=ytdlp, env={}, alert=broken, candidates_per_day=12
        )


def test_no_selected_candidate_is_a_quiet_skip(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    _candidate(conn, "Filtered", 99.0, skip_reason="too short")
    alerts: list[str] = []

    with pytest.raises(SkipJob, match="no candidate to fetch"):
        _fetch(conn, media, ytdlp, alerts=alerts)

    assert alerts == []
    assert ytdlp.calls == []


def test_a_rerun_does_not_download_a_fetched_candidate_again(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    cid = _candidate(conn, "A", 30.0)
    ytdlp.clip("A")
    _fetch(conn, media, ytdlp)
    calls = len(ytdlp.calls)

    _fetch(conn, media, ytdlp)
    assert len(ytdlp.calls) == calls

    (media / _row(conn, cid)["source_path"]).unlink()  # a file lost since: fetched again
    _fetch(conn, media, ytdlp)
    assert len(ytdlp.downloads()) == 2


def test_a_dropped_candidate_is_not_tried_again(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    _candidate(conn, "Gone", 30.0)
    _candidate(conn, "Ok", 20.0)
    ytdlp.clip("Gone", error=GONE)
    ytdlp.clip("Ok")
    _fetch(conn, media, ytdlp)
    gone_calls = sum(1 for args in ytdlp.calls if args[-1].endswith("/Gone"))

    _fetch(conn, media, ytdlp)

    assert sum(1 for args in ytdlp.calls if args[-1].endswith("/Gone")) == gone_calls


def test_a_retried_day_tries_its_dropped_candidates_again(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """When every candidate is dropped (the day was skipped), the next run of the stage
    (`clipper retry`) tries them all again: the cause may be fixed by now."""
    cid = _candidate(conn, "A", 30.0)
    ytdlp.clip("A", error=GONE.replace("Gone", "A"))
    with pytest.raises(SkipJob):
        _fetch(conn, media, ytdlp)

    ytdlp.clip("A")
    _fetch(conn, media, ytdlp)

    row = _row(conn, cid)
    assert row["drop_reason"] is None
    assert row["source_kind"] == "vertical"


def test_a_retried_day_clears_its_drops_in_one_fenced_write(
    conn: sqlite3.Connection, media: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [_candidate(conn, slug, score) for slug, score in (("A", 30.0), ("B", 20.0))]
    conn.execute("UPDATE story_candidates SET drop_reason = 'fetch: gone'")
    lease = to_iso(T0 + timedelta(hours=1))
    conn.execute("UPDATE jobs SET worker_id = 'w1', lease_until = ? WHERE id = 1", (lease,))
    fences: list[int] = []
    real = cs.hold_lease
    monkeypatch.setattr(cs, "hold_lease", lambda *a: (fences.append(1), real(*a)))
    candidates = cs.selected_candidates(conn, 1, 12)

    cleared = cs._retry_all_dropped(_ctx(conn, media, worker_id="w1"), _job(), candidates)

    assert [c.drop_reason for c in cleared] == [None, None]
    assert [_row(conn, cid)["drop_reason"] for cid in ids] == [None, None]
    assert fences == [1]


def test_candidate_writes_are_fenced_on_the_lease(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    cid = _candidate(conn, "A", 30.0)
    ytdlp.clip("A")
    lease = to_iso(T0 - timedelta(minutes=1))  # expired: another worker may have it
    conn.execute("UPDATE jobs SET worker_id = 'w1', lease_until = ? WHERE id = 1", (lease,))

    with pytest.raises(JobConflict):
        cs.fetch_sources(
            _job(),
            _ctx(conn, media, worker_id="w1"),
            runner=ytdlp,
            env={},
            alert=[].append,
            candidates_per_day=12,
        )

    assert _row(conn, cid)["source_path"] is None


def _write_config(root: Path, per_day: int) -> Path:
    """creators.yaml, accounts.yaml and commentary.yaml in `root/config`."""
    config = root / "config"
    config.mkdir(exist_ok=True)
    (config / "creators.yaml").write_text("creators: []\n")
    (config / "accounts.yaml").write_text(
        "accounts:\n"
        "  - id: random_gaming_yt\n"
        "    platform: youtube\n"
        "    content: commentary\n"
        "    channel_id: UCcMYw3Ae2crqXE2yzFBSUig\n"
        "    handle: '@randomgaming'\n"
        "    market: US\n"
        "    timezone: America/New_York\n"
    )
    (config / "commentary.yaml").write_text(
        "defaults:\n"
        "  candidates_per_day: 12\n"
        "  stories_per_day: 1\n"
        "channels:\n"
        "  - account: random_gaming_yt\n"
        "    source: twitch_clips\n"
        "    prompt: commentary_gaming_v1\n"
        "    games:\n"
        "      - name: Grand Theft Auto VI\n"
        "    overrides:\n"
        f"      candidates_per_day: {per_day}\n"
    )
    return config / "creators.yaml"


def test_candidates_per_day_comes_from_the_channel_config(tmp_path: Path) -> None:
    creators = _write_config(tmp_path, 4)

    assert cs.candidates_per_day(_job(), AppConfig(), env={}) == 4  # ./config/creators.yaml
    env = {"CLIPPER_CONFIG": str(creators)}
    assert cs.candidates_per_day(_job(), AppConfig(), env=env) == 4
    other = Job(1, SourceRef("commentary", "unknown_yt:2026-11-26"), "unknown_yt", "",
                JobStatus.FETCHING_SOURCES, False, 1)  # fmt: skip
    assert cs.candidates_per_day(other, AppConfig(), env=env) == 12  # the defaults


def test_without_a_commentary_config_the_default_count_applies(tmp_path: Path) -> None:
    assert cs.candidates_per_day(_job(), AppConfig(), env={}) == 12


def test_an_invalid_commentary_config_fails_the_day(tmp_path: Path) -> None:
    creators = _write_config(tmp_path, 4)
    (creators.parent / "commentary.yaml").write_text("channels: [nope]\n")

    with pytest.raises(PermanentError, match="commentary"):
        cs.candidates_per_day(_job(), AppConfig(), env={})


def test_the_stage_reads_candidates_per_day_from_the_config(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp, tmp_path: Path
) -> None:
    _write_config(tmp_path, 1)  # the tests run in tmp_path
    for slug, score in (("A", 30.0), ("B", 20.0)):
        _candidate(conn, slug, score)
        ytdlp.clip(slug)

    cs.fetch_sources(_job(), _ctx(conn, media), runner=ytdlp, env={}, alert=[].append)

    assert [s for s, _ in ytdlp.downloads()] == ["A"]


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [("not json", "yt-dlp returned invalid JSON"), ("[]", "yt-dlp returned unexpected JSON")],
)
def test_a_garbled_probe_is_retried_then_dropped(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp, stdout: str, reason: str
) -> None:
    cid = _candidate(conn, "A", 30.0)
    _candidate(conn, "Ok", 20.0)
    ytdlp.clip("A", probe_stdout=stdout)
    ytdlp.clip("Ok")

    with pytest.raises(YtDlpError, match=reason):
        _fetch(conn, media, ytdlp)
    _fetch(conn, media, ytdlp, attempts=MAX_ATTEMPTS)

    assert _row(conn, cid)["drop_reason"] == f"fetch: {reason}"


def test_a_download_that_leaves_no_file_is_retried_then_dropped(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp
) -> None:
    """yt-dlp exits 0 without a file when a clip is over --max-filesize."""
    cid = _candidate(conn, "Huge", 30.0)
    _candidate(conn, "Ok", 20.0)
    ytdlp.clip("Huge", no_file=True)
    ytdlp.clip("Ok")

    with pytest.raises(YtDlpError):
        _fetch(conn, media, ytdlp)
    _fetch(conn, media, ytdlp, attempts=MAX_ATTEMPTS)

    assert _row(conn, cid)["drop_reason"] == "fetch: expected one downloaded file, found 0"
    assert not list((media / "candidates" / str(cid)).iterdir())  # no info JSON left behind


def test_without_a_webhook_the_alert_goes_to_the_log(caplog: pytest.LogCaptureFixture) -> None:
    cs.default_alert("No commentary Short today")

    assert "NOTIFY: No commentary Short today" in caplog.text


def test_the_alert_goes_to_the_alerts_webhook_first(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    sent: list[tuple[str, dict[str, Any]]] = []

    def webhook(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(webhook), **kw)
    )
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/1/pings")
    monkeypatch.setenv("DISCORD_ALERTS_WEBHOOK_URL", "https://discord.com/api/webhooks/2/alerts")

    cs.default_alert("No commentary Short today")

    assert [(path, body["content"]) for path, body in sent] == [
        ("/api/webhooks/2/alerts", "No commentary Short today")
    ]


# --- transcribing -----------------------------------------------------------------------


@pytest.fixture
def fetched(conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp) -> list[int]:
    """Three fetched candidates of the day, one filtered row and one of another day."""
    ids = [_candidate(conn, slug, score) for slug, score in (("A", 30.0), ("B", 20.0), ("C", 10.0))]
    for slug in ("A", "B", "C"):
        ytdlp.clip(slug)
    _candidate(conn, "Filtered", 99.0, skip_reason="too short")
    _candidate(conn, "OtherDay", 99.0, job_id=2)
    _fetch(conn, media, ytdlp)
    return ids


def test_every_fetched_candidate_gets_a_word_timed_transcript(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    transcriber = FakeTranscriber()

    _transcribe(conn, media, transcriber)

    assert sorted(transcriber.calls) == sorted(fetched)
    for cid in fetched:
        row = _row(conn, cid)
        assert row["transcript_path"] == f"candidates/{cid}/transcript.json"
        data = json.loads((media / row["transcript_path"]).read_text())
        assert data["model"] == "fake-whisper"
        assert [w["word"] for w in data["words"]] == ["no", "way"]
        assert data["words"][0]["start"] == pytest.approx(0.1)


def test_a_candidate_that_cannot_be_transcribed_is_dropped(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    transcriber = FakeTranscriber()
    transcriber.fail[fetched[1]] = PermanentError("chunk 0: transcription unusable twice")
    alerts: list[str] = []

    _transcribe(conn, media, transcriber, alerts=alerts)

    dropped = _row(conn, fetched[1])
    assert dropped["drop_reason"] == "transcribe: chunk 0: transcription unusable twice"
    assert dropped["transcript_path"] is None
    assert _row(conn, fetched[0])["transcript_path"] is not None
    assert _row(conn, fetched[2])["transcript_path"] is not None
    assert alerts == []


def test_a_transient_transcription_error_retries_then_drops_on_the_last_attempt(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    transcriber = FakeTranscriber()
    transcriber.fail[fetched[0]] = MediaError("local transcription failed (exit -9)")

    with pytest.raises(MediaError):
        _transcribe(conn, media, transcriber, attempts=1)
    assert _row(conn, fetched[0])["drop_reason"] is None
    assert _row(conn, fetched[1])["transcript_path"] is not None  # not held up
    assert _row(conn, fetched[2])["transcript_path"] is not None

    with pytest.raises(MediaError):
        _transcribe(conn, media, transcriber, attempts=MAX_ATTEMPTS - 1)

    _transcribe(conn, media, transcriber, attempts=MAX_ATTEMPTS)
    assert _row(conn, fetched[0])["drop_reason"].startswith("transcribe: local transcription")
    assert transcriber.calls.count(fetched[1]) == 1  # done once, not again on the retry


def test_all_transcriptions_failing_skips_the_day_with_one_alert(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    transcriber = FakeTranscriber()
    for cid in fetched:
        transcriber.fail[cid] = PermanentError("cannot read the duration")
    alerts: list[str] = []

    with pytest.raises(SkipJob, match=r"^no candidate could be transcribed \(3 dropped"):
        _transcribe(conn, media, transcriber, alerts=alerts)

    assert len(alerts) == 1 and "no candidate could be transcribed" in alerts[0]


def test_a_retried_day_transcribes_its_dropped_candidates_again(
    conn: sqlite3.Connection, media: Path, fetched: list[int], ytdlp: FakeYtDlp
) -> None:
    """A day skipped because no transcript could be made (a model that would not load):
    the retried day fetches nothing again and transcribes every candidate anew."""
    broken = FakeTranscriber()
    for cid in fetched:
        broken.fail[cid] = PermanentError("cannot load the model")
    with pytest.raises(SkipJob):
        _transcribe(conn, media, broken, alerts=[])
    downloads = len(ytdlp.downloads())

    _fetch(conn, media, ytdlp)  # the retried day runs fetching_sources first
    _transcribe(conn, media, FakeTranscriber())

    assert len(ytdlp.downloads()) == downloads
    for cid in fetched:
        row = _row(conn, cid)
        assert (row["drop_reason"], row["transcript_path"] is not None) == (None, True)


def test_transcribing_takes_only_the_days_top_candidates(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    transcriber = FakeTranscriber()

    _transcribe(conn, media, transcriber, per_day=2)

    assert sorted(transcriber.calls) == sorted(fetched[:2])
    assert _row(conn, fetched[2])["transcript_path"] is None


def test_transcribing_skips_dropped_and_unfetched_candidates(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    conn.execute(
        "UPDATE story_candidates SET drop_reason = 'fetch: gone' WHERE id = ?", (fetched[0],)
    )
    transcriber = FakeTranscriber()

    _transcribe(conn, media, transcriber)

    assert sorted(transcriber.calls) == sorted(fetched[1:])


def test_a_lost_audio_file_drops_the_candidate(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    (media / "candidates" / str(fetched[0]) / "audio_16k.flac").unlink()

    _transcribe(conn, media, FakeTranscriber())

    assert _row(conn, fetched[0])["drop_reason"] == "transcribe: audio_16k.flac is missing"


def test_a_rerun_does_not_transcribe_again(
    conn: sqlite3.Connection, media: Path, fetched: list[int]
) -> None:
    transcriber = FakeTranscriber()
    _transcribe(conn, media, transcriber)
    calls = len(transcriber.calls)

    _transcribe(conn, media, transcriber)

    assert len(transcriber.calls) == calls


def test_transcribing_with_nothing_fetched_is_a_quiet_skip(
    conn: sqlite3.Connection, media: Path
) -> None:
    alerts: list[str] = []
    with pytest.raises(SkipJob, match="no fetched candidate to transcribe"):
        _transcribe(conn, media, FakeTranscriber(), alerts=alerts)
    assert alerts == []


def test_the_default_transcriber_is_local_whisper_with_the_clip_settings() -> None:
    settings = AppConfig().settings.model_copy(
        update={
            "transcriber": "groq",
            "whisper_model": "tiny",
            "whisper_compute_type": "int8",
            "whisper_cpu_threads": 2,
        }
    )

    backend = cs.local_transcriber(settings)

    assert isinstance(backend, FasterWhisperTranscriber)
    assert (backend.model, backend.compute_type, backend.cpu_threads) == ("tiny", "int8", 2)


def test_the_stage_transcribes_with_the_local_model_by_default(
    conn: sqlite3.Connection, media: Path, fetched: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[Any] = []
    fake = FakeTranscriber()

    def local(settings: Any) -> FakeTranscriber:
        built.append(settings)
        return fake

    monkeypatch.setattr(cs, "local_transcriber", local)

    cs.transcribe_sources(_job(JobStatus.TRANSCRIBING), _ctx(conn, media), alert=[].append)

    assert built == [AppConfig().settings]
    assert sorted(fake.calls) == sorted(fetched)


# --- through the worker -----------------------------------------------------------------


def test_the_worker_runs_both_stages_under_its_lease(
    tmp_path: Path, ytdlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real stage map, claims, leases and fenced writes: a day goes from
    fetching_sources through transcribing to the scripting placeholder (CLP-146)."""
    from clipper.jobs.queue import enqueue
    from clipper.jobs.worker import Worker
    from clipper.pipeline import build_stages
    from clipper.pipeline import ytdlp as ytdlp_module

    db = connect(tmp_path / "worker.db")
    migrate(db)
    job, _ = enqueue(db, DAY, T0, creator="random_gaming_yt")
    db.execute("UPDATE jobs SET status = 'fetching_sources' WHERE id = ?", (job.id,))
    ids = [_candidate(db, slug, score, job_id=job.id) for slug, score in (("A", 2.0), ("B", 1.0))]
    ytdlp.clip("A")
    ytdlp.clip("B", LANDSCAPE)
    monkeypatch.setattr(ytdlp_module, "run_command", ytdlp)  # default_runner() looks it up
    monkeypatch.setattr(cs, "local_transcriber", lambda settings: FakeTranscriber())
    alerts: list[str] = []
    worker = Worker(
        conn=db,
        config=AppConfig(),
        worker_id="w1",
        stages=build_stages(),
        media_root=tmp_path / "media",
        alert=alerts.append,
        commentary_stages=build_commentary_stages(),
    )

    for _ in range(3):
        assert worker.run_once()

    status, reason = db.execute(
        "SELECT status, skip_reason FROM jobs WHERE id = ?", (job.id,)
    ).fetchone()
    assert (status, reason) == ("skipped", "commentary scripting is not built yet (CLP-146)")
    media = tmp_path / "media" / DAY.key
    for cid, kind in zip(ids, ("vertical", "landscape"), strict=True):
        row = _row(db, cid)
        assert (row["source_kind"], row["drop_reason"]) == (kind, None)
        assert (media / row["transcript_path"]).is_file()
    assert alerts == []
    db.close()


# --- real model (nightly) ---------------------------------------------------------------

SLOW_MODEL_DIR = Path(__file__).resolve().parents[1] / "data" / "models"
TINY_REVISION = "d90ca5fe260221311c53c58e660288d3deb8d356"


@pytest.mark.slow
def test_the_real_tiny_model_transcribes_a_fetched_candidate(
    conn: sqlite3.Connection, media: Path, ytdlp: FakeYtDlp, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = SLOW_MODEL_DIR / "models--Systran--faster-whisper-tiny" / "snapshots" / TINY_REVISION
    if os.environ.get("CLIPPER_SLOW_TESTS") != "1" or not (snapshot / "model.bin").is_file():
        pytest.skip("set CLIPPER_SLOW_TESTS=1 and download `tiny` into data/models")
    monkeypatch.setenv("CLIPPER_MODELS_DIR", str(SLOW_MODEL_DIR))
    cid = _candidate(conn, "A", 30.0)
    ytdlp.clip("A")
    _fetch(conn, media, ytdlp)
    settings = AppConfig().settings.model_copy(update={"whisper_model": "tiny"})
    ctx = StageContext(media, conn, AppConfig(settings=settings), lambda: T0)

    cs.transcribe_sources(_job(JobStatus.TRANSCRIBING), ctx, alert=[].append)

    data = json.loads((media / _row(conn, cid)["transcript_path"]).read_text())
    assert data["model"] == "tiny"
    assert set(data) == {"language", "duration_s", "model", "segments", "words"}

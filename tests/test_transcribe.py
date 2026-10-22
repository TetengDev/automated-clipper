import dataclasses
import hashlib
import inspect
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from asr_support import (
    FAKE_MODEL_SHA256,
    FakeAsr,
    FakeWhisperModel,
    install_fake_asr,
    seg,
    write_snapshot,
)
from clipper.config import AppConfig, load_config
from clipper.db import connect, migrate
from clipper.jobs import Deferred, Job, PermanentError, SkipJob, StageContext, enqueue
from clipper.jobs.models import JobStatus
from clipper.pipeline import local_asr, ytdlp
from clipper.pipeline import transcribe as tr
from clipper.pipeline.common import (
    Deadline,
    MediaError,
    StageTimeout,
    spend_today,
    write_json_atomic,
)
from clipper.pipeline.transcribe import (
    Chunk,
    ChunkTranscript,
    GroqTranscriber,
    Segment,
    Word,
    plan_chunks,
    stitch,
)
from clipper.urls import SourceRef

FIXTURES = Path(__file__).parent / "fixtures" / "groq"
T0 = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
KEY = "gsk_test_secret_key_value_1234567890"
PH = "Raffy Tulfo in Action"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg missing"
)


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def ok() -> httpx.Response:
    return httpx.Response(200, json=fixture("verbose_chunk.json"))


# --------------------------------------------------------------------------- fixtures


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    c = connect(tmp_path / "state.db")
    migrate(c)
    yield c
    c.close()


@pytest.fixture
def config(config_file: Path) -> AppConfig:
    return load_config(config_file)


@pytest.fixture(scope="session")
def audio_30s(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg missing")
    out = tmp_path_factory.mktemp("audio") / "audio_16k.flac"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=30",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "flac",
            str(out),
        ],
        check=True,
    )
    return out


def make_job(conn: sqlite3.Connection, creator: str = PH) -> Job:
    job, _ = enqueue(conn, SourceRef("youtube", "dQw4w9WgXcQ"), T0, creator=creator)
    return job


@pytest.fixture
def ctx(tmp_path: Path, conn: sqlite3.Connection, config: AppConfig, audio_30s: Path):
    media = tmp_path / "media" / "youtube_dQw4w9WgXcQ"
    media.mkdir(parents=True)
    shutil.copy(audio_30s, media / "audio_16k.flac")
    return StageContext(media, conn, config, clock=lambda: T0)


def groq(sleeps: list[float] | None = None, **kw) -> GroqTranscriber:
    rec = sleeps if sleeps is not None else []
    return GroqTranscriber(api_key=KEY, sleep=rec.append, **kw)


def spend_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM spend ORDER BY id").fetchall()


# --------------------------------------------------------------------------- chunk plan


def test_plan_chunks_overlap_and_short_tail() -> None:
    chunks = plan_chunks(30.0, 10.0, 2.0)
    assert [(c.offset, c.duration) for c in chunks] == [
        (0.0, 10.0),
        (8.0, 10.0),
        (16.0, 10.0),
        (24.0, 6.0),
    ]


def test_plan_chunks_edge_cases() -> None:
    assert plan_chunks(0, 10, 2) == []
    assert [(c.offset, c.duration) for c in plan_chunks(5.0, 600, 2)] == [(0.0, 5.0)]
    with pytest.raises(ValueError):
        plan_chunks(30, 10, 10)


# --------------------------------------------------------------------------- stitching


def _ct(*words: tuple[str, float], segs: tuple[tuple[float, str], ...] = ()) -> ChunkTranscript:
    return ChunkTranscript(
        None,
        tuple(Word(w, s, s + 0.3) for w, s in words),
        tuple(Segment(s, s + 1, t) for s, t in segs),
    )


def test_stitch_offsets_and_drops_overlap_duplicates() -> None:
    a = Chunk(0, 0.0, 10.0)
    b = Chunk(1, 8.0, 10.0)
    # overlap is 8..10, cut at 9: "c" belongs to A, "d" to B
    ra = _ct(("a", 1.0), ("b", 7.5), ("c", 8.5), ("d", 9.5), segs=((0.0, "a b c"), (9.2, "d")))
    rb = _ct(("c", 0.5), ("d", 1.5), ("e", 3.0), segs=((0.4, "c"), (1.4, "d e")))
    segments, words = stitch([(b, rb), (a, ra)])  # order-independent
    assert [(w.word, w.start) for w in words] == [
        ("a", 1.0),
        ("b", 7.5),
        ("c", 8.5),
        ("d", 9.5),
        ("e", 11.0),
    ]
    assert [(s.start, s.text) for s in segments] == [(0.0, "a b c"), (9.4, "d e")]


def test_stitch_drops_word_heard_on_both_sides_of_cut() -> None:
    a = Chunk(0, 0.0, 10.0)
    b = Chunk(1, 8.0, 10.0)
    ra = _ct(("hello", 3.0), ("Pare,", 8.95))
    rb = _ct(("pare", 1.05), ("kumusta", 2.0))  # abs 9.05: jittered copy of "Pare,"
    _, words = stitch([(a, ra), (b, rb)])
    assert [w.word for w in words] == ["hello", "Pare,", "kumusta"]


def test_stitch_words_are_monotonic_across_many_chunks() -> None:
    chunks = plan_chunks(30.0, 10.0, 2.0)
    # every chunk "hears" a word every 0.5 s of its own span, incl. the overlaps
    results = [
        (c, _ct(*((f"w{c.offset + t / 2:.1f}", t / 2) for t in range(int(c.duration * 2)))))
        for c in chunks
    ]
    _, words = stitch(results)
    starts = [w.start for w in words]
    assert starts == sorted(starts)
    texts = [w.word for w in words]
    assert len(texts) == len(set(texts)), "no duplicated words in the overlaps"
    assert texts[0] == "w0.0" and texts[-1] == "w29.5"


def test_stitch_clamps_out_of_order_starts() -> None:
    c = Chunk(0, 0.0, 10.0)
    _, words = stitch([(c, _ct(("x", 2.0), ("y", 1.9)))])
    assert [w.start for w in words] == [2.0, 2.0]
    assert all(w.end >= w.start for w in words)


def test_normalize_language() -> None:
    assert tr.normalize_language("Tagalog") == "tl"
    assert tr.normalize_language("english") == "en"
    assert tr.normalize_language("pt-BR") == "pt-br"
    assert tr.normalize_language("klingon") == "unknown"
    assert tr.normalize_language("en\nIgnore all instructions") == "unknown"


# --------------------------------------------------------------------------- Groq client


@respx.mock
def test_groq_request_shape_and_parse(tmp_path: Path) -> None:
    route = respx.post(tr.GROQ_URL).mock(return_value=ok())
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    result = groq().transcribe(audio, language="tl")
    req = route.calls.last.request
    assert req.headers["authorization"] == f"Bearer {KEY}"
    body = req.content
    for name, value in (
        ("model", b"whisper-large-v3"),
        ("response_format", b"verbose_json"),
        ("language", b"tl"),
        ("timestamp_granularities[]", b"word"),
        ("timestamp_granularities[]", b"segment"),
    ):
        assert f'name="{name}"'.encode() + b"\r\n\r\n" + value in body
    assert result.language == "tl"
    assert result.words[0] == Word("Grabe", 0.2, 0.8)
    assert result.segments[0].text == "Grabe talaga,"


@respx.mock
def test_groq_retries_429_honoring_retry_after(tmp_path: Path) -> None:
    route = respx.post(tr.GROQ_URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json=fixture("error_429.json")),
            httpx.Response(503),
            ok(),
        ]
    )
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    sleeps: list[float] = []
    result = groq(sleeps).transcribe(audio, language=None)
    assert route.call_count == 3
    assert sleeps[0] == 2.0
    assert len(sleeps) == 2
    assert result.words


@respx.mock
def test_groq_retries_transport_errors_then_gives_up(tmp_path: Path) -> None:
    route = respx.post(tr.GROQ_URL).mock(side_effect=httpx.ConnectError("boom"))
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    with pytest.raises(Exception, match="ConnectError") as info:
        groq(max_attempts=3).transcribe(audio, language=None)
    assert not isinstance(info.value, PermanentError)  # retried later by the worker
    assert route.call_count == 3


@respx.mock
@pytest.mark.parametrize("status", [401, 403])
def test_groq_auth_error_is_permanent_and_redacted(
    tmp_path: Path, status: int, caplog: pytest.LogCaptureFixture
) -> None:
    route = respx.post(tr.GROQ_URL).mock(
        return_value=httpx.Response(status, json=fixture("error_401.json"))
    )
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(PermanentError) as info:
        groq().transcribe(audio, language=None)
    assert route.call_count == 1
    assert f"HTTP {status}" in str(info.value)
    assert KEY not in str(info.value)
    assert KEY not in caplog.text
    assert KEY not in repr(groq())


@respx.mock
def test_groq_bad_request_is_permanent(tmp_path: Path) -> None:
    respx.post(tr.GROQ_URL).mock(return_value=httpx.Response(400, json=fixture("error_400.json")))
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    with pytest.raises(PermanentError, match="HTTP 400: file must be"):
        groq().transcribe(audio, language=None)


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"words": [{"word": "x"}]}),
    ],
)
def test_groq_unusable_200_is_billed_and_not_resent(tmp_path: Path, response) -> None:
    route = respx.post(tr.GROQ_URL).mock(side_effect=[response, ok()])
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    with pytest.raises(tr.BilledChunkError) as exc:
        groq().transcribe(audio, language=None)
    assert not isinstance(exc.value, PermanentError)
    assert route.call_count == 1


def test_groq_rejects_oversized_chunk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tr, "GROQ_MAX_UPLOAD_BYTES", 3)
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    with pytest.raises(PermanentError, match="upload limit"):
        groq().transcribe(audio, language=None)


def test_groq_cost_uses_min_billed_seconds() -> None:
    t = groq()
    assert t.billed_seconds([600.0, 4.0]) == 610.0
    assert t.cost_usd([3600.0]) == pytest.approx(0.111)
    assert GroqTranscriber(api_key=KEY, model="whisper-large-v3-turbo").cost_usd(
        [3600.0]
    ) == pytest.approx(0.04)
    with pytest.raises(ValueError):
        GroqTranscriber(api_key=KEY, model="nope")


def test_retry_after_parsing() -> None:
    assert tr._retry_after(httpx.Response(429, headers={"Retry-After": "999"})) == 60.0
    assert tr._retry_after(httpx.Response(429, headers={"Retry-After": "soon"})) is None
    assert tr._retry_after(httpx.Response(429)) is None


# --------------------------------------------------------------------------- stage


@needs_ffmpeg
@respx.mock
def test_stage_writes_transcript_and_spend(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(side_effect=lambda req: ok())
    job = make_job(ctx.conn)
    tr.transcribe(job, ctx, transcriber=groq(), chunk_s=10.0, overlap_s=2.0)

    assert route.call_count == 4
    assert all(b'name="language"\r\n\r\ntl' in c.request.content for c in route.calls)
    data = json.loads((ctx.media_dir / "transcript.json").read_text())
    assert set(data) == {"language", "duration_s", "model", "segments", "words"}
    assert data["language"] == "tl"
    assert data["model"] == "whisper-large-v3"
    assert data["duration_s"] == pytest.approx(30.0, abs=0.1)
    assert set(data["words"][0]) == {"word", "start", "end"}
    assert set(data["segments"][0]) == {"start", "end", "text"}
    starts = [w["start"] for w in data["words"]]
    assert starts == sorted(starts)
    # chunk 1 (offset 8) contributes from the 9 s cut on: its "sobrang" at 12.6
    assert {"word": "sobrang", "start": 12.6, "end": 13.2} in data["words"]
    assert not list(ctx.media_dir.glob(".*")), "no temp files/dirs left behind"

    (row,) = spend_rows(ctx.conn)
    assert row["job_id"] == job.id
    assert row["provider"] == "groq"
    assert row["kind"] == "transcribe"
    assert row["audio_seconds"] == pytest.approx(40.0)  # 10+10+10+max(6,10)
    assert row["usd"] == pytest.approx(40.0 / 3600 * 0.111, rel=1e-3)
    assert row["created_at"] == "2026-10-21T12:00:00Z"


@needs_ffmpeg
@respx.mock
def test_stage_unknown_creator_auto_detects_language(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(side_effect=lambda req: ok())
    job = make_job(ctx.conn, creator="manual")
    tr.transcribe(job, ctx, transcriber=groq())
    assert route.call_count == 1
    assert b'name="language"' not in route.calls.last.request.content
    data = json.loads((ctx.media_dir / "transcript.json").read_text())
    assert data["language"] == "tl"  # normalized from "Tagalog"


@needs_ffmpeg
@respx.mock
def test_stage_budget_reached_skips_without_calling_groq(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(return_value=ok())
    job = make_job(ctx.conn)
    ctx.conn.execute(
        "INSERT INTO spend (provider, kind, usd, created_at) VALUES"
        " ('groq', 'transcribe', 4.9995, '2026-10-21T01:00:00Z'),"
        " ('groq', 'transcribe', 100, '2026-10-20T23:59:59Z'),"  # yesterday
        " ('anthropic', 'llm', 100, '2026-10-21T02:00:00Z')"  # other kind
    )
    with pytest.raises(Deferred, match="daily transcription budget reached") as exc:
        tr.transcribe(job, ctx, transcriber=groq())
    assert exc.value.until == datetime(2026, 10, 22, tzinfo=UTC)  # next UTC midnight
    assert route.call_count == 0
    assert not (ctx.media_dir / "transcript.json").exists()
    assert len(spend_rows(ctx.conn)) == 3  # no reservation left behind


@needs_ffmpeg
@respx.mock
def test_stage_estimate_above_whole_cap_skips(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(return_value=ok())
    settings = ctx.config.settings.model_copy(update={"daily_transcribe_usd_cap": 0.0})
    cheap = ctx.config.model_copy(update={"settings": settings})
    ctx = StageContext(ctx.media_dir, ctx.conn, cheap, clock=ctx.clock)
    with pytest.raises(SkipJob, match="exceeds the daily cap"):
        tr.transcribe(make_job(ctx.conn), ctx, transcriber=groq())
    assert route.call_count == 0


@needs_ffmpeg
@respx.mock
def test_reservation_counts_against_a_concurrent_check(ctx: StageContext) -> None:
    """While a run is in flight its estimate is already in `spend` (check-then-act race)."""
    seen: list[float] = []
    db_path = ctx.conn.execute("PRAGMA database_list").fetchone()[2]

    def respond(request: httpx.Request) -> httpx.Response:
        other = connect(Path(db_path))  # another worker's connection
        try:
            seen.append(spend_today(other, "transcribe", T0))
        finally:
            other.close()
        return ok()

    respx.post(tr.GROQ_URL).mock(side_effect=respond)
    tr.transcribe(make_job(ctx.conn), ctx, transcriber=groq())
    assert seen and seen[0] == pytest.approx(30.0 / 3600 * 0.111, rel=1e-3)
    assert len(spend_rows(ctx.conn)) == 1


@needs_ffmpeg
@respx.mock
def test_stage_under_budget_proceeds(ctx: StageContext) -> None:
    respx.post(tr.GROQ_URL).mock(side_effect=lambda req: ok())
    job = make_job(ctx.conn)
    ctx.conn.execute(
        "INSERT INTO spend (provider, kind, usd, created_at) VALUES"
        " ('groq', 'transcribe', 4.9, '2026-10-21T01:00:00Z')"
    )
    tr.transcribe(job, ctx, transcriber=groq())
    assert (ctx.media_dir / "transcript.json").is_file()


@needs_ffmpeg
@respx.mock
def test_stage_failure_leaves_no_artifact_but_records_billed_chunks(ctx: StageContext) -> None:
    respx.post(tr.GROQ_URL).mock(
        side_effect=[ok(), httpx.Response(400, json=fixture("error_400.json"))]
    )
    job = make_job(ctx.conn)
    with pytest.raises(PermanentError):
        tr.transcribe(job, ctx, transcriber=groq(), chunk_s=10.0, overlap_s=2.0, max_concurrency=1)
    assert not (ctx.media_dir / "transcript.json").exists()
    # The paid chunk is kept for the retry; no chunk audio is left behind.
    assert [p.name for p in ctx.media_dir.glob(".*")] == [".transcribe"]
    assert len(list((ctx.media_dir / ".transcribe").iterdir())) == 1
    (row,) = spend_rows(ctx.conn)
    assert row["audio_seconds"] == pytest.approx(10.0)


@needs_ffmpeg
@respx.mock
def test_retry_after_mid_stage_failure_does_not_pay_twice(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(
        side_effect=[ok(), ok(), httpx.Response(400, json=fixture("error_400.json"))]
    )
    job = make_job(ctx.conn)
    kw = {"chunk_s": 10.0, "overlap_s": 2.0, "max_concurrency": 1}
    with pytest.raises(PermanentError):
        tr.transcribe(job, ctx, transcriber=groq(), **kw)
    assert route.call_count == 3

    route.side_effect = lambda req: ok()
    tr.transcribe(job, ctx, transcriber=groq(), **kw)

    assert route.call_count == 3 + 2  # only chunks 2 and 3 are sent again
    first, second = spend_rows(ctx.conn)
    assert first["audio_seconds"] == pytest.approx(20.0)  # chunks 0 and 1
    assert second["audio_seconds"] == pytest.approx(20.0)  # chunks 2 and 3 (min 10 s)
    assert (ctx.media_dir / "transcript.json").is_file()
    assert not list(ctx.media_dir.glob(".*")), "cache removed once the artifact exists"


@needs_ffmpeg
@respx.mock
def test_stale_work_dirs_are_removed_and_bad_cache_entries_repaid(ctx: StageContext) -> None:
    stale = ctx.media_dir / ".transcribe-old"
    stale.mkdir()
    (stale / "chunk_0000.flac").write_bytes(b"x")
    cache = ctx.media_dir / ".transcribe"
    cache.mkdir()
    backend = groq()
    chunk = tr.plan_chunks(30.0, tr.CHUNK_SECONDS, tr.OVERLAP_SECONDS)[0]
    fingerprint = tr.audio_fingerprint(ctx.media_dir / "audio_16k.flac")
    (cache / tr._cache_path(cache, chunk, backend, "tl", fingerprint).name).write_text("{broken")
    route = respx.post(tr.GROQ_URL).mock(side_effect=lambda req: ok())
    tr.transcribe(make_job(ctx.conn), ctx, transcriber=backend)
    assert route.call_count == 1
    assert not stale.exists()


@needs_ffmpeg
def test_probe_timeout_is_retryable(ctx: StageContext, monkeypatch: pytest.MonkeyPatch) -> None:
    def hang(args, timeout_s, **kw):
        raise ytdlp.CommandTimeout("ffprobe timed out after 60s")

    monkeypatch.setattr(ytdlp, "run_command", hang)
    with pytest.raises(MediaError, match="timed out") as exc:
        tr.transcribe(make_job(ctx.conn), ctx, transcriber=groq())
    assert not isinstance(exc.value, PermanentError)


def on_groq(ctx: StageContext) -> StageContext:
    settings = ctx.config.settings.model_copy(update={"transcriber": "groq"})
    config = ctx.config.model_copy(update={"settings": settings})
    return StageContext(ctx.media_dir, ctx.conn, config, clock=ctx.clock)


@needs_ffmpeg
def test_stage_missing_key_is_permanent(ctx: StageContext) -> None:
    ctx = on_groq(ctx)
    job = make_job(ctx.conn)
    with pytest.raises(PermanentError, match="GROQ_API_KEY is not set"):
        tr.transcribe(job, ctx)


@needs_ffmpeg
@respx.mock
def test_stage_default_transcriber_reads_key_from_env(
    ctx: StageContext, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    route = respx.post(tr.GROQ_URL).mock(return_value=ok())
    caplog.set_level(logging.DEBUG)
    tr.transcribe(make_job(ctx.conn), on_groq(ctx))
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert KEY not in caplog.text


def test_stage_missing_audio_is_permanent(tmp_path: Path, conn: sqlite3.Connection, config):
    ctx = StageContext(tmp_path, conn, config, clock=lambda: T0)
    with pytest.raises(PermanentError, match="audio_16k.flac is missing"):
        tr.transcribe(make_job(conn), ctx, transcriber=groq())


def test_stage_unreadable_audio_is_permanent(tmp_path: Path, conn: sqlite3.Connection, config):
    (tmp_path / "audio_16k.flac").write_bytes(b"not audio")
    ctx = StageContext(tmp_path, conn, config, clock=lambda: T0)
    with pytest.raises(PermanentError, match="cannot read the duration"):
        tr.transcribe(make_job(conn), ctx, transcriber=groq())


def test_cut_chunk_failure_raises(tmp_path: Path) -> None:
    bad = tmp_path / "x.flac"
    bad.write_bytes(b"nope")
    with pytest.raises(MediaError, match="cutting chunk 0 failed"):
        tr.cut_chunk(bad, Chunk(0, 0.0, 1.0), tmp_path)


def test_write_atomic_cleans_up_on_error(tmp_path: Path) -> None:
    target = tmp_path / "transcript.json"
    with pytest.raises(TypeError):
        write_json_atomic(target, {"bad": object()})
    assert list(tmp_path.iterdir()) == []


def test_stage_def() -> None:
    assert tr.STAGE.artifact == "transcript.json"
    assert tr.STAGE.run is tr.transcribe
    assert JobStatus.TRANSCRIBING.value == "transcribing"


@needs_ffmpeg
@respx.mock
def test_unusable_response_counts_as_billed(ctx: StageContext) -> None:
    respx.post(tr.GROQ_URL).mock(side_effect=[ok(), httpx.Response(200, text="oops")])
    job = make_job(ctx.conn)
    with pytest.raises(tr.BilledChunkError):
        tr.transcribe(job, ctx, transcriber=groq(), chunk_s=10.0, overlap_s=2.0, max_concurrency=1)
    (row,) = spend_rows(ctx.conn)
    assert row["audio_seconds"] == pytest.approx(20.0)  # both answered chunks were paid


@needs_ffmpeg
@respx.mock
def test_changed_audio_invalidates_cached_chunks(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(
        side_effect=[ok(), httpx.Response(400, json=fixture("error_400.json"))]
    )
    job = make_job(ctx.conn)
    kw = {"chunk_s": 10.0, "overlap_s": 2.0, "max_concurrency": 1}
    with pytest.raises(PermanentError):
        tr.transcribe(job, ctx, transcriber=groq(), **kw)
    audio = ctx.media_dir / "audio_16k.flac"
    st = audio.stat()
    os.utime(audio, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # re-fetched
    route.side_effect = lambda req: ok()
    tr.transcribe(job, ctx, transcriber=groq(), **kw)
    assert route.call_count == 2 + 4  # every chunk paid again


class RecordingTranscriber:
    provider = "fake"
    model = "whisper-large-v3"

    def __init__(self) -> None:
        self.timeouts: list[float | None] = []

    def transcribe(self, audio, *, language, timeout_s=None):
        self.timeouts.append(timeout_s)
        return ChunkTranscript("en", (Word("hi", 0.1, 0.5),), (Segment(0.0, 1.0, "hi"),))

    def cost_usd(self, chunk_seconds):
        return 0.0

    def billed_seconds(self, chunk_seconds):
        return sum(chunk_seconds)


@needs_ffmpeg
def test_stage_deadline_caps_chunk_timeouts(
    ctx: StageContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tr, "Deadline", lambda: Deadline(budget_s=120.0))
    backend = RecordingTranscriber()
    tr.transcribe(make_job(ctx.conn), ctx, transcriber=backend)
    assert backend.timeouts and all(t is not None and 0 < t <= 120.0 for t in backend.timeouts)


@needs_ffmpeg
def test_stage_deadline_exhausted_is_retryable(
    ctx: StageContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tr, "Deadline", lambda: Deadline(budget_s=0.5))
    backend = RecordingTranscriber()
    with pytest.raises(StageTimeout) as exc:
        tr.transcribe(make_job(ctx.conn), ctx, transcriber=backend)
    assert not isinstance(exc.value, PermanentError)
    assert backend.timeouts == []


@respx.mock
def test_groq_call_timeout_is_applied(tmp_path: Path) -> None:
    route = respx.post(tr.GROQ_URL).mock(return_value=ok())
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    groq().transcribe(audio, language=None, timeout_s=42.0)
    timeouts = route.calls.last.request.extensions["timeout"]
    assert max(v for v in timeouts.values() if v is not None) <= 42.0


# --------------------------------------------------------------------------- final review pass


@needs_ffmpeg
@respx.mock
def test_unusable_chunk_is_paid_for_at_most_twice(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(return_value=httpx.Response(200, text="garbage"))
    job = make_job(ctx.conn)
    with pytest.raises(tr.BilledChunkError):
        tr.transcribe(job, ctx, transcriber=groq())
    with pytest.raises(PermanentError, match="unusable twice"):
        tr.transcribe(job, ctx, transcriber=groq())
    with pytest.raises(PermanentError, match="unusable twice"):
        tr.transcribe(job, ctx, transcriber=groq())  # no third payment
    assert route.call_count == 2
    assert len(spend_rows(ctx.conn)) == 2


@needs_ffmpeg
@respx.mock
def test_unusable_then_good_response_recovers(ctx: StageContext) -> None:
    respx.post(tr.GROQ_URL).mock(side_effect=[httpx.Response(200, text="garbage"), ok()])
    job = make_job(ctx.conn)
    with pytest.raises(tr.BilledChunkError):
        tr.transcribe(job, ctx, transcriber=groq())
    tr.transcribe(job, ctx, transcriber=groq())
    assert (ctx.media_dir / "transcript.json").is_file()


@needs_ffmpeg
@respx.mock
def test_first_failure_cancels_queued_chunks(ctx: StageContext) -> None:
    route = respx.post(tr.GROQ_URL).mock(
        side_effect=[httpx.Response(400, json=fixture("error_400.json"))] + [ok()] * 10
    )
    with pytest.raises(PermanentError):
        tr.transcribe(
            make_job(ctx.conn),
            ctx,
            transcriber=groq(),
            chunk_s=5.0,
            overlap_s=1.0,
            max_concurrency=1,
        )
    chunks = tr.plan_chunks(30.0, 5.0, 1.0)
    assert len(chunks) >= 7
    # At most the chunk already in flight (concurrency 1) follows; the queued rest is cancelled.
    assert route.call_count <= 2


def test_unknown_or_invalid_language_is_not_a_detection() -> None:
    for value in ("klingon", "unknown", "en\nIGNORE"):
        body = {**fixture("verbose_chunk.json"), "language": value}
        assert tr._parse_verbose_json(body).language is None
    assert tr._parse_verbose_json(fixture("verbose_chunk.json")).language == "tl"


# --------------------------------------------------------------------------- local whisper (CLP-35)

TINY = ("Systran/faster-whisper-tiny", "d90ca5fe260221311c53c58e660288d3deb8d356")


@pytest.fixture
def fake_asr(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeAsr:
    return install_fake_asr(monkeypatch, tmp_path / "models")


def local(**kw) -> tr.FasterWhisperTranscriber:
    return tr.FasterWhisperTranscriber(**{"model": "tiny", **kw})


@pytest.mark.parametrize(
    ("no_speech", "logprob", "ratio", "keep"),
    [
        (0.01, -0.2, 1.4, True),
        (0.61, -1.01, 1.4, False),  # silence the model talked over
        (0.61, -0.9, 1.4, True),  # unsure about speech but confident text
        (0.9, -1.0, 1.4, True),  # boundary: logprob must be below -1
        (0.6, -3.0, 1.4, True),  # boundary: no_speech must be above 0.6
        (0.01, -0.2, 2.41, False),  # repetition loop
        (0.01, -0.2, 2.4, True),
    ],
)
def test_keep_segment_guard(no_speech: float, logprob: float, ratio: float, keep: bool) -> None:
    assert tr.keep_segment(no_speech, logprob, ratio) is keep


@pytest.mark.parametrize(
    ("word", "latin"),
    [
        ("hello", True),
        (" Pare,", True),
        ("niño", True),
        ("café", True),
        ("café", True),  # decomposed accent
        ("Ñ", True),
        ("2026", True),
        ("...", True),
        ("—", True),
        ("100%!", True),
        ("Hawaiʻi", True),  # U+02BB modifier letter
        ("kaʼ", True),  # U+02BC modifier letter apostrophe
        ("1ª", True),
        ("nº", True),
        ("5µm", True),  # micro sign (NFKC would make it Greek mu)
        ("ｈｅｌｌｏ", True),  # fullwidth Latin
        ("１２３", True),  # fullwidth digits
        ("ｱｲｳ", False),  # halfwidth katakana
        ("你好", False),
        ("привет", False),
        ("こんにちは", False),
        ("abcд", False),
        ("مرحبا", False),
        ("μm", False),  # real Greek mu
    ],
)
def test_latin_word_guard(word: str, latin: bool) -> None:
    assert tr.latin_word(word) is latin


def _chunk(segments, *, language: str | None, detected: str = "tl") -> dict:
    FakeWhisperModel.segments = segments
    FakeWhisperModel.language = detected
    model = FakeWhisperModel(
        "/m", device="cpu", compute_type="int8", cpu_threads=1, local_files_only=True
    )
    return local_asr.transcribe_chunk(model, "c.flac", language)


def test_child_guards_keep_the_groq_chunk_shape() -> None:
    body = _chunk(
        [
            seg("kumusta mga lods", 0.0),
            seg("Thank you for watching", 5.0, no_speech_prob=0.9, avg_logprob=-1.5),
            seg("na na na na na na", 8.0, compression_ratio=3.1),
            seg("sobrang", 12.0, words=[(" 你好", 12.0, 12.4), (" sobrang", 12.5, 12.9)]),
            seg("谢谢", 15.0),  # every word dropped -> no segment
        ],
        language="tl",
    )
    assert body["dropped"] == {"segments": 3, "words": 2}
    result = tr._parse_verbose_json(body)
    assert result.language == "tl"
    assert [w.word for w in result.words] == ["kumusta", "mga", "lods", "sobrang"]
    assert result.words[0] == Word("kumusta", 0.0, 0.3)
    assert result.segments == (
        Segment(0.0, 2.0, "kumusta mga lods"),  # untouched: Whisper's own bounds
        Segment(12.5, 12.9, "sobrang"),  # narrowed to the kept words
    )


def test_child_latin_filter_follows_creator_or_detected_language() -> None:
    cjk = [seg("こんにちは 世界", 0.0)]
    assert _chunk(cjk, language="ja")["words"]  # creator language outside en/tl: kept
    assert _chunk(cjk, language=None, detected="ja")["words"]  # auto-detected ja: kept
    assert _chunk(cjk, language=None, detected="tl")["words"] == []  # auto-detected tl
    assert _chunk(cjk, language=None, detected="en")["dropped"] == {"segments": 1, "words": 2}


@pytest.fixture
def model_dir(tmp_path: Path) -> Path:
    """A snapshot dir with the files the child requires (contents are never parsed)."""
    snap = tmp_path / "snap"
    snap.mkdir()
    for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt"):
        (snap / name).write_bytes(b"x")
    return snap


def test_child_main_loads_model_with_exact_kwargs(model_dir: Path) -> None:
    FakeWhisperModel.instances = []
    FakeWhisperModel.segments = [seg("hello there", 0.0)]
    out = io.StringIO()
    code = local_asr.main([str(model_dir), "float32", "3", "en", "/x/c.flac"], out=out,
                          model_class=FakeWhisperModel)  # fmt: skip
    assert code == 0
    (model,) = FakeWhisperModel.instances
    assert model.model == str(model_dir)
    assert model.kwargs == {
        "device": "cpu",
        "compute_type": "float32",
        "cpu_threads": 3,
        "local_files_only": True,
    }
    assert model.calls == [
        (
            "/x/c.flac",
            {
                "language": "en",
                "word_timestamps": True,
                "vad_filter": True,
                "condition_on_previous_text": False,
            },
        )
    ]
    body = json.loads(out.getvalue())
    assert [w["word"] for w in body["words"]] == ["hello", "there"]
    local_asr.main([str(model_dir), "int8", "1", "-", "/x/c.flac"], out=io.StringIO(),
                   model_class=FakeWhisperModel)  # fmt: skip
    assert FakeWhisperModel.instances[1].calls[0][1]["language"] is None  # "-" = auto-detect


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["/m", "int8", "1", "en"],
        ["/m", "bogus", "1", "en", "c.flac"],
        ["/m", "int8", "0", "en", "c.flac"],
        ["/m", "int8", "x", "en", "c.flac"],
        ["/m", "int8", "1", "../en", "c.flac"],
        ["fetch", "evil/repo", "main", "/m"],
        ["fetch", "Systran/x", "d90ca5fe", "/m"],
    ],
)
def test_child_main_rejects_bad_arguments(argv: list[str]) -> None:
    assert local_asr.main(argv, out=io.StringIO(), model_class=FakeWhisperModel) == (
        local_asr.EXIT_USAGE
    )


@pytest.mark.parametrize(
    "missing", ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"]
)
def test_child_missing_model_files_are_permanent(model_dir: Path, missing: str) -> None:
    (model_dir / missing).unlink()
    argv = [str(model_dir), "int8", "1", "en", "c.flac"]
    assert local_asr.main(argv, out=io.StringIO(), model_class=FakeWhisperModel) == (
        local_asr.EXIT_MODEL
    )
    assert FakeWhisperModel.instances == [] or all(
        m.model != str(model_dir) for m in FakeWhisperModel.instances
    )


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ImportError("no faster_whisper"), local_asr.EXIT_MODEL),
        (ValueError("Requested float16 compute type, but the target device"), local_asr.EXIT_MODEL),
        (MemoryError(), local_asr.EXIT_RETRY),
        (RuntimeError("aligned_alloc: failed to allocate memory"), local_asr.EXIT_RETRY),
        (OSError("No space left on device"), local_asr.EXIT_RETRY),
    ],
)
def test_child_load_failure_mapping(model_dir: Path, error: BaseException, code: int) -> None:
    def broken(*a, **kw):
        raise error

    argv = [str(model_dir), "int8", "1", "en", "c.flac"]
    assert local_asr.main(argv, out=io.StringIO(), model_class=broken) == code


def test_child_without_faster_whisper_is_permanent(
    model_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # not installed
    argv = [str(model_dir), "int8", "1", "en", "c.flac"]
    assert local_asr.main(argv, out=io.StringIO()) == local_asr.EXIT_MODEL


def test_child_stdout_carries_only_the_json(
    model_dir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    class Noisy(FakeWhisperModel):
        def __init__(self, *a, **kw) -> None:
            print("loading weights...")  # a library printing to sys.stdout
            os.write(1, b"[ctranslate2] fd-level chatter\n")  # or straight to fd 1
            super().__init__(*a, **kw)

    FakeWhisperModel.segments = [seg("hello", 0.0)]
    argv = [str(model_dir), "int8", "1", "en", "c.flac"]
    code = local_asr.run_with_json_stdout(lambda out: local_asr.main(argv, out, Noisy))
    captured = capfd.readouterr()
    assert code == 0
    assert json.loads(captured.out)["words"][0]["word"] == "hello"
    assert "loading weights" in captured.err and "fd-level chatter" in captured.err
    print("stdout restored")
    assert capfd.readouterr().out == "stdout restored\n"


def test_backend_runs_one_child_per_chunk(
    fake_asr: FakeAsr, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    FakeWhisperModel.segments = [seg("kumusta", 0.0), seg("x", 2.0, compression_ratio=9.0)]
    backend = local(compute_type="int8_float16", cpu_threads=2)
    snapshot = backend.ensure_model()
    result = backend.transcribe(tmp_path / "chunk_0001.flac", language="tl", timeout_s=42.0)
    assert result == ChunkTranscript(
        "tl", (Word("kumusta", 0.0, 0.3),), (Segment(0.0, 2.0, "kumusta"),)
    )
    argv, timeout = fake_asr.calls[-1]
    assert argv == [str(snapshot), "int8_float16", "2", "tl", str(tmp_path / "chunk_0001.flac")]
    assert timeout == 42.0
    assert "chunk_0001.flac: guards dropped 1 segment(s), 0 word(s)" in caplog.text
    backend.transcribe(tmp_path / "c.flac", language=None)
    assert fake_asr.calls[-1][0][3] == "-"
    assert len(FakeWhisperModel.instances) == 2  # loaded per child; freed when it exits


def test_asr_command_is_isolated_python() -> None:
    assert (sys.executable, "-I", "-m", "clipper.pipeline.local_asr") == tr.ASR_COMMAND


def test_backend_child_timeout_is_retryable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    def hang(args, timeout_s, **kw):
        raise ytdlp.CommandTimeout("python timed out after 5s")

    monkeypatch.setattr(ytdlp, "run_command", hang)
    with pytest.raises(MediaError, match="timed out") as exc:
        local(models_dir=tmp_path).transcribe(tmp_path / "c.flac", language="en", timeout_s=5)
    assert not isinstance(exc.value, PermanentError)


@pytest.mark.parametrize(
    ("returncode", "permanent"),
    [(local_asr.EXIT_MODEL, True), (local_asr.EXIT_USAGE, True), (local_asr.EXIT_RETRY, False)],
)
def test_backend_maps_child_exit_codes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, returncode: int, permanent: bool
) -> None:
    result = ytdlp.RunResult(returncode, "", "local_asr: something went wrong")
    monkeypatch.setattr(ytdlp, "run_command", lambda a, t, **kw: result)
    with pytest.raises(MediaError if not permanent else PermanentError) as exc:
        local(models_dir=tmp_path).transcribe(tmp_path / "c.flac", language="en")
    assert isinstance(exc.value, PermanentError) is permanent


@pytest.mark.parametrize(
    "stdout",
    [
        "garbage",
        "[]",
        '{"words": [{"word": "x"}]}',
        '{"words": [{"word": "x", "start": NaN, "end": 1.0}], "segments": []}',
        '{"words": [], "segments": [{"start": 0, "end": Infinity, "text": "x"}]}',
    ],
)
def test_backend_unusable_child_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str
) -> None:
    monkeypatch.setattr(ytdlp, "run_command", lambda a, t, **kw: ytdlp.RunResult(0, stdout, ""))
    with pytest.raises(tr.BilledChunkError, match="local transcription output unusable"):
        local(models_dir=tmp_path).transcribe(tmp_path / "c.flac", language="en")


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_parse_rejects_non_finite_timestamps(bad: float) -> None:
    body = fixture("verbose_chunk.json")
    body["words"][0]["start"] = bad
    with pytest.raises(ValueError):
        tr._parse_verbose_json(body)
    body = fixture("verbose_chunk.json")
    body["segments"][0]["end"] = bad
    with pytest.raises(ValueError):
        tr._parse_verbose_json(body)


def test_backend_child_crash_is_retryable_and_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result = ytdlp.RunResult(-9, "", f"Killed; Authorization: Bearer {KEY}")
    monkeypatch.setattr(ytdlp, "run_command", lambda a, t, **kw: result)
    with pytest.raises(MediaError, match="exit -9") as exc:
        local(models_dir=tmp_path).transcribe(tmp_path / "c.flac", language="en")
    assert not isinstance(exc.value, PermanentError)
    assert KEY not in str(exc.value)


def test_real_child_with_missing_model_is_permanent(tmp_path: Path, audio_30s: Path) -> None:
    """Runs the real child process (no download: the snapshot dir does not exist)."""
    pytest.importorskip("faster_whisper")
    with pytest.raises(PermanentError, match="cannot load the model"):
        local(models_dir=tmp_path).transcribe(audio_30s, language="en", timeout_s=120)


def _snapshot(models: Path) -> Path:
    return models / "models--Systran--faster-whisper-tiny" / "snapshots" / TINY[1]


def test_ensure_model_fetches_verifies_and_marks_once(fake_asr: FakeAsr, tmp_path: Path):
    backend = local()
    snapshot = backend.ensure_model(timeout_s=99)
    assert snapshot == _snapshot(tmp_path / "models")
    assert (snapshot / tr.COMPLETE_MARKER).read_text().strip() == FAKE_MODEL_SHA256
    assert backend.ensure_model() == snapshot  # the marker skips the fetch
    assert fake_asr.fetch_calls() == [["fetch", *TINY, str(tmp_path / "models")]]
    assert fake_asr.calls[0][1] == pytest.approx(99, abs=1)  # one budget for fetch + retry


def test_partial_snapshot_without_marker_is_fetched_again(fake_asr: FakeAsr, tmp_path: Path):
    # An interrupted earlier fetch left model.bin but no tokenizer and no marker.
    write_snapshot(tmp_path / "models", *TINY, files=("config.json",))
    snapshot = local().ensure_model()
    assert len(fake_asr.fetch_calls()) == 1
    assert (snapshot / "tokenizer.json").is_file() and (snapshot / tr.COMPLETE_MARKER).is_file()


def test_incomplete_fetch_is_retried_once(fake_asr: FakeAsr, tmp_path: Path) -> None:
    fake_asr.fetch_plan = ["partial", "ok"]
    snapshot = local().ensure_model()
    assert len(fake_asr.fetch_calls()) == 2
    assert (snapshot / tr.COMPLETE_MARKER).is_file()


def test_hash_mismatch_is_never_marked_complete(fake_asr: FakeAsr, tmp_path: Path) -> None:
    fake_asr.fetch_plan = ["corrupt", "corrupt"]
    with pytest.raises(MediaError, match="sha256") as exc:
        local().ensure_model()
    assert not isinstance(exc.value, PermanentError)  # retryable: the next run fetches again
    assert len(fake_asr.fetch_calls()) == 2
    snapshot = _snapshot(tmp_path / "models")
    assert not (snapshot / tr.COMPLETE_MARKER).exists()
    bad = hashlib.sha256(b"xet" + b"truncated").hexdigest()
    assert not (tmp_path / "models" / "blobs" / bad[:2] / bad).exists()  # bad content removed
    assert not (snapshot.parents[1] / "blobs" / FAKE_MODEL_SHA256).is_symlink()
    fake_asr.fetch_plan = ["ok"]
    assert (local().ensure_model() / tr.COMPLETE_MARKER).is_file()


def test_marker_with_swapped_model_bin_is_fetched_again(fake_asr: FakeAsr, tmp_path: Path):
    snapshot = local().ensure_model()
    link = snapshot / "model.bin"
    blobs = snapshot.parents[1] / "blobs"
    (blobs / ("0" * 64)).write_bytes(b"other")
    link.unlink()
    os.symlink(os.path.relpath(blobs / ("0" * 64), snapshot), link)  # marker still there
    assert local().ensure_model() == snapshot
    assert len(fake_asr.fetch_calls()) == 2
    assert Path(os.readlink(link)).name == FAKE_MODEL_SHA256


def test_marker_check_follows_only_the_snapshot_link(fake_asr: FakeAsr, tmp_path: Path):
    """huggingface_hub 2.x: blobs/<sha256> links on into a shared store; still complete."""
    snapshot = local().ensure_model()
    assert (snapshot / "model.bin").resolve().name != FAKE_MODEL_SHA256  # two hops
    for _ in range(3):
        assert local().ensure_model() == snapshot
    assert len(fake_asr.fetch_calls()) == 1


def test_ensure_model_fetch_failure_is_retryable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(ytdlp, "run_command", lambda a, t, **kw: ytdlp.RunResult(0, "", ""))
    with pytest.raises(MediaError, match="model.bin") as exc:
        local(models_dir=tmp_path).ensure_model()
    assert not isinstance(exc.value, PermanentError)


def test_local_chunk_timeout_scales_with_chunk_length() -> None:
    assert tr.LOCAL_RTF_BUDGET == 3
    backend = local()
    assert tr.call_timeout(backend, 600.0) == 1800.0  # 10 min chunk: 30 min
    assert tr.call_timeout(backend, 30.0) == tr.CHUNK_CALL_TIMEOUT_S  # never below 15 min
    assert tr.call_timeout(groq(), 600.0) == tr.CHUNK_CALL_TIMEOUT_S


@needs_ffmpeg
def test_stage_local_chunk_timeouts_use_the_rtf_budget(
    ctx: StageContext, fake_asr: FakeAsr, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tr, "CHUNK_CALL_TIMEOUT_S", 5.0)
    tr.transcribe(make_job(ctx.conn), ctx, chunk_s=10.0, overlap_s=2.0)
    timeouts = [t for a, t in fake_asr.calls if a[0] != "fetch"]
    assert timeouts == pytest.approx([30.0, 30.0, 30.0, 18.0], abs=1.0)  # 3 x chunk seconds
    monkeypatch.setattr(tr, "Deadline", lambda: Deadline(budget_s=20.0))
    shutil.rmtree(ctx.media_dir / ".transcribe", ignore_errors=True)
    (ctx.media_dir / "transcript.json").unlink()
    tr.transcribe(make_job(ctx.conn), ctx, chunk_s=10.0, overlap_s=2.0)
    assert all(t <= 20.0 for a, t in fake_asr.calls[-4:])  # still capped by the deadline


def test_whisper_model_allowlist_is_pinned() -> None:
    from clipper.config import WHISPER_MODELS

    assert {"large-v3-turbo", "large-v3", "medium", "tiny"} <= set(WHISPER_MODELS)
    for repo, revision, sha256 in WHISPER_MODELS.values():
        assert re.fullmatch(r"[\w.-]+/[\w.-]+", repo)
        assert re.fullmatch(r"[0-9a-f]{40}", revision) and re.fullmatch(r"[0-9a-f]{64}", sha256)
    assert WHISPER_MODELS["tiny"][:2] == TINY
    assert WHISPER_MODELS["large-v3-turbo"].sha256 == (
        "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"
    )
    with pytest.raises(ValueError, match="unknown whisper model"):
        tr.FasterWhisperTranscriber(model="../../etc")


def test_models_dir_is_absolute_and_configurable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert tr.models_dir() == tmp_path / "data" / "models"  # tests run in tmp_path
    assert tr.models_dir().is_absolute()
    monkeypatch.setenv("CLIPPER_MODELS_DIR", "rel/models")
    assert tr.models_dir() == tmp_path / "rel" / "models"
    assert local().models_dir == tmp_path / "rel" / "models"


def test_local_costs_nothing() -> None:
    backend = tr.FasterWhisperTranscriber()
    assert (backend.provider, backend.model, backend.cpu_threads) == ("local", "large-v3-turbo", 4)
    assert backend.cost_usd([600.0, 30.0]) == 0.0
    assert backend.billed_seconds([600.0, 30.0]) == pytest.approx(630.0)


def test_cache_key_includes_compute_type_commit_and_guard_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    chunk = Chunk(0, 0.0, 10.0)

    def key(backend) -> Path:
        return tr._cache_path(tmp_path, chunk, backend, "tl", (1, 2))

    a = key(local(compute_type="int8"))
    assert key(local(compute_type="float32")) != a
    from clipper import config

    pin = config.WHISPER_MODELS["tiny"]
    monkeypatch.setitem(config.WHISPER_MODELS, "tiny", pin._replace(revision="f" * 40))
    b = key(local(compute_type="int8"))
    assert b != a  # another pinned commit is another model
    monkeypatch.setattr(local_asr, "GUARD_VERSION", local_asr.GUARD_VERSION + 1)
    assert key(local(compute_type="int8")) not in (a, b)


def test_module_imports_without_faster_whisper() -> None:
    code = (
        "import sys; sys.modules['faster_whisper'] = None; sys.modules['huggingface_hub'] = None\n"
        "import clipper.pipeline.transcribe, clipper.pipeline.local_asr, clipper.cli"
    )
    subprocess.run([sys.executable, "-I", "-c", code], check=True)  # noqa: S603


def test_default_transcriber_local_needs_no_key() -> None:
    from clipper.config import Settings

    settings = Settings(
        whisper_model="medium", whisper_compute_type="float32", whisper_cpu_threads=6
    )
    backend = tr.default_transcriber(settings)
    assert isinstance(backend, tr.FasterWhisperTranscriber)
    assert (backend.model, backend.compute_type, backend.cpu_threads) == ("medium", "float32", 6)


def test_default_transcriber_groq_needs_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from clipper.config import Settings

    with pytest.raises(PermanentError, match="GROQ_API_KEY is not set"):
        tr.default_transcriber(Settings(transcriber="groq"))
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    backend = tr.default_transcriber(Settings(transcriber="groq"))
    assert isinstance(backend, GroqTranscriber)
    assert KEY not in repr(backend)


@needs_ffmpeg
def test_stage_uses_creator_transcriber_override(ctx: StageContext, fake_asr: FakeAsr) -> None:
    overrides = ctx.config.creator(PH).overrides.model_copy(update={"transcriber": "groq"})
    creators = tuple(
        c.model_copy(update={"overrides": overrides}) if c.name == PH else c
        for c in ctx.config.creators
    )
    config = ctx.config.model_copy(update={"creators": creators})
    groq_ctx = StageContext(ctx.media_dir, ctx.conn, config, clock=ctx.clock)
    with pytest.raises(PermanentError, match="GROQ_API_KEY is not set"):
        tr.transcribe(make_job(ctx.conn), groq_ctx)
    other, _ = enqueue(ctx.conn, SourceRef("youtube", "aaaaaaaaaaa"), T0, creator="MrBeast")
    tr.transcribe(other, groq_ctx)  # no override: still local
    assert fake_asr.chunk_calls()


@needs_ffmpeg
def test_stage_local_default_writes_same_schema_and_free_spend(
    ctx: StageContext, fake_asr: FakeAsr, tmp_path: Path
) -> None:
    job = make_job(ctx.conn)
    tr.transcribe(job, ctx, chunk_s=10.0, overlap_s=2.0)  # no key, no transcriber passed

    turbo = ("dropbox-dash/faster-whisper-large-v3-turbo", tr.WHISPER_MODELS["large-v3-turbo"][1])
    assert fake_asr.fetch_calls() == [["fetch", *turbo, str(tmp_path / "models")]]
    chunks = fake_asr.chunk_calls()
    assert len(chunks) == 4 and fake_asr.max_active == 1  # sequential, one child at a time
    assert all(c[1:4] == ["int8", "4", "tl"] for c in chunks)
    data = json.loads((ctx.media_dir / "transcript.json").read_text())
    assert set(data) == {"language", "duration_s", "model", "segments", "words"}
    assert data["language"] == "tl" and data["model"] == "large-v3-turbo"
    assert set(data["words"][0]) == {"word", "start", "end"}
    assert set(data["segments"][0]) == {"start", "end", "text"}
    starts = [w["start"] for w in data["words"]]
    assert starts == sorted(starts)
    # stitched: chunk 1 (offset 8) contributes "salamat" at 8+3=11 s
    assert {"word": "salamat", "start": 11.0, "end": 11.3} in data["words"]
    assert not list(ctx.media_dir.glob(".*"))

    (row,) = spend_rows(ctx.conn)
    assert (row["provider"], row["kind"], row["usd"]) == ("local", "transcribe", 0.0)
    assert row["audio_seconds"] == pytest.approx(36.0)  # 10+10+10+6


@needs_ffmpeg
def test_stage_local_runs_when_daily_cap_is_spent(ctx: StageContext, fake_asr: FakeAsr) -> None:
    ctx.conn.execute(
        "INSERT INTO spend (provider, kind, usd, created_at) VALUES"
        " ('groq', 'transcribe', 7.5, '2026-10-21T01:00:00Z')"  # over the $5 cap
    )
    tr.transcribe(make_job(ctx.conn), ctx)
    assert (ctx.media_dir / "transcript.json").is_file()
    rows = spend_rows(ctx.conn)
    assert [(r["provider"], r["usd"]) for r in rows] == [("groq", 7.5), ("local", 0.0)]


def test_faster_whisper_contract(audio_30s: Path) -> None:
    """The real library still accepts exactly what the child passes and returns the fields
    it reads (no model download). Decoding catches PyAV breaks like av 19."""
    fw = pytest.importorskip("faster_whisper")
    from faster_whisper.audio import decode_audio
    from faster_whisper.transcribe import Segment as FwSegment
    from faster_whisper.transcribe import TranscriptionInfo
    from faster_whisper.transcribe import Word as FwWord

    inspect.signature(fw.WhisperModel.__init__).bind(
        None, "/m", **local_asr.model_kwargs("int8", 4)
    )
    inspect.signature(fw.WhisperModel.transcribe).bind(
        None, "c.flac", language="tl", **local_asr.TRANSCRIBE_KWARGS
    )
    fields = {f.name for f in dataclasses.fields(FwSegment)}
    assert {"start", "end", "words", "no_speech_prob", "avg_logprob", "compression_ratio"} <= fields
    assert {"start", "end", "word"} <= {f.name for f in dataclasses.fields(FwWord)}
    assert "language" in {f.name for f in dataclasses.fields(TranscriptionInfo)}
    samples = decode_audio(str(audio_30s))
    assert len(samples) == pytest.approx(30 * 16000, abs=1600)


SLOW_MODEL_DIR = Path(__file__).resolve().parents[1] / "data" / "models"


@pytest.mark.slow
@needs_ffmpeg
def test_local_tiny_model_transcribes_the_fixture(
    ctx: StageContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = SLOW_MODEL_DIR / "models--Systran--faster-whisper-tiny" / "snapshots" / TINY[1]
    if os.environ.get("CLIPPER_SLOW_TESTS") != "1" or not (snapshot / "model.bin").is_file():
        pytest.skip("set CLIPPER_SLOW_TESTS=1 and download `tiny` into data/models")
    monkeypatch.setenv("CLIPPER_MODELS_DIR", str(SLOW_MODEL_DIR))
    settings = ctx.config.settings.model_copy(update={"whisper_model": "tiny"})
    ctx = StageContext(
        ctx.media_dir, ctx.conn, ctx.config.model_copy(update={"settings": settings}), ctx.clock
    )
    tr.transcribe(make_job(ctx.conn, creator="MrBeast"), ctx)  # the real child process
    data = json.loads((ctx.media_dir / "transcript.json").read_text())
    assert set(data) == {"language", "duration_s", "model", "segments", "words"}
    assert data["model"] == "tiny" and data["language"] == "en"
    assert data["duration_s"] == pytest.approx(30.0, abs=0.1)
    (row,) = spend_rows(ctx.conn)
    assert (row["provider"], row["usd"]) == ("local", 0.0)

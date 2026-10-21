import json
import logging
import shutil
import sqlite3
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from clipper.config import AppConfig, load_config
from clipper.db import connect, migrate
from clipper.jobs import Deferred, Job, PermanentError, SkipJob, StageContext, enqueue
from clipper.jobs.models import JobStatus
from clipper.pipeline import transcribe as tr
from clipper.pipeline.common import MediaError, spend_today, write_json_atomic
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
    assert tr.normalize_language("klingon") == "klingon"


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
def test_groq_non_json_is_retried(tmp_path: Path) -> None:
    respx.post(tr.GROQ_URL).mock(
        side_effect=[httpx.Response(200, text="<html>"), httpx.Response(200, json=[]), ok()]
    )
    audio = tmp_path / "c.flac"
    audio.write_bytes(b"fLaC")
    assert groq().transcribe(audio, language=None).words


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
    (cache / tr._cache_path(cache, chunk, backend, "tl").name).write_text("{broken")
    route = respx.post(tr.GROQ_URL).mock(side_effect=lambda req: ok())
    tr.transcribe(make_job(ctx.conn), ctx, transcriber=backend)
    assert route.call_count == 1
    assert not stale.exists()


@needs_ffmpeg
def test_probe_timeout_is_retryable(ctx: StageContext, monkeypatch: pytest.MonkeyPatch) -> None:
    from clipper.pipeline import ytdlp

    def hang(args, timeout_s, **kw):
        raise ytdlp.CommandTimeout("ffprobe timed out after 60s")

    monkeypatch.setattr(ytdlp, "run_command", hang)
    with pytest.raises(MediaError, match="timed out") as exc:
        tr.transcribe(make_job(ctx.conn), ctx, transcriber=groq())
    assert not isinstance(exc.value, PermanentError)


@needs_ffmpeg
def test_stage_missing_key_is_permanent(ctx: StageContext) -> None:
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
    tr.transcribe(make_job(ctx.conn), ctx)
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

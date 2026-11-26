"""Commentary stages `fetching_sources` and `transcribing` (CLP-145).

The day's candidates are the top `candidates_per_day` `story_candidates` rows of the job
that discovery (CLP-144) let through (`skip_reason IS NULL`): pinned streamers' clips
first (settled 2026-11-24), then by score, then the newer clip.

`fetching_sources` downloads each candidate's Twitch clip, video and audio, through the
hardened yt-dlp runner (`clipper.pipeline.ytdlp`). A clip's vertical version is fetched
when it has one: the highest of yt-dlp's `portrait-<height>` formats. Only a clip with no
vertical version is fetched in its (highest) landscape format. Every format whose id
starts with `portrait` counts as vertical, as in the probe's selector: yt-dlp names one
without a known height just `portrait`, and renames duplicate ids to `<id>-<n>`. The
audio is converted to 16 kHz mono FLAC as for clip jobs, and its loudest moments
(`loudness_peaks` from `fetch_audio`, sized for clips of under a minute) become the
candidate's moment hints.

`transcribing` runs the clip pipeline's own transcription (`transcribe.transcribe`:
chunking, word timestamps, transcript.json) on each fetched candidate's audio, always
with local faster-whisper and the clip jobs' whisper settings, whatever `transcriber`
says.

Files live in the job's media dir, one dir per candidate named by its row id:
`candidates/<id>/source.mp4`, `audio_16k.flac` and `transcript.json`. The candidate's
row records the paths (relative to the media dir), the format kind, the peaks, and the
reason it was dropped (migration 0015). The rows are the stages' progress: neither stage
declares an artifact, and a re-run (a retry, or a worker killed mid-stage) skips every
candidate that is already done (its row says so and its file is still there) or dropped.
Each row write is fenced on the job's lease (`hold_lease`).

Failures. A candidate that cannot be fetched or transcribed (a removed clip, no usable
format, an audio file that cannot be read, a transcript that is unusable twice) is
dropped with the reason, and the stage goes on with the rest. A failure that may pass
(a network error, an ffmpeg or whisper crash, the stage's time budget) is raised instead,
so the worker retries the stage with backoff, after the stage has gone on with the
candidates ranked after it. On the job's last attempt such a candidate is dropped
instead. The stage's time budget running out (`StageTimeout`) never drops a candidate:
it is the stage's budget, not the candidate's fault, so it is raised on every attempt
and the worker retries the stage (each run resumes after the candidates already done)
and, after its last attempt, fails the day with its own alert. It is not a `Deferred`,
which uses no attempt and could put off a day that never fits the budget forever.

When candidates existed but none is left, the day is skipped with the reason and one
alert ("No commentary Short today ..."); the worker's skip itself sends none. The alert
is sent before the worker records the skip, so a marker in the job's media dir
(`.no_short_alert_sent`) keeps a run claimed again after a crash from sending it twice;
each new run of the day (the fetching stage's first attempt) removes it.
A day with no candidate at all is a quiet skip. A stage that starts, at the first
attempt, with every candidate in its scope dropped (the day was skipped that way and is
now retried with `clipper retry`) clears those drops and the marker and tries them all
again, since the cause (a broken model, a network outage) may be fixed by now. A later
attempt that finds them all dropped is a crashed run claimed again: it only skips.

Each candidate is fetched from `https://clips.twitch.tv/<twitch_clip_id>`, built from a
validated clip id, never from the stored `url`. yt-dlp's clip selectors for YouTube
(`CLIP_FORMAT`, `AUDIO_FORMAT`) cannot be used here: they require a known audio codec,
and the Twitch clips extractor reports none, so they reject every Twitch clip.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from clipper.config import DEFAULT_CONFIG_PATH, AppConfig, ConfigError, Settings
from clipper.db import transaction
from clipper.jobs.models import Job, to_iso, utcnow
from clipper.jobs.queue import MAX_ATTEMPTS, hold_lease
from clipper.jobs.stages import PermanentError, SkipJob, StageContext, StageDef
from clipper.notify import Notify
from clipper.pipeline import transcribe as tr
from clipper.pipeline.common import (
    PROTOCOL_WHITELIST,
    Deadline,
    MediaError,
    StageTimeout,
    default_runner,
    run_media,
    settings_for,
    write_json_atomic,
)
from clipper.pipeline.fetch_audio import SAMPLE_RATE, loudness_peaks, parse_ebur128
from clipper.pipeline.ytdlp import Runner, YtDlp, YtDlpError
from clipper.redact import redact

logger = logging.getLogger(__name__)

CANDIDATES_DIR = "candidates"
SOURCE_STEM = "source"
AUDIO_NAME = tr.AUDIO_FILE
TRANSCRIPT_NAME = tr.ARTIFACT
INFO_TMP_NAME = ".info.tmp.json"

CLIP_URL = "https://clips.twitch.tv/{}"
_CLIP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
"""A Twitch clip slug, e.g. `AwkwardHelplessSalamanderKreygasm-AbC_dEf123`."""
_LEADING_HEIGHT = re.compile(r"(?:portrait-?)?(\d+)")
"""The height in a Twitch format id (`1080`, `portrait-720`, `portrait-720-0`)."""
CLIP_SELECTOR = "b[format_id^=portrait]/b"
"""The probe's yt-dlp selector: the best portrait format, else the best format. Twitch
clips have only muxed mp4 formats without codec fields, which plain `b` accepts. The
probe fails ("requested format not available") only when the clip has no format at
all; `choose_format` then picks the exact format the download asks for."""
CLIP_MAX_FILESIZE = "500M"
"""A Twitch clip is at most 60 s; 1080p60 is about 60 MB."""

CONVERT_TIMEOUT_S = 5 * 60.0
LOUDNESS_TIMEOUT_S = 5 * 60.0
PEAK_COUNT = 5
PEAK_MIN_SPACING_S = 3.0
"""Moment hints per clip: `fetch_audio`'s defaults (20 peaks, 30 s apart) are sized for
long videos and would give a 30 s clip a single peak."""
MAX_REASON_CHARS = 500

Kind = Literal["vertical", "landscape"]
TRANSIENT: tuple[type[Exception], ...] = (YtDlpError, MediaError, tr.BilledChunkError)
"""Failures of one candidate that may pass on a retry of the stage (network, a crashed
child). `StageTimeout` is the stage's own, and anything else that is not a
`PermanentError` is a bug: both stay raised."""
ALERT_MARKER = ".no_short_alert_sent"
"""In the job's media dir: the day's "no commentary Short" alert went out."""


class _Drop(Exception):
    """This candidate cannot be used; the message is its reason."""


@dataclass(frozen=True)
class Candidate:
    id: int
    twitch_clip_id: str
    source_path: str | None
    transcript_path: str | None
    drop_reason: str | None

    def dir(self, media_dir: Path) -> Path:
        return media_dir / CANDIDATES_DIR / str(self.id)


_ORDER = "pinned DESC, score DESC, clip_created_at DESC, id"
_COLUMNS = "id, twitch_clip_id, source_path, transcript_path, drop_reason"


def selected_candidates(conn: sqlite3.Connection, job_id: int, limit: int) -> list[Candidate]:
    """The day's top `limit` candidates that discovery let through, in rank order."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM story_candidates"  # noqa: S608 - fixed text
        f" WHERE job_id = ? AND skip_reason IS NULL ORDER BY {_ORDER} LIMIT ?",
        (job_id, limit),
    ).fetchall()
    return [Candidate(*row) for row in rows]


def fetched_candidates(conn: sqlite3.Connection, job_id: int, limit: int) -> list[Candidate]:
    """The day's selected candidates that `fetching_sources` fetched (dropped ones
    included), in rank order."""
    selected = selected_candidates(conn, job_id, limit)
    return [c for c in selected if c.source_path is not None]


def candidates_per_day(job: Job, creators: AppConfig, env: Mapping[str, str] | None = None) -> int:
    """The channel's `candidates_per_day` from commentary.yaml (next to the creators
    config, `CLIPPER_CONFIG`, or where `CLIPPER_COMMENTARY` says), read when the stage
    runs; the defaults' value when the job's account has no channel there. An invalid
    file fails the day: the monitor would not have made the job with it."""
    from clipper.accounts import accounts_path, load_accounts
    from clipper.commentary.config import commentary_path, load_commentary

    env = os.environ if env is None else env
    config_path = Path(env.get("CLIPPER_CONFIG") or DEFAULT_CONFIG_PATH)
    try:
        accounts = load_accounts(accounts_path(config_path, env), creators)
        commentary = load_commentary(commentary_path(config_path, env), accounts)
    except ConfigError as exc:
        raise PermanentError(str(exc)) from None
    for channel in commentary.channels:
        if channel.account == job.creator:
            return commentary.settings_for(channel).candidates_per_day
    return commentary.defaults.candidates_per_day


def clip_url(twitch_clip_id: str) -> str:
    if not _CLIP_ID.fullmatch(twitch_clip_id):
        raise _Drop("not a Twitch clip id")
    return CLIP_URL.format(twitch_clip_id)


def choose_format(formats: object) -> tuple[str, Kind] | None:
    """The format to fetch: the highest portrait one, else the highest other one; None
    when there is no format with a URL."""
    usable = [
        f
        for f in (formats if isinstance(formats, list) else [])
        if isinstance(f, dict) and isinstance(f.get("format_id"), str) and f.get("url")
    ]

    portrait = [f for f in usable if f["format_id"].startswith("portrait")]
    if portrait:
        return max(portrait, key=_height)["format_id"], "vertical"
    if usable:
        return max(usable, key=_height)["format_id"], "landscape"
    return None


def _height(f: dict[str, Any]) -> int:
    """yt-dlp's `height`, else the one in the format id, else 0. `max` keeps the first
    of equal heights, which is yt-dlp's own order."""
    value = f.get("height")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    match = _LEADING_HEIGHT.match(f["format_id"])
    return int(match[1]) if match else 0


@dataclass(frozen=True)
class ClipYtDlp(YtDlp):
    """`YtDlp` (same runner, env allowlist, cookies, proxy and error mapping) with the
    two calls a Twitch clip needs."""

    def probe_clip(self, url: str) -> dict[str, Any]:
        """The clip's metadata and formats. yt-dlp's Twitch clips extractor fails with an
        IndexError on a clip with no qualities, which yt-dlp reports as a generic
        (retryable) error; for a Twitch clip it is final."""
        extra = ["--dump-single-json", "--skip-download", "-f", CLIP_SELECTOR]
        try:
            stdout = self._run(extra, url, self.probe_timeout_s).stdout
        except YtDlpError as exc:
            if "[twitch:clips]" in str(exc) and "list index out of range" in str(exc):
                raise _Drop("the Twitch clip has no formats") from None
            raise
        try:
            info = json.loads(stdout)
        except json.JSONDecodeError:
            raise YtDlpError("yt-dlp returned invalid JSON") from None
        if not isinstance(info, dict):
            raise YtDlpError("yt-dlp returned unexpected JSON")
        return info

    def download_clip(self, url: str, out_dir: Path, format_id: str, info_json: Path) -> Path:
        """Download format `format_id` (muxed video and audio) to `out_dir/source.<ext>`."""
        template = str(out_dir.resolve() / SOURCE_STEM).replace("%", "%%") + ".%(ext)s"
        extra = [
            "--no-continue", "--no-part", "-f", format_id,
            "--max-filesize", CLIP_MAX_FILESIZE, "-o", template,
            "--load-info-json", str(info_json),
        ]  # fmt: skip
        self._run(extra, url, self.download_timeout_s)
        found = _sources(out_dir)
        if len(found) != 1:
            raise YtDlpError(f"expected one downloaded file, found {len(found)}")
        return found[0]


def _sources(out_dir: Path) -> list[Path]:
    return [p for p in out_dir.glob(f"{SOURCE_STEM}.*") if p.is_file()]


# --------------------------------------------------------------------------- shared


def _record(ctx: StageContext, job: Job, candidate_id: int, **fields: object) -> None:
    """Update one candidate row, fenced on the job's lease when run by a worker."""
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with transaction(ctx.conn):
        if ctx.worker_id is not None:
            hold_lease(ctx.conn, job.id, ctx.worker_id, ctx.clock())
        ctx.conn.execute(
            f"UPDATE story_candidates SET {assignments}, updated_at = ?"  # noqa: S608 - names
            " WHERE id = ? AND job_id = ?",
            (*fields.values(), to_iso(ctx.clock()), candidate_id, job.id),
        )


def _retry_all_dropped(
    ctx: StageContext, job: Job, candidates: Sequence[Candidate]
) -> list[Candidate]:
    """`candidates` as they are, or, when every one of them was dropped and this is the
    stage's first attempt (the day was skipped for it and is being retried), with their
    drops cleared, in one write fenced on the lease."""
    if not candidates or any(c.drop_reason is None for c in candidates):
        return list(candidates)
    if job.attempts > 1:  # a crashed run claimed again: its skip was not recorded
        return list(candidates)
    logger.info("job %s: all %d candidate(s) were dropped; trying again", job.id, len(candidates))
    ids = [c.id for c in candidates]
    marks = ", ".join("?" * len(ids))
    with transaction(ctx.conn):
        if ctx.worker_id is not None:
            hold_lease(ctx.conn, job.id, ctx.worker_id, ctx.clock())
        ctx.conn.execute(
            "UPDATE story_candidates SET drop_reason = NULL, updated_at = ?"  # noqa: S608
            f" WHERE id IN ({marks}) AND job_id = ?",
            (to_iso(ctx.clock()), *ids, job.id),
        )
    (ctx.media_dir / ALERT_MARKER).unlink(missing_ok=True)
    return [dataclasses.replace(c, drop_reason=None) for c in candidates]


def _each(
    job: Job,
    ctx: StageContext,
    candidates: Sequence[Candidate],
    what: str,
    work: Callable[[Candidate], None],
    cleared: Mapping[str, object] | None = None,
) -> None:
    """Run `work` on each candidate in turn. A candidate that cannot be used is dropped
    (with the `cleared` fields reset). A failure that may pass does not stop the others:
    the first one is raised after the loop, for a retry of the stage, except on the job's
    last attempt, where it drops its candidate too. The stage's time budget running out
    is raised at once: nothing after it could run, and it is no candidate's fault."""
    retry: Exception | None = None
    for candidate in candidates:
        try:
            work(candidate)
        except (_Drop, PermanentError) as exc:
            _drop(ctx, job, candidate, what, exc, cleared)
        except StageTimeout:
            raise
        except TRANSIENT as exc:
            if job.attempts >= MAX_ATTEMPTS:
                _drop(ctx, job, candidate, what, exc, cleared)
            elif retry is None:
                logger.warning(
                    "job %s: candidate %s: %s; retrying later", job.id, candidate.id, exc
                )
                retry = exc
            else:
                logger.warning("job %s: candidate %s: %s", job.id, candidate.id, exc)
    if retry is not None:
        raise retry


def _drop(
    ctx: StageContext,
    job: Job,
    candidate: Candidate,
    what: str,
    exc: Exception,
    cleared: Mapping[str, object] | None,
) -> None:
    detail = redact(str(exc)) or type(exc).__name__
    reason = f"{what}: {detail}"[:MAX_REASON_CHARS]
    logger.warning("job %s: candidate %s dropped: %s", job.id, candidate.id, reason)
    _record(ctx, job, candidate.id, **(cleared or {}), drop_reason=reason)


def _finish(
    job: Job,
    ctx: StageContext,
    candidates: Sequence[Candidate],
    verb: str,
    usable: Callable[[Candidate], bool],
    alert: Notify | None,
) -> None:
    """Skip the day, with one alert, when no candidate is left."""
    left = [c for c in candidates if c.drop_reason is None and usable(c)]
    logger.info("job %s: %d of %d candidate(s) %s", job.id, len(left), len(candidates), verb)
    if left:
        return
    reasons = [c.drop_reason for c in candidates if c.drop_reason]
    first = f"; first: {reasons[0]}" if reasons else ""
    reason = f"no candidate could be {verb} ({len(reasons)} dropped{first})"
    _alert(job, ctx.media_dir, reason, alert)
    raise SkipJob(reason)


def _alert(job: Job, media_dir: Path, reason: str, alert: Notify | None) -> None:
    """One alert for a day that had candidates but has nothing usable left (spec,
    "Failure"), unless the marker says it went out already. One that cannot be sent is
    only logged (and a run claimed again tries once more); the day is skipped anyway."""
    marker = media_dir / ALERT_MARKER
    if marker.is_file():
        logger.info("job %s: no-Short alert already sent", job.id)
        return
    text = (
        f"No commentary Short today (job {job.id}, {job.ref.key}): {reason}."
        f" Run `clipper retry {job.id}` to try again."
    )
    try:
        (alert or default_alert)(text)
    except Exception as exc:
        logger.warning("job %s: alert not sent: %s", job.id, redact(str(exc)))
        return
    marker.write_text(f"{to_iso(utcnow())}\n", encoding="utf-8")


def default_alert(text: str) -> None:
    """The worker's alert route: DISCORD_ALERTS_WEBHOOK_URL, else DISCORD_WEBHOOK_URL,
    else the log."""
    import httpx

    from clipper.config import Secrets
    from clipper.notify import DiscordWebhook, log_notify

    secrets = Secrets()
    for value in (secrets.discord_alerts_webhook_url, secrets.discord_webhook_url):
        url = value.get_secret_value().strip() if value is not None else ""
        if url:
            with httpx.Client(follow_redirects=False) as http:
                DiscordWebhook(url, http).send(text)
            return
    log_notify(text)


# --------------------------------------------------------------------------- fetching


_configured_per_day = candidates_per_day


def fetch_sources(
    job: Job,
    ctx: StageContext,
    *,
    runner: Runner | None = None,
    env: Mapping[str, str] | None = None,
    alert: Notify | None = None,
    candidates_per_day: int | None = None,
) -> None:
    per_day = candidates_per_day
    if per_day is None:
        per_day = _configured_per_day(job, ctx.config)
    candidates = selected_candidates(ctx.conn, job.id, per_day)
    if not candidates:
        raise SkipJob("no candidate to fetch")
    if job.attempts == 1:  # a new run of the day (first or `clipper retry`): it may alert
        (ctx.media_dir / ALERT_MARKER).unlink(missing_ok=True)
    candidates = _retry_all_dropped(ctx, job, candidates)
    runner = runner if runner is not None else default_runner()
    deadline = Deadline()
    ytdlp = ClipYtDlp.from_env(runner, env, cap_timeout=deadline.timeout)
    todo = [c for c in candidates if c.drop_reason is None and not _fetched(c, ctx.media_dir)]

    def work(candidate: Candidate) -> None:
        source, kind, peaks = _fetch_one(candidate, ctx.media_dir, ytdlp, runner, deadline)
        _record(
            ctx,
            job,
            candidate.id,
            source_path=source,
            source_kind=kind,
            loudness_peaks=json.dumps(peaks),
        )
        logger.info("job %s: candidate %s fetched (%s)", job.id, candidate.id, kind)

    cleared = {"source_path": None, "source_kind": None, "loudness_peaks": None}
    _each(job, ctx, todo, "fetch", work, cleared)
    after = selected_candidates(ctx.conn, job.id, per_day)
    _finish(job, ctx, after, "fetched", lambda c: c.source_path is not None, alert)


def _fetched(candidate: Candidate, media_dir: Path) -> bool:
    """Done earlier: the row says so and the files are still there. The files are found
    by their fixed names, never by the path stored in the row."""
    cdir = candidate.dir(media_dir)
    return (
        candidate.source_path is not None
        and len(_sources(cdir)) == 1
        and (cdir / AUDIO_NAME).is_file()
    )


def _fetch_one(
    candidate: Candidate, media_dir: Path, ytdlp: ClipYtDlp, runner: Runner, deadline: Deadline
) -> tuple[str, Kind, list[dict[str, float]]]:
    url = clip_url(candidate.twitch_clip_id)
    cdir = candidate.dir(media_dir)
    cdir.mkdir(parents=True, exist_ok=True)
    info = ytdlp.probe_clip(url)
    choice = choose_format(info.get("formats"))
    if choice is None:
        raise _Drop("no downloadable format")
    format_id, kind = choice
    for stale in (*_sources(cdir), cdir / AUDIO_NAME):
        stale.unlink(missing_ok=True)
    info_tmp = cdir / INFO_TMP_NAME
    flac_tmp = cdir / f".{AUDIO_NAME}.tmp"
    try:
        write_json_atomic(info_tmp, info, indent=None)
        source = ytdlp.download_clip(url, cdir, format_id, info_tmp)
        info_tmp.unlink()  # holds signed format URLs; never keep it
        _to_flac(runner, source, flac_tmp, deadline)
        samples = _momentary_loudness(runner, flac_tmp, deadline)
        peaks = loudness_peaks(samples, PEAK_COUNT, PEAK_MIN_SPACING_S)
        os.replace(flac_tmp, cdir / AUDIO_NAME)
    finally:
        info_tmp.unlink(missing_ok=True)
        flac_tmp.unlink(missing_ok=True)
    return source.relative_to(media_dir).as_posix(), kind, peaks


def _to_flac(runner: Runner, source: Path, dest: Path, deadline: Deadline) -> None:
    """The clip's audio as 16 kHz mono FLAC, as `fetching_audio` makes it for clip jobs."""
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        *PROTOCOL_WHITELIST, "-i", str(source.resolve()),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "flac", "-f", "flac",
        str(dest.resolve()),
    ]  # fmt: skip
    run_media(args, deadline.timeout(CONVERT_TIMEOUT_S), "audio conversion", runner=runner)


def _momentary_loudness(
    runner: Runner, path: Path, deadline: Deadline
) -> list[tuple[float, float]]:
    """ebur128's momentary loudness every 100 ms, parsed line by line as ffmpeg logs it:
    a runner keeps only the last 64 KB of stderr, less than a 60 s clip's log, so reading
    the kept stderr would lose the clip's opening seconds (where the hook usually is)."""
    args = [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats",
        *PROTOCOL_WHITELIST, "-i", str(path.resolve()),
        "-af", "ebur128=framelog=info", "-f", "null", "-",
    ]  # fmt: skip
    samples: list[tuple[float, float]] = []
    run_media(
        args,
        deadline.timeout(LOUDNESS_TIMEOUT_S),
        "loudness",
        runner=runner,
        on_stderr_line=lambda line: samples.extend(parse_ebur128(line)),
    )
    return samples


FETCH_STAGE = StageDef(fetch_sources)


# --------------------------------------------------------------------------- transcribing


def local_transcriber(settings: Settings) -> tr.FasterWhisperTranscriber:
    """Local faster-whisper with the clip jobs' whisper settings (spec: transcripts are
    local, whatever `transcriber` says)."""
    return tr.FasterWhisperTranscriber(
        model=settings.whisper_model,
        compute_type=settings.whisper_compute_type,
        cpu_threads=settings.whisper_cpu_threads,
    )


def transcribe_sources(
    job: Job,
    ctx: StageContext,
    *,
    transcriber: tr.Transcriber | None = None,
    alert: Notify | None = None,
    candidates_per_day: int | None = None,
) -> None:
    per_day = candidates_per_day
    if per_day is None:
        per_day = _configured_per_day(job, ctx.config)
    candidates = fetched_candidates(ctx.conn, job.id, per_day)
    if not candidates:
        raise SkipJob("no fetched candidate to transcribe")
    candidates = _retry_all_dropped(ctx, job, candidates)
    if transcriber is None:
        transcriber = local_transcriber(settings_for(job, ctx.config))
    backend = transcriber
    todo = [c for c in candidates if c.drop_reason is None and not _transcribed(c, ctx.media_dir)]

    def work(candidate: Candidate) -> None:
        cdir = candidate.dir(ctx.media_dir)
        if not (cdir / AUDIO_NAME).is_file():
            raise _Drop(f"{AUDIO_NAME} is missing")
        tr.transcribe(job, dataclasses.replace(ctx, media_dir=cdir), transcriber=backend)
        path = f"{CANDIDATES_DIR}/{candidate.id}/{TRANSCRIPT_NAME}"
        _record(ctx, job, candidate.id, transcript_path=path)

    _each(job, ctx, todo, "transcribe", work)
    after = fetched_candidates(ctx.conn, job.id, per_day)
    _finish(job, ctx, after, "transcribed", lambda c: c.transcript_path is not None, alert)


def _transcribed(candidate: Candidate, media_dir: Path) -> bool:
    return (
        candidate.transcript_path is not None
        and (candidate.dir(media_dir) / TRANSCRIPT_NAME).is_file()
    )


TRANSCRIBE_STAGE = StageDef(transcribe_sources)

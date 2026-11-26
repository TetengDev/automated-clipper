"""Commentary stage `scripting` (CLP-146): write the day's stories with one `claude -p` call.

Input: the day's usable candidates, i.e. `discovery.day_candidates` (pinned streamers'
clips first, then score, then the newer clip) that `fetching_sources` and `transcribing`
(CLP-145) kept: no `drop_reason`, and a transcript at `candidates/<id>/transcript.json`.
The channel (the job's account) comes from commentary.yaml, read as discovery reads it.

The call always goes through the Claude Code CLI on the owner's subscription (the
`claude_cli` backend of `clipper.pipeline.select`, CLP-81), whatever `selector` says: no
API key, no tools, the prompt on stdin. The system prompt is the channel's prompt file,
`clipper/pipeline/prompts/<prompt>.md` (the first is `commentary_gaming_v1`). The user
prompt has, for each candidate, its rank, score, streamer, game, title, duration, URL,
loudness peaks and transcript (the title and transcript fenced as untrusted data); the
formats of the channel's last 5 Shorts; and the owner's approve and reject decisions on
past commentary Shorts as feedback examples.

Every channel game needs its Twitch `game_id` (`clipper resolve`); one without it fails
the day before any call. Until voicing and rendering are built (CLP-147, CLP-148) the
stage skips the day before any call: a scripted story could never become a video.

The reply is checked story by story (`clipper.commentary.script`); a story that breaks a
rule is dropped and the reason logged and written to `script.json` in the job's media
dir, with the whole reply. A reply that is not `{"stories": [...]}` is asked for once more;
invalid twice, the stage is retried by the worker, and on the job's last attempt the day
is skipped with one alert. When fewer than `stories_per_day` stories are kept and a
dropped one can be fixed (schema, length, spacing, title, plan, links), one repair call
sends those stories back with their reasons and checks the corrected ones; a name,
profanity or rotation drop is never repaired. A run of the stage makes at most 2 calls
(a worker retry after an invalid reply is a new run). When
candidates exist but no story is left, the day is skipped with the reason and one alert
("No commentary Short today ..."). A day with no usable candidate is a quiet skip.

Storage, in one transaction fenced on the job's lease: each kept story becomes a `clips`
row under the day's job (rank 1..`stories_per_day`, status `re_rendering` so it stays out
of review until CLP-148 renders it), version 1 in `clip_versions` (`start_s` 0, `end_s`
the runtime, the title, the description with one credit line per clip as `caption`, the
hashtags, the prompt name as `prompt_version`), and a `commentary_stories` row:

- `hook`: the hook card text.
- `script`: JSON `{"lines": [{text, time_s, kind}], "cards": [{text, after_clip,
  duration_s}]}`, lines in time order.
- `excerpts`: the clip plan, a JSON array of `{clip_id, start_s, end_s, at_s, candidate_id,
  login, url, source_kind}` (`clip_id` the Twitch clip id, `at_s` where it starts on the
  Short's timeline).
- `guardrails`: the clips-first figures (`script.Plan.figures`).
- `format`: `{hook_type, structure, topic}` (CLP-148 adds `palette`).

The title and description are stored cleaned (`metadata.clean_title`,
`clean_caption`). Each used candidate gets `used_by_clip_id`; a candidate taken meanwhile
fails the transaction. A re-run that finds the job's stories stored
returns at once, without a second call.

`draft_stories` writes stories without storing them, so the review app's "regenerate
with note" (CLP-149) can ask for one story with the reviewer's note, reusing that Short's
own clips (`regenerate`).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, NoReturn

from clipper.commentary import discovery
from clipper.commentary import script as sc
from clipper.commentary.config import Channel, CommentaryConfig, CommentarySettings
from clipper.db import transaction
from clipper.jobs.models import Job, JobStatus, to_iso
from clipper.jobs.queue import MAX_ATTEMPTS, hold_lease
from clipper.jobs.stages import PermanentError, SkipJob, StageContext, StageDef
from clipper.metadata import clean_caption, clean_title
from clipper.notify import Notify
from clipper.pipeline import common
from clipper.pipeline.commentary_sources import (
    CANDIDATES_DIR,
    TRANSCRIPT_NAME,
    _alert,
    _Drop,
    clip_url,
)
from clipper.pipeline.common import strip_control, write_json_atomic
from clipper.pipeline.select import (
    CREDIT_MARKER,
    MODEL,
    Reply,
    _CliBackend,
    _CreditDeferred,
    _paid_call,
)
from clipper.pipeline.ytdlp import RunResult
from clipper.urls import COMMENTARY

logger = logging.getLogger(__name__)

ARTIFACT = "script.json"
"""The reply and why each dropped story was dropped (not a stage artifact: the stage's
output is its rows)."""
STATUS = "re_rendering"
"""A scripted story waits for voicing and rendering; never reviewable before it has a video."""
MAX_REPLY_ATTEMPTS = 2
MAX_CALLS = 2
"""`claude` calls one run of the stage may make: the first reply's retry or one repair call.
A worker retry (an invalid reply twice, a storage error) is a new run."""
WAITING = "commentary scripting waits for voicing and rendering (CLP-147, CLP-148)"
REPAIR_HEADER = "Repair: these stories were dropped."
FEEDBACK_LIMIT = 10
MAX_TITLE_CHARS = 300
MAX_NOTE_CHARS = 500


class InvalidReply(Exception):
    """The reply is not `{"stories": [...]}`."""


# --- inputs ---------------------------------------------------------------------------------


def load_prompt(name: str) -> str:
    """The channel's prompt file, `clipper/pipeline/prompts/<name>.md`."""
    path = resources.files("clipper.pipeline") / "prompts" / f"{name}.md"
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        raise PermanentError(f"commentary prompt {name!r} not found ({name}.md)") from None


def _games_by_id(channel: Channel) -> dict[str, tuple[str, ...]]:
    return {g.game_id: (g.name, *g.aliases) for g in channel.games if g.game_id}


def require_game_ids(channel: Channel) -> None:
    """Every game needs its Twitch id: without it no candidate maps to the game, and the
    title and name checks would drop every story."""
    for game in channel.games:
        if not game.game_id:
            raise PermanentError(
                f"game {game.name!r} of {channel.account} has no Twitch game_id in"
                " commentary.yaml; run `clipper resolve`"
            )


@dataclass(frozen=True)
class _Transcript:
    text: str
    words: tuple[tuple[float, float], ...]
    segments: tuple[tuple[float, float, str], ...]


def _read_transcript(path: Path) -> _Transcript | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        segments = tuple(
            (float(s["start"]), float(s["end"]), strip_control(str(s["text"])))
            for s in data.get("segments") or ()
        )
        words = tuple((float(w["start"]), float(w["end"])) for w in data.get("words") or ())
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    return _Transcript(" ".join(s[2] for s in segments if s[2]), words, segments)


@dataclass(frozen=True)
class DayCandidate:
    """A usable candidate with what the prompt shows of it."""

    source: sc.Source
    rank: int
    score: float
    pinned: bool
    view_count: int
    peaks: tuple[tuple[float, float], ...]
    segments: tuple[tuple[float, float, str], ...]


def usable_candidates(
    conn: sqlite3.Connection,
    job_id: int,
    media_dir: Path,
    channel: Channel,
    regenerate: int | None = None,
) -> list[DayCandidate]:
    """The day's candidates that are fetched and transcribed and not dropped, in rank
    order (`discovery.day_candidates`, plus the clips the `regenerate` Short used). The
    transcript is read from its fixed path, never from the stored one."""
    games = _games_by_id(channel)
    conn_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        if regenerate is None:
            rows = discovery.day_candidates(conn, job_id)
        else:  # day_candidates' order, with the regenerated Short's clips back in
            rows = conn.execute(
                "SELECT * FROM story_candidates WHERE job_id = ? AND skip_reason IS NULL"
                " AND (used_by_clip_id IS NULL OR used_by_clip_id = ?)"
                " ORDER BY pinned DESC, score DESC, clip_created_at DESC, twitch_clip_id",
                (job_id, regenerate),
            ).fetchall()
    finally:
        conn.row_factory = conn_factory
    out: list[DayCandidate] = []
    for row in rows:
        if row["drop_reason"] is not None or row["transcript_path"] is None:
            continue
        path = media_dir / CANDIDATES_DIR / str(row["id"]) / TRANSCRIPT_NAME
        transcript = _read_transcript(path)
        if transcript is None:
            logger.warning("job %s: candidate %s has no readable transcript", job_id, row["id"])
            continue
        try:
            url = clip_url(row["twitch_clip_id"])
        except _Drop:
            logger.warning("job %s: candidate %s has no valid clip id", job_id, row["id"])
            continue
        source = sc.Source(
            clip_id=row["twitch_clip_id"],
            candidate_id=row["id"],
            login=row["broadcaster_login"],
            name=row["broadcaster_name"],
            title=row["title"],
            game_names=games.get(row["game_id"] or "", ()),
            duration_s=float(row["duration_s"]),
            transcript=transcript.text,
            words=transcript.words,
            url=url,
            source_kind=row["source_kind"],
        )
        out.append(
            DayCandidate(
                source=source,
                rank=len(out) + 1,
                score=float(row["score"]),
                pinned=bool(row["pinned"]),
                view_count=int(row["view_count"]),
                peaks=_peaks(row["loudness_peaks"]),
                segments=transcript.segments,
            )
        )
    return out


def _peaks(raw: str | None) -> tuple[tuple[float, float], ...]:
    try:
        peaks = json.loads(raw or "[]")
        return tuple((float(p["t_s"]), float(p["lufs"])) for p in peaks)
    except (ValueError, TypeError, KeyError):
        return ()


def lexicon(sources: Sequence[sc.Source], channel: Channel) -> sc.Lexicon:
    """The streamers of the candidates the prompt shows, the channel's pinned and excluded
    streamers, and the channel's games with their aliases. Streamers the scan saw but the
    prompt does not show are left out: a login that is a plain word ("chat") would drop
    clean stories."""
    streamers: list[str] = []
    for source in sources:
        streamers += [n for n in (source.name, source.login) if n]
    for pinned in channel.streamers:
        streamers += [pinned.name, *([pinned.twitch] if pinned.twitch else [])]
    streamers += [e.lstrip("@") for e in channel.exclude_streamers]
    games = [n for g in channel.games for n in (g.name, *g.aliases)]
    return sc.Lexicon(tuple(streamers), tuple(games))


def recent_formats(
    conn: sqlite3.Connection, account: str, exclude_job: int, limit: int = sc.ROTATION_WINDOW
) -> list[dict[str, Any]]:
    """The `format` of the channel's latest commentary Shorts before `exclude_job`'s day,
    most recent first: by day (the item id's `YYYY-MM-DD`), then rank, so a retried older
    day is never the latest. A Short that never goes out does not count: a rejected or
    archived one, or a story whose day was skipped or failed before it was rendered."""
    row = conn.execute("SELECT item_id FROM jobs WHERE id = ?", (exclude_job,)).fetchone()
    before = str(row[0])[-10:] if row else "9999-12-31"
    rows = conn.execute(
        "SELECT s.format FROM commentary_stories s"
        " JOIN clips c ON c.id = s.clip_id AND s.version = c.current_version"
        " JOIN jobs j ON j.id = c.job_id"
        " WHERE j.platform = ? AND j.creator = ? AND j.id != ? AND substr(j.item_id, -10) < ?"
        " AND c.status NOT IN ('rejected', 'archived')"
        " AND NOT (c.status = ? AND j.status IN ('skipped', 'failed'))"
        " ORDER BY substr(j.item_id, -10) DESC, c.rank DESC, c.id DESC LIMIT ?",
        (COMMENTARY, account, exclude_job, before, STATUS, limit),
    ).fetchall()
    out = []
    for (raw,) in rows:
        fmt = _loads(raw, dict)
        out.append(fmt if fmt is not None else {})
    return out


def _loads(raw: str | None, kind: type) -> Any:
    try:
        value = json.loads(raw or "null")
    except ValueError:
        return None
    return value if isinstance(value, kind) else None


def feedback_examples(
    conn: sqlite3.Connection, account: str, limit: int = FEEDBACK_LIMIT
) -> list[dict[str, Any]]:
    """The owner's latest approve and reject decisions on this channel's commentary
    Shorts, newest first, with the story each was about."""
    rows = conn.execute(
        "SELECT r.action, r.reason_tag, s.hook, s.script, s.format, v.title"
        " FROM clip_reviews r"
        " JOIN clips c ON c.id = r.clip_id JOIN jobs j ON j.id = c.job_id"
        " JOIN commentary_stories s ON s.clip_id = r.clip_id AND s.version = r.version"
        " JOIN clip_versions v ON v.clip_id = r.clip_id AND v.version = r.version"
        " WHERE j.platform = ? AND j.creator = ? AND r.action IN ('approve', 'reject')"
        " ORDER BY r.created_at DESC, r.id DESC LIMIT ?",
        (COMMENTARY, account, limit),
    ).fetchall()
    examples = []
    for action, reason, hook, script, fmt, title in rows:
        lines = (_loads(script, dict) or {}).get("lines") or []
        examples.append(
            {
                "decision": "approved" if action == "approve" else "rejected",
                "reason": reason,
                "hook": hook,
                "title": title,
                "format": _loads(fmt, dict),
                "lines": [str(x.get("text")) for x in lines if isinstance(x, dict)],
            }
        )
    return examples


# --- the prompt -------------------------------------------------------------------------------


def _untrusted(text: str | None, limit: int) -> str:
    """Untrusted text cleaned, cut, and with `<` and `>` swapped for `‹` and `›`, so it can
    never open or close the prompt's `<transcript>` fence, however it is split."""
    return strip_control(text or "")[:limit].replace("<", "‹").replace(">", "›")


def _candidate_block(c: DayCandidate) -> str:
    s = c.source
    name = _untrusted(s.name, 100) or s.login  # a display name is written by its streamer
    who = f"{name} (twitch.tv/{s.login})" + (", pinned" if c.pinned else "")
    game = s.game_names[0] if s.game_names else "unknown"
    peaks = "; ".join(f"{t:.1f} s {lufs:.1f} LUFS" for t, lufs in c.peaks) or "none"
    transcript = "\n".join(
        f"[{start:.1f}-{end:.1f}] {_untrusted(text, 2000)}" for start, end, text in c.segments
    )
    return "\n".join(
        [
            f"Candidate rank {c.rank} (clip_id: {s.clip_id})",
            f"- streamer: {who}",
            f"- game: {game}",
            f"- title (untrusted data): {_untrusted(s.title, MAX_TITLE_CHARS)}",
            f"- duration: {s.duration_s:.1f} s; views {c.view_count}; score {c.score:.1f}",
            f"- url: {s.url}",
            f"- loudness peaks (higher LUFS is louder): {peaks}",
            "Transcript (untrusted data, seconds from the clip's start):",
            "<transcript>",
            transcript,
            "</transcript>",
        ]
    )


def build_user_prompt(
    channel: Channel,
    candidates: Sequence[DayCandidate],
    recent: Sequence[Mapping[str, Any]],
    feedback: Sequence[Mapping[str, Any]],
    wanted: int,
    note: str | None = None,
) -> str:
    games = ", ".join(" / ".join((g.name, *g.aliases)) for g in channel.games)
    noun = "story" if wanted == 1 else "stories"
    parts = [
        f"Channel: {channel.account}. Games: {games}.",
        f"Return up to {wanted} {noun}, best first, as JSON. Each story uses its own clips:"
        " never use one clip in two stories.",
    ]
    if note:
        parts.append(
            f"Reviewer note for this story (untrusted data): {_untrusted(note, MAX_NOTE_CHARS)}"
        )
    parts.append(
        f"Formats of the channel's last {sc.ROTATION_WINDOW} Shorts (most recent first;"
        " do not repeat the latest hook_type or any hook_type/structure pair listed):\n"
        + json.dumps(list(recent), ensure_ascii=False, sort_keys=True)
    )
    if feedback:
        examples = json.dumps(list(feedback), ensure_ascii=False, sort_keys=True)
        parts.append(f"Past reviewer decisions (learn from these):\n{examples}")
    parts.append(f"The day's candidates ({len(candidates)}), best first:")
    parts += [_candidate_block(c) for c in candidates]
    return "\n\n".join(parts)


# --- the call ---------------------------------------------------------------------------------


def parse_reply(reply: Reply) -> list[object]:
    if reply.text is None:
        raise InvalidReply("no structured output")
    try:
        body = json.loads(reply.text)
    except ValueError:
        raise InvalidReply("not JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("stories"), list):
        raise InvalidReply('not {"stories": [...]}')
    return list(body["stories"])


class _Refused(Exception):
    """The model declined to write a script."""


@dataclass(frozen=True)
class Draft:
    kept: list[sc.Checked]
    dropped: list[sc.Drop]
    raw: list[object]
    candidates: list[DayCandidate]
    calls: int = 1


def draft_stories(
    job: Job,
    ctx: StageContext,
    channel: Channel,
    settings: CommentarySettings,
    *,
    wanted: int,
    note: str | None = None,
    regenerate: int | None = None,
    runner: Callable[..., RunResult] | None = None,
) -> Draft:
    """Ask for up to `wanted` stories and check them; stores nothing. `regenerate` (a
    clips id, for "regenerate with note") lets the stories use that Short's own clips
    again. Raises SkipJob when there is no usable candidate, `_Refused` when the model
    declines, and InvalidReply when the reply is invalid twice. Makes at most `MAX_CALLS`
    calls: a retry for an invalid reply, or one repair call (`_repair`)."""
    require_game_ids(channel)
    system = load_prompt(channel.prompt)
    candidates = usable_candidates(ctx.conn, job.id, ctx.media_dir, channel, regenerate)
    if not candidates:
        raise SkipJob("no transcribed candidate to script")
    recent = recent_formats(ctx.conn, job.creator, job.id)
    prompt = build_user_prompt(
        channel, candidates, recent, feedback_examples(ctx.conn, job.creator), wanted, note
    )
    backend = _CliBackend(runner, sc.CLI_SCHEMA)  # always the subscription (spec, "Selector")
    cap = common.settings_for(job, ctx.config).daily_llm_usd_cap
    blocks = [{"type": "text", "text": system}]
    problem = ""
    calls = 0
    for _ in range(MAX_REPLY_ATTEMPTS):
        calls += 1
        reply = _paid_call(backend, blocks, prompt, job, ctx, cap)
        if reply.stop_reason == "refusal":
            raise _Refused
        try:
            raw = parse_reply(reply)
            break
        except InvalidReply as exc:
            problem = str(exc)
            logger.warning("job %s: script reply invalid: %s", job.id, problem)
    else:
        raise InvalidReply(f"script reply invalid {MAX_REPLY_ATTEMPTS} times: {problem}")
    sources = {c.source.clip_id: c.source for c in candidates}
    names = lexicon(list(sources.values()), channel)
    kept, dropped = sc.evaluate(raw, sources, names, recent, wanted)
    for drop in dropped:
        logger.warning("job %s: story %d dropped: %s", job.id, drop.index + 1, drop.reason)
    fixable = [d for d in dropped if d.fixable]
    if len(kept) < wanted and fixable and calls < MAX_CALLS:
        calls += 1
        ask = repair_prompt(prompt, raw, fixable, kept, wanted - len(kept))
        fixed = _repair(job, ctx, backend, blocks, ask, cap)
        if fixed:
            more, again = sc.evaluate(
                fixed, sources, names, recent, wanted - len(kept), before=kept, first_index=len(raw)
            )
            for drop in again:
                logger.warning(
                    "job %s: repaired story %d dropped: %s", job.id, drop.index + 1, drop.reason
                )
            kept, dropped, raw = kept + more, dropped + again, raw + fixed
    return Draft(kept, dropped, raw, candidates, calls)


def repair_prompt(
    prompt: str,
    raw: Sequence[object],
    fixable: Sequence[sc.Drop],
    kept: Sequence[sc.Checked],
    room: int,
) -> str:
    """The first prompt, then the dropped stories with their exact reasons, and the clips
    and formats the kept stories already use (the call does not see the first reply)."""
    dropped = [{"story": raw[d.index], "reason": d.reason} for d in fixable]
    taken = [
        {"clips": [e.clip_id for e in c.story.clips], "format": c.story.format.model_dump()}
        for c in kept
    ]
    noun = "story" if room == 1 else "stories"
    return "\n\n".join(
        [
            prompt,
            f"{REPAIR_HEADER} Return corrected versions of them, at most {room} {noun}, best"
            " first, as JSON. Fix each reason; keep every other rule. Count the hook's words"
            " and each line's words before answering.\n"
            + json.dumps(dropped, ensure_ascii=False, sort_keys=True),
            "Stories already kept today (most recent last). Do not use their clips, and treat"
            " their formats as the latest Shorts for the rotation rule:\n"
            + json.dumps(taken, ensure_ascii=False, sort_keys=True),
        ]
    )


def _repair(
    job: Job,
    ctx: StageContext,
    backend: _CliBackend,
    blocks: list[dict[str, Any]],
    prompt: str,
    cap: float,
) -> list[object]:
    """One repair call; its stories, or none when it fails (the first result stands)."""
    try:
        reply = _paid_call(backend, blocks, prompt, job, ctx, cap)
        if reply.stop_reason == "refusal":
            raise InvalidReply("the model declined the repair")
        return parse_reply(reply)
    except _CreditDeferred:
        logger.warning("job %s: repair call deferred for credit; the first result stands", job.id)
    except (InvalidReply, RuntimeError, OSError, ValueError) as exc:
        logger.warning("job %s: repair call failed (%s); the first result stands", job.id, exc)
    return []


# --- storage ----------------------------------------------------------------------------------


def credit_lines(sources: Sequence[sc.Source]) -> list[str]:
    """One credit line per clip (spec, "Attribution"), built from the validated clip id."""
    return [f"Clip: twitch.tv/{s.login} {s.url}" for s in sources]


def _stored(conn: sqlite3.Connection, job_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM clips WHERE job_id = ?", (job_id,)).fetchone()[0])


def store_stories(
    ctx: StageContext, job: Job, kept: Sequence[sc.Checked], prompt_version: str
) -> list[int]:
    """Insert the stories (see the module doc) in one transaction fenced on the lease."""
    stamp = to_iso(ctx.clock())
    ids: list[int] = []
    with transaction(ctx.conn):
        if ctx.worker_id is not None:
            hold_lease(ctx.conn, job.id, ctx.worker_id, ctx.clock())
        for rank, checked in enumerate(kept, start=1):
            story, plan = checked.story, checked.plan
            clip_id = ctx.conn.execute(
                "INSERT INTO clips (job_id, rank, status, current_version, created_at,"
                " updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                (job.id, rank, STATUS, stamp, stamp),
            ).lastrowid
            assert clip_id is not None
            credits = "\n".join(credit_lines(checked.sources))
            caption = "\n\n".join([clean_caption(story.description), credits])
            ctx.conn.execute(
                "INSERT INTO clip_versions (clip_id, version, start_s, end_s, title, caption,"
                " hashtags, prompt_version, created_at) VALUES (?, 1, 0, ?, ?, ?, ?, ?, ?)",
                (
                    clip_id,
                    round(plan.runtime_s, 3),
                    clean_title(story.title),
                    caption,
                    json.dumps(story.hashtags, ensure_ascii=False),
                    prompt_version,
                    stamp,
                ),
            )
            script = {
                "lines": [line.model_dump() for line in story.lines],
                "cards": [card.model_dump() for card in story.cards],
            }
            ctx.conn.execute(
                "INSERT INTO commentary_stories (clip_id, version, hook, script, excerpts,"
                " guardrails, created_at, format) VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
                (
                    clip_id,
                    story.hook,
                    json.dumps(script, ensure_ascii=False),
                    json.dumps(list(plan.excerpts), ensure_ascii=False),
                    json.dumps(plan.figures()),
                    stamp,
                    json.dumps(story.format.model_dump(), ensure_ascii=False),
                ),
            )
            marked = 0
            for source in checked.sources:
                marked += ctx.conn.execute(
                    "UPDATE story_candidates SET used_by_clip_id = ?, updated_at = ?"
                    " WHERE id = ? AND job_id = ? AND used_by_clip_id IS NULL",
                    (clip_id, stamp, source.candidate_id, job.id),
                ).rowcount
            if marked != len(checked.sources):
                raise RuntimeError(
                    f"job {job.id}: a candidate of story {rank} was already used"
                    f" ({marked} of {len(checked.sources)} marked); nothing stored"
                )
            ids.append(int(clip_id))
    return ids


# --- the stage --------------------------------------------------------------------------------


def write_scripts(
    job: Job,
    ctx: StageContext,
    *,
    runner: Callable[..., RunResult] | None = None,
    load: Callable[[StageContext], CommentaryConfig] | None = None,
    alert: Notify | None = None,
) -> None:
    """The stage. `runner` (the CLI), `load` (commentary.yaml) and `alert` are seams."""
    try:
        _write_scripts(job, ctx, runner, load or discovery.load_commentary_config, alert)
    except _CreditDeferred:
        raise
    except BaseException:
        (ctx.media_dir / CREDIT_MARKER).unlink(missing_ok=True)
        raise
    (ctx.media_dir / CREDIT_MARKER).unlink(missing_ok=True)


def _write_scripts(
    job: Job,
    ctx: StageContext,
    runner: Callable[..., RunResult] | None,
    load: Callable[[StageContext], CommentaryConfig],
    alert: Notify | None,
) -> None:
    from clipper.pipeline import commentary  # imports this module

    if JobStatus.VOICING in commentary.NOT_BUILT or JobStatus.RENDERING in commentary.NOT_BUILT:
        raise SkipJob(WAITING)  # interim, until CLP-147 and CLP-148 ship
    if count := _stored(ctx.conn, job.id):
        logger.info("job %s: %d stories already scripted", job.id, count)
        return
    config = load(ctx)
    channel = next((c for c in config.channels if c.account == job.creator), None)
    if channel is None:
        raise SkipJob(f"no commentary channel for account {job.creator}")
    settings = config.settings_for(channel)
    try:
        draft = draft_stories(
            job, ctx, channel, settings, wanted=settings.stories_per_day, runner=runner
        )
    except _Refused:
        _skip(job, "the model declined to write a script", alert)
    except InvalidReply as exc:
        if job.attempts < MAX_ATTEMPTS:
            raise RuntimeError(str(exc)) from None
        _skip(job, str(exc), alert)
    write_json_atomic(
        ctx.media_dir / ARTIFACT,
        {
            "prompt": channel.prompt,
            "model": MODEL,
            "stories": draft.raw,
            "kept": [c.index for c in draft.kept],
            "dropped": [
                {"index": d.index, "reason": d.reason, "fixable": d.fixable} for d in draft.dropped
            ],
            "calls": draft.calls,
        },
    )
    if not draft.kept:
        if not draft.raw:
            _skip(job, "the script call returned no story", alert)
        first = draft.dropped[0].reason if draft.dropped else ""
        _skip(
            job,
            f"no story passed the checks ({len(draft.dropped)} dropped; first: {first})",
            alert,
        )
    ids = store_stories(ctx, job, draft.kept, channel.prompt)
    logger.info(
        "job %s: %d stories scripted (clips %s), %d dropped",
        job.id,
        len(ids),
        ids,
        len(draft.dropped),
    )


def _skip(job: Job, reason: str, alert: Notify | None) -> NoReturn:
    """Skip the day with one alert: candidates existed, but no Short came of them."""
    _alert(job, reason, alert)
    raise SkipJob(reason)


STAGE = StageDef(write_scripts)

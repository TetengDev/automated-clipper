"""Jump cuts inside a clip (CLP-73). The rules follow nateherkai/herk-edit-kit's
cut-silences and cut-mistakes (MIT); the code is ours.

Times are seconds in the section file, like the EDL words. The rules are pure:
- `silence_cuts`: a word gap of GAP_S or more is cut down to a breath (BREATH_S, longer
  after a sentence end or a long pause), SPLIT of it kept after the word and the rest
  before the next one, but only where silencedetect heard silence over the whole cut. On
  streams a gap in the transcript is often game audio or laughter.
- `stutter_cuts`: the same word twice within STUTTER_GAP_S, the first clearly cut short
  (STUTTER_SHORT); the first one goes. With Whisper's timestamps this is rare by design:
  missing a stutter beats cutting deliberate repetition.
- `filler_cuts`: um/uh/eh/ah alone between two pauses that silencedetect calls silent.
- `retake_candidates`: a phrase much like a later one (Jaccard of the content words). These
  are only proposed (applied=False), because picking the better take is a human call.
Tagalog words like "ano", "yung", "parang" and "kasi" are real words: never a filler and
never a stutter (reduplication is grammar).

`apply_cuts` turns the applied cuts into kept segments on the frame grid, and
`crop_on_output` moves CLP-58 crop keys onto the output timeline. `detect_silences` runs
ffmpeg silencedetect on the section's audio through `boundaries.cached_child` (timeout,
cache, fail open: None means no silence cuts).
"""

from __future__ import annotations

import math
import re
from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Literal

from clipper.edl import FPS, MAX_CUTS, MIN_SEGMENT_SECONDS, Cut, CutKind, Word, frames
from clipper.pipeline.boundaries import cached_child
from clipper.pipeline.common import Deadline
from clipper.pipeline.ytdlp import Runner
from clipper.render import FFMPEG_INPUT_GUARD

Mode = Literal["off", "silences", "full"]
Span = tuple[float, float]

GAP_S = 0.55
BREATH_S = 0.14
SENTENCE_BREATH_S = 0.20
LONG_BREATH_S = 0.24
LONG_GAP_S = 2.0
SPLIT = 0.55
"""Share of the kept breath that stays after the earlier word."""
WORD_END_PAD_S = 0.06
"""A cut never starts closer than this to a word's end."""
SILENCE_TOLERANCE_S = 0.05
"""Slack between transcript and silencedetect times when checking that a cut is silent."""
STUTTER_GAP_S = 0.5
STUTTER_SHORT = 0.6
"""A stutter's first word is cut short: at most this share of the second's length."""
FILLER_PAUSE_S = 0.2
"""A filler needs a pause at least this long on each side; otherwise it is speech."""
RETAKE_JACCARD = 0.34
PHRASE_GAP_S = 0.85
RETAKE_WINDOW_S = 75.0
MIN_SEGMENT_S = MIN_SEGMENT_SECONDS
SILENCE_NOISE_DB = -40
SILENCE_MIN_S = 0.2
SILENCE_TIMEOUT_S = 60.0
FILE_END_S = 1e6
"""End of a silence that runs to the end of the file (silencedetect reports no end)."""
MAX_SILENCES = 20_000
MAX_REASON_CHARS = 200

_FILLER = re.compile(r"u+m+|u+h+|e+h+|a+h+")
_EDGE = re.compile(r"^\W+|\W+$")
_SILENCE = re.compile(r"^lavfi\.silence_(start|end)=(-?[\d.]+(?:e[-+]?\d+)?)$", re.MULTILINE)
_TAGALOG_WORDS = "ano yung iyong yun iyon parang kasi naman talaga ganun ganon diba lang sige"
_REDUPLICATED = "araw isa dahan gabi unti sabay iba buo tig paulit taon linggo buwan bahay"
_EMPHATIC = "no yes yeah yep nope very really so bye go come now wait hey oh ha haha okay ok"
_EMPHATIC += " please more too never well right sure fast slow"
NEVER_CUT = frozenset(f"{_TAGALOG_WORDS} {_REDUPLICATED} {_EMPHATIC}".split())
"""Real Tagalog words, roots Tagalog doubles ("araw araw") and English words doubled for
emphasis ("no no"): never a filler or a stutter."""
_ENGLISH_STOP = (
    "a an and are as at be but by for from i if in is it of on or so that the this to was"
    " we with you your they their there what when where why how like um uh just really"
)
_TAGALOG_STOP = (
    "ang ng mga sa na at ay ko mo siya kami tayo sila niya nila namin natin ito iyan yan"
    " hindi oo eh ba daw raw nga kay para pero kung kapag pag dito doon diyan din rin pa po"
    " opo nung noon wala meron may"
)
STOP_WORDS = frozenset(f"{_ENGLISH_STOP} {_TAGALOG_STOP} {_TAGALOG_WORDS}".split())
"""Left out of a phrase's retake signature."""


def _token(text: str) -> str:
    return _EDGE.sub("", text.lower())


def _cut(
    a: float, b: float, kind: CutKind, confidence: float, reason: str, *, applied: bool = True
) -> list[Cut]:
    """The cut [a, b], or none when it rounds to nothing (e.g. two phrases that Whisper
    started at one time)."""
    a, b = round(a, 3), round(b, 3)
    if b <= a:
        return []
    return [
        Cut(
            in_s=a,
            out_s=b,
            kind=kind,
            confidence=round(confidence, 3),
            reason=reason[:MAX_REASON_CHARS],
            applied=applied,
        )
    ]


def _silent(silences: Sequence[Span], a: float, b: float) -> bool:
    return any(s - SILENCE_TOLERANCE_S <= a and b <= e + SILENCE_TOLERANCE_S for s, e in silences)


def _pause(prev: Word, nxt: Word, keep: float) -> Span:
    """The cut between two words that keeps a `keep`-second breath."""
    return prev.end_s + max(WORD_END_PAD_S, keep * SPLIT), nxt.start_s - keep * (1 - SPLIT)


def silence_cuts(words: Sequence[Word], silences: Sequence[Span]) -> list[Cut]:
    cuts = []
    for prev, nxt in zip(words, words[1:], strict=False):
        gap = nxt.start_s - prev.end_s
        if gap < GAP_S:
            continue
        sentence = prev.text.rstrip().endswith((".", "!", "?"))
        keep = LONG_BREATH_S if gap >= LONG_GAP_S else SENTENCE_BREATH_S if sentence else BREATH_S
        a, b = _pause(prev, nxt, keep)
        if _silent(silences, a, b):
            cuts += _cut(a, b, CutKind.SILENCE, 1.0, f"pause {gap:.2f}s -> {keep:.2f}s")
    return cuts


def stutter_cuts(words: Sequence[Word]) -> list[Cut]:
    cuts = []
    for a, b in zip(words, words[1:], strict=False):
        token = _token(a.text)
        if (
            token
            and token not in NEVER_CUT
            and token == _token(b.text)
            and b.start_s - a.end_s < STUTTER_GAP_S
            and a.end_s - a.start_s <= STUTTER_SHORT * (b.end_s - b.start_s)
        ):
            cuts += _cut(a.start_s, b.start_s, CutKind.STUTTER, 0.9, f'repeated "{token[:40]}"')
    return cuts


def filler_cuts(words: Sequence[Word], silences: Sequence[Span]) -> list[Cut]:
    cuts = []
    for prev, word, nxt in zip(words, words[1:], words[2:], strict=False):
        token = _token(word.text)
        if (
            not _FILLER.fullmatch(token)
            or word.start_s - prev.end_s < FILLER_PAUSE_S
            or nxt.start_s - word.end_s < FILLER_PAUSE_S
        ):
            continue
        a, b = _pause(prev, nxt, BREATH_S)
        if _silent(silences, a, word.start_s) and _silent(silences, word.end_s, b):
            cuts += _cut(a, b, CutKind.FILLER, 0.8, f'filler "{token}"')
    return cuts


def _phrases(words: Sequence[Word]) -> list[list[Word]]:
    """Split at long pauses, at a sentence end after 7.5 s, and at 16 s."""
    phrases: list[list[Word]] = []
    for word in words:
        if phrases:
            cur = phrases[-1]
            elapsed = word.end_s - cur[0].start_s
            sentence = cur[-1].text.rstrip().endswith((".", "!", "?"))
            if (
                word.start_s - cur[-1].end_s <= PHRASE_GAP_S
                and not (sentence and elapsed >= 7.5)
                and elapsed < 16
            ):
                cur.append(word)
                continue
        phrases.append([word])
    return phrases


def _signature(phrase: Sequence[Word]) -> list[str]:
    tokens = re.split(r"\W+", " ".join(w.text for w in phrase).lower())
    return [t for t in tokens if len(t) > 2 and t not in STOP_WORDS][:18]


def retake_candidates(words: Sequence[Word]) -> list[Cut]:
    """An earlier take of a later phrase, cut up to the restart; never applied."""
    phrases = _phrases(words)
    sigs = [_signature(p) for p in phrases]
    cuts = []
    for i, first in enumerate(phrases):
        for j in range(i + 1, min(len(phrases), i + 9)):
            gap = phrases[j][0].start_s - first[-1].end_s
            if gap > RETAKE_WINDOW_S:
                break
            a, b = set(sigs[i]), set(sigs[j])
            score = len(a & b) / max(1, len(a | b))
            if score < RETAKE_JACCARD:
                continue
            same_opening = sum(x == y for x, y in zip(sigs[i][:4], sigs[j][:4], strict=False)) >= 2
            short = first[-1].end_s - first[0].start_s < 6 and gap < 12
            kind = CutKind.FALSE_START if same_opening and short else CutKind.RETAKE
            reason = f"phrase {i + 1} ~ {j + 1} (jaccard {score:.2f})"
            cuts += _cut(
                first[0].start_s, phrases[j][0].start_s, kind, score, reason, applied=False
            )
    return cuts


def plan_cuts(words: Sequence[Word], silences: Sequence[Span], mode: Mode) -> list[Cut]:
    """The cuts for a creator's `jump_cuts` mode, sorted by time."""
    if mode == "off":
        return []
    cuts = silence_cuts(words, silences)
    if mode == "full":
        cuts += stutter_cuts(words) + filler_cuts(words, silences)
    cuts += retake_candidates(words)
    return sorted(cuts, key=lambda c: (c.in_s, c.out_s))[:MAX_CUTS]


def apply_cuts(span: Span, cuts: Sequence[Cut]) -> list[Span]:
    """The kept segments of `span` after the applied cuts, in whole frames (Remotion's
    rounding). A segment under MIN_SEGMENT_S is merged back into the neighbour across the
    shorter cut; if everything is cut, the span is kept whole."""
    lo, hi = frames(span[0]), frames(span[1])
    removed = sorted((max(frames(c.in_s), lo), min(frames(c.out_s), hi)) for c in cuts if c.applied)
    keep: list[list[int]] = []
    cursor = lo
    for a, b in removed:
        if b <= a:
            continue
        if a > cursor:
            keep.append([cursor, a])
        cursor = max(cursor, b)
    if cursor < hi:
        keep.append([cursor, hi])
    keep = keep or [[lo, hi]]
    shortest = math.ceil(MIN_SEGMENT_S * FPS - 1e-9)
    i = 0
    while len(keep) > 1 and i < len(keep):
        if keep[i][1] - keep[i][0] >= shortest:
            i += 1
            continue
        before = keep[i][0] - keep[i - 1][1] if i > 0 else math.inf
        after = keep[i + 1][0] - keep[i][1] if i + 1 < len(keep) else math.inf
        j = i - 1 if before <= after else i
        keep[j : j + 2] = [[keep[j][0], keep[j + 1][1]]]
        i = j
    return [(a / FPS, b / FPS) for a, b in keep]


def _removed(span: Span, segments: Sequence[Span]) -> list[tuple[int, int]]:
    """The frame ranges of `span` that `segments` leave out."""
    edges = [frames(span[0]), *(frames(t) for seg in segments for t in seg), frames(span[1])]
    return [(a, b) for a, b in zip(edges[::2], edges[1::2], strict=True) if b > a]


def cut_clip(span: Span, cuts: Sequence[Cut], min_s: float = 0.0) -> tuple[list[Cut], list[Span]]:
    """`apply_cuts`, keeping the clip at `min_s` or more: the applied cuts go back least
    confident first (then shortest) until it is. Each cut comes back with `applied` true
    only if it really removed something (a cut merged back is not applied)."""
    active = [c for c in cuts if c.applied]
    segments = apply_cuts(span, active)
    for c in sorted(active, key=lambda c: (c.confidence, c.out_s - c.in_s)):
        if sum(b - a for a, b in segments) >= min_s - 1e-9:
            break
        active.remove(c)
        segments = apply_cuts(span, active)
    removed = _removed(span, segments) if len(segments) > 1 else []
    settled = [
        c.model_copy(
            update={
                "applied": c in active
                and any(frames(c.in_s) < b and frames(c.out_s) > a for a, b in removed)
            }
        )
        for c in cuts
    ]
    return settled, segments


def crop_on_output(
    keys: Sequence[tuple[float, float]], segments: Sequence[Span], origin: float
) -> list[tuple[float, float]]:
    """Crop keys (seconds from `origin`) moved onto the output timeline of `segments`
    (section seconds): each segment's keys, framed by a key at its start and one at its
    end, so each join is two keys at one time (a jump)."""
    times = [t for t, _ in keys]

    def at(t: float, *, left: bool = False) -> float:
        i = (bisect_left if left else bisect_right)(times, t)
        if i == 0 or i == len(keys):
            return keys[min(i, len(keys) - 1)][1]
        (t0, x0), (t1, x1) = keys[i - 1], keys[i]
        return x0 + (x1 - x0) * (t - t0) / (t1 - t0)

    out: list[tuple[float, float]] = []
    start = 0  # output frame of the segment's first frame: joins land exactly on from/FPS
    for a, b in segments:
        o, a, b = start / FPS, a - origin, b - origin
        out.append((o, at(a)))
        out += [(o + t - a, x) for t, x in keys if a < t < b]
        start += frames(b - a)
        out.append((start / FPS, at(b, left=True)))
    return out


def parse_silencedetect(text: str) -> list[list[float]]:
    """[start, end] pairs from silencedetect's output; an open silence runs to FILE_END_S."""
    out: list[list[float]] = []
    start: float | None = None
    for kind, value in _SILENCE.findall(text):
        t = max(0.0, float(value))
        if kind == "start":
            start = t
        elif start is not None:
            out.append([start, t])
            start = None
    if start is not None:
        out.append([start, FILE_END_S])
    return out[:MAX_SILENCES]


def _silences(data: Any) -> list[Span]:
    if not isinstance(data, list) or len(data) > MAX_SILENCES:
        raise ValueError("not a list of silences")
    pairs = []
    for pair in data:
        ok = isinstance(pair, list) and len(pair) == 2
        a, b = pair if ok else (None, None)
        if not (ok and all(_number(x) for x in (a, b)) and 0 <= a <= b):
            raise ValueError("not a list of silences")
        pairs.append((float(a), float(b)))
    return pairs


def _number(x: Any) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def silence_args(section: Path) -> list[str]:
    """ffmpeg silencedetect on the first audio stream; the frame metadata goes to stdout
    (bounded far above stderr's tail), errors only to stderr."""
    detect = f"silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_S}"
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-v", "error",
        *FFMPEG_INPUT_GUARD, "-i", str(section),
        "-map", "0:a:0", "-vn", "-sn", "-dn",
        "-af", f"{detect},ametadata=mode=print:file=-", "-f", "null", "-",
    ]  # fmt: skip


def detect_silences(
    section: Path, deadline: Deadline, *, runner: Runner | None = None
) -> list[Span] | None:
    """Silent ranges (section seconds) of `section`; None with a warning on any failure."""
    return cached_child(
        section, "silences", silence_args(section), _silences, deadline,
        what="silence detection", fallback="no silence cuts", timeout_s=SILENCE_TIMEOUT_S,
        runner=runner, read=lambda out: parse_silencedetect(out.stdout),
    )  # fmt: skip

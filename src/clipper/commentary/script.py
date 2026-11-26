"""A commentary Short's script (CLP-146): the reply schema and the checks run on it.

The `scripting` stage (`clipper.pipeline.commentary_script`) asks Claude for up to
`stories_per_day` stories. Each story is checked here, in this order, and dropped with
the first reason that applies:

1. **Schema** (`Story`): `hook` (at most 12 words), `lines` (each with its `text`, its
   `time_s` on the Short's timeline and its `kind`, `spoken` or `callout`), optional
   `cards`, `title` (at most 70 characters), `description`, `hashtags` (3-5, specific,
   no generic reach or paid-partnership tags), `clips` (an ordered plan of
   `{clip_id, start_s, end_s}`, where `clip_id` is a Twitch clip id) and `format`
   (`hook_type`, `structure`, `topic`).
2. **The clip plan** (`make_plan`): every clip is one of the day's usable candidates, ends
   within it, and was not taken by a story kept before it; every card follows a clip of
   the plan.
3. **Length** (owner, 2026-11-26): 8-12 lines, each at most 10 words, and 40-75 spoken
   words in all (the voice reads about 3.8 words a second at speed 1.15).
4. **Clips first** (spec, "Clips first"): runtime 28-38 s; streamer clips fill the frame
   for at least 70 % of it, script cards and stills for at most 20 %; one of our lines
   (spoken or callout) at least every 4 s, measured between line start times, from the
   Short's start to its end; speech (our spoken line or the streamer's, from the clip's
   transcript) starts by 0.3 s.
5. **Title**: names a streamer and the game of the story's clips.
6. **Names**: every known streamer and game name in the script, title, description or
   hashtags appears in the story's own clips (their transcript, title, streamer or game).
   Known names are the streamers of the candidates the prompt shows, the channel's
   pinned and excluded ones, and the channel's games with their aliases; other words are
   never taken for names.
7. **Profanity** (`better-profanity`'s default word list) in the hook, lines, cards,
   title, description or hashtags.
8. **Rotation** (owner, 2026-11-26): a story is dropped when its `hook_type` is the most
   recent Short's, or its (`hook_type`, `structure`) pair is among the channel's last 5
   Shorts. A story kept earlier in the same reply counts as a more recent Short. The
   palette is the rendering stage's (CLP-148) and never drops a story.

The timeline: cards with `after_clip = -1` come first, then each clip in order, each
followed by the cards with its index in `after_clip`. The runtime is the clips' and cards'
seconds together. The hook card is an overlay on the first clip, so it takes no frame time.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from clipper.metadata import (
    inline_disclosure_tags,
    inline_unusable_tags,
    is_disclosure,
    is_generic,
    links_or_mentions,
)
from clipper.pipeline.common import strip_control

HOOK_TYPES = ("question", "stakes", "quote", "bold_claim", "numbers")
"""How the hook grabs: a question to the viewer; what is at stake; the streamer's own
words; a strong claim; a number or count. `question`, `stakes` and `quote` are the
`HookCard` styles the render already has."""
STRUCTURES = ("play_by_play", "story_arc", "ranking", "versus", "hot_take")
"""How the Short is built: one clip called beat by beat; 2-4 clips told as setup,
escalation, payoff; 2-4 moments ranked; two streamers compared; one clip under an opinion
we argue."""
HookType = Literal["question", "stakes", "quote", "bold_claim", "numbers"]
Structure = Literal["play_by_play", "story_arc", "ranking", "versus", "hot_take"]
LineKind = Literal["spoken", "callout"]

MAX_HOOK_WORDS = 12
MAX_TITLE_CHARS = 70
MAX_DESCRIPTION_CHARS = 1000
"""The model's part; the credit lines are added after it."""
MIN_HASHTAGS, MAX_HASHTAGS = 3, 5
MIN_LINES, MAX_LINES = 8, 12
MAX_LINE_WORDS = 10
MIN_SPOKEN_WORDS, MAX_SPOKEN_WORDS = 40, 75
MAX_LINE_GAP_S = 4.0
SPOKEN_WORDS_PER_S = 3.8
"""Kokoro at speed 1.15 (owner, 2026-11-26)."""
SPOKEN_SLACK_S = 0.1
"""A spoken line may run this far into the next one."""
MIN_RUNTIME_S, MAX_RUNTIME_S = 28.0, 38.0
MIN_CLIP_SHARE = 0.70
MAX_CARD_SHARE = 0.20
MAX_SPEECH_START_S = 0.3
MIN_EXCERPT_S = 1.0
MAX_CARD_S = 4.0
ROTATION_WINDOW = 5
MIN_NAME_CHARS = 3
"""Shorter names (after dropping spaces and punctuation) are too common to look for."""
_EPS = 1e-6
_HASHTAG = re.compile(r"#\w{2,60}")
_TAG_PART = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+|[^\W\d_]+")
MIN_COMPOUND_CHARS = 6
"""A one-case hashtag part this long may be several words run together (#whatthehell)."""
SWEAR_STEMS = (
    "fuck", "shit", "bitch", "damn", "hell", "crap", "piss", "cunt", "slut", "whore",
    "bastard", "asshole", "wtf",
)  # fmt: skip
"""Swears looked for at the start or end of a long one-case hashtag part (#whatthehell,
#hellnah). The full default list there trips ordinary words (#arsenal, #saturday,
#therapist), so the stems are few and must start or end the part (#shellshock passes)."""


# --- the reply schema -----------------------------------------------------------------------


def _text(value: Any) -> Any:
    return strip_control(value) if isinstance(value, str) else value


def _words(text: str) -> int:
    return len(text.split())


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Line(_Model):
    text: str = Field(min_length=1, max_length=200)
    time_s: float = Field(ge=0)
    """When the line starts, in seconds on the Short's timeline."""
    kind: LineKind

    _clean = field_validator("text", mode="before")(_text)


class Card(_Model):
    """A full-frame script card or still between clips."""

    text: str = Field(max_length=120)
    after_clip: int = Field(ge=-1)
    """Index of the clip it follows; -1 opens the Short with it."""
    duration_s: float = Field(gt=0, le=MAX_CARD_S)

    _clean = field_validator("text", mode="before")(_text)


class Excerpt(_Model):
    clip_id: str = Field(min_length=1, max_length=128)
    start_s: float = Field(ge=0)
    end_s: float

    @model_validator(mode="after")
    def _long_enough(self) -> Excerpt:
        if self.end_s - self.start_s < MIN_EXCERPT_S - _EPS:
            raise ValueError(f"an excerpt must be at least {MIN_EXCERPT_S:g} s long")
        return self


class Format(_Model):
    hook_type: HookType
    structure: Structure
    topic: str = Field(min_length=1, max_length=60)

    _clean = field_validator("topic", mode="before")(_text)


class Story(_Model):
    hook: str = Field(min_length=1, max_length=120)
    lines: list[Line] = Field(min_length=1, max_length=40)
    cards: list[Card] = Field(default_factory=list, max_length=8)
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_CHARS)
    hashtags: list[str] = Field(min_length=MIN_HASHTAGS, max_length=MAX_HASHTAGS)
    clips: list[Excerpt] = Field(min_length=1, max_length=8)
    format: Format

    _clean = field_validator("hook", "title", mode="before")(_text)

    @field_validator("lines")
    @classmethod
    def _in_time_order(cls, value: list[Line]) -> list[Line]:
        return sorted(value, key=lambda line: line.time_s)

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        return "\n".join(filter(None, (strip_control(line) for line in value.splitlines())))

    @field_validator("hook")
    @classmethod
    def _short_hook(cls, value: str) -> str:
        if _words(value) > MAX_HOOK_WORDS:
            raise ValueError(f"has {_words(value)} words (at most {MAX_HOOK_WORDS})")
        return value

    @field_validator("hashtags")
    @classmethod
    def _specific_tags(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        for tag in value:
            if not _HASHTAG.fullmatch(tag):
                raise ValueError(f"{tag!r} is not a hashtag")
            if is_generic(tag) or is_disclosure(tag):
                raise ValueError(f"{tag} is a generic or paid-partnership tag")
            if tag.casefold() in seen:
                raise ValueError(f"{tag} is listed twice")
            seen.add(tag.casefold())
        return value


def _schema_for(model: type[BaseModel]) -> dict[str, Any]:
    """A structural JSON schema for `claude -p --json-schema`: types, enums and required
    fields. Counts and lengths are checked per story after the call, so one bad story
    drops alone."""
    props: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        props[name] = _schema_of(info.annotation)
    return {
        "type": "object",
        "properties": props,
        "required": list(model.model_fields),
        "additionalProperties": False,
    }


def _schema_of(annotation: Any) -> dict[str, Any]:
    origin = getattr(annotation, "__origin__", None)
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _schema_for(annotation)
    if origin is list:
        return {"type": "array", "items": _schema_of(annotation.__args__[0])}
    if origin is Literal:
        return {"type": "string", "enum": list(annotation.__args__)}
    return {str: {"type": "string"}, float: {"type": "number"}, int: {"type": "integer"}}[
        annotation
    ]


CLI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"stories": {"type": "array", "items": _schema_for(Story)}},
    "required": ["stories"],
    "additionalProperties": False,
}


# --- inputs ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Source:
    """A usable candidate as the checks see it."""

    clip_id: str
    """The Twitch clip id, e.g. `AwkwardHelplessSalamander-AbC123`."""
    candidate_id: int
    login: str
    name: str | None
    title: str | None
    game_names: tuple[str, ...]
    """The channel's name and aliases for the clip's game."""
    duration_s: float
    transcript: str
    words: tuple[tuple[float, float], ...]
    """(start, end) of each transcript word, in seconds from the clip's start."""
    url: str
    source_kind: str | None = None


@dataclass(frozen=True)
class Lexicon:
    """Names the checks look for in a script."""

    streamers: tuple[str, ...] = ()
    games: tuple[str, ...] = ()

    def names(self) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for name in (*self.streamers, *self.games):
            key = _compact(name)
            if len(key) >= MIN_NAME_CHARS and key not in seen:
                seen.add(key)
                out.append(name)
        return out


@dataclass(frozen=True)
class Plan:
    """The clips-first figures of a story, stored in `commentary_stories.guardrails`."""

    runtime_s: float
    clip_s: float
    card_s: float
    line_count: int
    spoken_words: int
    max_gap_s: float
    speech_start_s: float | None
    excerpts: tuple[dict[str, Any], ...] = field(default=())

    @property
    def clip_share(self) -> float:
        return self.clip_s / self.runtime_s if self.runtime_s > 0 else 0.0

    @property
    def card_share(self) -> float:
        return self.card_s / self.runtime_s if self.runtime_s > 0 else 0.0

    def figures(self) -> dict[str, Any]:
        return {
            "runtime_s": round(self.runtime_s, 3),
            "clip_s": round(self.clip_s, 3),
            "card_s": round(self.card_s, 3),
            "clip_share": round(self.clip_share, 4),
            "card_share": round(self.card_share, 4),
            "line_count": self.line_count,
            "spoken_words": self.spoken_words,
            "max_gap_s": round(self.max_gap_s, 3),
            "speech_start_s": None
            if self.speech_start_s is None
            else round(self.speech_start_s, 3),
            "clip_count": len(self.excerpts),
        }


class Rejected(Exception):
    """The story breaks a rule; the message is the reason. `fixable`: a repair call may
    ask for a corrected version (schema, plan, length, spacing, title, links); a name,
    profanity or rotation drop is final."""

    def __init__(self, reason: str, *, fixable: bool = True) -> None:
        super().__init__(reason)
        self.fixable = fixable


class Drop(NamedTuple):
    index: int
    reason: str
    fixable: bool


# --- the checks -------------------------------------------------------------------------------


def parse_story(raw: object) -> Story:
    if not isinstance(raw, dict):
        raise Rejected("schema: a story must be a JSON object")
    try:
        return Story.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc']) or 'story'}: {e['msg']}" for e in exc.errors()[:3]
        ]
        raise Rejected(f"schema: {'; '.join(problems)}") from None


def make_plan(
    story: Story, sources: Mapping[str, Source], taken: Iterable[str] = ()
) -> tuple[Plan, list[Source]]:
    """The story's timeline and figures, and the sources of its clips in plan order."""
    taken = set(taken)
    used: list[Source] = []
    for excerpt in story.clips:
        source = sources.get(excerpt.clip_id)
        if source is None:
            raise Rejected(f"clip {excerpt.clip_id} is not a usable candidate of the day")
        if excerpt.clip_id in taken:
            raise Rejected(f"clip {excerpt.clip_id} is already used by an earlier story")
        if excerpt.end_s > source.duration_s + 0.05:
            raise Rejected(
                f"clip {excerpt.clip_id} ends at {excerpt.end_s:.1f} s, past the clip's end"
                f" ({source.duration_s:.1f} s)"
            )
        used.append(source)
    if len({e.clip_id for e in story.clips}) != len(story.clips):
        raise Rejected("the plan uses one clip twice")
    for card in story.cards:
        if card.after_clip >= len(story.clips):
            raise Rejected(f"card after_clip {card.after_clip} has no such clip")

    t = 0.0
    card_s = 0.0
    excerpts: list[dict[str, Any]] = []
    speech: list[float] = []

    def cards_after(index: int) -> None:
        nonlocal t, card_s
        for card in story.cards:
            if card.after_clip == index:
                t += card.duration_s
                card_s += card.duration_s

    cards_after(-1)
    for index, (excerpt, source) in enumerate(zip(story.clips, used, strict=True)):
        excerpts.append(
            {
                "clip_id": excerpt.clip_id,
                "start_s": round(excerpt.start_s, 3),
                "end_s": round(excerpt.end_s, 3),
                "at_s": round(t, 3),
                "candidate_id": source.candidate_id,
                "login": source.login,
                "url": source.url,
                "source_kind": source.source_kind,
            }
        )
        speech += [
            t + max(0.0, start - excerpt.start_s)
            for start, end in source.words
            if end > excerpt.start_s and start < excerpt.end_s
        ]
        t += excerpt.end_s - excerpt.start_s
        cards_after(index)
    runtime = t

    for line in story.lines:
        if line.time_s >= runtime:
            raise Rejected(f"a line at {line.time_s:.1f} s is past the end ({runtime:.1f} s)")
    spoken = [line for line in story.lines if line.kind == "spoken"]
    for line, after in zip(spoken, [*spoken[1:], None], strict=True):
        end = line.time_s + _words(line.text) / SPOKEN_WORDS_PER_S
        if after is not None and end > after.time_s + SPOKEN_SLACK_S + _EPS:
            raise Rejected(
                f"the spoken line at {line.time_s:.1f} s runs into the next spoken line at"
                f" {after.time_s:.1f} s (it needs {end - line.time_s:.1f} s at"
                f" {SPOKEN_WORDS_PER_S:g} words/s)"
            )
        if end > runtime + _EPS:
            raise Rejected(
                f"the spoken line at {line.time_s:.1f} s runs past the end ({runtime:.1f} s)"
            )
    times = sorted(line.time_s for line in story.lines)
    gaps = [times[0], *(b - a for a, b in zip(times, times[1:], strict=False))]
    gaps.append(runtime - times[-1])
    speech += [line.time_s for line in story.lines if line.kind == "spoken"]
    plan = Plan(
        runtime_s=runtime,
        clip_s=runtime - card_s,
        card_s=card_s,
        line_count=len(story.lines),
        spoken_words=_spoken_words(story),
        max_gap_s=max(gaps),
        speech_start_s=min(speech) if speech else None,
        excerpts=tuple(excerpts),
    )
    return plan, used


def _spoken_words(story: Story) -> int:
    return sum(_words(line.text) for line in story.lines if line.kind == "spoken")


def length_problem(story: Story) -> str | None:
    count = len(story.lines)
    if not MIN_LINES <= count <= MAX_LINES:
        return f"{count} lines (needs {MIN_LINES}-{MAX_LINES})"
    for line in story.lines:
        if _words(line.text) > MAX_LINE_WORDS:
            return f"a line has {_words(line.text)} words (at most {MAX_LINE_WORDS})"
    spoken = _spoken_words(story)
    if not MIN_SPOKEN_WORDS <= spoken <= MAX_SPOKEN_WORDS:
        return f"{spoken} spoken words (needs {MIN_SPOKEN_WORDS}-{MAX_SPOKEN_WORDS})"
    return None


def clips_first_problem(plan: Plan) -> str | None:
    if not MIN_RUNTIME_S - _EPS <= plan.runtime_s <= MAX_RUNTIME_S + _EPS:
        return f"runtime {plan.runtime_s:.1f} s (needs {MIN_RUNTIME_S:g}-{MAX_RUNTIME_S:g} s)"
    if plan.clip_share < MIN_CLIP_SHARE - _EPS:
        return f"clips fill {plan.clip_share:.0%} of the runtime (needs at least 70%)"
    if plan.card_share > MAX_CARD_SHARE + _EPS:
        return f"cards and stills {plan.card_share:.0%} of the runtime (at most 20%)"
    if plan.max_gap_s > MAX_LINE_GAP_S + _EPS:
        return f"no line of ours for {plan.max_gap_s:.1f} s (needs one every 4 s)"
    if plan.speech_start_s is None:
        return "no speech: no spoken line and no streamer speech"
    if plan.speech_start_s > MAX_SPEECH_START_S + _EPS:
        return f"speech starts at {plan.speech_start_s:.1f} s (needs by 0.3 s)"
    return None


def _compact(text: str) -> str:
    return re.sub(r"[\W_]+", "", text.casefold())


@functools.cache
def _name_pattern(name: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in name.casefold().split()]
    return re.compile(r"(?<![^\W_])" + r"\s+".join(parts) + r"(?![^\W_])")


def _mentions(text: str, name: str) -> bool:
    return bool(_name_pattern(name).search(text.casefold()))


def _script(story: Story) -> str:
    """Our script: the hook, the lines and the cards."""
    return "\n".join(
        [story.hook, *(line.text for line in story.lines), *(c.text for c in story.cards)]
    )


def _script_text(story: Story) -> str:
    return "\n".join([_script(story), story.title, story.description])


def title_problem(story: Story, used: Sequence[Source]) -> str | None:
    """The title names a streamer and the game of the story's clips, as whole words."""
    streamers = [n for s in used for n in (s.login, s.name) if n]
    if not any(_mentions(story.title, name) for name in streamers):
        return "title does not name the streamer"
    games = [g for s in used for g in s.game_names]
    if not any(_mentions(story.title, game) for game in games):
        return "title does not name the game"
    return None


def copy_problem(story: Story) -> str | None:
    """No links, web addresses or @mentions, and no generic or paid-partnership hashtags
    written into the title or description (the credit lines are added after this)."""
    for label, text in (("title", story.title), ("description", story.description)):
        found = links_or_mentions(text)
        if found:
            return f"links or mentions in the {label}: {', '.join(found[:3])}"
        tags = inline_unusable_tags(text) + inline_disclosure_tags(text)
        if tags:
            return f"generic or paid tag in the {label}: {', '.join(tags[:3])}"
    return None


def _tag_parts(tag: str) -> list[str]:
    """A hashtag's words: CamelCase and digits split (#xQcGTA6 -> x, Qc, GTA, 6)."""
    return _TAG_PART.findall(tag.lstrip("#"))


def _tag_mentions(tag: str, name: str) -> bool:
    """`name` is written in `tag`: as a run of its words (#KaiCenatGTA6 names Kai Cenat and
    GTA 6), or at the start or end of a long one-case part (#kaicenatclips, #xqcgta6; not
    "Rust" in #frustrating)."""
    key = _compact(name)
    parts = [_compact(p) for p in _tag_parts(tag)]
    runs = {"".join(parts[i:j]) for i in range(len(parts)) for j in range(i + 1, len(parts) + 1)}
    if key in runs:
        return True
    return len(key) >= MIN_NAME_CHARS and any(
        part.startswith(key) or part.endswith(key)
        for part in parts
        if len(part) >= MIN_COMPOUND_CHARS
    )


def ungrounded_names(story: Story, used: Sequence[Source], lexicon: Lexicon) -> list[str]:
    """Known names the story mentions that its own clips do not show."""
    text = _script_text(story)
    ground = "\n".join(
        part
        for s in used
        for part in (s.login, s.name or "", s.title or "", s.transcript, *s.game_names)
    )
    ground_keys = {_compact(p) for s in used for p in (s.login, s.name or "", *s.game_names)}
    missing = []
    for name in lexicon.names():
        key = _compact(name)
        if not (_mentions(text, name) or any(_tag_mentions(t, name) for t in story.hashtags)):
            continue
        if key in ground_keys or _mentions(ground, name):
            continue
        missing.append(name)
    return missing


@functools.cache
def _profanity() -> Any:
    from better_profanity import Profanity

    checker = Profanity()
    checker.load_censor_words()
    return checker


def _profane_tag(tag: str, known: Iterable[str] = ()) -> bool:
    """A swear word in a hashtag: as one of its CamelCase words, or a `SWEAR_STEMS` stem
    starting or ending a long one-case part (#whatthehell) once the known names are taken
    out (#helldivers2 names a game)."""
    parts = _tag_parts(tag)
    if _profanity().contains_profanity(" ".join(parts)):
        return True
    names = sorted(
        {
            k
            for n in known
            for k in (_compact(n), _compact(n).rstrip("0123456789"))
            if len(k) >= MIN_NAME_CHARS + 1
        },
        key=len,
        reverse=True,
    )
    for part in parts:
        if len(part) < MIN_COMPOUND_CHARS or not (part.islower() or part.isupper()):
            continue
        rest = part.casefold()
        for name in names:
            rest = rest.replace(name, " ")
        for chunk in rest.split():
            if any(chunk.startswith(w) or chunk.endswith(w) for w in SWEAR_STEMS):
                return True
    return False


def profanity_problem(story: Story, known: Iterable[str] = ()) -> str | None:
    """`known`: names (games, streamers) a one-case hashtag may hold without counting as
    swearing."""
    checker = _profanity()
    parts = (
        ("the script", _script(story)),
        ("the title", story.title),
        ("the description", story.description),
    )
    for label, text in parts:
        if checker.contains_profanity(text):
            return f"profanity in {label}"
    known = tuple(known)
    if any(_profane_tag(tag, known) for tag in story.hashtags):
        return "profanity in the hashtags"
    return None


def rotation_problem(fmt: Mapping[str, Any], recent: Sequence[Mapping[str, Any]]) -> str | None:
    """`recent`: the formats of the channel's latest Shorts, most recent first."""
    window = list(recent)[:ROTATION_WINDOW]
    hook, structure = fmt.get("hook_type"), fmt.get("structure")
    if window and window[0].get("hook_type") == hook:
        return f"same hook_type as the latest Short ({hook})"
    if any(r.get("hook_type") == hook and r.get("structure") == structure for r in window):
        return f"{hook}/{structure} was used in the last {ROTATION_WINDOW} Shorts"
    return None


@dataclass(frozen=True)
class Checked:
    """A story that passed every check."""

    index: int
    story: Story
    plan: Plan
    sources: tuple[Source, ...]


def check_story(
    raw: object,
    sources: Mapping[str, Source],
    lexicon: Lexicon,
    recent: Sequence[Mapping[str, Any]],
    taken: Iterable[str] = (),
) -> tuple[Story, Plan, list[Source]]:
    """Every check but rotation's history update; raises Rejected with the reason."""
    story = parse_story(raw)
    plan, used = make_plan(story, sources, taken)
    for problem in (
        length_problem(story),
        clips_first_problem(plan),
        title_problem(story, used),
        copy_problem(story),
    ):
        if problem:
            raise Rejected(problem)
    missing = ungrounded_names(story, used, lexicon)
    if missing:
        raise Rejected(f"names not in its clips: {', '.join(missing)}", fixable=False)
    for problem in (
        profanity_problem(story, lexicon.names()),
        rotation_problem(story.format.model_dump(), recent),
    ):
        if problem:
            raise Rejected(problem, fixable=False)
    return story, plan, used


def evaluate(
    raw_stories: Sequence[object],
    sources: Mapping[str, Source],
    lexicon: Lexicon,
    recent: Sequence[Mapping[str, Any]],
    wanted: int,
    *,
    before: Sequence[Checked] = (),
    first_index: int = 0,
) -> tuple[list[Checked], list[Drop]]:
    """The first `wanted` stories that pass, in reply order, and each dropped one. Stories
    after the `wanted`-th kept one are not checked. `before`: stories already kept (an
    earlier reply of the same day), whose clips and formats count; `first_index` numbers
    this reply's stories after the earlier ones."""
    kept: list[Checked] = []
    dropped: list[Drop] = []
    history = [c.story.format.model_dump() for c in reversed(before)] + list(recent)
    taken: set[str] = {e.clip_id for c in before for e in c.story.clips}
    for index, raw in enumerate(raw_stories, start=first_index):
        if len(kept) >= wanted:
            break
        try:
            story, plan, used = check_story(raw, sources, lexicon, history, taken)
        except Rejected as exc:
            dropped.append(Drop(index, str(exc), exc.fixable))
            continue
        kept.append(Checked(index, story, plan, tuple(used)))
        taken |= {e.clip_id for e in story.clips}
        history.insert(0, story.format.model_dump())
    return kept, dropped

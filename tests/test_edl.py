import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipper.edl import (
    EDL,
    EDL_SCHEMA_VERSION,
    MAX_CROP_KEYS,
    MAX_CUTS,
    MAX_SEGMENTS,
    dump_json_schema,
    export_json_schema,
    parse_edl,
)

REPO = Path(__file__).resolve().parents[1]
SCHEMA_FILE = REPO / "render" / "schema" / "edl.schema.json"
FIXTURES = REPO / "render" / "test" / "fixtures"


def _fixtures(kind: str) -> list[Path]:
    return sorted((FIXTURES / kind).glob("*.json"))


def test_fixtures_exist() -> None:
    assert _fixtures("valid")
    assert _fixtures("invalid")


@pytest.mark.parametrize("path", _fixtures("valid"), ids=lambda p: p.stem)
def test_valid_fixtures_parse(path: Path) -> None:
    """Parity with render/test/validate.test.ts: same files, same verdict."""
    edl = parse_edl(path.read_text())
    assert parse_edl(edl.to_props()) == edl


@pytest.mark.parametrize("path", _fixtures("invalid"), ids=lambda p: p.stem)
def test_invalid_fixtures_rejected(path: Path) -> None:
    with pytest.raises(ValidationError):
        parse_edl(path.read_text())


def test_committed_schema_matches_model() -> None:
    """Drift check. Regenerate with:
    uv run python -m clipper.edl > render/schema/edl.schema.json
    """
    assert SCHEMA_FILE.read_text() == dump_json_schema()


def test_schema_shape() -> None:
    schema = export_json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["source"]
    assert schema["$defs"]["Layout"]["enum"] == ["blurred_fill", "face_track"]
    assert schema["$defs"]["HookStyle"]["enum"] == ["stakes", "question", "quote"]
    assert schema["$defs"]["CaptionStyle"]["enum"] == ["bold_pop"]


def test_defaults_and_props() -> None:
    edl = parse_edl({"source": {"path": "a.mp4", "in_s": 0, "out_s": 5}})
    assert edl.to_props() == {
        "schema_version": 2,
        "source": {"path": "a.mp4", "in_s": 0.0, "out_s": 5.0},
        "layout": "blurred_fill",
        "hook": None,
        "words": [],
        "captions_enabled": True,
        "caption_style": "bold_pop",
        "progress_bar": True,
        "crop": None,
        "segments": None,
        "cuts": [],
    }
    json.dumps(edl.to_props())  # JSON-serializable


def test_hook_defaults() -> None:
    edl = parse_edl({"source": {"path": "a.mp4", "in_s": 0, "out_s": 5}, "hook": {"text": "Hi"}})
    assert edl.hook is not None
    assert (edl.hook.style, edl.hook.duration_s) == ("stakes", 2.0)


@pytest.mark.parametrize(
    ("in_s", "out_s", "frames"),
    [(12.5, 42.5, 900), (0, 1.05, 32), (0, 5, 150), (1.0, 1.0 + 1 / 60, None)],
)
def test_duration_in_frames(in_s: float, out_s: float, frames: int | None) -> None:
    data = {"source": {"path": "a.mp4", "in_s": in_s, "out_s": out_s}}
    if frames is None:
        with pytest.raises(ValidationError):
            parse_edl(data)
        return
    edl = parse_edl(data)
    assert edl.duration_in_frames == frames
    assert edl.duration_s == pytest.approx(out_s - in_s)


def test_model_is_frozen_and_strict() -> None:
    edl = parse_edl({"source": {"path": "a.mp4", "in_s": 0, "out_s": 5}})
    with pytest.raises(ValidationError):
        edl.progress_bar = False  # type: ignore[misc]
    with pytest.raises(ValidationError):
        EDL.model_validate({"source": {"path": "a.mp4", "in_s": 0, "out_s": 5}, "x": 1})


def test_parse_accepts_bytes() -> None:
    raw = b'{"source": {"path": "dir/a b.mp4", "in_s": 0, "out_s": 5}}'
    assert parse_edl(raw).source.path == "dir/a b.mp4"


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("field", ["in_s", "out_s", "hook", "word_start", "word_end"])
def test_non_finite_numbers_rejected(bad: float, field: str) -> None:
    data: dict = {
        "source": {"path": "a.mp4", "in_s": 0.0, "out_s": 5.0},
        "hook": {"text": "x", "duration_s": 2.0},
        "words": [{"text": "w", "start_s": 0.5, "end_s": 1.0}],
    }
    if field in ("in_s", "out_s"):
        data["source"][field] = bad
    elif field == "hook":
        data["hook"]["duration_s"] = bad
    else:
        data["words"][0][field.replace("word_", "") + "_s"] = bad
    with pytest.raises(ValidationError):
        parse_edl(data)  # JSON path ("Infinity"/"NaN" tokens)
    with pytest.raises(ValidationError):
        EDL.model_validate(data)  # Python path


def _face_track(crop: list[dict]) -> dict:
    return {
        "source": {"path": "a.mp4", "in_s": 0, "out_s": 5},
        "layout": "face_track",
        "crop": crop,
    }


def test_face_track_crop_keys() -> None:
    edl = parse_edl(_face_track([{"t_s": 0, "x_center": 0.4}, {"t_s": 0, "x_center": 0.6}]))
    assert edl.layout == "face_track"
    assert edl.to_props()["crop"] == [{"t_s": 0.0, "x_center": 0.4}, {"t_s": 0.0, "x_center": 0.6}]


@pytest.mark.parametrize(
    ("layout", "crop", "message"),
    [
        ("face_track", None, "crop is required"),
        ("blurred_fill", [{"t_s": 0, "x_center": 0.5}], "crop is only allowed"),
        ("face_track", [{"t_s": 1, "x_center": 0.5}, {"t_s": 0.5, "x_center": 0.5}], "sorted"),
        ("face_track", [{"t_s": 0, "x_center": -0.01}], "greater than or equal"),
        ("face_track", [{"t_s": 0, "x_center": 0.5}] * (MAX_CROP_KEYS + 1), "at most"),
    ],
)
def test_crop_validation(layout: str, crop: list[dict] | None, message: str) -> None:
    data = {**_face_track(crop or []), "layout": layout, "crop": crop}
    with pytest.raises(ValidationError, match=message):
        parse_edl(data)


def test_crop_key_limit() -> None:
    assert MAX_CROP_KEYS == 2000
    parse_edl(_face_track([{"t_s": i / 100, "x_center": 0.5} for i in range(MAX_CROP_KEYS)]))


# --- EDL v2: segments and cuts (CLP-73) ---------------------------------------------------

F = 1 / 30


def _v2(segments: list[tuple[float, float]], **kw: object) -> dict:
    return {
        "source": {"path": "a.mp4", "in_s": segments[0][0], "out_s": segments[-1][1]},
        "segments": [{"in_s": a, "out_s": b} for a, b in segments],
        **kw,
    }


def _cut(**kw: object) -> dict:
    return {
        "in_s": 2.0,
        "out_s": 3.0,
        "kind": "silence",
        "confidence": 1.0,
        "reason": "pause 1.20s -> 0.14s",
        "applied": True,
        **kw,
    }


def test_schema_version_is_2_and_v1_still_parses() -> None:
    assert EDL_SCHEMA_VERSION == 2
    v1 = parse_edl({"schema_version": 1, "source": {"path": "a.mp4", "in_s": 1.0, "out_s": 6.0}})
    # A v1 EDL migrates to one segment: the whole range.
    assert v1.schema_version == 1 and v1.segments is None
    assert v1.spans == ((1.0, 6.0),)
    assert (v1.duration_s, v1.duration_in_frames) == (5.0, 150)


def test_v1_cannot_carry_segments_or_cuts() -> None:
    with pytest.raises(ValidationError, match="schema_version 2"):
        parse_edl({**_v2([(0.0, 2.0), (3.0, 5.0)]), "schema_version": 1})
    with pytest.raises(ValidationError, match="schema_version 2"):
        parse_edl(
            {"schema_version": 1, "source": {"path": "a.mp4", "in_s": 0, "out_s": 5},
             "cuts": [_cut()]}
        )  # fmt: skip


def test_segments_set_the_duration() -> None:
    edl = parse_edl(_v2([(1.0, 3.0), (90 * F, 6.0)], cuts=[_cut(in_s=3.0, out_s=90 * F)]))
    assert edl.spans == ((1.0, 3.0), (90 * F, 6.0))
    assert edl.duration_s == pytest.approx(2.0 + 3.0)
    assert edl.duration_in_frames == 60 + 90
    assert edl.cuts[0].kind == "silence" and edl.cuts[0].applied is True


def test_hook_is_checked_against_the_cut_duration() -> None:
    with pytest.raises(ValidationError, match="hook.duration_s"):
        parse_edl(_v2([(0.0, 0.5), (5.0, 6.0)], hook={"text": "x", "duration_s": 2.0}))


@pytest.mark.parametrize(
    ("segments", "message"),
    [
        ([(0.0, 2.0), (1.5, 4.0)], "sorted and must not overlap"),
        ([(2.0, 4.0), (0.0, 1.0)], "sorted and must not overlap"),
        ([(0.0, 2.0), (3.0, 3.2)], "at least 0.3"),
        ([(0.0, 2.0), (3.01, 4.0)], "frame grid"),
        ([(0.0, 0.4), (1.0, 1.4)], "between 1 and 180"),
    ],
)
def test_segment_rules(segments: list[tuple[float, float]], message: str) -> None:
    data = _v2(segments)
    data["source"]["in_s"] = min(seg[0] for seg in segments)
    data["source"]["out_s"] = max(seg[1] for seg in segments)
    with pytest.raises(ValidationError, match=message):
        parse_edl(data)


def test_segments_must_span_the_source() -> None:
    data = _v2([(0.0, 2.0), (3.0, 5.0)])
    data["source"]["out_s"] = 6.0
    with pytest.raises(ValidationError, match="span source"):
        parse_edl(data)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "laugh"},
        {"confidence": 1.5},
        {"confidence": -0.1},
        {"reason": "x" * 201},
        {"reason": " "},
        {"in_s": 3.0, "out_s": 2.0},
        {"applied": 1},
        {"extra": True},
    ],
)
def test_cut_rules(bad: dict) -> None:
    with pytest.raises(ValidationError):
        parse_edl(_v2([(0.0, 2.0), (3.0, 5.0)], cuts=[_cut(**bad)]))


def test_segment_and_cut_limits() -> None:
    assert (MAX_SEGMENTS, MAX_CUTS) == (600, 1000)
    with pytest.raises(ValidationError):
        parse_edl(_v2([(0.0, 5.0)], cuts=[_cut()] * (MAX_CUTS + 1)))

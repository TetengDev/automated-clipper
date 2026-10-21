import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from clipper.edl import EDL, dump_json_schema, export_json_schema, parse_edl

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
    assert schema["$defs"]["Layout"]["enum"] == ["blurred_fill"]
    assert schema["$defs"]["HookStyle"]["enum"] == ["stakes", "question", "quote"]
    assert schema["$defs"]["CaptionStyle"]["enum"] == ["bold_pop"]


def test_defaults_and_props() -> None:
    edl = parse_edl({"source": {"path": "a.mp4", "in_s": 0, "out_s": 5}})
    assert edl.to_props() == {
        "schema_version": 1,
        "source": {"path": "a.mp4", "in_s": 0.0, "out_s": 5.0},
        "layout": "blurred_fill",
        "hook": None,
        "words": [],
        "captions_enabled": True,
        "caption_style": "bold_pop",
        "progress_bar": True,
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

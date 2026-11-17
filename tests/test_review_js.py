"""Keyboard shortcuts (CLP-20) and the per-platform text counters (CLP-117) in
static/review.js, run under node with a tiny DOM stub."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from clipper.review.app import STATIC_DIR

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [scriptPath, scenario] = process.argv.slice(1);
const spec = JSON.parse(scenario);
const log = [];
function el(id, tag, extra) {
  return Object.assign({ id, tagName: tag, disabled: false, value: "",
    click() { log.push("click:" + id); }, focus() { log.push("focus:" + id); } }, extra);
}
const els = {};
for (const [id, tag, extra] of spec.elements) els[id] = el(id, tag, extra || {});
let handler = null;
const document = {
  getElementById: (id) => els[id] || null,
  addEventListener: (type, fn) => { if (type === "keydown") handler = fn; },
  querySelectorAll: () => [],
};
const window = { location: { assign: (href) => log.push("go:" + href) } };
vm.runInNewContext(fs.readFileSync(scriptPath, "utf8"), { document, window });
for (const [key, targetId] of spec.keys) {
  const event = { key, target: els[targetId] || { tagName: "BODY" }, ctrlKey: false,
    metaKey: false, altKey: false, defaultPrevented: false,
    preventDefault() { log.push("prevent:" + key); } };
  handler(event);
}
process.stdout.write(JSON.stringify(log));
"""

CLIP_PAGE = [
    ["approve-btn", "BUTTON"],
    ["reject-btn", "BUTTON"],
    ["reason-tag", "SELECT"],
    ["next-clip", "A", {"href": "/clip/2"}],
    ["prev-clip", "A", {"href": "/clip/4"}],
]


def _node(harness: str, scenario: object, script: Path = STATIC_DIR / "review.js") -> Any:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    out = subprocess.run(  # noqa: S603
        [node, "-e", harness, str(script), json.dumps(scenario)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


def run(elements: list[list[object]], keys: list[list[str | None]]) -> list[str]:
    return _node(HARNESS, {"elements": elements, "keys": keys})


def test_a_approves_and_j_k_navigate() -> None:
    log = run(CLIP_PAGE, [["a", None], ["j", None], ["k", None]])

    assert log == [
        "click:approve-btn",
        "prevent:a",
        "go:/clip/2",
        "prevent:j",
        "go:/clip/4",
        "prevent:k",
    ]


def test_r_focuses_reason_then_r_in_picker_submits() -> None:
    elements = [*CLIP_PAGE[:2], ["reason-tag", "SELECT", {"value": ""}], *CLIP_PAGE[3:]]

    first = run(elements, [["r", None], ["r", "reason-tag"]])
    chosen = [*CLIP_PAGE[:2], ["reason-tag", "SELECT", {"value": "boring"}]]
    second = run(chosen, [["r", "reason-tag"]])

    assert first == ["focus:reason-tag", "prevent:r"]  # no reason yet: picker r does nothing
    assert second == ["click:reject-btn", "prevent:r"]


def test_r_with_reason_selected_submits_from_page() -> None:
    elements = [*CLIP_PAGE[:2], ["reason-tag", "SELECT", {"value": "unsafe"}]]

    assert run(elements, [["r", None]]) == ["click:reject-btn", "prevent:r"]


def test_keys_do_nothing_on_pages_without_controls() -> None:
    assert run([], [["a", None], ["r", None], ["j", None], ["k", None], ["x", None]]) == []


def test_typing_in_inputs_and_disabled_buttons_are_ignored() -> None:
    elements = [["approve-btn", "BUTTON", {"disabled": True}], ["note", "INPUT"]]

    assert run(elements, [["a", None], ["j", "note"]]) == []


def _sensitive_page(checked: bool) -> list[list[object]]:
    return [*CLIP_PAGE, ["confirm-sensitive", "INPUT", {"type": "checkbox", "checked": checked}]]


def test_a_on_unconfirmed_sensitive_clip_focuses_the_confirm_box() -> None:
    """CLP-87: like `r` and the reason picker; never approves while the box is unticked."""
    log = run(_sensitive_page(False), [["a", None], ["a", "confirm-sensitive"]])

    assert "click:approve-btn" not in log
    assert log[:2] == ["focus:confirm-sensitive", "prevent:a"]


def test_shortcuts_still_work_with_focus_on_the_confirm_box() -> None:
    """Review: a checkbox is not text entry, so j/k/r keep working after `a` focused it."""
    keys = [["j", "confirm-sensitive"], ["k", "confirm-sensitive"], ["r", "confirm-sensitive"]]

    log = run(_sensitive_page(False), keys)

    assert log == [
        "go:/clip/2",
        "prevent:j",
        "go:/clip/4",
        "prevent:k",
        "focus:reason-tag",
        "prevent:r",
    ]


def test_a_approves_once_the_sensitive_clip_is_confirmed() -> None:
    log = run(_sensitive_page(True), [["a", None], ["a", "confirm-sensitive"]])

    assert log == ["click:approve-btn", "prevent:a", "click:approve-btn", "prevent:a"]


# --- counters (CLP-117) -------------------------------------------------------------------------

COUNTER_HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const [scriptPath, scenario] = process.argv.slice(1);
const spec = JSON.parse(scenario);
const els = {};
function el(id) {
  return els[id] || (els[id] = { id, textContent: "", classes: [],
    classList: { toggle(c, on) { const s = new Set(els[id].classes); on ? s.add(c) : s.delete(c);
      els[id].classes = [...s]; } } });
}
const attrs = { "data-counter": "ig-count", "data-limit": String(spec.limit),
  "data-count": spec.count, "data-unit": spec.unit, "data-label": "Instagram caption" };
const field = { value: spec.initial, hasAttribute: (k) => k in attrs,
  getAttribute: (k) => attrs[k] };
el("variant-count-status");
el("ig-count");
let handler = null;
const timers = new Map();
let next = 1;
const document = {
  getElementById: (id) => els[id] || null,
  querySelectorAll: () => [field],
  addEventListener: (type, fn) => { if (type === "input") handler = fn; },
};
const window = {
  setTimeout: (fn) => { timers.set(next, fn); return next++; },
  clearTimeout: (t) => { timers.delete(t); },
  location: {},
};
vm.runInNewContext(fs.readFileSync(scriptPath, "utf8"), { document, window });
const log = [];
for (const step of spec.steps) {
  field.value = step.value;
  handler({ target: field });
  if (step.pause) { for (const fn of [...timers.values()]) fn(); timers.clear(); }
  const status = els["variant-count-status"];
  log.push({ counter: els["ig-count"].textContent, over: els["ig-count"].classes.includes("over"),
    said: status.textContent });
  status.textContent = "";
}
handler({ target: { hasAttribute: () => false } });  // other inputs are ignored
process.stdout.write(JSON.stringify(log));
"""
FIRE = "\U0001f525"  # one code point, two UTF-16 units


def count(
    steps: list[tuple[int, bool]],
    *,
    limit: int = 10,
    initial: int = 4,
    utf16: bool = False,
    script: Path = STATIC_DIR / "review.js",
) -> list[dict[str, Any]]:
    """Types `n` emoji at each step (pausing after it when the flag is set) and returns
    what the counter and the live region show after each."""
    scenario = {
        "limit": limit,
        "count": "utf16" if utf16 else "code-points",
        "unit": "UTF-16 units" if utf16 else "characters",
        "initial": FIRE * initial,
        "steps": [{"value": FIRE * n, "pause": pause} for n, pause in steps],
    }
    return _node(COUNTER_HARNESS, scenario, script)


def test_the_counter_counts_in_the_platforms_units() -> None:
    (code_points,) = count([(7, False)])
    (utf16,) = count([(4, False)], utf16=True)

    assert code_points == {"counter": "7 of 10 characters", "over": False, "said": ""}
    assert utf16 == {"counter": "8 of 10 UTF-16 units", "over": False, "said": ""}
    (over,) = count([(12, False)])
    assert over["counter"] == "2 characters over the limit of 10" and over["over"]


def test_the_live_region_speaks_only_when_the_count_changes_zone() -> None:
    """Fix round 1: silent while well within the limit, even after a pause; it speaks on
    entering the last 10%, on passing the limit, and on dropping back out of each."""
    steps = [(5, True), (6, True), (9, True), (10, True), (11, True), (12, True)]
    steps += [(10, True), (3, True), (4, True)]

    said = [step["said"] for step in count(steps)]

    assert said == [
        "",
        "",
        "Instagram caption: 9 of 10 characters",
        "",
        "Instagram caption: 1 characters over the limit of 10",
        "",
        "Instagram caption: 10 of 10 characters",
        "Instagram caption: 3 of 10 characters",
        "",
    ]


def test_crossing_into_the_last_tenth_and_back_before_a_pause_says_nothing() -> None:
    log = count([(9, False), (2, True)])

    assert [step["said"] for step in log] == ["", ""]
    assert log[-1]["counter"] == "2 of 10 characters"  # the visible counter stays live

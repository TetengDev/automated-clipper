"""Keyboard shortcuts in static/review.js, run under node with a tiny DOM stub (CLP-20)."""

from __future__ import annotations

import json
import shutil
import subprocess

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


def run(elements: list[list[object]], keys: list[list[str | None]]) -> list[str]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    scenario = json.dumps({"elements": elements, "keys": keys})
    out = subprocess.run(  # noqa: S603
        [node, "-e", HARNESS, str(STATIC_DIR / "review.js"), scenario],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


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

"""Generate src/clipper/hashtag_data.py: the Unicode data the hashtag checks need (CLP-112).

Sources, Unicode 15.0.0 (the version of Python 3.12's `unicodedata`):

    https://www.unicode.org/Public/security/15.0.0/confusables.txt  (UTS #39)
    https://www.unicode.org/Public/15.0.0/ucd/Scripts.txt  (UAX #24)
    https://www.unicode.org/Public/15.0.0/ucd/ScriptExtensions.txt
    https://www.unicode.org/Public/15.0.0/ucd/PropertyValueAliases.txt

Run it with the project's Python, so the character categories match the runtime:

    uv run python scripts/gen_hashtag_data.py [DIR]

DIR holds the four files already downloaded; without it they are fetched. The output is
checked in, so nothing is downloaded at runtime or in tests.

What it writes:
- CONFUSABLES: each character that survives `metadata._prefold` (compatibility-decomposed,
  case-folded, marks dropped) mapped to the ASCII it looks like, from confusables.txt. Only
  letters and digits, and only targets that are [a-z0-9_] after the same folding: a hashtag
  is compared with ASCII reach and disclosure tags, so nothing else can matter. Added to
  confusables.txt, which leaves some out (`ᴀ`, `ᴅ`, `ꜰ`, `ᴘ`) or maps them to another
  non-ASCII letter (`ᴍ` -> `ʍ`): the Latin small capitals, read from their names
  (LATIN LETTER SMALL CAPITAL X -> x). Each target is read through the table again, so the
  module needs one pass (`ᴍ` -> m -> rn, as `m` itself reads in confusables.txt).
- SCRIPT_STARTS / SCRIPT_NAMES: the Script property of every letter, mark and number, as
  sorted range starts (other characters fall in the gaps and are never looked up).
- MARK_SCRIPTS: the Script_Extensions of the combining marks that have one (the kana voiced
  sound marks are Hiragana and Katakana; most Inherited marks have none).
"""

from __future__ import annotations

import re
import sys
import unicodedata
import urllib.request
from collections.abc import Iterator
from pathlib import Path

VERSION = "15.0.0"
URLS = {
    "confusables.txt": f"https://www.unicode.org/Public/security/{VERSION}/confusables.txt",
    "Scripts.txt": f"https://www.unicode.org/Public/{VERSION}/ucd/Scripts.txt",
    "ScriptExtensions.txt": f"https://www.unicode.org/Public/{VERSION}/ucd/ScriptExtensions.txt",
    "PropertyValueAliases.txt": f"https://www.unicode.org/Public/{VERSION}/ucd/PropertyValueAliases.txt",
}
OUT = Path(__file__).resolve().parent.parent / "src" / "clipper" / "hashtag_data.py"
_ASCII = re.compile(r"[a-z0-9_]+")
_SMALL_CAPITAL = re.compile(r"LATIN LETTER SMALL CAPITAL ([A-Z])")


def _read(name: str, folder: Path | None) -> str:
    if folder is not None:
        return (folder / name).read_text(encoding="utf-8")
    with urllib.request.urlopen(URLS[name], timeout=60) as resp:  # noqa: S310 (fixed https URL)
        return resp.read().decode("utf-8")


def _rows(text: str) -> Iterator[list[str]]:
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            yield [field.strip() for field in line.split(";")]


def _span(field: str) -> range:
    first, _, last = field.partition("..")
    return range(int(first, 16), int(last or first, 16) + 1)


def _prefold(text: str) -> str:
    """Must match `clipper.metadata._prefold`."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text).casefold())
    return "".join(c for c in text if not unicodedata.category(c).startswith("M"))


def confusables(text: str) -> dict[int, str]:
    raw: dict[str, str] = {}
    for source, target, *_ in _rows(text):
        raw[chr(int(source, 16))] = "".join(chr(int(cp, 16)) for cp in target.split())
    for cp in range(0x110000):
        match = _SMALL_CAPITAL.fullmatch(unicodedata.name(chr(cp), ""))
        if match and not _ASCII.fullmatch(_prefold(raw.get(chr(cp), "-"))):
            raw[chr(cp)] = match.group(1).lower()
    # Only characters the runtime sees: a capital is case-folded first, and its small letter
    # need not look like the same thing (long s is confusable with f, but folds to s).
    table: dict[int, str] = {}
    for source, target in sorted(raw.items()):
        if unicodedata.category(source)[0] not in "LN" or _prefold(source) != source:
            continue
        ascii_ = _prefold(target)
        if _ASCII.fullmatch(ascii_) and ascii_ != source:
            table[ord(source)] = ascii_
    while closed := {cp: t.translate(table) for cp, t in table.items() if t.translate(table) != t}:
        table.update(closed)
    return table


def _script_names(aliases: str) -> dict[str, str]:
    """Short and long Script property names to the long name in capitals ("TAI LE")."""
    names: dict[str, str] = {}
    for row in _rows(aliases):
        if row[0] == "sc":
            long = row[2].replace("_", " ").upper()
            for alias in row[1:]:
                names[alias] = long
    return names


def scripts(text: str, names: dict[str, str]) -> list[tuple[int, str]]:
    script: dict[int, str] = {}
    for field, value in _rows(text):
        for cp in _span(field):
            script[cp] = names[value]
    starts: list[tuple[int, str]] = []
    for cp in range(0x110000):
        if unicodedata.category(chr(cp))[0] not in "LMN":
            continue
        name = script.get(cp, "UNKNOWN")
        if not starts or starts[-1][1] != name:
            starts.append((cp, name))
    return starts


def mark_scripts(text: str, names: dict[str, str]) -> dict[int, tuple[str, ...]]:
    marks: dict[int, tuple[str, ...]] = {}
    for field, value in _rows(text):
        for cp in _span(field):
            if unicodedata.category(chr(cp)).startswith("M"):
                marks[cp] = tuple(sorted(names[v] for v in value.split()))
    return marks


def _char(cp: int) -> str:
    return unicodedata.name(chr(cp), f"U+{cp:04X}")


def _mark_entry(cp: int, names: tuple[str, ...]) -> list[str]:
    """One MARK_SCRIPTS entry, laid out the way `ruff format` would."""
    quoted = [f'"{name}"' for name in names]
    line = f"    0x{cp:04X}: ({', '.join(quoted)}{',' if len(quoted) == 1 else ''}),"
    if len(line) <= 100:
        return [f"    # {_char(cp)}", line]
    return [
        f"    # {_char(cp)}",
        f"    0x{cp:04X}: (",
        *(f"        {q}," for q in quoted),
        "    ),",
    ]


def render(
    table: dict[int, str], starts: list[tuple[int, str]], marks: dict[int, tuple[str, ...]]
) -> str:
    out = [
        '"""Unicode data for the hashtag checks in `clipper.metadata` (CLP-112). Generated by',
        f"scripts/gen_hashtag_data.py from Unicode {VERSION} confusables.txt (UTS #39),",
        'Scripts.txt and ScriptExtensions.txt; do not edit by hand."""',
        "",
        f'UNICODE_VERSION = "{VERSION}"',
        "",
        "CONFUSABLES: dict[int, str] = {",
        *(f'    0x{cp:04X}: "{target}",  # {_char(cp)}' for cp, target in table.items()),
        "}",
        '"""A folded letter or digit -> the ASCII it looks like."""',
        "",
        "SCRIPT_STARTS: tuple[int, ...] = (",
        *(f"    0x{cp:04X},  # {_char(cp)}" for cp, _ in starts),
        ")",
        "SCRIPT_NAMES: tuple[str, ...] = (",
        *(f'    "{name}",' for _, name in starts),
        ")",
        '"""The Script of letters, marks and numbers from SCRIPT_STARTS[i] to the next start."""',
        "",
        "MARK_SCRIPTS: dict[int, tuple[str, ...]] = {",
        *(line for cp, names in marks.items() for line in _mark_entry(cp, names)),
        "}",
        '"""Script_Extensions of the combining marks that have one: the scripts a mark may',
        'sit on."""',
        "",
    ]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    if unicodedata.unidata_version != VERSION:
        print(f"run with Python's Unicode {VERSION}, not {unicodedata.unidata_version}")
        return 1
    folder = Path(argv[1]) if len(argv) > 1 else None
    names = _script_names(_read("PropertyValueAliases.txt", folder))
    OUT.write_text(
        render(
            confusables(_read("confusables.txt", folder)),
            scripts(_read("Scripts.txt", folder), names),
            mark_scripts(_read("ScriptExtensions.txt", folder), names),
        ),
        encoding="utf-8",
    )
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

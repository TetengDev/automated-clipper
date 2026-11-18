"""Generate src/clipper/tld_data.py: the IANA root-zone TLDs the link checks need (CLP-116).

Source, the IANA list of top-level domains in the root zone:

    https://data.iana.org/TLD/tlds-alpha-by-domain.txt

Run it with the project's Python, so the Unicode folding matches the runtime:

    uv run python scripts/gen_tld_data.py [FILE]

FILE is the list already downloaded; without it the list is fetched. A file whose first
line is not the IANA header (`# Version YYYYMMDDNN, Last Updated ...`) is refused, and so
is a list older than the one vendored now. The output is checked in, so nothing is
downloaded at runtime or in tests.

What it writes:
- IANA_VERSION, IANA_UPDATED: the list's version and date, from its header.
- TLDS: every TLD in lower case. An IDN TLD is there twice, as its punycode label
  (`xn--p1ai`) and as Unicode (`рф`), the Unicode one folded as the link pattern reads
  text (`metadata._match_copy`): NFKC, combining marks dropped, so `कॉम` is `कम`.
"""

from __future__ import annotations

import re
import sys
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

URL = "https://data.iana.org/TLD/tlds-alpha-by-domain.txt"
OUT = Path(__file__).resolve().parent.parent / "src" / "clipper" / "tld_data.py"
_HEADER = re.compile(r"# Version (\d{10}), Last Updated (.+)")
_VENDORED = re.compile(r'^IANA_VERSION = "(\d{10})"$', re.MULTILINE)


def _read(path: Path | None) -> str:
    if path is not None:
        return path.read_text(encoding="utf-8")
    with urllib.request.urlopen(URL, timeout=60) as resp:  # noqa: S310 (fixed URL)
        return resp.read().decode("utf-8")


def check_header(text: str) -> tuple[str, str]:
    """The list's version and last-updated date, from its first line; a file without that
    header (or with a version that is not a date and a two-digit serial) is refused."""
    first = text.split("\n", 1)[0].strip()
    match = _HEADER.fullmatch(first)
    try:
        if match is None:
            raise ValueError(first)
        datetime.strptime(match.group(1)[:8], "%Y%m%d")
    except ValueError:
        raise SystemExit("this is not the IANA root-zone list") from None
    return match.group(1), match.group(2).strip()


def check_not_older(version: str, vendored: str | None) -> None:
    """Refuse a list older than the vendored one: regenerating must not drop new TLDs."""
    if vendored is not None and version < vendored:
        raise SystemExit(f"list {version} is older than the vendored {vendored}")


def _fold(label: str) -> str:
    """`label` as `clipper.metadata._match_copy` reads it, in lower case. Must match it."""
    chars = (c for char in label for c in unicodedata.normalize("NFKC", char))
    return "".join(c for c in chars if not unicodedata.category(c).startswith("M")).lower()


def entries(text: str) -> dict[str, str]:
    """Each TLD, sorted, mapped to the label the list gives it (`XN--11B4C3D` for `कम`)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        label = line.strip()
        if not label or label.startswith("#"):
            continue
        out[label.lower()] = label
        if label.lower().startswith("xn--"):
            out[_fold(label[4:].lower().encode("ascii").decode("punycode"))] = label
    return dict(sorted(out.items()))


def tlds(text: str) -> list[str]:
    return list(entries(text))


def render(names: dict[str, str], version: str, updated: str) -> str:
    def entry(name: str, label: str) -> str:
        comment = "" if name == label.lower() else f"  # {label}"
        return f'        "{name}",{comment}'

    out = [
        '"""IANA root-zone TLDs for the link checks in `clipper.metadata` (CLP-116). Generated',
        "by scripts/gen_tld_data.py from https://data.iana.org/TLD/tlds-alpha-by-domain.txt;",
        'do not edit by hand."""',
        "",
        f'IANA_VERSION = "{version}"',
        f'IANA_UPDATED = "{updated}"',
        "",
        "TLDS: frozenset[str] = frozenset(",
        "    {",
        *(entry(name, label) for name, label in names.items()),
        "    }",
        ")",
        '"""Every TLD in lower case; an IDN TLD as punycode and as folded Unicode."""',
        "",
    ]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    text = _read(Path(argv[1]) if len(argv) > 1 else None)
    version, updated = check_header(text)
    vendored = _VENDORED.search(OUT.read_text(encoding="utf-8")) if OUT.exists() else None
    check_not_older(version, vendored.group(1) if vendored else None)
    OUT.write_text(render(entries(text), version, updated), encoding="utf-8")
    print(f"wrote {OUT} (IANA list {version})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

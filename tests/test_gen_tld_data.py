"""The generator of `clipper.tld_data` (CLP-116), run on a small inline sample of the IANA
root-zone list: no network."""

from __future__ import annotations

import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_tld_data.py"
# Loaded from source, so no __pycache__ lands in scripts/ (test_accounts reads every file there).
gen = types.ModuleType("gen_tld_data")
gen.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_text("utf-8"), str(SCRIPT), "exec"), gen.__dict__)  # noqa: S102

HEADER = "# Version 2026111700, Last Updated Tue Nov 17 07:07:01 2026 UTC"
SAMPLE = f"""{HEADER}
COM
ROCKS
XN--P1AI
XN--80ASEHDB
XN--11B4C3D
"""


def test_the_header_gives_the_list_version_and_date():
    assert gen.check_header(SAMPLE) == ("2026111700", "Tue Nov 17 07:07:01 2026 UTC")


@pytest.mark.parametrize(
    "text",
    [
        "COM\nNET\n",  # no header
        "# Version 20261117, Last Updated Tue Nov 17 07:07:01 2026 UTC\nCOM\n",  # 8 digits
        "# Version 2026133100, Last Updated Tue Nov 17 07:07:01 2026 UTC\nCOM\n",  # month 13
        "\nCOM\n" + HEADER,  # the header is not the first line
    ],
)
def test_a_file_without_a_valid_iana_header_is_refused(text):
    with pytest.raises(SystemExit, match="not the IANA root-zone list"):
        gen.check_header(text)


def test_an_older_list_than_the_vendored_one_is_refused():
    gen.check_not_older("2026111700", "2026111700")
    gen.check_not_older("2026111800", "2026111700")
    gen.check_not_older("2026111700", None)  # nothing vendored yet
    with pytest.raises(SystemExit, match="older than the vendored 2026111800"):
        gen.check_not_older("2026111700", "2026111800")


def test_idn_tlds_are_kept_as_punycode_and_as_folded_unicode():
    tlds = gen.tlds(SAMPLE)
    assert tlds == sorted(set(tlds))
    for ascii_ in ("com", "rocks", "xn--p1ai", "xn--80asehdb", "xn--11b4c3d"):
        assert ascii_ in tlds
    assert "рф" in tlds and "онлайн" in tlds
    # Combining marks are dropped, as on the copy the link pattern reads (कॉम).
    assert "कम" in tlds and "कॉम" not in tlds


def test_the_rendered_module_is_valid_python_and_records_its_source():
    source = gen.render(gen.entries(SAMPLE), "2026111700", "Tue Nov 17 07:07:01 2026 UTC")
    namespace: dict[str, object] = {}
    exec(compile(source, "tld_data.py", "exec"), namespace)  # noqa: S102 (generated here)
    assert namespace["IANA_VERSION"] == "2026111700"
    assert namespace["IANA_UPDATED"] == "Tue Nov 17 07:07:01 2026 UTC"
    assert namespace["TLDS"] == frozenset(gen.tlds(SAMPLE))
    assert '"कम",  # XN--11B4C3D' in source  # the label as listed
    assert '"com",\n' in source  # an ASCII TLD needs no comment


def test_main_writes_the_module_and_refuses_to_downgrade_it(tmp_path, monkeypatch):
    out = tmp_path / "tld_data.py"
    monkeypatch.setattr(gen, "OUT", out)
    source = tmp_path / "tlds-alpha-by-domain.txt"
    source.write_text(SAMPLE, encoding="utf-8")

    assert gen.main(["gen_tld_data.py", str(source)]) == 0
    written = out.read_text(encoding="utf-8")
    assert 'IANA_VERSION = "2026111700"' in written and '"рф",  # XN--P1AI' in written

    older = SAMPLE.replace("2026111700", "2026111600")
    source.write_text(older, encoding="utf-8")
    with pytest.raises(SystemExit, match="older than the vendored 2026111700"):
        gen.main(["gen_tld_data.py", str(source)])
    assert out.read_text(encoding="utf-8") == written  # left as it was

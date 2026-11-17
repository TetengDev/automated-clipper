"""The generator of `clipper.hashtag_data` (CLP-112), run on small inline samples of the
Unicode files: no network."""

from __future__ import annotations

import types
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "gen_hashtag_data.py"
# Loaded from source, so no __pycache__ lands in scripts/ (test_accounts reads every file there).
gen = types.ModuleType("gen_hashtag_data")
gen.__file__ = str(SCRIPT)
exec(compile(SCRIPT.read_text("utf-8"), str(SCRIPT), "exec"), gen.__dict__)  # noqa: S102

CONFUSABLES = """
# a comment line
0430 ;\t0061 ;\tMA\t# ( а → a ) CYRILLIC SMALL LETTER A → LATIN SMALL LETTER A
006D ;\t0072 006E ;\tMA\t# ( m → rn )
0030 ;\t004F ;\tMA\t# ( 0 → O )
1D0D ;\t028D ;\tMA\t# ( ᴍ → ʍ ) a small capital mapped to another non-ASCII letter
017F ;\t0066 ;\tMA\t# ( ſ → f ) long s folds to s, which must not read as f
0049 ;\t006C ;\tMA\t# ( I → l ) a capital is case-folded before the table
0410 ;\t0041 ;\tMA\t# ( А → A ) likewise
30CE ;\t002F ;\tMA\t# ( ノ → / ) not ASCII letters or digits
2010 ;\t002D ;\tMA\t# ( ‐ → - ) not a letter or digit
"""


def test_only_folded_letters_and_digits_that_read_as_ascii_are_kept():
    table = gen.confusables(CONFUSABLES)
    assert table[0x0430] == "a"
    assert table[ord("m")] == "rn"
    assert table[ord("0")] == "o"  # the target is case-folded
    assert table[0x1D0D] == "rn"  # small capital M -> m, read through the table again
    assert table[0x1D00] == "a"  # small capital A, from its name
    for left_out in (0x017F, ord("s"), ord("I"), 0x0410, 0x30CE, 0x2010):
        assert left_out not in table


def test_scripts_and_mark_extensions_use_the_long_names():
    names = gen._script_names(
        "sc ; Hira ; Hiragana\nsc ; Kana ; Katakana\nsc ; Talu ; New_Tai_Lue\n"
    )
    assert names["Talu"] == names["New_Tai_Lue"] == "NEW TAI LUE"
    starts = gen.scripts("3041..3096 ; Hiragana\n30A1..30FA ; Katakana\n", names)
    assert (0x3041, "HIRAGANA") in starts and (0x30A1, "KATAKANA") in starts
    marks = gen.mark_scripts("3099..309A ; Hira Kana\n30FC ; Hira Kana\n", names)
    assert marks == {0x3099: ("HIRAGANA", "KATAKANA"), 0x309A: ("HIRAGANA", "KATAKANA")}


def test_the_rendered_module_is_valid_python():
    source = gen.render({0x0430: "a"}, [(0x30, "COMMON"), (0x41, "LATIN")], {0x3099: ("HIRAGANA",)})
    namespace: dict[str, object] = {}
    exec(compile(source, "hashtag_data.py", "exec"), namespace)  # noqa: S102 (generated here)
    assert namespace["CONFUSABLES"] == {0x0430: "a"}
    assert namespace["SCRIPT_NAMES"] == ("COMMON", "LATIN")
    assert namespace["MARK_SCRIPTS"] == {0x3099: ("HIRAGANA",)}

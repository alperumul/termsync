"""Tests for `termsync.titles.strip_title` (§3.5, §13.4).

Glyphs are written as ``\\uXXXX`` escapes throughout this file, not as
literal characters, and nothing here calls ``print()`` on a glyph-bearing
string. This machine's Python console is cp1252, and printing these
codepoints raw raises ``UnicodeEncodeError`` that has nothing to do with
the code under test (see the brief's console-encoding warning).

Codepoint key: \\u2733 \\u2734 \\u2731 \\u2732 are the four asterisk-family
glyphs (STATUS_GLYPHS[0:4]); \\u25d0 \\u25d1 \\u25d2 \\u25d3 are the four
circle-quadrant spinner glyphs (STATUS_GLYPHS[4:8]); \\u00b7 is MIDDLE DOT
and \\u2022 is BULLET (STATUS_GLYPHS[8:10]). \\u2605 (BLACK STAR) is used
below as a glyph deliberately NOT in STATUS_GLYPHS.
"""

from __future__ import annotations

from termsync.titles import STATUS_GLYPHS, normalize_title, strip_title


def test_asterisk_glyph_strips_to_bare_title() -> None:
    assert strip_title("\u2733 Project plan phase 3") == "Project plan phase 3"


def test_half_circle_glyph_strips_to_bare_title() -> None:
    assert strip_title("\u25d0 Project plan phase 3") == "Project plan phase 3"


def test_bracket_prefix_is_unchanged() -> None:
    """The `^\\W+` bug the glyph set exists to prevent.

    A leading `[` is punctuation, so a naive `^\\W+` strip (revision 3's
    original wording) would eat it. The explicit glyph set does not
    contain `[`, so it must survive untouched.
    """
    title = "[lab] Demo page"
    assert strip_title(title) == title


def test_hash_prefix_is_unchanged() -> None:
    """Same `^\\W+` bug, second shape: a leading `#` is punctuation too."""
    title = "#3 demo module"
    assert strip_title(title) == title


def test_unknown_leading_glyph_is_left_in_place() -> None:
    """A glyph not in STATUS_GLYPHS is left in place, not eaten.

    This is the additive-set guarantee: an unrecognised future Claude Code
    status glyph must show up as a visibly odd title, never a silently
    truncated one. \\u2605 (BLACK STAR) is not a member of STATUS_GLYPHS.
    """
    unknown_glyph = "\u2605"
    assert unknown_glyph not in STATUS_GLYPHS
    title = f"{unknown_glyph} Unknown future glyph"
    assert strip_title(title) == title


def test_all_glyphs_title_does_not_strip_to_empty() -> None:
    """A title that is entirely glyphs returns the original raw title.

    Required by §13.4's test list: TITLE_PREFIX would otherwise consume
    the whole string, and an empty title is worse than a glyph-only one.
    """
    title = "\u2733\u2734\u2731"
    assert strip_title(title) == title


def test_all_glyphs_and_whitespace_title_does_not_strip_to_empty() -> None:
    """Same rule, with interior whitespace mixed into the all-glyph run."""
    title = "\u2733 \u2734 \u2731"
    assert strip_title(title) == title


def test_empty_string_is_unchanged() -> None:
    assert strip_title("") == ""


def test_whitespace_only_is_unchanged() -> None:
    """Whitespace-only input strips to empty, which triggers the
    empty-strip fallback and returns the original (whitespace-only) title."""
    title = "   "
    assert strip_title(title) == title


def test_interior_glyph_is_untouched() -> None:
    """TITLE_PREFIX only matches a leading run; a glyph appearing later in
    the title (e.g. mid-sentence punctuation reuse) must survive."""
    title = "Project plan \u2733 phase 3"
    assert strip_title(title) == title


def test_multi_glyph_prefix_strips_fully() -> None:
    """A run of more than one glyph in the prefix is stripped entirely,
    not just the first character of it."""
    assert strip_title("\u2733\u2733 x") == "x"


def test_leading_whitespace_alone_is_stripped() -> None:
    assert strip_title("   Project plan phase 3") == "Project plan phase 3"


def test_mixed_whitespace_and_glyph_prefix_strips_fully() -> None:
    assert strip_title("  \u2733  Project plan phase 3") == "Project plan phase 3"


def test_status_glyphs_contains_exactly_the_spec_set_in_order() -> None:
    """Pins §3.5's exact glyph set and order, so a future edit that widens
    or reorders it is caught here rather than discovered via a title that
    quietly stopped stripping."""
    expected = "\u2733\u2734\u2731\u2732\u25d0\u25d1\u25d2\u25d3\u00b7\u2022"
    assert STATUS_GLYPHS == expected


def test_normalized_title_is_idempotent_and_preserves_meaningful_punctuation() -> None:
    raw = '\u2733 [lab] "Gr\u00fc\u00dfe"; phase 2;'
    expected = '[lab] "Gr\u00fc\u00dfe"\uff1b phase 2\uff1b'
    assert normalize_title(raw) == expected
    assert normalize_title(expected) == expected

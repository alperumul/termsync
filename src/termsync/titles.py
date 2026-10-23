"""Strip the status-glyph prefix from a live console title (§3.5).

Console titles are animated: an idle tab is prefixed with a glyph
(U+2733 ✳), and the busy tab cycles a spinner (U+25D0 ◐, U+25D1 ◑
observed on the same tab). Revision 3 of the design said "strip the
leading non-word prefix," which as ``^\\W+`` would also eat a legitimate
leading ``[``, ``#`` or ``(`` -- exactly the shape of titles like
``[lab] Demo page`` or ``#3 demo module``. The rule is therefore an
**explicit glyph set**, not a character class.

Unknown glyphs are left in place rather than eaten, so a new Claude Code
status glyph this set has not seen yet shows up as a visibly odd title
rather than as a silently truncated one. The set is additive: extend it,
never widen it into a character class.

This strip happens once, in Python (here), on ``titleRaw`` as reported by
``Snapshot-Desk.ps1 -Emit`` (§5 step 8). It is deliberately not
implemented in PowerShell: PowerShell is the one component the golden-file
contract test (``test_model_contract.py``) cannot cover, and putting policy
there would put it in the one place it cannot be pinned down (§13.2's
design note on this module).

This module's own source is plain UTF-8 and holds the glyphs as literal
characters -- editors and diffs read UTF-8 fine. The failure mode §13.4
warns about is specific to *printing* them through this machine's cp1252
Python console (``UnicodeEncodeError``); it is not a reason to avoid
literal glyphs in a source file. Tests exercising this module therefore
write the glyphs as ``\\uXXXX`` escapes in test *source* and avoid
``print()``-ing them, rather than avoiding literal glyphs here.
"""

from __future__ import annotations

import re

# ✳ ✴ ✱ ✲ ◐ ◑ ◒ ◓ · •  (U+2733 U+2734 U+2731 U+2732 U+25D0 U+25D1 U+25D2
# U+25D3 U+00B7 U+2022) -- per §3.5, verbatim, same order.
STATUS_GLYPHS = "✳✴✱✲◐◑◒◓·•"
TITLE_PREFIX = re.compile(rf"^[\s{re.escape(STATUS_GLYPHS)}]+")


def strip_title(title_raw: str) -> str:
    """Strip a leading run of whitespace and/or known status glyphs.

    Pure and total: no I/O, no clock, no Windows interop, and no input this
    function raises on.

    The one rule not expressible in the regex alone: a title that is
    *entirely* glyphs (and/or whitespace) must not strip to an empty
    string -- that would turn a real, if degenerate, title into nothing.
    In that case the original raw title is returned unchanged. This is a
    deliberate special case, spelled out here rather than left as an
    accident of ``TITLE_PREFIX`` fully matching the whole string.
    """
    stripped = TITLE_PREFIX.sub("", title_raw)
    if stripped == "":
        return title_raw
    return stripped


def normalize_title(title: str) -> str:
    """One display title for capture and restore, safe for WT's own parser.

    Windows Terminal splits literal ASCII semicolons even inside a quoted
    argv value. Use the visually similar fullwidth punctuation instead;
    ordinary quotes, Unicode, and meaningful leading punctuation survive.
    Apply at restore too, because older layouts contain unsanitized titles.
    """
    return strip_title(title).replace(";", "\uff1b")

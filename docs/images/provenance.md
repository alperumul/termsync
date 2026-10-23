# Picker examples

`picker.png` and `picker-filter.png` show output from the current
`termsync.picker.render` function at a width of 140 columns. The header comes
from `termsync.cli._header_lines`. The images use synthetic sessions with
example project directories and a fixed local time of October 23, 2026, 14:35.
No real history, provider data, or desktop observations were read to make them.

The filtered example uses the same picker state after entering `field-notes`.
It keeps all four selected sessions, including two hidden by the filter.

The renderer's text and ANSI styles were drawn as PNG images using Consolas
on a dark background. These are documentation renderings, not native terminal
screenshots. Font rendering and colors can vary with terminal settings.

# Sanitized test fixtures

These files are derived, sanitized inputs for offline tests. They are not raw
measurements, and tests do not need access to the original local evidence.

`emit_golden.json` derives from an August 2026 three-session PowerShell `-Emit`
capture. It retains the original field names, JSON types, status glyphs, and
matching process-start strings. All identities, directories, titles, PIDs,
socket/bridge identifiers, process starts, and timestamps are synthetic.
`emit_golden_single.json` is the third sanitized row with a process count of one
and the same boot time; it exercises a single-element session array.

`windows_replay/` derives from eight October 21, 2026 G3 observations and the
final/selection oracles held locally under `docs/research/g3`. Cases are ordered
by observation time:

| File | Preserved observation |
| --- | --- |
| `01-distinct.json` | Distinct session titles across three fixture windows |
| `02-duplicate.json` | Two live sessions share one title, with only one matching tab |
| `03-manual-rename-minimized.json` | A manually renamed tab becomes unknown; plain windows remain present |
| `04-moved-duplicate.json` | The duplicate moves to a different window and remains ambiguous |
| `05-reordered.json` | A session changes tab index around a plain shell tab |
| `06-same-cwd-distinct.json` | Distinct sessions share a directory and occupy different windows |
| `07-tab-closed.json` | One fixture tab closes and attributable window ordinals change |
| `08-window-closed.json` | A fixture window closes with both of its sessions |

Each case contains the emitter envelope and the complete before/after desktop
enumerations plus every scanned tab in its original order. This includes the
background work windows, their unmatched sessions, duplicate plain tab names,
and the plain-only window. Their presence determines global title uniqueness,
physical window counts, and ordinal assignment. UIA geometry, selection state,
runtime IDs, foreground handles, timing diagnostics, registry sockets, and other
unused metadata were omitted after checking the production readers.

Synthetic UUID prefixes distinguish named fixture sessions (`10000000`),
background Claude sessions (`20000000`), Codex (`30000000`), and the golden
capture (`40000000`). Each replay identity, directory, title, and window handle
uses one mapping across all cases and oracles. Status-glyph prefixes, title
equality and ambiguity, shared directories, session/provider distinctions,
process-start equality, and time ordering are preserved. Epoch times use one
constant offset; process-start strings are distinct 18-digit decimal values.

`oracle.json` retains the named-session labels and IDs from the final oracle,
including closed rows. `selection-oracle.json` retains selection/preflight
expectations and dense destination positions. The same-directory case also
contains `comparison` and `claudeCandidatesIsolated`, derived from the corrected
provider-isolated comparison and candidates. Its Codex ownership error is
preserved with a synthetic PID; it does not invalidate independent Claude
placement. Expected test outcomes and background counts were not changed.

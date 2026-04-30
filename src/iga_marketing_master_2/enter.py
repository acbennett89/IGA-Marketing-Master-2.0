"""enter.py — field-by-field EPIC data entry.

Reads the approved `state.json` and drives EPIC from the operator's current
screen.

Selector resolution chain (per field): `data-automation-id` → `name` →
`get_by_label` fallback.

Pre-flight selector smoke check (Amendment #6): at the start of every entry
session, walk only the screens we're about to touch (derived from approved
fields' `screen_code` set), confirm each `data-automation-id` resolves.
Surface a single non-blocking yellow banner if any selector looks stale —
operator must acknowledge but the run proceeds.

On error / unexpected DOM state: pause via `OperatorModal` (defined in
`gui.py`); on resume, re-read the DOM as ground truth. Per-field `entered`
status persisted to state.json so a mid-entry crash resumes from the
un-entered fields only.
"""

# TODO: implementation pending

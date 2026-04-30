"""state.py — per-client `state.json` read/write.

Owns the canonical extracted state per client. Schema:
- Top-level keyed by `domain_tag` (cache of Claude's mapping decisions).
- Repeatables nest as arrays of records.
- Per-field `history[]` and top-level `run_history[]` — both record
  `os.getlogin()` for single-user audit and v1.5 forward-compat.
- Atomic writes via `os.replace()` + rolling `.bak`.
- Daily snapshots to `Working Library/<Client>/snapshots/state-YYYY-MM-DD.json`,
  30-day retention (Amendment #2). Only the first successful write per day per
  client triggers a snapshot.

No `state.json.lock`, no conflict-copy detection (Path B, single-user).
"""

# TODO: implementation pending

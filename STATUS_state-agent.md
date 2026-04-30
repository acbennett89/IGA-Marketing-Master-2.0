# STATUS — state-agent

**Status:** Complete
**Owner module:** `src/iga_marketing_master_2/state.py`
**Companion docs:** `DECISION-MAP-state-agent.md` (Mermaid flowcharts)
**Tests:** `tests/test_state.py` — 32 tests, all green (Python 3.12 + pytest 9.0.2 locally; targets Python 3.13 per `pyproject.toml`).

---

## Deliverables

| Path | Purpose |
|---|---|
| `src/iga_marketing_master_2/state.py` | Implementation of the §5 contract |
| `tests/test_state.py` | 32 unit tests covering schema, atomicity, snapshots, merge, conflicts, natural keys, pause/extraction, schema_version, recovery, history |
| `DECISION-MAP-state-agent.md` | Mermaid flowcharts: load/save/merge/snapshot/pause/recovery; natural-key policy; atomic-write timing |
| `STATUS_state-agent.md` | This file |

---

## Open items addressed

### Open Item #1 — Per-repeatable natural keys

Published in DECISION-MAP-state-agent.md §2 and implemented in `state.natural_key_for(group, item)`. Summary:

| Group | Natural key |
|---|---|
| `vehicle` | `vehicle.vin` (uppercased, length-17 sanity check; otherwise treated as missing key) |
| `driver` | `(driver.license_number, driver.license_state)`, fallback `(driver.name, driver.date_of_birth)` |
| `location` | `(line1, city, state, zip)` |
| `loss_payee` | `(name, address.line1)`, fallback name alone |
| `additional_insured` | `(name, address.line1)`, fallback name alone |
| `prior_carrier` | `policy_number`, fallback `name` |
| `loss` | `(date_of_loss, amount_paid, description[:64])` |

Records with no natural-key value are appended as new (per the build prompt directive). The `MergeReport.appended_without_key` counter surfaces this back to the caller / GUI. Ambiguous matches log a warning and merge into the first match deterministically.

### Architecture ruling on `schema_version: 1` (ARCHITECTURE.md §13 #3)

**Confirmed and implemented as written.** No push-back. Rationale (also in DECISION-MAP §3):

- Distinct concern from amendment #11 (no `version` on Field Map for CAS). `schema_version` on `state.json` is **schema-migration metadata**, not concurrency control.
- Cost: one int per file. No write amplification.
- Migration hook is in place (`_migrate_state(raw, from_version=...)`) — empty for v1, single point of change when v2 lands.
- `StateSchemaVersionError` is raised on read of a future version (downgrade guard) so a stale build can't silently corrupt data by writing back without the newer fields.

---

## Public API surface (matches ARCHITECTURE.md §5.3)

```python
# Read / write
load(client_path: Path) -> State
save_atomic(state: State, client_path: Path) -> None
take_daily_snapshot(client_path: Path) -> Path | None

# Merge / conflict
merge_extraction(state, claude_records, run_id, model_used) -> MergeReport
detect_conflicts(state, domain_tag, candidate) -> ConflictDetection
write_history_entry(state, *, domain_tag, run_id, user, actor, action, prior, new) -> None

# Pending blocks
get_pending_pause(state) -> PendingPause | None
set_pending_pause(state, pause) -> None
get_pending_extraction(state) -> PendingExtraction | None
set_pending_extraction(state, pending) -> None

# Run history
append_run_history(state, entry: RunHistoryEntry) -> None

# Convenience (test/caller)
new_state(client: str) -> State
new_run_id() -> str
natural_key_for(group: str, item: RepeatableItem) -> str | None
```

Plus typed exceptions: `StateError`, `StateCorruptError`, `StateSchemaVersionError`, `StateMergeError`. All inherit from `Exception` per §13 ruling #12.

---

## Atomic-write protocol summary

1. `mkdir -p` the client dir; clean any leftover `state.json.tmp.*` orphans.
2. Stamp `state.updated_at = now`.
3. Serialize: canonical JSON (indent=2, UTF-8, sorted nested keys, trailing newline).
4. Write to `state.json.tmp.<rand>` via `tempfile.mkstemp` in the client dir.
5. `fh.flush()` + `os.fsync(fh.fileno())` — durable on disk before any rename.
6. If `state.json` exists, `shutil.copy2` it to `state.json.bak` (overwrites prior). If this copy fails, abort and clean tmp; original is untouched.
7. `os.replace(tmp, state.json)` — atomic on same volume.
8. Daily snapshot: if `snapshots/state-YYYY-MM-DD.json` (today's date) does not exist, copy state.json into it. Snapshot creation failures are logged but do NOT fail the save.
9. Prune snapshots older than 30 days. Failures logged, non-fatal.

**Crash invariant:** any failure before step 7 leaves `state.json` and `state.json.bak` byte-identical to before the call. Only artifact left is one orphan tmp, which is GC'd on the next save.

---

## Recovery chain (load)

1. `state.json` parses cleanly → return.
2. `state.json` missing or corrupt → try `state.json.bak`. Log warning on success.
3. Both corrupt → walk `snapshots/state-*.json` newest-first. Log warning on success.
4. All three corrupt → raise `StateCorruptError`. The GUI is expected to catch this and surface an `OperatorModal` (per ARCHITECTURE.md §8.3).

`schema_version > STATE_SCHEMA_VERSION` raises `StateSchemaVersionError` immediately, regardless of which file the data came from — we will not silently downgrade.

---

## Test summary (32/32 passing)

| Coverage area | Tests |
|---|---|
| Schema round-trip | 3 (empty, populated, missing-file fresh) |
| Atomic write crash safety | 2 (replace failure + .bak rotation timing) |
| Daily snapshots | 3 (once-per-day, prune stale, retention boundary) |
| Conflict merge | 4 (two-doc conflict, lower-conf drop, match-appends-source, no_existing) |
| Repeatable natural keys | 7 (vehicle merge by VIN, distinct VINs, missing VIN, short VIN, loss_payee by name+addr, unknown group, namespace mismatch) |
| Pending pause / extraction | 2 (round-trip both blocks) |
| schema_version | 3 (future raises, legacy assumed v1, corrupt type) |
| Recovery chain | 3 (.bak fallback, snapshot fallback, all-corrupt raises) |
| History entries | 3 (append, unknown-field raises, run history persist) |
| Bonus | 2 (create logs creation, merge does not save) |

```
tests/test_state.py ..............................  [100%]
============================= 32 passed in 0.19s ==============================
```

---

## Logging

`iga.state` logger used throughout:

- INFO on every `save_atomic` with `{client, bytes, snapshot_taken, pruned}`.
- INFO on every `merge_extraction` summarizing `MergeReport`.
- WARNING on recovery fallbacks (`.bak`, snapshot) with the file we recovered from and the parse error.
- WARNING on ambiguous repeatable natural-key matches.
- DEBUG on snapshot/orphan-tmp cleanup steps.

No `print()`. No bare `except`. All exception clauses name a type. All path handling uses `pathlib.Path`.

---

## Notes for the orchestrator and downstream agents

1. **`extract.py` integration point.** `merge_extraction` accepts `list[ExtractedField]` and either accepts the structural `state.ExtractedField` or `claude_client.ExtractedField` — the function only reads attributes, so duck-typed instances work. extract-agent does NOT need to import `state.ExtractedField` if they prefer the canonical claude_client class; both have the same attribute surface.

2. **`gui.py` integration points.**
   - `MergeReport` exposes `domain_tag_proposals: list[str]` for the JIT confirmation dialog flow (state-agent never writes to the Field Map; we just surface the list).
   - `MergeReport.appended_without_key` should be surfaced in the audit log pane so the user knows when rows were added without a natural-key dedupe (e.g., a vehicle missing its VIN).
   - `StateCorruptError` is the typed exception to catch around `state.load(...)` and route to a recovery `OperatorModal`.
   - `StateSchemaVersionError` is the typed exception for the "this build is too old for this state file" path.

3. **`enter.py` integration points.**
   - Set `pending_pause` BEFORE opening the modal (we documented this; enter-agent should follow). The contract is: durably persist context, then ask the human.
   - On resume, use `set_pending_pause(state, None)` and `save_atomic` to clear the block.

4. **Per-field `model_used` handling.** Implemented as a `str | None` on `FieldRecord` (per §13 ruling #4). Singleton merge propagates `r.model_used` on conflict-win and on creation. Repeatable merge does the same per RepeatableItem field.

5. **History-entry user field.** `merge_extraction` writes "create"/"update" history entries with `user=""` because the run-level `RunHistoryEntry` is the authoritative source for who ran the extraction. Per-field `HistoryEntry.user` is filled in by GUI / entry actions where the operator is the actor (e.g., manual edits, approves, locks). Caller can post-fill if they want; we wrote it as "" rather than `os.getlogin()` because that side-effect belongs at the call site, not in the merge primitive.

6. **No GUI / Claude / Extract imports.** The forbidden-edge rules in §11 are honored — `state.py` imports only stdlib. Even `ExtractedField` is locally defined as a structural mirror so we don't depend on `claude_client`.

---

## What's NOT in this module (boundary clarifications)

- No Field Map I/O (field-map-agent owns `field_map.py`).
- No JIT enrichment writes — we only surface unknown `domain_tag` proposals via `MergeReport.domain_tag_proposals`. extract-agent / gui-agent handle the user-confirmation flow and the actual `field_map.update_field` call.
- No Claude / Anthropic SDK calls.
- No EPIC / Playwright concerns.
- No GUI rendering.
- No global config or path discovery (config-and-cli-agent owns `config.py`). State-agent only takes a `client_path: Path` from callers.

---

## Conflicts surfaced

None. The architecture ruling on `schema_version` was accepted; no push-back. The natural-key policy is published per the orchestrator's request.

---

## Known small caveats

- The local test machine is Python 3.12.10, not 3.13 — tests pass anyway because the type-hint syntax used (PEP 604 `X | None`, builtin generics) is 3.12-compatible. Project still targets 3.13 per `pyproject.toml`. CI on 3.13 should also pass.
- `take_daily_snapshot` uses local-system date for the YYYY-MM-DD filename. If the user's clock is wildly skewed across runs, day boundaries could be inconsistent. This matches the plan's "first successful write per day" wording and is acceptable for v1 single-user.
- `_values_match` does case-insensitive string compare and float-cast numeric compare. Edge cases (e.g., "$1,000,000" vs `1000000`) are NOT normalized — that's the extractor's responsibility. We treat string-vs-number as a conflict.

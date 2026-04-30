# DECISION-MAP — state-agent

**Owner module:** `src/iga_marketing_master_2/state.py`
**Authoritative contract:** ARCHITECTURE.md §5 (state.json schema + I/O contract).
**Open items addressed:** #1 (per-repeatable natural keys); confirm `schema_version: 1` ruling.

---

## 1. End-to-end flow (top-level)

```mermaid
flowchart TD
    Start([state.py call site]) --> Op{Operation?}
    Op -- "load(client_path)" --> Load
    Op -- "save_atomic(state, client_path)" --> Save
    Op -- "merge_extraction(state, records, run_id, model)" --> Merge
    Op -- "set_pending_pause / clear" --> Pause
    Op -- "set_pending_extraction / clear" --> PEx
    Op -- "append_run_history(entry)" --> Hist

    %% --- LOAD ---
    Load --> L1{state.json exists?}
    L1 -- No --> L2[Return fresh State<br/>schema_version=1, client=<folder>,<br/>empty fields/repeatables]
    L1 -- Yes --> L3[Try parse state.json]
    L3 -- ok --> L4[Migrate if schema_version != 1<br/>v1: identity migration hook only]
    L4 --> L5[Return State]
    L3 -- corrupt --> L6{state.json.bak exists?}
    L6 -- Yes --> L7[Try parse .bak]
    L7 -- ok --> L8[Log warning iga.state<br/>'recovered from .bak']
    L8 --> L4
    L7 -- corrupt --> L9{snapshots/ has any?}
    L6 -- No --> L9
    L9 -- Yes --> L10[Try most-recent snapshot first,<br/>then prior days, until parse ok]
    L10 -- ok --> L11[Log warning 'recovered from snapshot YYYY-MM-DD']
    L11 --> L4
    L10 -- all corrupt --> L12[raise StateCorruptError<br/>GUI catches → operator modal]
    L9 -- No --> L12

    %% --- SAVE (atomic) ---
    Save --> S1[Stamp updated_at = now-iso]
    S1 --> S2[Serialize → JSON bytes<br/>indent=2, sort_keys=True top-level, UTF-8]
    S2 --> S3[Write tmp: state.json.tmp.NNN]
    S3 --> S4[fsync tmp]
    S4 --> S5{state.json exists?}
    S5 -- Yes --> S6[copy state.json → state.json.bak<br/>only AFTER tmp is durable]
    S5 -- No --> S7
    S6 --> S7[os.replace tmp → state.json<br/>(atomic on same volume)]
    S7 --> S8[Daily snapshot decision]
    S8 --> S8a{snapshots/state-YYYY-MM-DD.json<br/>exists for today?}
    S8a -- Yes --> S9[Skip snapshot]
    S8a -- No --> S8b[copy state.json →<br/>snapshots/state-YYYY-MM-DD.json]
    S8b --> S10[Prune snapshots > 30 days old]
    S9 --> S10
    S10 --> SDone([return None])
    S3 -- OSError --> SE1[Cleanup tmp; raise OSError<br/>orig state.json + .bak intact]
    S7 -- OSError --> SE1

    %% --- MERGE EXTRACTION ---
    Merge --> M1[For each ExtractedField r in records]
    M1 --> M2{r.repeatable_group set?}
    M2 -- No --> M3[Singleton field merge]
    M2 -- Yes --> M4[Repeatable group merge]

    M3 --> M3a{state.fields[r.domain_tag] exists?}
    M3a -- No --> M3b[Create FieldRecord<br/>status=pending, history append CREATE]
    M3a -- Yes --> M3c[detect_conflicts]
    M3c -- match --> M3d[Append to source[]<br/>history append UPDATE if confidence higher]
    M3c -- conflict --> M3e[Add candidate to conflicts[]<br/>If higher confidence, swap canonical;<br/>old canonical → conflicts[]]
    M3c -- lower_confidence_dropped --> M3f[Append to conflicts[] only]

    M4 --> M4a[Resolve repeatable index by NATURAL KEY<br/>per group rule (see §2)]
    M4a --> M4b{Existing item with matching key?}
    M4b -- Yes --> M4c[Treat as same row → singleton merge<br/>on each tag inside that item]
    M4b -- No, key value present --> M4d[Append new RepeatableItem]
    M4b -- No natural-key value --> M4e[Append new RepeatableItem<br/>flag merge_report.appended_without_key]

    M3b & M3d & M3e & M3f & M4c & M4d & M4e --> M5{r.domain_tag in known<br/>field_map enum?}
    M5 -- No --> M6[merge_report.domain_tag_proposals.add]
    M5 -- Yes --> M7[continue]
    M6 --> M7
    M7 --> MDone[Return MergeReport]

    %% --- HISTORY ---
    Hist --> H1[append RunHistoryEntry to state.run_history<br/>caller passes os.getlogin user]
    H1 --> HDone([return None])

    %% --- PAUSE ---
    Pause --> P1[Set/clear state.pending_pause]
    P1 --> PDone([return None — caller saves])

    %% --- PENDING EXTRACTION ---
    PEx --> PE1[Set/clear state.pending_extraction]
    PE1 --> PEDone([return None — caller saves])
```

---

## 2. Per-repeatable natural-key policy (Open Item #1)

The merger needs a deterministic per-group rule that lets us recognize "this is the same row I saw in the previous PDF" without inventing IDs Claude doesn't supply. The natural key is the **set of `domain_tag` values inside one `RepeatableItem`** whose combined value identifies the row.

| Group | Natural-key tag(s) | Match rule | Fallback when key value missing |
|---|---|---|---|
| `vehicle` | `vehicle.vin` | exact, case-insensitive, whitespace-trimmed; reject if length != 17 (treat as missing) | append new item; `merge_report.appended_without_key += 1` |
| `driver` | `driver.license_number` + `driver.license_state` | both must match (case-insensitive on number, exact on state code) | if either missing, fall back to `driver.name` + `driver.date_of_birth`; if also missing, append new |
| `location` | `location.address.line1` + `location.address.city` + `location.address.state` + `location.address.zip` | normalized compare (strip, lowercase, collapse internal whitespace) | append new |
| `loss_payee` | `loss_payee.name` + `loss_payee.address.line1` | normalized compare (strip, lowercase) | if address missing, fall back to `loss_payee.name` alone (acceptable risk for v1; loss payee names are usually unique within a client) |
| `additional_insured` | `additional_insured.name` + `additional_insured.address.line1` | same as loss_payee | name alone if address missing |
| `prior_carrier` | `prior_carrier.name` + `prior_carrier.policy_number` | exact match on policy_number (case-insensitive); name secondary | append new (rare to have two prior carrier records anyway) |
| `loss` | `loss.date_of_loss` + `loss.amount_paid` + `loss.description` (first 64 chars) | composite — losses rarely have a real natural ID; this triple is a strong heuristic | append new (losses lean toward "always append; dedup via GUI") |

**Normalization helpers used by all key compares:**
- `_norm(s) = s.strip().casefold().replace(" "," ")` then collapse internal whitespace runs
- VINs are normalized to upper case with a length-17 sanity check before they qualify as a key
- Numeric values cast to `str` before normalization

**Ambiguity rule.** If multiple existing items match the same key, log `iga.state` warning and merge into the **first** match (deterministic by insertion order). The GUI can spot this in `run_history` notes.

**Why not a synthetic ID?** Claude doesn't supply stable row IDs across PDFs. A natural key derived from the data is the only way merge across two docs can recognize "Vehicle 41 in PDF A" == "row #3 in PDF B".

**No-natural-key fallback.** Per the open-item directive: "If a record has no natural-key value, it's appended as new (no merge)." We honor that explicitly and surface the count via `MergeReport.appended_without_key` so the GUI can hint to the user that some rows may be duplicates needing manual reconciliation.

---

## 3. `schema_version: 1` ruling — confirmed (Architecture §13 ruling #3)

**Decision: Implemented as written.** Rationale:

- Distinct concern from amendment #11's removal of `version` from the **Field Map** (CAS field). `schema_version` on `state.json` is **schema migration metadata**, not concurrency control.
- Cost is one integer per file. No write amplification.
- Migration hook (`_migrate_state(raw, from_version)`) is stubbed to identity for v1; future schema changes will fill it in. Reading a `state.json` with `schema_version > STATE_SCHEMA_VERSION` raises `StateSchemaVersionError` (typed, GUI-catchable) so a downgrade can't silently corrupt.
- No conflict surfaced to the orchestrator; the state-agent accepts the ruling.

---

## 4. Atomic-write invariant (timing-precise)

```mermaid
sequenceDiagram
    participant C as caller
    participant FS as filesystem
    participant SP as snapshot dir

    C->>FS: open state.json.tmp.<pid>.<rand>
    C->>FS: write JSON bytes
    C->>FS: fsync(tmp)
    C->>FS: close(tmp)
    Note over C,FS: only NOW is the new bytes durable on disk
    alt state.json exists
        C->>FS: copy state.json → state.json.bak (overwrite)
    end
    C->>FS: os.replace(tmp, state.json)
    Note over FS: atomic rename on same volume<br/>tmp may briefly exist on crash; orig still intact
    C->>SP: ensure snapshots/ exists
    alt snapshots/state-YYYY-MM-DD.json absent
        C->>SP: copy state.json → state-YYYY-MM-DD.json
    end
    C->>SP: enumerate state-*.json; delete any > 30 days old
```

**Crash-safety claim:** if the process dies anywhere before `os.replace`, the original `state.json` and prior `.bak` are byte-identical to before the call. The only artifact left behind is one orphaned `state.json.tmp.*` file in the client dir; this is harmless and is GC'd opportunistically by `_cleanup_orphan_tmps()` at the start of the next save.

**Rotation order matters.** We copy to `.bak` *after* the tmp is durably written but *before* `os.replace`. If the copy itself fails (rare — same-folder copy of a small JSON), we abort the write entirely and leave the prior file untouched.

---

## 5. Pending-pause / pending-extraction lifecycle

```mermaid
flowchart LR
    subgraph Extraction["pending_extraction (resume on crash)"]
        E1[extract.py: at run start<br/>set_pending_extraction] --> E2[save_atomic]
        E2 --> E3[per PDF: append basename<br/>to completed_pdf_basenames]
        E3 --> E4[save_atomic]
        E4 --> E5{more PDFs?}
        E5 -- Yes --> E3
        E5 -- No --> E6[set_pending_extraction(None)]
        E6 --> E7[save_atomic]
    end

    subgraph Pause["pending_pause (entry session)"]
        P1[enter.py: selector unresolved<br/>or validation rejected] --> P2[set_pending_pause]
        P2 --> P3[save_atomic - durable BEFORE modal]
        P3 --> P4[GUI on_pause_callback]
        P4 -- resume --> P5[set_pending_pause(None)]
        P4 -- skip --> P5
        P4 -- abort --> P5
        P5 --> P6[save_atomic]
    end

    subgraph CrashRecovery["next launch"]
        R1[GUI: state.load] --> R2{pending_extraction != None?}
        R2 -- Yes --> R3[show RecoverInterruptedRunDialog]
        R3 -- resume --> R4[extract.run_extraction with<br/>pdf_paths minus completed]
        R3 -- discard --> R5[set_pending_extraction(None); save]
    end
```

Both pending blocks are plain dataclasses serialized into `state.json`. They are durable by virtue of being part of the canonical state file — no separate sidecar files, no in-memory-only state.

---

## 6. Module surface (matches §5.3)

```python
# Public:
load(client_path: Path) -> State
save_atomic(state: State, client_path: Path) -> None
merge_extraction(state: State, claude_records: list[ExtractedField], run_id: str, model_used: str) -> MergeReport
detect_conflicts(state: State, domain_tag: str, candidate: ExtractedField) -> ConflictDetection
write_history_entry(state: State, *, domain_tag: str, run_id: str, actor: str, action: str, prior, new) -> None
take_daily_snapshot(client_path: Path) -> Path | None
get_pending_pause(state: State) -> PendingPause | None
set_pending_pause(state: State, pause: PendingPause | None) -> None
get_pending_extraction(state: State) -> PendingExtraction | None
set_pending_extraction(state: State, pending: PendingExtraction | None) -> None
append_run_history(state: State, entry: RunHistoryEntry) -> None

# Typed exceptions:
StateError(Exception)
StateCorruptError(StateError)
StateSchemaVersionError(StateError)
StateMergeError(StateError)
```

---

## 7. Logging convention (per §10)

- Every `save_atomic` logs at INFO via `iga.state` with `{client, bytes, snapshot_taken, snapshot_pruned}`.
- Every fallback-on-load logs at WARNING with `{from: "bak"|"snapshot:YYYY-MM-DD", reason}`.
- Every `merge_extraction` logs INFO with the `MergeReport` summary fields.
- No `print()`. No bare `except`.

---

## 8. Test plan (mirrors `tests/test_state.py`)

1. Schema round-trip — create a synthetic State, save, reload, compare deep-equal.
2. Atomic write survives mid-write crash — monkeypatch `os.replace` to raise; assert `.tmp` is cleaned up and original intact.
3. Daily snapshot — save twice on same day → only one snapshot. Travel time → second day creates second. >30-day-old files prune.
4. Merge with conflict — same `domain_tag` from two source docs with different values populates `conflicts[]` and selects the higher-confidence canonical.
5. Repeatable merge by natural key — vehicles by VIN: same VIN merges; different VIN appends; missing VIN appends + flagged in MergeReport.
6. Pending pause set / clear / serialize — round-trips cleanly.
7. `schema_version` honored — synthetic file with `schema_version=2` raises `StateSchemaVersionError`; v1 file loads cleanly.
8. Recovery — corrupt state.json + valid .bak loads from .bak. Both corrupt + valid snapshot loads from snapshot. All three corrupt raises `StateCorruptError`.
9. History entry append — `write_history_entry` on a `FieldRecord` produces correct prior/new shape.

# STATUS — extraction-agent

**Agent:** extraction-agent
**Date:** 2026-04-30
**Status:** ✅ COMPLETE
**Authoritative reference:** [ARCHITECTURE.md](ARCHITECTURE.md), [DECISION-MAP-extraction-agent.md](DECISION-MAP-extraction-agent.md)

---

## Files written (only files in this agent's scope)

- `src/iga_marketing_master_2/extract.py` — full implementation
- `tests/test_extract.py` — 14 tests, all passing
- `DECISION-MAP-extraction-agent.md` — Mermaid diagrams for orchestration, per-PDF flow, conflict detection, JIT proposal queue, pending-extraction crash safety, doc_id strategy, logging, dependency boundary
- `STATUS_extraction-agent.md` (this file)

No other files touched.

---

## Public API surface (`extract.py`)

```python
def run_extraction(
    client_name: str,
    pdf_paths: Sequence[Path],
    force_opus: bool = False,
    *,
    settings: config.Settings | None = None,
    resume_run_id: str | None = None,
    glossary: str | None = None,
    system_prompt: str | None = None,
) -> ExtractionResult: ...

def resume_extraction_clear(client_path: Path) -> None: ...
```

Dataclasses: `DomainTagProposal`, `CacheStats`, `DocSummary`, `ExtractionResult`.
Exceptions: `ExtractionError` (base), `ExtractionInputError`,
`DuplicatePdfBasenameError`, `PendingExtractionDetectedError`.

---

## Open Item §14 #5 resolved — PDF doc_id strategy

**Decision: basename** (e.g., `renewal-dec.pdf`).

**Why:** human readability in `state.json` audit trails matters more than
hash stability for a single-user, per-client v1. Hash is overkill (extra
I/O + opacity for the operator) and basename collisions are detectable at
input-validation time.

**Mitigation for within-run basename collisions:**
`DuplicatePdfBasenameError` is raised before any state load or API call.
The operator must rename or skip one of the colliding files. Tested in
`test_duplicate_basename_guard_raises_before_api_call`.

**Tradeoff accepted:** if the operator manually re-OCRs a PDF and overwrites
the file in place, source quotes recorded in state will still reference the
same `doc_id` even though the bytes changed. Acceptable for v1; revisit in
Phase 2 if it becomes a real workflow concern.

See DECISION-MAP §F for the full rationale.

---

## Contract handoffs

### To the GUI

- `run_extraction(...)` returns an `ExtractionResult` with:
  - `run_id`, `outcome` (`"completed" | "partial" | "aborted"`)
  - `per_doc[]` — DocSummary per PDF (counts, model_used, error)
  - `pending_proposals[]` — DomainTagProposals queued for confirmation
  - `cache_stats` — aggregated prompt-cache + token usage
  - `conflicts_surfaced`, `unknown_tags_queued`, `malformed_tags_dropped`,
    `repeatable_items_added`, `fields_extracted`
- The GUI is responsible for opening the `DomainTagConfirmationDialog` for
  each proposal and calling `field_map.update_field` / `field_map.save_atomic`
  on accept. **The extraction-agent never writes to the Field Map.**
- On `PendingExtractionDetectedError`, the GUI shows the recovery prompt
  ("Resume? Discard?") and either calls `run_extraction(..., resume_run_id=...)`
  or `resume_extraction_clear(client_path)` followed by a fresh
  `run_extraction(...)`.

### To the state-agent (consumed)

`extract.py` calls (per ARCHITECTURE.md §5.3):
- `state.load(client_path)`
- `state.save_atomic(state, client_path)`
- `state.get_pending_extraction(state)`
- `state.set_pending_extraction(state, pending)`
- `state.set_pending_domain_tag_proposals(state, [...])` — *new helper*
- `state.merge_extraction(state, records, run_id, model_used) -> MergeReport`
- `state.append_run_history(state, RunHistoryEntry)`
- Constructs `state.PendingExtraction(...)` and `state.RunHistoryEntry(...)`

**Note for state-agent:** `set_pending_domain_tag_proposals(state, list)` is
not in §5.3 of ARCHITECTURE.md — it's a small additive helper this agent
needs so JIT proposals survive a crash before GUI handoff. The accepted
shape is a list of dicts (see `_proposal_to_dict` in `extract.py`).
If the state-agent prefers a different name or shape, this agent's
`_persist_pending_proposals` falls back gracefully (uses `getattr` with a
None default — the call is skipped if the helper doesn't exist; the
`pending_proposals` are still returned via `ExtractionResult` for live
GUI handoff).

### To the field-map-agent (consumed)

- `field_map.load() -> FieldMap`
- `field_map.generate_domain_tag_enum(fm) -> list[str]`

**Not used:** `update_field` and `save_atomic` are explicitly NOT called by
`extract.py` (proven by `test_jit_unknown_tag_is_queued_not_written`).

### To the claude-client-agent (consumed)

- `claude_client.extract_from_pdf(pdf_path, fm, glossary, system_prompt, *, force_opus, run_id, debug_dir) -> list[ExtractedField]`
- `claude_client.ClaudeError` — caught at the per-doc loop boundary
- The per-call cache usage is consumed via `getattr(records, "cache_usage", None)`
  on the returned list (an attribute-bearing list subclass) or
  `records[0]._call_usage`. Both shapes are accepted; missing => zero.
  The claude-client-agent should attach `cache_usage` to its returned
  list if it wants per-call stats to flow into `ExtractionResult.cache_stats`.

---

## Test coverage (14 tests, all passing on Python 3.13)

| Test | Verifies |
|---|---|
| `test_run_extraction_happy_path_merges_known_tags` | Full happy path; both PDFs merged; pending block cleared; run_history appended |
| `test_conflict_count_surfaces_in_result` | MergeReport conflicts flow to `ExtractionResult.conflicts_surfaced` and per-doc summary |
| `test_jit_unknown_tag_is_queued_not_written` | Unknown tag → proposal; `field_map.update_field` / `save_atomic` NEVER called; persisted to state.pending_domain_tag_proposals |
| `test_malformed_tag_is_dropped_not_queued` | §3.1 grammar violators dropped with warning, NOT queued |
| `test_pending_extraction_block_persists_across_doc_loop` | Per-doc save records completed_pdf_basenames incrementally |
| `test_pending_extraction_detected_on_resume_without_run_id` | Stale pending block raises `PendingExtractionDetectedError`; no Claude call |
| `test_resume_with_matching_run_id_skips_completed` | Resume path skips already-completed PDFs |
| `test_resume_extraction_clear_drops_pending_block` | Discard path clears pending and saves |
| `test_duplicate_basename_guard_raises_before_api_call` | doc_id collision blocked before state.load |
| `test_empty_pdf_list_raises_input_error` | Empty input rejected |
| `test_missing_pdf_raises_input_error` | Nonexistent file rejected |
| `test_claude_error_aborts_run_and_records_partial_outcome` | ClaudeError mid-loop → outcome="aborted", run_history entry, pending cleared |
| `test_force_opus_propagates_to_claude_client` | `force_opus=True` flows through to claude_client and run_history |
| `test_cache_stats_aggregate_across_docs` | Per-call `cache_usage` aggregates into `ExtractionResult.cache_stats` |

Run command:
```
PYTHONPATH=src py -3.13 -m pytest tests/test_extract.py -q
```

---

## Critical rules — compliance

| Rule | Status |
|---|---|
| Do NOT call the real Anthropic API in tests | ✅ `claude_client.extract_from_pdf` mocked in every test |
| Do NOT modify any other agent's files | ✅ Only the four files listed above were written |
| Stay in scope — `extract.py` only | ✅ |
| Allowed imports only (`field_map`, `state`, `claude_client`, `config`, `logger`) | ✅ Verified — no `gui`, `enter`, `epic_session`, or `secret_store` imports |
| Type hints required (Python 3.13) | ✅ All public APIs and internal helpers fully typed; `X \| None`, `list[T]` syntax |

---

## Logging

All significant events log at INFO under `iga.extract`. See DECISION-MAP §G
for the full event catalog. Aggregated cache stats are logged at run end.

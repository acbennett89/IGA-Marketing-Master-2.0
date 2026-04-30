# STATUS — Architecture Finalization Agent

**Agent:** architecture-finalization-agent
**Date:** 2026-04-30
**Status:** ✅ Complete
**Authoritative reference:** [ARCHITECTURE.md](ARCHITECTURE.md) (now synced to delivered surface)
**Predecessor:** [STATUS_ARCHITECTURE_INITIAL.md](STATUS_ARCHITECTURE_INITIAL.md)

---

## 1. Summary

ARCHITECTURE.md has been synced to reflect what the seven developer agents actually shipped. Edits were additive and surgical — no section was rewritten. All eight open items from §14 are now closed (seven resolved during build by the owning agents; one — TROUBLESHOOTING.md — explicitly deferred to Documentation Phase 2). Build-delivery summary added as Appendix C, and the change-log row appended.

No code, config, requirements, scripts, or test files were touched at this stage. Only `ARCHITECTURE.md` was edited and `STATUS_ARCHITECTURE_FINAL.md` (this file) was created.

---

## 2. ARCHITECTURE.md edits

| Section | Change | Rationale |
|---|---|---|
| Header | Status flipped from "✅ Initial" to "✅ Finalized — synced to delivered surface (post-build)"; author byline updated. | Reflects that ARCHITECTURE.md is no longer the pre-build contract; it is the as-built spec. |
| §5.1 (State schema) | Added `pending_domain_tag_proposals: dict[]` with default `[]` to the top-level State shape. | Completes the JIT proposal contract that `extract.py` had been calling via `getattr` fallback (Issue #5 / state-agent fixup). |
| §5.3 (state.py I/O contract) | Added `get_pending_domain_tag_proposals(state)` and `set_pending_domain_tag_proposals(state, proposals)`; documented the GUI consumption path (`DomainTagConfirmationDialog` calls `field_map.update_field()` on accept). | Same as above — durable proposal queue survives crashes between extraction and GUI confirmation. |
| §9.3 (secret_store API) | Removed `prompt_for_anthropic_api_key_via_gui` from the documented surface; added `prompt_for_anthropic_api_key_via_console`; added `SecretStoreError` to the listed surface; added a "Prompt-UI ownership" subsection clarifying that `gui.operator_modal.ApiKeyPromptDialog` owns the GUI prompt and calls `secret_store.set_anthropic_api_key()` directly. | Issue #2: the GUI-coupled function would have violated §11's `gui → secret_store` one-way edge. Resolution preserves the dependency graph and matches what shipped. |
| §11 (Module Dependency Graph) | Added a "Note on `gui` packaging" paragraph above the Mermaid diagram explaining the `gui.py` → `gui/` subpackage restructure (Python disallows same-named module + package side-by-side); listed the internal modules; noted that the dependency graph treats `gui` as a single node. Updated the diagram label to read `gui/ package`. | Reflects gui-agent's restructure. The inter-module contract is unchanged; the restructure is internal to the `gui` subpackage. |
| §14 (Open Items) | Each of the 8 items is now marked ✅ Resolved (or 🟡 Pending for #8) with a one-line resolution note pointing at the owning agent's STATUS file. Original framing preserved in italics for traceability. | All eight tracked items have been adjudicated or routed. |
| Appendix C (new) | Added "Build Delivery (post-build summary)" — module roster, test counts (217 → 224), dependency pins (anthropic 0.42/0.97.0, playwright 1.55, psutil 5.9, pywin32 removed, pytest 8), notable rulings, and the pre-existing pytest-pythonpath issue flagged for /check. | Closes the loop between the architecture spec and the as-built artifact. |
| Appendix B (change log) | Appended a 2026-04-30 row attributing the finalization edits. | Standard change-log discipline. |

---

## 3. Open-item closure (§14) — final disposition

| # | Title | Owning agent | Resolution location |
|---|---|---|---|
| 1 | Per-repeatable natural keys | state-agent | `STATUS_state-agent.md` + `DECISION-MAP-state-agent.md` §2 |
| 2 | Anthropic SDK exact pin + tool-use response shape | claude-client-agent | `STATUS_claude-client-agent.md` "Open item" section + `DECISION-MAP-claude-client-agent.md` |
| 3 | Section tab derivation rule | gui-agent | `STATUS_gui-agent.md` + `DECISION-MAP-gui-agent.md` §1 |
| 4 | `log_dir` cleanup policy | config-and-cli-agent | `STATUS_config-and-cli-agent.md` §4 + `DECISION-MAP-config-and-cli-agent.md` §D |
| 5 | PDF doc_id strategy | extraction-agent | `STATUS_extraction-agent.md` "Open Item §14 #5" + `DECISION-MAP-extraction-agent.md` §F |
| 6 | Field Map enrichment ordering during JIT | field-map-agent | `STATUS_field-map-agent.md` "Open-item ruling" + `DECISION-MAP-field-map-agent.md` |
| 7 | Bootstrap script | config-and-cli-agent | `STATUS_config-and-cli-agent.md` §4 + `DECISION-MAP-config-and-cli-agent.md` §E |
| 8 | TROUBLESHOOTING.md | Documentation Agent | Pending Documentation Phase 2 |

---

## 4. Drift between architecture spec and shipped code

After the finalization edits above, residual drift is **near-zero**. Specifically:

- `secret_store.py` API (delivered) ↔ ARCHITECTURE §9.3 — **in sync** (post-edit).
- `state.py` API (delivered, including fixup helpers) ↔ ARCHITECTURE §5.1 / §5.3 — **in sync** (post-edit).
- `gui` packaging (delivered as subpackage) ↔ ARCHITECTURE §11 — **in sync** (post-edit, with note).
- `claude_client.py` API (delivered, with additive `api_key=None` kwarg on `extract_from_pdf` / `reextract_low_confidence_fields` and an additive `build_record_field_tool_schema` helper) ↔ ARCHITECTURE §6.1 — **in sync** materially. The added kwargs are optional and defaulted, so the documented signatures remain correct as a subset; the helper is a private-style export and not contractually required to be documented in §6. No spec edit needed.
- `extract.py` API (delivered) ↔ ARCHITECTURE §6 / §12 — **in sync**. `run_extraction` is described narratively in §12 rather than as a normative signature; the delivered surface (with `force_opus`, `resume_run_id`, `glossary`, `system_prompt` kwargs) is consistent with the narrative.
- `epic_session.py` / `enter.py` (delivered) ↔ ARCHITECTURE §7 — **in sync**. The delivered `enter.run_entry_session` accepts an additional `save_state_callback` injection point (per epic-driver-agent's decoupling discipline while `state.py` was still landing). This is additive and benign; the documented behavior matches.
- `cli.py` / `config.py` / `logger.py` (delivered) ↔ ARCHITECTURE §9 — **in sync**, with the noted addition of `Settings.cli_initial_client: str | None` (additive, declared in `STATUS_config-and-cli-agent.md` Deviation #1). Not breaking; not raised to a §-level edit.

**Verdict:** ARCHITECTURE.md and the delivered code are aligned. Future readers can use ARCHITECTURE.md as a reliable map of the codebase.

---

## 5. Architectural concerns flagged for /check

Only one item, carried from COMMS.md Issue #7:

- **`pytest` discovery configuration in `pyproject.toml`.** Single-file test runs (`pytest tests/test_state.py`) currently fail with `ModuleNotFoundError: iga_marketing_master_2` unless the operator sets `PYTHONPATH=src`. Full-suite runs via package-layout discovery work fine. The three-line fix is:

  ```toml
  [tool.pytest.ini_options]
  pythonpath = ["src"]
  ```

  This is **not blocking**: all 224 tests pass via the full-suite path. Flagged so /check can verify and (if it wants) route a one-line edit. Documented in Appendix C of ARCHITECTURE.md.

No other architectural concerns. The dependency graph (§11) is acyclic and respected by every module's actual imports (verified per-module via the agents' own STATUS files). Forbidden edges (`*.py → gui`, `claude_client → state/extract`, `epic_session → enter/state`) are honored.

---

## 6. Final checklist

- [x] ARCHITECTURE.md edited only (no code, no `.toml`, no `.ps1`, no other STATUS files).
- [x] §9 secret_store API synced (Issue #2).
- [x] §5 `pending_domain_tag_proposals` added to schema and contract (Issue #5).
- [x] §14 all 8 open items closed with owning-agent attribution.
- [x] §11 `gui` subpackage note added.
- [x] Appendix C (Build Delivery) added with test counts, SDK pin, ruling list, /check flag.
- [x] Change log updated.
- [x] Diff is readable: section-level updates with clear headings; existing structure preserved.
- [x] STATUS_ARCHITECTURE_FINAL.md (this file) created.

---

## 7. Handoff

Architecture is finalized. The orchestrator may now spawn Documentation Phase 2 (which will consume the operator-readable error vocabulary in §8.8 and the as-built module roster in Appendix C) and/or proceed to /check. The pending pytest-pythonpath flag is the only architecture-adjacent item left for /check to consider.

Green light.

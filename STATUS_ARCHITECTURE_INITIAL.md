# STATUS_ARCHITECTURE_INITIAL.md — Architecture Agent report

**Project:** IGA Marketing Master 2.0
**Project root:** `C:/Users/Andrew/Documents/GitHub/IGA-Marketing-Master-2.0/`
**Date:** 2026-04-30
**Mode:** NEW PROJECT — Path B (single-user, local disk)
**Status:** ✅ ARCHITECTURE.md initial complete. Architecture Agent remains available for conflict resolution.

---

## 1. Deliverable

`ARCHITECTURE.md` written at the project root. Comprehensive — covers all 14 required sections plus two appendices:

1. System overview (5 subsystems + data flow + v1-vs-v2 inversion)
2. Project-wide naming conventions (Python 3.13 idioms; forbidden patterns enumerated)
3. Domain Tag namespacing convention — **PLAN-REVIEW open question #1 RESOLVED**: dot-separated lowercase, `<namespace>.<segment>+`, fixed top-level namespace list (11 entries), fixed LOB code list (12 codes for `policy.<lob>.*`), 18 worked examples
4. Epic Field Map schema — existing keys preserved, all new metadata keys typed and provenance-tagged, `field_map.py` I/O contract with normative signatures, atomic write protocol, JIT enrichment flow
5. Canonical state.json schema — full TypeScript-style schema, `state.py` I/O contract, atomic write protocol with daily snapshots and 30-day retention, resume-on-crash flow via `pending_extraction`
6. Claude API integration contract — `record_extracted_field` tool schema, two cache breakpoints, auto-escalation policy (`< 0.7` / `needs_review` / required-missing), cache verification logging (per Finding 5), PDF preflight + 80-page split with 1-page overlap, error type hierarchy
7. Entry driver contract — `epic_session.py` + `enter.py` signatures, Playwright pinned `>=1.55`, `cleanup_user_data_dir_lock` runs before every launch (per Finding 4), pre-flight selector smoke at session start (touched screens only), pause-for-human protocol with DOM-as-truth resume
8. GUI contract — `IgaApp.run()` entry, mandatory `OperatorModal` widget class with 4-part structure (per amendment #5), six concrete subclasses enumerated, table/delegate/confidence-tint/Opus-badge spec, repeatable list+form pattern, conflict UI, operator-facing copy guidelines
9. CLI/config/secret-store/logging contracts — `keyring` wrapper signatures (per amendment #14), `platformdirs` discovery (per amendment #15), per-module logger naming (`iga.<module>`), config persistence at `<user_config_dir>/config.json`
10. Cross-cutting `--debug` discipline — per-module behavior table, auto-prune to last 5 runs per client
11. Module dependency graph (Mermaid) — acyclic, with forbidden edges enumerated
12. Data flow narrative — full happy-path walkthrough using "Bobby Luttrell & Sons" as the worked example, naming every module touched in order
13. Assumptions and rulings — 15 explicit Architecture Agent judgment calls, each justified and revisable
14. Open items carried into the build — 8 items routed to specific developer agents

Plus Appendix A (constants reference) and Appendix B (document change log).

## 2. Open question resolved

- **PLAN-REVIEW open question #1 (`domain_tag` namespacing):** ruled in §3 of ARCHITECTURE.md. Grammar, namespace list, LOB list, repeatable-group convention, alias rules, and JIT enrichment flow all codified.

## 3. Open items carried forward

These are items the Architecture Agent did NOT fully resolve and that need a developer agent's input. Each is routed in §14 of ARCHITECTURE.md to a specific agent:

1. Per-repeatable natural keys (state-agent)
2. Anthropic SDK exact pin + tool-use response shape verification (claude-client-agent)
3. Section tab derivation rule for the GUI (gui-agent)
4. `log_dir` cleanup policy specifics (config-and-cli-agent)
5. PDF doc_id strategy — basename vs hash-prefixed (extraction-agent)
6. JIT enrichment write batching (field-map-agent)
7. `scripts/bootstrap.ps1` content (config-and-cli-agent)
8. TROUBLESHOOTING.md operator vocabulary contribution (Documentation Agent)

None are blocking. All can be settled inside the developer-agent's normal Phase work.

## 4. Flags / things the orchestrator should know

- **No application code was written.** ARCHITECTURE.md is documentation. All placeholder source files in `src/iga_marketing_master_2/` remain untouched per the prompt's critical rules.
- **No workflow MDs were modified.** PLAN.md, PLAN-REVIEW.md, PROCESS-MAP.md, RESEARCH.md, STATUS_SETUP.md, COMMS.md unchanged.
- **`Library/Epic Field Map.json` was not modified.** Read-only inspection only.
- **One ruling worth flagging:** §13 ruling #3 introduces `schema_version: 1` on `state.json` (not in the original plan). This is internal to state, doesn't conflict with amendment #11's removal of `version` from the Field Map (different files, different concerns), and gives future migrations an explicit hook. If state-agent disagrees, the Architecture Agent will reconsider.
- **Two-cache-breakpoint placement** in §6.3 is opinionated: system block carries Breakpoint 1, the Field Map is the **second** content block in the user message (before the PDF) and carries Breakpoint 2. This matches Anthropic's documented prefix-caching semantics. If claude-client-agent finds a current SDK quirk that requires a different layout, raise via the orchestrator.

## 5. Architecture Agent posture going forward

- Persistent agent, available for the duration of the build.
- Conflicts route in via the orchestrator (per the agent contract).
- On each conflict: read affected parties' concrete needs, rule by the most restrictive real need, update `ARCHITECTURE.md` atomically (read → modify → write), report back with a one-sentence summary.
- Will write `STATUS_ARCHITECTURE_FINAL.md` when all developer agents have completed and the orchestrator confirms.

Green light. Orchestrator may proceed to spawn the Documentation Agent and the seven Developer Agents.

# COMMS.md — Project Communication Ledger
**Project:** IGA Marketing Master 2.0
**Build Started:** 2026-04-30
**Build Mode:** New Project
**Authoritative plan:** PLAN-REVIEW.md (✅ APPROVED WITH MATERIAL SCOPE CHANGE; Path B + 17 amendments folded in via /research)
**Orchestrator Status:** 🟢 Active

---

## Status Board
| Agent | Type | Status | Notes |
|---|---|---|---|
| Setup | Persistent | ✅ Complete | Commit `37182af`, branch `feature/iga-marketing-master-2-build`. STATUS_SETUP.md written. |
| Architecture | Persistent | ✅ Final | ARCHITECTURE.md synced to delivered code (§5/§9/§11/§14/Appendix C edits). All 8 open items closed. STATUS_ARCHITECTURE_FINAL.md written. |
| Documentation | Persistent | ✅ Complete | Phase 2 done: 19 source files annotated (591 comment lines); README + TROUBLESHOOTING fully rewritten. STATUS_DOCUMENTATION.md written. 3 code concerns flagged (2 real, 1 cosmetic). |
| Tool | Persistent | ⏳ On-demand | Activated when developer agents request tools |
| config-and-cli-agent | Developer | ✅ Complete | 5 source files + 3 test files (52 hermetic tests) + bootstrap.ps1. Open items #4 (log retention 10MB×5) and #7 (bootstrap script) resolved. 3 flags logged in Issues. |
| field-map-agent | Developer | ✅ Complete | `field_map.py` + 31 tests passing. Open item #6 resolved (per-call vs batch is GUI policy, not storage). DECISION-MAP + STATUS written. |
| state-agent | Developer | ✅ Complete | `state.py` (~1,486 LOC, stdlib-only) + 32 tests passing. Open item #1 resolved (natural-key policy published per repeatable). `schema_version: 1` confirmed. Recovery chain: state→.bak→newest snapshot→typed error. |
| claude-client-agent | Developer | ✅ Complete | `claude_client.py` + 30/30 tests passing. Open item #2 resolved (anthropic 0.97.0 verified; `cache_creation_input_tokens`/`cache_read_input_tokens` attribute names confirmed). All escalation triggers + retry + 80-page split covered. Issue #1 (empty enum) handled gracefully. |
| extraction-agent | Developer | ✅ Complete | `extract.py` + 14/14 tests on Python 3.13. Open item #5 resolved (basename doc_id; duplicate guard `DuplicatePdfBasenameError`). JIT proposals queued; GUI is sole writer to Field Map. Crash-safe via `pending_extraction`. |
| gui-agent | Developer | ✅ Complete | `gui/` subpackage (8 files) + 29/29 model tests. Open item #3 resolved (tabs derive from state namespaces; LOB rollup; static TAB_ORDER for known keys, alpha for unknown). Note: `gui.py` stub removed (Python can't have same-name module + package); canonical entry is `gui/__init__.py`. |
| epic-driver-agent | Developer | ✅ Complete | `epic_session.py` + `enter.py` + 29/29 in-scope tests + 190/190 across the full repo (no regressions). Lock cleanup covers 6 lock files; 3-strategy locator chain; pause-for-human with DOM-as-truth resume; --debug trace.zip + auto-prune. |

---

## Build Mode Determination
- **Mode:** New Project
- **Reason:** No source code, no git history, no ARCHITECTURE.md, plan explicitly describes ground-up rewrite ("No code is being reused from v1 — the conceptual reset is too deep" — PLAN-REVIEW.md).
- **Existing assets in project root (preserved, not reset):**
  - `Library/Epic Field Map.json` — authoritative field universe (~16K lines, ~1,631 fields)
  - `Testing and Example Library/Extracted.xlsx` — historical reference for review layout
  - `PLAN.md`, `PLAN-REVIEW.md`, `PROCESS-MAP.md`, `RESEARCH.md`, `COMMS.md` — workflow artifacts

## Required Deliverables (from PLAN-REVIEW.md)
- `TROUBLESHOOTING.md` (mandatory)
- `ARCHITECTURE.md` (produced by architecture agent)
- `STATUS_*.md` for each persistent and developer agent
- `DECISION-MAP-<agent>.md` per developer agent (Mermaid flowcharts)
- All 10 source modules per the module breakdown in PLAN-REVIEW.md

## Active Build Constraints (carried from /plan-review and /research)
- Path B: single-user, local disk, no SharePoint/lock-files
- DO NOT propose Applied EPIC REST API — explicitly rejected by user
- `keyring` library for secrets (not raw `pywin32`)
- `platformdirs` for paths (not deprecated `appdirs`)
- Playwright pinned ≥1.55 with `cleanup_user_data_dir_lock()` before every launch
- `OperatorModal` widget mandatory for all error/conflict/pause UIs
- Cache hit verification: log `cache_creation_input_tokens` / `cache_read_input_tokens`
- PDF split: 80-page chunks, 1-page overlap, merge by `domain_tag`
- state.json daily snapshots, 30-day retention
- Pre-flight selector smoke at start of every entry session (touched screens only)

---

## Orchestrator Log

### 2026-04-30 — Build initialized
- /build invoked. Gate: PLAN-REVIEW.md ✅ APPROVED.
- Mode determined: New Project.
- COMMS.md initialized.
- Developer agent breakdown logged (7 agents).
- Spawning Setup Agent next.

### 2026-04-30 — Setup complete
- 30 files staged, initial commit `37182af` on branch `feature/iga-marketing-master-2-build`.
- All 11 placeholder modules created in `src/iga_marketing_master_2/`. Tests, tools, docs, assets, scripts folders created.
- `requirements.txt` floored at versions noted in plan; developer agents to tighten during Phase 1.
- Minor flag: `pyproject.toml` declares `cli:main` script entry — `main()` doesn't exist yet; config-and-cli-agent will land it.
- Spawning Architecture Agent next (blocks all developer agents).

### 2026-04-30 — Architecture initial complete
- ARCHITECTURE.md written (14 sections + 2 appendices). STATUS_ARCHITECTURE_INITIAL.md written.
- PLAN-REVIEW open question #1 RESOLVED: `domain_tag` grammar (dot-separated lowercase, fixed namespaces, fixed LOB list, alias rules, 18 worked examples).
- Notable architecture ruling: `schema_version: 1` added to state.json (not in original plan). State-agent has authority to push back if it sees a real conflict.
- 8 open items routed to specific developer agents.
- Spawning all 7 Developer Agents in background-parallel. Documentation Agent deferred to Phase 2.

### 2026-04-30 — Developer agents spawned (background, parallel)
- 7 agents working concurrently against ARCHITECTURE.md contracts.
- Each will produce: `DECISION-MAP-<agent>.md`, implementation files, tests under `tests/test_<module>.py`, and `STATUS_<AGENT>.md` on completion.
- Conflicts surfacing during build will route through orchestrator → architecture agent → ruling → back to affected dev agents.
- When all 7 STATUS files exist, orchestrator will spawn Documentation Phase 2 and write STATUS_BUILD.md.

### 2026-04-30 — All 7 developer agents complete
- field-map-agent ✅ (31 tests) → config-and-cli-agent ✅ (52 tests) → state-agent ✅ (32 tests) → extraction-agent ✅ (14 tests) → claude-client-agent ✅ (30 tests) → epic-driver-agent ✅ (29 tests; 190/190 across repo) → gui-agent ✅ (29 tests).
- **Aggregate: 217 hermetic tests passing, no cross-module regressions.**
- All 8 ARCHITECTURE.md §14 open items resolved within their owning agents.
- Issues 1-6 logged (5 minor; 1 self-resolved). Routing dep cleanup back to config-and-cli-agent (Issues #3, #6) and helper completion to state-agent (Issue #5) before Documentation Phase 2.
- Issue #2 (secret_store API change) self-resolved: gui-agent owns `ApiKeyPromptDialog` per the dependency graph; architecture-agent will update §9 at finalization.

---

## Architecture Requests
*None yet.*

---

## Tool Requests
*None yet.*

---

## Issues Log
| # | Agent | Issue | Status | Resolution |
|---|---|---|---|---|
| 1 | field-map-agent | Empty `domain_tag` enum on fresh Field Map — claude-client-agent must drop the enum constraint when `generate_domain_tag_enum()` returns `[]` (otherwise no extraction can happen on bootstrap) | ✅ Resolved | claude-client-agent confirmed: "enum gracefully omitted when empty (early v1)". |
| 2 | config-and-cli-agent | Removed `prompt_for_anthropic_api_key_via_gui` from `secret_store.py` because it would violate the §11 dependency graph (`gui → secret_store` is one-way). gui-agent's `ApiKeyPrompt` modal now owns the dialog and calls `secret_store.set_anthropic_api_key()` directly. Console-fallback retained in secret_store. | 🟢 Self-resolved | Correct per dependency graph. ARCHITECTURE.md §9 should be updated by architecture-agent to remove the function from secret_store's API. Will route at finalization if not already done. gui-agent already has `ApiKeyPrompt` in scope per ARCHITECTURE §8.3. |
| 3 | config-and-cli-agent | `pytest` not in `requirements.txt` (out of scope to add). Bootstrap script can't install it. | ✅ Resolved | fixup agent added `[project.optional-dependencies] test = ["pytest>=8"]` to `pyproject.toml`. |
| 4 | config-and-cli-agent | `pywin32` in `requirements.txt` may be unused (`keyring` uses its own ctypes shim). Left in place defensively. | ✅ Resolved | fixup agent verified no `win32*` import anywhere under `src/`; removed from both `requirements.txt` and `pyproject.toml`. |
| 5 | extraction-agent | Requested optional helper `state.set_pending_domain_tag_proposals(state, list)` not specified in ARCHITECTURE §5.3. | ✅ Resolved | fixup agent added the helper + getter, plus `pending_domain_tag_proposals` field on `State` dataclass with default `[]`. Round-trip preserved across save/load. 5 new tests; total now 224 passing. |
| 6 | epic-driver-agent | `psutil` not in `requirements.txt`. PID-liveness check falls back to `os.kill(0)` (POSIX) or "treat-as-stale" (Windows). | ✅ Resolved | fixup agent added `psutil>=5.9` to both `requirements.txt` and `pyproject.toml` with a comment pointing at `epic_session.cleanup_user_data_dir_lock`. |
| 7 | fixup agent | Pre-existing issue: missing pytest config means single-file test runs (`pytest tests/test_X.py`) fail with `ModuleNotFoundError` unless `PYTHONPATH=src` is set. Full-suite runs work via package-layout discovery. | 🟡 Defer to /check | Small `[tool.pytest.ini_options] pythonpath = ["src"]` block in pyproject.toml would fix it. Not blocking — flagging for /check verification. |
| 8 | Documentation Phase 2 | gui/main_window.py `_launch_extraction` passes `self._client.path` (a Path) where `extract.run_extraction` expects `client_name: str`. Silent mis-resolution risk. | ✅ Resolved | gui-fix agent: replaced `self._client.path` with `self._client.name`; removed bad kwarg; added `settings=self._settings`. 224/224 tests passing. |
| 9 | Documentation Phase 2 | gui/main_window.py `_on_begin_entry` signature mismatch with `enter.run_entry_session`. Hard TypeError on Begin Entry. | ✅ Resolved | gui-fix agent: composed `state.load + field_map.load + epic_session.launch_with_persistent_context` locally on the GUI thread; passes `(state, field_map, browser_context, *, on_pause_callback=..., on_progress_callback=..., settings=..., client_path=...)`. Added `try/finally` for browser_context + Playwright cleanup. |
| 10 | Documentation Phase 2 | enter.py ~line 272 has a late re-import of `field_map` inside a loop body (cosmetic). | 🟡 Defer to /optimize | Cosmetic; performance-neutral after import cache. |
| 11 | gui-fix agent | Latent vocab mismatch: `gui.operator_modal.PauseChoice.CANCEL.value == "cancel"` but `enter.run_entry_session` expects `"resume" \| "skip" \| "abort"`. Would have caused every operator-cancelled pause to log `enter.pause_invalid_resolution` and fall through to abort. | ✅ Resolved (bridge mapping) | Mapped at the GUI bridge: `cancel → abort`. Both sides' vocabularies preserved. Cleaner long-term: rename `PauseChoice.CANCEL → ABORT` at the source — flagged for /optimize. |

---

## Clarifying Questions
| # | From Agent | Question | Escalated? | Answer |
|---|---|---|---|---|
| — | — | *None yet* | — | — |

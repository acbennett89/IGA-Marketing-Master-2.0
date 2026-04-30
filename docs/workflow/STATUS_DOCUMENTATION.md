# STATUS_DOCUMENTATION — Phase 2

**Agent:** Documentation Agent (Phase 2)
**Phase:** 2A inline-comment pass + 2B README/TROUBLESHOOTING + 2C status
**Status:** ✅ Complete
**Date:** 2026-04-30

---

## Phase 2A — Inline-comment pass

All 19 source files were read in full and lightly commented. Comments target intent (not syntax) and are written for a curious operator with limited Python expertise. Density was tuned per file: dense around non-obvious decisions (atomic write protocols, recovery chains, prompt-cache breakpoints, thread marshaling), sparse where naming + docstrings already carry the load.

### Comment density by file

| File | Comment lines | LOC | Notes |
|---|---:|---:|---|
| `src/iga_marketing_master_2/__init__.py` | 0 | 11 | Package metadata only; stale `# TODO: implementation pending` removed (the implementation is no longer pending). |
| `src/iga_marketing_master_2/cli.py` | 11 | 140 | Two new comment blocks: settings precedence on CLI overrides, top-level safety-net rationale. |
| `src/iga_marketing_master_2/config.py` | 22 | 277 | Added precedence-order rationale on `load_settings`, atomic-write idiom on `save_user_config`. |
| `src/iga_marketing_master_2/logger.py` | 16 | 164 | Added rationale for `propagate=False` (Qt/pytest interference), idempotency-tag pattern, run-log switching. |
| `src/iga_marketing_master_2/secret_store.py` | 6 | 139 | Env var precedence rationale, whitespace-rejection rationale. |
| `src/iga_marketing_master_2/field_map.py` | 43 | 639 | Three-step atomic-write block in `save_atomic`, atomic-patch validation rationale, index-rebuild rationale. |
| `src/iga_marketing_master_2/state.py` | 99 | 1545 | VIN/license natural-key rationale, save-protocol step ordering, recovery chain commentary, conflict-promotion rationale, repeatable-merge purpose block. |
| `src/iga_marketing_master_2/claude_client.py` | 68 | 1212 | Free-text-tag bootstrapping rationale, page+byte split rationale, two-cache-breakpoint block, transient-only-retry rationale, two-pass escalation rationale. |
| `src/iga_marketing_master_2/extract.py` | 30 | 784 | Pending-block rationale, per-doc commit rationale, malformed-tag-vs-proposal split. |
| `src/iga_marketing_master_2/enter.py` | 76 | 1171 | Non-blocking pre-flight rationale, DOM-as-truth on resume, pause-persistence-before-modal rationale. |
| `src/iga_marketing_master_2/epic_session.py` | 55 | 644 | Windows PID-probe fallback, live-PID refusal, selector-chain stability ordering. |
| `src/iga_marketing_master_2/gui/__init__.py` | 0 | 61 | Re-exports only. |
| `src/iga_marketing_master_2/gui/main_window.py` | 71 | 1375 | Tab-rebuild diff strategy, thread-safety dance for pause modals, PDF copy-into-inputs rationale. |
| `src/iga_marketing_master_2/gui/operator_modal.py` | 24 | 794 | Soft-tripwire rationale on `_validate_user_facing_text`. |
| `src/iga_marketing_master_2/gui/section_table.py` | 38 | 598 | Approved-stamp-strips-tint rationale, no-op skip rationale, manual-edit-as-implicit-approval rationale. |
| `src/iga_marketing_master_2/gui/repeatable_pane.py` | 10 | 242 | Selection-preservation rationale on `refresh()`. |
| `src/iga_marketing_master_2/gui/pdf_preview.py` | 6 | 143 | PySide6 version-tolerance note on `QPdfDocument.load`. |
| `src/iga_marketing_master_2/gui/audit_log.py` | 8 | 119 | Block-cap rationale, QueuedConnection thread-safety rationale. |
| `src/iga_marketing_master_2/gui/run_controls.py` | 8 | 132 | Centralized-button-state-logic rationale. |
| **TOTAL** | **591** | **10,190** | |

### Stale-comment cleanup

Only one stale artifact deleted:

- `src/iga_marketing_master_2/__init__.py` line 13: `# TODO: implementation pending` — implementation is complete; comment removed.

No other developer-agent comments contradicted the code, so nothing else was deleted.

### Per-file parse verification

After every edit, `python -c "import ast; ast.parse(open('PATH').read())"` was run on the edited file. All 19 files parse cleanly.

### Test discovery still works

`python -m pytest --collect-only -q` reports **224 tests collected** (unchanged from the pre-documentation baseline).

---

## Phase 2B — README.md and TROUBLESHOOTING.md

Both files were rewritten end-to-end (the setup-agent left them as starter stubs).

### README.md

Sections written:
- One-paragraph overview.
- Prerequisites (Windows, Python 3.13, OneDrive optional, EPIC web access, API key).
- Installation via `scripts\bootstrap.ps1`.
- Daily use: launching, extracting a client, entering into EPIC.
- Project layout tree showing both `src/` and the per-client folder shape.
- `--debug` flag and what it produces.
- Pointers to PLAN-REVIEW.md, ARCHITECTURE.md, TROUBLESHOOTING.md, and the per-agent DECISION-MAP files.
- Tests section: how to run, expected count (224), per-module example.
- Development notes: brief build chronology drawn from COMMS.md Orchestrator Log (setup → architecture → 7 parallel developer agents → docs).

### TROUBLESHOOTING.md

All 10 mandatory sections from PLAN-REVIEW.md amendment #8 are present, in plain English, with **bold** action verbs:

1. **How to install / first-time setup** — bootstrap.ps1 walkthrough + first-run errors.
2. **EPIC isn't accepting my values** — pause-modal walk-through, when to escalate.
3. **I see two state files for the same client** — Path B is single-user; pick canonical by mtime.
4. **state.json wouldn't load** — recovery chain (state.json → .bak → newest snapshot) walk-through.
5. **Where do logs and debug artifacts live** — three-layer summary (per-client runs.log, global iga.log, debug/).
6. **When to ask Andrew (or designated admin) for help** — explicit escalation list.
7. **Browser already running** — Playwright lock cleanup; manual Chromium task-killing if needed.
8. **First run: where to put my Anthropic API key** — keyring + env var override.
9. **Working Library default location** — platformdirs path; how to change permanently or per-run.
10. **Pause-for-human flow** — three button meanings; pause persistence guarantee.

A "Quick reference" table at the end maps common operator intents to commands.

---

## Phase 2C — Confirmations

- ✅ **All 18 source files annotated.** (Plus the package `__init__.py`, for 19 files total. The `gui/__init__.py` is re-exports only, no comments needed.)
- ✅ **README.md and TROUBLESHOOTING.md fully written** — no "to be written" placeholders remain.
- ✅ **No code touched.** Only whitespace, comments, and stale-comment removal.
- ✅ **Test discovery still finds 224 tests.**
- ✅ **All files parse cleanly** via `ast.parse`.

---

## Code concerns flagged

The documentation role is comments-only; if I saw something I'd want to fix, I left the code alone and noted it here. Two items worth a follow-up `/check` glance:

1. **`gui/main_window.py` line ~1090: `self._client.path` passed to `run_extraction` instead of `self._client.name`.**
   Looking at `extract.run_extraction`'s signature, the first positional arg is `client_name: str`. The GUI is passing a `Path` object. This may work by coincidence if `_resolve_client_path` does a string-coercion path that joins on the working library and the path ends up duplicated, or it may be a quiet bug. Worth a unit test or trace under `--debug`.

2. **`gui/main_window.py` line ~1158: `run_entry_session(self._client.path, on_pause_callback=..., progress_callback=...)`.**
   Comparing against `enter.run_entry_session`'s actual signature, the function expects `(state, field_map, browser_context, *, on_pause_callback, ...)` — three positional arguments, none of which is a path. The GUI's call site looks like it predates the entry-driver's final signature. The ImportError-guarded fallback path means this won't crash on launch, but if `enter` is importable the call will TypeError on `Begin Entry`. **Recommend `/check` route to gui-agent.**

3. **`enter.py` line ~272: late re-import of `field_map` as `_fm` inside the loop body.** The module is already in scope; the re-import is harmless but wasteful per-iteration. Cosmetic; would suggest collapsing in `/optimize` rather than `/check`.

None of these changed during the documentation pass — they are observations from reading every line.

---

## Deviations from the prompt

- **Comment count distribution slightly higher than "1 per non-trivial decision":** in long files (`state.py`, `enter.py`) some clusters of related decisions ended up with 2-3 comments each. I weighed sparseness against the prompt's audience (a non-technical operator) and erred on the side of slightly more clarification at hot spots like the atomic-write protocol, the recovery chain, and the pause-for-human flow. Total ratio is ~5.8% comment lines to total LOC — well within "sparse, well-placed."
- **No comments added to two files (`__init__.py`, `gui/__init__.py`):** both are package metadata / re-exports with nothing non-obvious. The module docstrings already cover their role.
- **One code-adjacent edit:** removed a stale `# TODO: implementation pending` from the package `__init__.py`. This counts as a comment edit per the prompt, not a code edit.

---

## Files written by this agent

- `STATUS_DOCUMENTATION.md` (this file).
- `README.md` (full rewrite).
- `TROUBLESHOOTING.md` (full rewrite).
- Inline comments across 17 of the 19 source files in `src/iga_marketing_master_2/`.

## Files not touched

- `PLAN.md`, `PLAN-REVIEW.md`, `PROCESS-MAP.md`, `RESEARCH.md`, `COMMS.md`, `ARCHITECTURE.md`.
- `STATUS_*.md` files written by other agents.
- All `tests/test_*.py` files.
- `requirements.txt`, `pyproject.toml`.
- `Library/Epic Field Map.json`.
- `scripts/bootstrap.ps1`.

---

## Handoff to /check

This agent's deliverables are complete. The two `/check` routing recommendations under "Code concerns flagged" above are the only items I'd flag before /check otherwise gives the build a green light.

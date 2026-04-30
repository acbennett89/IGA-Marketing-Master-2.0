# Plan Review: IGA Marketing Master 2.0
**Original Plan:** [PLAN.md](PLAN.md) (preserved, unmodified)
**Reviewed:** 2026-04-30
**Status:** ✅ APPROVED WITH MATERIAL SCOPE CHANGE — Path B chosen; re-run `/research` before `/build`
**Decision logged:** 2026-04-30 — User chose Path B (defer multi-user/SharePoint to v1.5). v1 ships single-user with local `Working Library/`.

---

## 4 Fundamentals Check

| Fundamental | Present? | Notes |
|---|---|---|
| Goal | ✅ | Explicit and bounded — extract from PDFs via Claude, enter into EPIC via Playwright, with per-client review GUI between. |
| Input | ✅ | 1-N PDFs per run, drag-drop or browse, single client per run. |
| Output | ✅ | (1) per-client `state.json`; (2) EPIC fields populated from a user-chosen entry page. |
| Stack | ✅ | Python 3.13, PySide6 + QtPdf, Anthropic SDK, Playwright. Windows-only. |

All four are present and specific. Proceeding to feasibility evaluation.

---

## Review Annotations Key
> 📝 Review Note: Informational — no change required
> ⚠️ Review Note: Concern flagged — amendment applied below
> 🔴 Review Note: Blocker — must be resolved before approval

---

# IGA Marketing Master 2.0 — Ground-Up Rewrite

**Status:** 🟡 DRAFT — Pending /plan-review approval
**Mode:** New build (informed by lessons from `IGA-Marketing-Master` v1)
**Gate:** /build is locked until /plan-review sets Status to ✅ APPROVED
**Artifacts:** PLAN.md · PROCESS-MAP.md
**Source plan file:** `C:\Users\Andrew\.claude\plans\q9-yes-build-binary-reddy.md`

---

## Context — Why we're rewriting

The original IGA Marketing Master was built functionality-first: it locked Excel as the source of truth, hardcoded coverage-per-sheet contracts into the workflow, and stalled when the team realized the foundation — "what does EPIC actually accept as input?" — was never modeled.

This rewrite inverts the foundation: **Applied EPIC's field universe is modeled first** (in `Library/Epic Field Map.json`, already ~75% scraped). Documents flow into a Claude-driven extractor that produces a single canonical JSON state per client; a PySide6 GUI is the human review/edit interface; a Playwright entry script reads the approved state and drives EPIC from a page the user has already navigated to. Excel as an editable artifact is dropped.

> 📝 Review Note: The "EPIC field universe first" framing is sound. v1's failure mode is well-understood and the rewrite genuinely addresses it. No concerns at the architectural-direction level.

---

## The 4 Fundamentals

| | |
|---|---|
| **Goal** | Extract insurance data from arbitrary PDF documents using the Claude API and enter it into Applied EPIC via Playwright, with a stateful per-client review/edit GUI in between. |
| **Input** | 1-N user-selected PDF files for a single client per run. |
| **Output** | (1) `Working Library/<Client>/state.json` — canonical extracted state. (2) Field-by-field data entry into Applied EPIC. |
| **Stack** | Python 3.13 · PySide6 (GUI + QtPdf) · Anthropic SDK · Playwright · Windows-only |

> ⚠️ Review Note: **Stack is missing an explicit secret-management story.** Plan says "env-driven secrets" in `config.py` — but for an operator who isn't comfortable setting environment variables, "set ANTHROPIC_API_KEY in your shell" is a real first-time failure point. Also, an env var is plain-text and persists in shell history.
>
> **Amended Stack addition:** First-run prompt for the Anthropic API key, stored in `%APPDATA%/IGA Marketing Master/secrets.dat` encrypted via Windows DPAPI (`win32crypt.CryptProtectData` from `pywin32`, or the stdlib equivalent). Env var override remains supported for power users / CI. This adds `pywin32` as an explicit dependency.

---

## Process Map

> See PROCESS-MAP.md for the full visual diagram.

> 📝 Review Note: Process map is comprehensive. The auto-escalation branch (`D1`) and the selector-fallback chain (`M`/`M2`) are well-modeled. No flow changes triggered by this review.

---

## Problem Statement

Insurance dec pages, schedules, and supplemental documents arrive in arbitrary shapes across many carriers. Today, transcribing them into Applied EPIC is manual — slow, error-prone, and the current v1 tool's Excel-first design can't generalize.

---

## Proposed Solution — Architecture

**The five subsystems:** Epic Field Map · Extractor · Canonical state · Review GUI · Entry Driver.

> 📝 Review Note: Subsystem boundaries are clean and minimal — each owns one job. No "god module" lurking. Good.

---

## The Epic Field Map — schema additions

Adds: `is_required`, `is_repeatable`, `repeatable_group`, `parent_field`, `enum_values`, `validation_pattern`, `depends_on`, `default_value`, `domain_tag`, `aliases`, `last_verified_at`, `epic_build_version`, `notes_for_claude`, `version`. Populated **just-in-time** as runs encounter gaps.

> ⚠️ Review Note: **The tool-use schema's `domain_tag` enum is regenerated per call from the live Field Map.** That's good for preventing hallucination but has a subtle interaction with prompt caching: tool definitions are part of the cacheable prefix. Whenever a new `domain_tag` is added (JIT), the next call invalidates the tool-definitions portion of the cache for that session.
>
> **Amendment (informational, no code change required, just documentation in `claude_client.py`):** Document this interaction explicitly. In practice, JIT enrichment clusters per review session — a few cache misses per session, not per call — so the cost impact is negligible. But the log should record cache_miss reasons so the operator (or future maintainer) can correlate cost spikes to Field Map churn.

---

## State.json schema (per client)

Keyed by `domain_tag`. Repeatables nest as arrays. Per-field `history[]` + top-level `run_history[]`. Atomic writes via `os.replace()` + `.bak`.

> ⚠️ Review Note: **Single rolling `.bak` file is thin disaster-recovery insurance.** If `state.json.bak` is itself stale (only updated on the previous successful write) and a corruption pattern persists across two writes, both files are lost.
>
> **Amendment:** Add a daily snapshot of every active client's `state.json` to `Working Library/<Client>/snapshots/state-YYYY-MM-DD.json`, retained for 30 days. Snapshots only created on the first successful write per day per client (so no cost on idle clients). This is cheap (small JSON files) and turns a class of "weeks of work lost" failures into "lose at most one day."

---

## Claude API — extraction details

Model: Sonnet 4.6 default; auto-escalate to Opus 4.7 per-field on `confidence < 0.7` OR `needs_review` OR required-field-missing. Tool use, not JSON mode. Two cache breakpoints. PDF document blocks with pagecount/size preflight.

### Sonnet 4.6 vs Opus 4.7 cost comparison

[Tables and policy preserved from PLAN.md.]

> ⚠️ Review Note: **PDF "split with overlap fallback" is under-specified.** The plan acknowledges Anthropic's ~100 page / 32 MB per-PDF limit but says "split with overlap" without defining the mechanism. For a non-technical maintainer, "the tool said extraction failed because the PDF is too big" with no automatic recovery is exactly the kind of failure /plan-review is supposed to catch.
>
> **Amendment:** Concrete spec for `extract.py` split logic:
> 1. Pre-flight: check `pypdf` page count + file size.
> 2. If over limit: split at page boundaries into chunks of `max_pages_per_call = 80` (under the 100 ceiling), with 1-page overlap between chunks (so a fact spanning a page break is seen by both chunks).
> 3. Run a separate Claude extraction call per chunk; merge results by `domain_tag` using the same conflict-detection logic used for multi-doc merging.
> 4. Log the split decisions to `runs.log` and `debug/` so operators can see what happened.

---

## Module breakdown

```
src/iga_marketing_master_2/
├── cli.py · gui.py · extract.py · enter.py · field_map.py
├── claude_client.py · epic_session.py · state.py · logger.py · config.py
Library/Epic Field Map.json
Working Library/<Client>/  # SharePoint-synced
```

> 📝 Review Note: Module list is appropriately granular. No god-module risk. Each file's responsibility is a single sentence.

---

## Multi-user via SharePoint

~~SharePoint sync via OneDrive client (not Graph API). Per-client `state.json.lock`; Field Map versioned with compare-and-swap; OneDrive conflict-copy detection.~~

> ⚠️ Review Note: **DEFERRED TO v1.5 — Path B chosen.** v1 ships single-user with all data on local disk. The original plan content above is preserved for v1.5 reference but is **not part of v1 scope**.
>
> **Amendment for v1 (Path B):**
> - **Working Library location:** local, configurable. Default: `%USERPROFILE%/Documents/IGA Marketing Master/Working Library/`. Operator can pick a different folder via first-run dialog (some users may want to put it on a OneDrive/SharePoint-synced path themselves — that's their choice, not the app's responsibility).
> - **Field Map location:** stays in repo at `Library/Epic Field Map.json`. JIT enrichment writes back to that file with plain atomic-replace. No `version` field, no compare-and-swap.
> - **state.json:** plain atomic write + rolling `.bak` + daily snapshot (per amendment #2). No `state.json.lock`. No conflict-copy detection.
> - **Per-user identity in history:** still record `os.getlogin()` on every history entry — cheap, useful for single-user audit trail and forward-compatible if v1.5 introduces multi-user.
> - **Tool's responsibility re: shared knowledge:** none. v1 operators sharing a Field Map can do so manually (commit to git, share via email, etc.) until v1.5 lands.
>
> **What v1.5 will add (out of scope for v1):**
> - SharePoint path discovery + auto-detection
> - Per-client lock file with stale-lock breaker
> - Field Map version + compare-and-swap
> - OneDrive conflict-copy detection + manual-merge UI
> - Persistent SharePoint sync status banner

---

## PySide6 GUI design

`QTableView` + `QAbstractTableModel`. `QStyledItemDelegate` for editor widgets. Confidence highlighting. Repeatable groups via list+form pattern. `QtPdf` preview. `QPlainTextEdit` audit log.

> ⚠️ Review Note: **Operator-facing error UX is currently described at the technical-control level, not at the operator-experience level.** "Pause modal during entry" is a control; what does the operator *see* and *understand*? When EPIC rejects "GA" for a State field, will the modal say "EPIC rejected the value 'GA' for the State field. Try fixing it manually in EPIC, then click Resume here" — or will it say "Locator failed: input.streState"? The first is operator-recoverable; the second is a panic.
>
> **Amendment (mandatory for /build):** Every operator-facing error/conflict/pause modal must follow a fixed structure:
> 1. **Plain-English headline** ("EPIC didn't accept 'GA' for State.")
> 2. **What to do now** ("Open the State dropdown in EPIC, pick the right value, then click Resume.")
> 3. **What happens if you click Cancel** ("Your edits are saved. Run won't continue.")
> 4. **Optional 'Show technical details' fold-out** for the underlying selector/error.
>
> This is a build-time discipline, not a separate feature. Add a single `OperatorModal` widget class in `gui.py` that enforces this structure.

---

## Playwright entry — the hard parts

Persistent context, not CDP attach. Selector chain: `data-automation-id` → `name` → label fallback. Pause-for-human with DOM-as-truth resume.

> ⚠️ Review Note: **EPIC selector smoke test is in Phase 5 hardening as "manual + on-demand from GUI; auto-trigger if `last_verified_at` > 30 days."** That's a 30-day window during which an EPIC update could break selectors silently and the operator would only find out mid-entry, hitting pause-after-pause.
>
> **Amendment:** Add a fast pre-flight check at the start of every entry session: walk only the screens we're about to touch (derived from approved fields' `screen_code` set), confirm each `data-automation-id` resolves, surface a single combined warning if any drift detected. Should add ~2-5 seconds to entry session startup, not 30. Specifically:
> - Run after user clicks "Begin Entry" but before the first field is entered.
> - If any selector fails: surface a non-blocking warning ("3 selectors look stale — drift may slow this run") and proceed. Don't block.
> - The Phase 5 full-Field-Map smoke test remains as planned for periodic full validation.

---

## Roadmap

Phase 1 (Foundation) → Phase 2 (Extraction) → Phase 3 (Review GUI) → Phase 4 (EPIC entry) → Phase 5 (Hardening).

> 📝 Review Note: Phasing is correct. Foundation-first sequencing is the explicit lesson from v1 and the plan honors it. No phase reordering.
>
> However, two adjustments based on annotations above:
> - Phase 1 adds: DPAPI secret store; daily-snapshot logic in `state.py`.
> - Phase 4 adds: pre-flight selector smoke check; the `OperatorModal` widget class.
> - Phase 5 picks up: TROUBLESHOOTING.md (also a /build deliverable; flagged in Breakage Analysis).

---

## Known Edge Cases & Risks

[Risk table preserved from PLAN.md.]

> ⚠️ Review Note: **Two operator-relevant risks are missing from the table.** Adding them:
>
> **Amendment — added rows:**
> | Risk | Severity | Mitigation |
> |---|---|---|
> | Anthropic API outage mid-run | Low | Retry with exponential backoff; clear operator message ("Claude is temporarily unavailable, will retry; click Cancel to abort"); state.json reflects partial progress on resume |
> | Operator kills the app mid-extraction | Low | API call completes server-side regardless; on next launch, GUI detects `pending_extraction` flag and offers to resume from where state.json left off vs. discard and restart |
> | API key expired / revoked | Low | First API failure surfaces an operator-friendly "Re-enter API key" prompt; doesn't crash the app |
> | Pre-flight selector check warning blindness | Medium | Pre-flight warnings are a yellow banner that stays visible during the entry run; operator must acknowledge before clicking through |

---

## Out of Scope (v1)

[Preserved from PLAN.md.]

> 📝 Review Note: Out-of-scope list is appropriate and bounded. Excellent that "client creation" and "EPIC navigation" were explicitly punted — these are high-risk subsystems that v1 doesn't need.

---

## Open Questions (carried forward to /build)

1. `domain_tag` namespacing convention.
2. Selector smoke-test cadence (resolved by amendment above — adds entry-session pre-flight).
3. Auto-escalation confidence threshold (`< 0.7` proposed; finalize in Phase 2).
4. SharePoint sync path discovery (resolution depends on multi-user Path A vs B decision).
5. Per-user identity source (only relevant under Path A).

> 📝 Review Note: Questions 1, 3 are proper "tune during build" questions. Questions 2, 4, 5 are partially or fully resolved by amendments above.

---

## Verification

[Preserved from PLAN.md.]

> ⚠️ Review Note: Verification list is missing a critical item — **/build must produce TROUBLESHOOTING.md**. See Breakage Analysis below.

---

## Self-Prompt for /build

[Preserved from PLAN.md, with the following additions for the amended plan]

> 📝 Review Note: The /build self-prompt should be updated to reflect the amendments before /build is triggered. Specifically:
> - Add DPAPI secret store + first-run API key prompt to Phase 1.
> - Add `OperatorModal` widget class spec to Phase 3.
> - Add pre-flight selector smoke check to Phase 4.
> - Add daily state.json snapshots to Phase 1.
> - Add concrete PDF split-with-overlap algorithm to `extract.py`.
> - Add TROUBLESHOOTING.md as a mandatory deliverable.
> - **Reflect the user's decision on Path A vs Path B** for multi-user concurrency.

---

## Breakage & Troubleshooting Analysis

| Failure Point | Likelihood | Impact | Recommended Mitigation | Baked-in or Doc-only? |
|---|---|---|---|---|
| EPIC selector rot (data-automation-id changes) | High | High | Label fallback; pre-flight smoke at entry start; periodic full Field Map smoke | **Baked-in** |
| Anthropic API key expired/missing | Medium | High | First-run DPAPI-encrypted prompt; clear re-prompt UI on auth failure | **Baked-in** |
| Anthropic API rate limit / outage | Low | Medium | Exponential backoff with operator-visible retry message; resumable state | **Baked-in** |
| PDF over Anthropic page/size limit | Low | Medium | Pre-flight pagecount/size; auto-split at 80-page boundaries with 1-page overlap | **Baked-in** |
| ~~OneDrive sync paused / disconnected~~ | — | — | Deferred to v1.5 (Path B) | — |
| ~~OneDrive conflict copy created~~ | — | — | Deferred to v1.5 (Path B) | — |
| ~~Two users open same client~~ | — | — | Deferred to v1.5 (Path B) | — |
| state.json corrupted across both file + .bak | Very Low | Catastrophic | Daily snapshots in `snapshots/state-YYYY-MM-DD.json`, 30-day retention | **Baked-in** |
| Operator picks a Working Library path on a network drive that disconnects | Low | Medium | First-run dialog warns about network paths; on `OSError` during write, surface operator-readable message | **Baked-in** |
| Mid-entry crash | Medium | Medium | Per-field `entered` status in state.json; resume picks up un-entered fields only | **Baked-in** |
| EPIC validation rejects entered value | High | Low | Pause-for-human modal with operator-readable error; DOM-as-truth on resume | **Baked-in** |
| Field Map missing `domain_tag` for new field | Medium (early) → Low (mature) | Low | JIT propose-and-confirm via Claude + GUI confirmation dialog | **Baked-in** |
| ~~User forgets they have a SharePoint sync error~~ | — | — | Deferred to v1.5 (Path B) | — |
| Operator can't tell why a field was flagged | Medium | Medium | Confidence color + Opus badge + click-to-show source page in PDF preview | **Baked-in** |
| Operator hits an error UI written for engineers | High (without enforcement) | High | `OperatorModal` widget class enforcing plain-English/what-to-do/cancel-effect structure | **Baked-in** |
| App stops working after a Windows reboot | Low | Medium | Bootstrap script idempotent; first-run health check verifies all deps load | **Baked-in** + Doc |
| Operator wants to know "what happened on this run" | High | Low | Run history viewer reading `runs.log` + `run_history[]` from state.json | **Baked-in (Phase 5)** |

> **Required /build deliverable: `TROUBLESHOOTING.md`** must be produced alongside the main code, covering at minimum:
> - "How to install / first-time setup" (bootstrap script + API key prompt)
> - "OneDrive isn't syncing — what to do" (with screenshots if possible)
> - "EPIC isn't accepting my values" (selector drift escalation path)
> - "I see two state files for the same client" (manual merge guidance)
> - "Where do logs and debug artifacts live" (per-client `runs.log` + `debug/`)
> - "When to ask Andrew (or designated admin) for help" (the escalation list — selector drift, Field Map structural issues, anything outside the GUI's pause-for-human path)

---

## Feasibility Scores

| Dimension | Score | Status | Comment |
|---|---|---|---|
| Build Complexity | ~~8/10~~ → **6.5/10** | ✅ | After Path B (multi-user deferred): tool-use with regenerated enums, prompt caching with 2 breakpoints, atomic JSON with snapshots, PySide6 with custom delegates + QtPdf + repeatable list/form pattern, Playwright with selector fallback chain + pause-for-human. Concurrency primitives are gone. Comfortable. |
| Maintenance Complexity | 7/10 | ⚠️ | EPIC selector rot is the dominant ongoing risk. Mitigations are baked in (label fallback + pre-flight smoke + JIT Field Map enrichment). Anthropic API drift is contained by SDK pinning. PySide6 version drift is minor. |
| Non-Technical Operator Safety | 7/10 | ⚠️ | GUI-first design + pause-for-human + operator-readable modals (after amendment) are good. Residual risk: when EPIC genuinely rots a selector, operator needs TROUBLESHOOTING.md and an escalation path. |
| Scope Appropriateness | ~~7/10~~ → **9/10** | ✅ | Path B yielded a tightly-scoped v1: extraction + review + entry, single-user, local. v1.5 layer (multi-user/SharePoint) is honestly punted. |

---

## Phase 3 — Build Complexity Warning (8/10) — RESOLVED

**Resolution:** Path B chosen. Multi-user/SharePoint complexity deferred to v1.5. **v1 Build Complexity drops to ~6.5/10** (no concurrency primitives, no OneDrive integration, simpler Field Map I/O). The plan now lands within the comfortable range without further changes.

**Original 8/10 analysis preserved below for reference.**

### What makes it complex

1. **Four distinct integration domains in one tool:** Anthropic API (cache + tool use + PDFs), Playwright (persistent context + selector strategies + pause-resume), PySide6 (custom models + delegates + QtPdf), and a versioned multi-user JSON store. Each individually is intermediate; together they are advanced.
2. **The Field Map is a 1,631-entry data model that is itself logic** — not a flat config. JIT enrichment, version CAS, regenerated tool enums, label fallback writeback all couple to it.
3. **Operator-recoverability across all of the above** is itself a build constraint that touches every subsystem.

### What could go wrong during build

- **Tool-use schema drift** between Field Map regeneration and cached prompts → cache misses spike, costs creep, confusing logs.
- **PySide6 + QtPdf integration on Windows** can produce DPI / font / threading issues that don't reproduce on dev machines. Real-user smoke testing is required, not just unit tests.
- **Playwright persistent context locking** can produce "browser already running" errors if the app crashes without releasing the user-data-dir. Needs explicit cleanup-on-launch.
- **Multi-user concurrency primitives** are notoriously hard to test deterministically — race conditions surface in production, not in CI.

### What could go wrong post-build

- EPIC ships an update that renames `data-automation-id` patterns at scale; label fallback works for some screens but not all; operators see a wave of pauses. The tool keeps working but slowly. (Recoverable; mitigated.)
- A user's OneDrive sync silently breaks; their edits don't reach others; everyone disagrees about the canonical Field Map. (Detectable; mitigated by the persistent-banner amendment.)
- Anthropic deprecates a model or API surface; SDK pin saves you for now, but eventually requires a maintenance pass. (Standard; not a v1 risk.)

### Skills required to recover from failure

- **First-tier (operator):** Read TROUBLESHOOTING.md, restart the app, retry the run, click "Resume" on pauses.
- **Second-tier (Andrew or designated admin):** Inspect `state.json` and `runs.log`, edit Field Map metadata by hand, re-run with `--debug`.
- **Third-tier (developer):** Anything touching Playwright internals, prompt structure, or concurrency logic.

### Is there a simpler alternative that hits the Goal?

**Honestly assessed: not really.** The architecture is well-justified by the lessons from v1 and the goal genuinely requires AI extraction, Field-Map-driven entry, human review, and multi-doc merging. The two ways to materially shrink v1 are:

- **Path B (defer multi-user to v1.5):** Single-user, local Working Library, no lock files, no Field Map versioning. **Saves an estimated 10-15% of build effort and removes a meaningful slice of the failure surface.** Multi-user added in v1.5 once usage is real. **This is the most credible scope reduction.**
- **(Not recommended) Drop auto-escalation to Opus:** Saves ~5% effort. Costs review quality on hard documents.
- **(Not recommended) Drop label-fallback selector:** Catastrophic given EPIC rot is the #1 risk. Don't do this.

### The decision required

**Path A — Build multi-user in v1.** Plan ships as written (with the amendments above). Highest fidelity to the original intent. Highest build effort.

**Path B — Defer multi-user to v1.5.** Plan ships single-user-local in v1; multi-user/SharePoint sync added as v1.5 once you confirm there are real concurrent users to support. Recommended unless production day 1 has multiple operators waiting.

> **Status remains 🟡 IN REVIEW until you choose A or B.** Once chosen, this review is updated, the decision logged in the Change Summary, and Status flips to ✅ APPROVED with the chosen path baked in.

---

## Change Summary

| # | Section | Original | Amendment | Reason | Severity |
|---|---|---|---|---|---|
| 1 | Stack | "env-driven secrets" | DPAPI-encrypted secret store + first-run API key prompt; `pywin32` added | Operator can't be expected to set env vars; plain-text env vars leak | High |
| 2 | State.json schema | Single rolling `.bak` | Add daily snapshot to `snapshots/`, 30-day retention | `.bak` thin against persistent corruption; cheap to add | Medium |
| 3 | Claude API / PDFs | "split with overlap fallback" | Concrete spec: 80-page chunks, 1-page overlap, merge by `domain_tag` | Under-specified; operators can't recover from "PDF too big" without spec | Medium |
| 4 | Tool-use schema | (no note on cache) | Document Field-Map-churn → cache-miss interaction in `claude_client.py` logs | Cost transparency; future maintenance | Low |
| 5 | PySide6 GUI | Pause modal during entry | Mandatory `OperatorModal` widget class enforcing 4-part structure (headline / what to do / cancel effect / technical fold-out) | Operator-facing error UI must be operator-readable, not technical | High |
| 6 | Playwright entry | Smoke test in Phase 5 only | Add pre-flight smoke at start of every entry session (touched screens only) | 30-day drift window too long; cheap to check on entry | Medium |
| 7 | Risk table | (missing rows) | Added: API outage, mid-extraction kill, API key expired, pre-flight smoke blindness | Operator-visible failure modes weren't enumerated | Medium |
| 8 | Verification | (no TROUBLESHOOTING.md) | TROUBLESHOOTING.md mandatory /build deliverable | Operator self-recovery requires written guidance | High |
| 9 | Multi-user / SharePoint | Built into v1 (full SharePoint sync, lock files, version CAS, OneDrive conflict detection) | **Path B chosen 2026-04-30: deferred to v1.5.** v1 is single-user with `Working Library/` on local disk (configurable, default `%USERPROFILE%/Documents/IGA Marketing Master/Working Library/`). No lock files, no `version` field, no OneDrive integration. `os.getlogin()` still recorded in history for forward-compat. | Operator-confirmed scope reduction; decreases v1 build effort by ~10-15% and removes a meaningful slice of operator-visible failure surface | High |
| 10 | Self-Prompt for /build | (original v1 plan with multi-user) | Updated to reflect amendments 1-9 (Path B). Specifically: add DPAPI secret store, daily snapshots, PDF split spec, OperatorModal class, pre-flight smoke, TROUBLESHOOTING.md; **remove SharePoint, lock files, Field Map versioning, OneDrive detection.** | Self-prompt must be source of truth for /build | High |
| 11 | Field Map schema | `version` int field added for compare-and-swap | Path B: drop `version` field entirely from schema. Plain atomic write only. | Path B has no concurrent writers in v1; CAS adds nothing. | Medium |
| 12 | Out of Scope | (no v1.5 mention) | Explicitly add: "Multi-user concurrency / SharePoint sync (deferred to v1.5)" with the v1.5 capability list. | Make the deferral visible in canonical scope doc | Low |
| 13 | Working Library default | SharePoint-synced OneDrive path | `%USERPROFILE%/Documents/IGA Marketing Master/Working Library/` with first-run picker for override. Operator may *choose* a OneDrive-synced path themselves; the app doesn't manage that. | Path B is local-first | High |
| 14 | Secret store | DPAPI via raw `pywin32 win32crypt.CryptProtectData/Unprotect` (~30 lines) | `keyring` library: `keyring.set_password("IGA Marketing Master", "anthropic_api_key", value)` / `keyring.get_password(...)`. Same DPAPI security under the hood (Windows Credential Manager backend uses `pywin32-ctypes`/`pywin32`). | RESEARCH.md Finding 2: ~3 lines instead of ~30, identical security model, active maintenance, forward-compat with macOS/Linux. | Low (purely tactical) |
| 15 | Path discovery library | (unspecified in original plan; could have defaulted to deprecated `appdirs`) | `platformdirs` (explicitly pinned). Default Working Library: `Path(platformdirs.user_documents_dir()) / "IGA Marketing Master" / "Working Library"`. `%APPDATA%` discovery: `platformdirs.user_config_dir("IGA Marketing Master")`. | RESEARCH.md Finding 3: `appdirs` is deprecated; `platformdirs` is the actively-maintained successor. | Low (purely tactical) |
| 16 | Playwright reliability | (relied on default behavior) | Pin Playwright version in requirements (1.55+ as of April 2026); add `cleanup_user_data_dir_lock()` helper that runs before every `launch_persistent_context` call (deletes `SingletonLock`/`LOCK` files if no live PID); use absolute paths only for `user_data_dir`. Document the lock-file recovery path in TROUBLESHOOTING.md. | RESEARCH.md Finding 4: known Windows-specific bugs in 1.50-1.52 (Playwright issues #34700, #35836, #21019); without explicit cleanup, an app crash leaves a lock that blocks the next launch. | Medium |
| 17 | Cache verification | (no explicit verification) | `claude_client.py` logs `cache_creation_input_tokens` and `cache_read_input_tokens` from every API response. If `cache_creation_input_tokens == 0` AND `cache_read_input_tokens == 0` on a request that should have cached, log a warning. | RESEARCH.md Finding 5: Sonnet 4.6 cache minimum is 2,048 tokens; below threshold the request silently succeeds at full input price. Both our breakpoints clear this comfortably, but verify don't assume. | Low |

---

## Approval Decision

### ✅ APPROVED WITH MATERIAL SCOPE CHANGE — Re-run /research first, then /build

> **Path B chosen on 2026-04-30.** v1 ships single-user with `Working Library/` on local disk; multi-user/SharePoint sync deferred to v1.5.
>
> **PLAN-REVIEW.md is the authoritative plan.** All Path A multi-user content in PLAN.md is superseded by amendment #9 above; the rest of PLAN.md remains accurate after applying amendments #1-8 and #11-13.
>
> **Required next step before /build: run `/research`.** Path B is a material scope change from the original plan — the stack drops SharePoint integration entirely, and the local-first design has different research considerations (e.g., should we use `appdirs` for path discovery? Is `pywin32` for DPAPI the cleanest path or is there a stdlib alternative now? Is there a known gotcha with PySide6 + QtPdf 6.6+ on Windows that wasn't relevant in the multi-user design?). `/research` should focus on:
> - Stack assumptions for single-user local Windows (DPAPI secret store, Working Library default path conventions, atomic JSON write libraries on Windows).
> - PySide6 + QtPdf 6.6+ Windows-specific known issues.
> - Anthropic SDK current best practice for tool use + caching + PDF document blocks (verify current API surface against the plan's assumptions).
> - Playwright `launch_persistent_context` Windows gotchas + cleanup-on-crash patterns.
>
> **Required /build deliverables (locked in by this approval):**
> - Amendments #1-8 + #11-13 from the Change Summary, baked into the implementation.
> - `TROUBLESHOOTING.md` alongside the main code.
> - Updated `/build` self-prompt reflecting Path B (see "Self-Prompt for /build" section above — it must be revised before triggering /build).
>
> **Workflow gate:** ✅ /research → ✅ /build → /check → /optimize.

### Updated /build self-prompt (Path B — replaces the draft in PLAN.md)

```
Your goal is to build IGA Marketing Master 2.0 v1 — a single-user Windows desktop tool that extracts insurance data from PDF documents using the Claude API and enters it into Applied EPIC via Playwright, with a stateful per-client review/edit GUI in between.

Input: 1-N user-selected PDF files for one client per run (drag-drop or browse via GUI).

Output: (1) Per-client `Working Library/<Client>/state.json` on local disk — the canonical extracted state, edited by the user via GUI. (2) Field-by-field data entry into Applied EPIC starting from a screen the user has navigated to manually.

Stack: Python 3.13, PySide6 (GUI + QtPdf 6.6+), Anthropic SDK (Claude Sonnet 4.6 default; prompt caching with 2 breakpoints; tool use; PDF document blocks), Playwright (pinned >=1.55, `launch_persistent_context`, headed, with `cleanup_user_data_dir_lock()` before every launch), `keyring` (Windows Credential Manager / DPAPI for the secret store; pulls `pywin32` transitively), `platformdirs` (path discovery). Windows-only, single-user.

Architecture (build in this order):
1. Foundation: cli, config (using `platformdirs` for all path discovery), logger, field_map (no `version`/CAS), state (atomic JSON + daily snapshots), claude_client skeleton (with PDF split spec, `cache_creation_input_tokens`/`cache_read_input_tokens` logging on every call), bootstrap script, secret store via `keyring` library with first-run API key prompt.
2. Extraction loop: extract.py merges Claude tool_use output into state.json; conflict detection; JIT domain_tag proposal queue; concrete PDF split (80-page chunks, 1-page overlap, merge by domain_tag).
3. Review GUI: PySide6 with QTableView + QAbstractTableModel per section; QStyledItemDelegate editors; confidence highlighting; QtPdf preview with page deep-link; repeatable list+form pattern; audit log; domain_tag confirmation dialogs; **mandatory `OperatorModal` widget class** (4-part: headline / what to do / cancel effect / technical fold-out).
4. EPIC entry: epic_session.py + enter.py with persistent-context Playwright (Playwright pinned >=1.55; absolute `user_data_dir` paths only; `cleanup_user_data_dir_lock()` runs before every launch to clear stale `SingletonLock`/`LOCK` files); selector fallback chain (data-automation-id → name → label); pause-for-human via `OperatorModal` with DOM-re-read on resume; **pre-flight selector smoke check at entry-session start (touched screens only)**; --debug Playwright tracing.
5. Hardening: full-Field-Map smoke test, run history viewer, drift alerts, cost dashboard.

Working Library default: %USERPROFILE%/Documents/IGA Marketing Master/Working Library/ (configurable via first-run picker).

Constraints and conventions:
- Epic Field Map.json is the authoritative source; enrich JIT with new metadata (is_required, is_repeatable, repeatable_group, parent_field, enum_values, validation_pattern, depends_on, default_value, domain_tag, aliases, last_verified_at, epic_build_version, notes_for_claude). NO `version` field — single-user only. Plain atomic write. Never break the existing structure.
- domain_tag is a namespaced stable business-concept ID (e.g., submission.name); used as the key in state.json. Treat as a *cache* of Claude's mapping decisions, not a precondition.
- state.json: keyed by domain_tag; repeatables as arrays of records; per-field history[] + top-level run_history[]; atomic writes via os.replace + rolling .bak; **daily snapshots in `snapshots/state-YYYY-MM-DD.json`, 30-day retention**; `os.getlogin()` recorded on every history entry (for single-user audit + v1.5 forward-compat). NO state.json.lock.
- Tool use, not JSON-mode prompting. Single `record_extracted_field` tool with domain_tag enum regenerated per call. Optional `needs_review` bool. Log cache hit/miss reasons for cost transparency.
- Two prompt-cache breakpoints: stable system+glossary+examples, then Field Map.
- Default Sonnet 4.6; auto-escalate to Opus 4.7 per-field on confidence < 0.7 OR `needs_review` OR required field missing. GUI badges Opus-sourced fields. Per-run "Force Opus" override available, off by default.
- Playwright: persistent_context, never CDP attach. Selector resolution: data-automation-id → name → get_by_label fallback. On error: pause via `OperatorModal`, on resume re-read DOM as ground truth.
- API key: first-run prompt; stored via `keyring.set_password("IGA Marketing Master", "anthropic_api_key", value)` (Windows Credential Manager backend, DPAPI under the hood). Env var override for power users.
- Working Library default: `Path(platformdirs.user_documents_dir()) / "IGA Marketing Master" / "Working Library"`. Config + state files: `platformdirs.user_config_dir("IGA Marketing Master")`.
- Cache verification: log `cache_creation_input_tokens` and `cache_read_input_tokens` from every API response; warn if both are zero on a request that should have cached.
- --debug saves raw Claude requests/responses, Playwright trace.zip, screenshots before/after each action, verbose logs. Auto-prune debug artifacts to last 5 runs per client.
- TROUBLESHOOTING.md is a mandatory deliverable — see Breakage Analysis for required topics.
- Out of scope v1: client creation, EPIC navigation, headless, non-PDF inputs, Excel export, **multi-user/SharePoint sync (deferred to v1.5)**.

Build this step by step. Start with Phase 1 — foundation, field_map, state, and DPAPI secret store.
```

---

*Status: ✅ APPROVED WITH MATERIAL SCOPE CHANGE. PLAN.md is unchanged (preserved as-is). PLAN-REVIEW.md is authoritative. PROCESS-MAP.md does not need to change — Path B doesn't alter the program flow, only the storage substrate. Next: run `/research` (focused on Path B stack assumptions), then `/build`.*

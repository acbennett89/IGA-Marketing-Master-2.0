# `docs/workflow/` — Project workflow & design artifacts

Everything in this folder is the **paper trail** for how IGA Marketing Master 2.0 was designed, built, and verified. None of these files are loaded at runtime; they're reference documentation for future maintainers and for re-triggering workflow skills (`/optimize`, future `/check` re-runs, etc.).

For day-to-day operation, see the project root [`README.md`](../../README.md) and [`TROUBLESHOOTING.md`](../../TROUBLESHOOTING.md).

---

## What's here, in workflow order

### 1. The plan (the `/plan` → `/research` → `/plan-review` chain)

| File | What it is |
|---|---|
| [`PLAN.md`](PLAN.md) | The original draft plan from `/plan`. **Preserved unmodified** as historical reference. |
| [`PROCESS-MAP.md`](PROCESS-MAP.md) | Mermaid flowchart of the end-to-end runtime flow. |
| [`RESEARCH.md`](RESEARCH.md) | `/research` findings — Applied EPIC API rejected; `keyring`, `platformdirs`, Playwright pinning, cache-hit logging adopted. |
| [`PLAN-REVIEW.md`](PLAN-REVIEW.md) | **The authoritative project plan.** Path B chosen, 17 amendments folded in. /build was gated on this file's `✅ APPROVED` status. |

### 2. The architecture (`/build` Phase 4B)

| File | What it is |
|---|---|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | The technical contract — every module's variables, types, function signatures, dependency edges, and data shapes. The single source of truth that all developer agents implemented against. |

### 3. The build communication ledger

| File | What it is |
|---|---|
| [`COMMS.md`](COMMS.md) | Full build communication log — every status change, every issue logged, every routing decision. The orchestrator's audit trail. |

### 4. Per-agent decision maps and status reports (`/build` Phase 5)

Each developer agent produced a Mermaid decision map *before* writing code, then a status report *after*:

| Agent | Decision map | Status |
|---|---|---|
| config-and-cli | [`DECISION-MAP-config-and-cli-agent.md`](DECISION-MAP-config-and-cli-agent.md) | [`STATUS_config-and-cli-agent.md`](STATUS_config-and-cli-agent.md) |
| field-map | [`DECISION-MAP-field-map-agent.md`](DECISION-MAP-field-map-agent.md) | [`STATUS_field-map-agent.md`](STATUS_field-map-agent.md) |
| state | [`DECISION-MAP-state-agent.md`](DECISION-MAP-state-agent.md) | [`STATUS_state-agent.md`](STATUS_state-agent.md) |
| claude-client | [`DECISION-MAP-claude-client-agent.md`](DECISION-MAP-claude-client-agent.md) | [`STATUS_claude-client-agent.md`](STATUS_claude-client-agent.md) |
| extraction | [`DECISION-MAP-extraction-agent.md`](DECISION-MAP-extraction-agent.md) | [`STATUS_extraction-agent.md`](STATUS_extraction-agent.md) |
| gui | [`DECISION-MAP-gui-agent.md`](DECISION-MAP-gui-agent.md) | [`STATUS_gui-agent.md`](STATUS_gui-agent.md) |
| epic-driver | [`DECISION-MAP-epic-driver-agent.md`](DECISION-MAP-epic-driver-agent.md) | [`STATUS_epic-driver-agent.md`](STATUS_epic-driver-agent.md) |

Persistent agents (no decision maps):

| File | What it is |
|---|---|
| [`STATUS_SETUP.md`](STATUS_SETUP.md) | Setup agent — file scaffold + git init + feature branch. |
| [`STATUS_ARCHITECTURE_INITIAL.md`](STATUS_ARCHITECTURE_INITIAL.md) | Architecture agent — first pass. |
| [`STATUS_ARCHITECTURE_FINAL.md`](STATUS_ARCHITECTURE_FINAL.md) | Architecture agent — post-build sync to delivered code. |
| [`STATUS_DOCUMENTATION.md`](STATUS_DOCUMENTATION.md) | Documentation Phase 2 — inline-comment pass + README/TROUBLESHOOTING. |
| [`STATUS_BUILD.md`](STATUS_BUILD.md) | Final build hand-off — 224 tests passing, 3 issues deferred. |

### 5. Verification (`/check`)

| File | What it is |
|---|---|
| [`CHECK-STATUS.md`](CHECK-STATUS.md) | Live verification inventory; per-agent static + plan-match grid. |
| [`CHECK-REVIEW-FINDINGS.md`](CHECK-REVIEW-FINDINGS.md) | Detailed static-analysis review — every agent's deliverables checked against ARCHITECTURE.md, DECISION-MAPs, and PLAN-REVIEW.md amendments. |
| [`CHECK-REPORT.md`](CHECK-REPORT.md) | The final `/check` verdict: ✅ VERIFIED. Tagged `v1.0-verified`. |

---

## How to use these

- **Future maintainer onboarding:** read `PLAN-REVIEW.md` → `ARCHITECTURE.md` → the `DECISION-MAP-*.md` for the module you're touching.
- **Debugging a design choice:** check `COMMS.md` Issues Log + the relevant agent's `STATUS_*.md`.
- **Re-running a workflow skill (`/optimize`, future `/check` re-run):** the skill expects these artifacts at this canonical location and will pick them up automatically.
- **Don't modify these files casually.** They are the historical record of how the build happened. If a re-build or refactor renders one obsolete, prefer to *append* a dated update rather than rewrite — the lineage matters.

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
| Setup | Persistent | ⏳ Spawning | New project mode: scaffold + git init + feature branch |
| Architecture | Persistent | ⏳ Pending | Awaits STATUS_SETUP.md |
| Documentation | Persistent | ⏳ Pending | Phase 1 active during build; Phase 2 final pass after all dev agents complete |
| Tool | Persistent | ⏳ On-demand | Activated when developer agents request tools |
| config-and-cli-agent | Developer | ⏳ Pending | Owns: `cli.py`, `config.py`, `logger.py`, `secret_store.py` |
| field-map-agent | Developer | ⏳ Pending | Owns: `field_map.py` (JIT enrichment of `Library/Epic Field Map.json`) |
| state-agent | Developer | ⏳ Pending | Owns: `state.py` (canonical state schema, atomic writes, daily snapshots) |
| claude-client-agent | Developer | ⏳ Pending | Owns: `claude_client.py` (caching, tool use, PDFs, Sonnet→Opus auto-escalation) |
| extraction-agent | Developer | ⏳ Pending | Owns: `extract.py` (per-doc orchestration, merge, JIT domain_tag proposals) |
| gui-agent | Developer | ⏳ Pending | Owns: `gui.py` (PySide6 review/edit, QtPdf preview, OperatorModal class) |
| epic-driver-agent | Developer | ⏳ Pending | Owns: `epic_session.py`, `enter.py` (Playwright persistent context, selector fallback, pause-for-human, pre-flight smoke) |

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
| — | — | *None yet* | — | — |

---

## Clarifying Questions
| # | From Agent | Question | Escalated? | Answer |
|---|---|---|---|---|
| — | — | *None yet* | — | — |

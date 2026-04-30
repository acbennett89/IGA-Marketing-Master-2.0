# IGA Marketing Master 2.0 — Ground-Up Rewrite

**Status:** 🟡 DRAFT — Pending /plan-review approval
**Mode:** New build (informed by lessons from `IGA-Marketing-Master` v1)
**Gate:** /build is locked until /plan-review sets Status to ✅ APPROVED
**Artifacts:** PLAN.md · PROCESS-MAP.md
**Source plan file:** `C:\Users\Andrew\.claude\plans\q9-yes-build-binary-reddy.md`

---

## Context — Why we're rewriting

The original IGA Marketing Master ([../IGA-Marketing-Master](../IGA-Marketing-Master)) was built functionality-first: it locked Excel as the source of truth, hardcoded coverage-per-sheet contracts (`General Liability`, `Business Auto`, etc.) into the workflow, and stalled when the team realized the foundation — "what does EPIC actually accept as input?" — was never modeled. The backlog lists "EPIC selectors pending" against every coverage; that wasn't going to resolve without a structural reset.

This rewrite inverts the foundation: **Applied EPIC's field universe is modeled first** (in [Library/Epic Field Map.json](Library/Epic%20Field%20Map.json), already ~75% scraped, recently renamed from `screen_catalog.json`). Documents flow into a Claude-driven extractor that produces a single canonical JSON state for one client per run; a PySide6 GUI is the human review/edit interface; a Playwright entry script reads the approved state and drives EPIC from a page the user has already navigated to. Excel as an editable artifact is **dropped entirely** — a future read-only export is fine, but the live contract is JSON + GUI.

Outcome: a tool that scales by *adding metadata to the field map* rather than by writing new code per coverage.

---

## The 4 Fundamentals

| | |
|---|---|
| **Goal** | Extract insurance data from arbitrary PDF documents using the Claude API and enter it into Applied EPIC via Playwright, with a stateful per-client review/edit GUI in between. |
| **Input** | 1-N user-selected PDF files for a single client per run. Drag-drop or browse via PySide6 GUI. PDFs only at v1 (other formats convertible upstream). |
| **Output** | (1) `Working Library/<Client>/state.json` — the canonical extracted state, edited by the user via GUI. (2) Field-by-field data entry into Applied EPIC starting from a screen the user has navigated to manually. |
| **Stack** | Python 3.13 · PySide6 (GUI + `QtPdf`) · Anthropic SDK (Claude Sonnet 4.6 default, prompt caching, tool use, PDF input) · Playwright (persistent context, headed) · Windows-only |

---

## Process Map

> See [PROCESS-MAP.md](PROCESS-MAP.md) for the full visual diagram. High-level summary:
>
> User drags PDFs into the GUI for a single client. `extract.py` sends each PDF to Claude (with cached system prompt + Field Map); Claude returns tool-use calls keyed by `domain_tag`. Low-confidence fields auto-escalate to Opus 4.7. Results merge into the per-client `state.json`. The GUI presents per-section editable tables with a PDF preview pane and audit log. User approves or edits fields, then navigates EPIC inside the Playwright browser to the entry page and clicks "Begin Entry." `enter.py` walks approved fields, resolves selectors via Field Map (with label fallback), enters values, and pauses for human intervention on any error.

---

## Problem Statement

Insurance dec pages, schedules, and supplemental documents arrive in arbitrary shapes across many carriers. Today, transcribing them into Applied EPIC is manual — slow, error-prone, and the current v1 tool's Excel-first design can't generalize. We need a system where the operator drops in raw PDFs, an AI does the heavy lift of mapping content to EPIC's field universe, a human reviews and corrects in a purpose-built UI, and a robot does the keystrokes.

---

## Proposed Solution — Architecture

**The five subsystems:**

1. **Epic Field Map** ([Library/Epic Field Map.json](Library/Epic%20Field%20Map.json)) — the authoritative model of every EPIC screen, field, selector, and field metadata. Living document; enriched just-in-time as runs encounter gaps.
2. **Extractor** (`extract.py`) — sends PDFs to Claude with the Field Map as cached context; receives structured tool-use calls keyed by `domain_tag`; merges into state.
3. **Canonical state** (`state.py` + per-client `state.json`) — single source of truth for one client's extracted data, conflicts, audit log, and approval status. Atomic writes.
4. **Review GUI** (`gui.py`, PySide6) — editable section tables, PDF preview pane with deep-link to cited pages, conflict resolution, audit log viewer, run controls.
5. **Entry Driver** (`enter.py`) — Playwright persistent-context browser; reads approved state; walks fields; pause-for-human on errors with DOM-as-truth resume; label-fallback locator strategy.

---

## The Epic Field Map — schema additions

The existing JSON has `screens → fields[]` where each field has `type, label, name, required (legacy), format, hint`, and supports nested `tabs/sub_tabs`. We add the following metadata to every field — populated **just-in-time** as runs encounter the field:

| Key | Type | Purpose |
|---|---|---|
| `is_required` | bool | EPIC won't save without it |
| `is_repeatable` | bool | Field belongs to a repeatable group (table) |
| `repeatable_group` | string | If repeatable, the group identifier (e.g., `vehicles`) |
| `parent_field` | string | For nested repeatables, the containing group |
| `enum_values` | string[] | For `select` fields — valid options |
| `validation_pattern` | string (regex) | E.g., VIN = 17 chars; ZIP = 5/9 digits |
| `depends_on` | object | E.g., `{field: "has_ebl", value: "Yes"}` |
| `default_value` | any | What EPIC pre-fills |
| `domain_tag` | string | **Stable business-concept ID** (e.g., `submission.name`); namespaced; the key extraction emits and entry consumes |
| `aliases` | string[] | Alt domain_tags this field can fulfill |
| `last_verified_at` | ISO date | When we last confirmed selector + behavior in EPIC |
| `epic_build_version` | string | If scrapeable from EPIC chrome — pairs with `last_verified_at` |
| `notes_for_claude` | string | Free-text hints (e.g., "EPIC truncates at 50 chars") |
| `version` | int | Compare-and-swap counter for multi-user concurrency |

**Bootstrap reality:** `domain_tag` is treated as a **cache of Claude's mapping decisions**, not a precondition. On a brand-new field, Claude proposes a tag from `label`/`name`/`screen` context; the GUI surfaces it for one-click confirm; it's written back. First runs are slower, not broken. No 1,631-field manual tagging marathon.

---

## State.json schema (per client)

```jsonc
{
  "client": "Bobby Luttrell & Sons LLC",
  "run_history": [
    { "run_id": "2026-04-30T14:22:01", "user": "abennett@iga", "docs": ["BL - GL Declarations.pdf"], "model": "claude-sonnet-4-6", "errors": [] }
  ],
  "fields": {
    "submission.name": {
      "value": "Bobby Luttrell & Sons, LLC",
      "confidence": 0.98,
      "status": "approved",                  // pending | approved | locked | entered
      "source": [{ "doc_id": "BL - GL Declarations.pdf", "page": 1, "quote": "..." }],
      "conflicts": [],
      "history": [
        { "run_id": "...", "ts": "...", "user": "abennett", "actor": "claude", "prior": null, "new": "Bobby Luttrell & Sons, LLC", "action": "extracted" },
        { "run_id": "...", "ts": "...", "user": "abennett", "actor": "user",   "prior": "...", "new": "...", "action": "edited" }
      ]
    }
  },
  "repeatables": {
    "vehicles": [
      { "vehicle.year": { "value": "2024", ... }, "vehicle.vin": { "value": "1GT49PEY0RF235619", ... }, ... }
    ]
  }
}
```

**Schema rules:**
- Keyed by `domain_tag`, never by EPIC field `name` (selectors drift; tags don't).
- Repeatables are arrays of records, **not flat-with-index** (`vehicle_1.year`). Preserves reorder + conflict semantics.
- Two audit logs: per-field `history[]` for value changes; top-level `run_history[]` for run-level events. Every entry has a `user`.
- Atomic writes: write to `state.json.tmp` then `os.replace()`; rolling `state.json.bak` kept.

---

## Claude API — extraction details

| Decision | Choice | Why |
|---|---|---|
| Model | Sonnet 4.6 default; Opus 4.7 escapable per-field | Sonnet is cost-effective; Opus reserved for low-confidence fields |
| Output mechanism | **Tool use** with single tool `record_extracted_field` | More reliable than JSON-mode; gives free citations |
| Tool params | `domain_tag`, `value`, `source_doc`, `source_page`, `source_quote`, `confidence`, optional `needs_review`, optional `repeatable_group`, optional `repeatable_index` | Forces Claude to cite, dramatically reduces hallucination |
| `domain_tag` constraint | Tool schema includes `enum: [...]` regenerated from current Field Map per call | Prevents Claude from inventing tags that don't exist |
| Cache breakpoints | **2 markers** | (1) after system + glossary + few-shot (stable for weeks); (2) after Field Map (stable per session) |
| PDF delivery | `document` content block; pre-flight pagecount + size check | Anthropic limits ~100 pages / 32 MB; split with overlap if exceeded |
| Cache TTL | Default 5-min; consider 1-hour extended for high-volume days | Math: write +25%, read -90%; break-even = 1 reuse |

When the user drops 5 PDFs for one client, the structure is one cache write on doc 1, four cache reads on docs 2-5. Net ~70-80% input-cost savings versus uncached.

### Sonnet 4.6 vs Opus 4.7 — practical cost comparison

Per Anthropic's published rates, with prompt caching applied:

| Cost basis | Sonnet 4.6 | Opus 4.7 |
|---|---|---|
| Input (uncached) | $3 / M tokens | $15 / M tokens |
| Cache write (1.25×) | $3.75 / M | $18.75 / M |
| Cache read (0.10×) | $0.30 / M | $1.50 / M |
| Output | $15 / M | $75 / M |

**Per-document estimate** (assuming ~60K tokens cached context = system + glossary + Field Map; ~30K input tokens for the PDF being extracted; ~5K output tokens of tool calls; cache hit on doc 2+):

| Scenario | Sonnet 4.6 | Opus 4.7 | Δ |
|---|---|---|---|
| 1st doc of session (cache write) | ~$0.34 | ~$1.71 | 5× |
| 2nd-Nth doc (cache hit) | ~$0.18 | ~$0.92 | 5× |
| **Typical 5-doc client submission** | **~$0.90** | **~$4.60** | **5×** |
| 50 submissions / week | ~$45 | ~$230 | 5× |
| 200 submissions / month | ~$180 | ~$920 | 5× |

These are illustrative estimates — actual costs depend on PDF size and output verbosity. **Opus is roughly 5× more expensive than Sonnet across input, cache, and output.**

**Auto-escalation policy:**
1. **Default model: Sonnet 4.6** for the first pass on every doc.
2. **Auto-escalate to Opus 4.7 per-field**, not per-doc, when *any* of:
   - Sonnet returned a tool call with `confidence < 0.7`
   - Sonnet's tool call included a `needs_review` flag (optional bool in tool schema)
   - The field has `is_required: true` in Field Map AND no value was extracted
3. Opus is given just the un-resolved fields + relevant document pages, not the whole doc again. Net cost adder: typically <10% of Sonnet baseline.
4. **GUI badges** Opus-sourced fields so the user can review more carefully.
5. **GUI override:** per-run "Force Opus on everything" toggle for hard documents. Off by default.

---

## Module breakdown

```
C:/Users/Andrew/Documents/GitHub/IGA-Marketing-Master-2.0/
├── src/iga_marketing_master_2/
│   ├── cli.py             # entry points; --debug flag plumbed everywhere
│   ├── gui.py             # PySide6 review/edit interface; orchestration
│   ├── extract.py         # PDFs → state.json via Claude API
│   ├── enter.py           # state.json → EPIC via Playwright
│   ├── field_map.py       # Load/update Epic Field Map.json; JIT enrichment; domain_tag enum gen
│   ├── claude_client.py   # Anthropic SDK wrapper: caching, tool use, PDF preflight, retries
│   ├── epic_session.py    # Playwright launch_persistent_context; locator helpers; label fallback
│   ├── state.py           # Canonical state schema + atomic I/O + merge/conflict logic
│   ├── logger.py          # Debug + production logging; trace artifact handling
│   └── config.py          # Paths, API keys (env), defaults, model selection
├── Library/
│   └── Epic Field Map.json   # The authoritative EPIC field universe
├── Testing and Example Library/   # already exists
└── tests/

# Working Library lives OUTSIDE the repo, on a SharePoint-synced folder
# (see "Multi-user via SharePoint" below). Path is configurable.
# Default discovery order:
#   1. config.py override
#   2. %ONEDRIVE%/<Tenant>/.../IGA Marketing Master/Working Library/
#   3. First-run picker dialog
```

**`--debug` semantics** (uniform across modules):
- Save raw Anthropic API request/response JSON to `debug/`.
- Save Playwright trace.zip (`tracing.start(screenshots=True, snapshots=True, sources=True)`) per entry run.
- Save screenshots before + after every entry action.
- Verbose console + rotating file log.
- Pause between major phases for inspection (extract complete → review → entry).

---

## Multi-user via SharePoint

Multiple users will run this concurrently. The shared assets are:
- **`Library/Epic Field Map.json`** — read-mostly, write rare (JIT enrichment + selector drift updates).
- **`Working Library/<Client>/`** — per-client extraction state. One client is typically owned by one operator at a time, but two users *could* open the same client.

**Strategy: SharePoint-sync via OneDrive client, not direct Graph API.** OneDrive client already handles auth, conflict resolution, and sync transparently. The app reads/writes local files at a synced path.

**Setup (one-time per user):**
1. User has OneDrive for Business signed in to the IGA tenant.
2. User syncs the SharePoint document library that hosts `Library/` and `Working Library/` to local disk.
3. On first run, the app prompts for the synced folder path and stores it in `%APPDATA%/IGA Marketing Master/config.json`.

**Concurrency design:**

| Concern | Approach |
|---|---|
| Two users editing the same client's `state.json` simultaneously | Per-client lock file (`state.json.lock`) with `{user, host, opened_at, pid}`; stale-lock breaker after 30 min idle; hard-block on fresh peer lock with friendly message |
| Two users JIT-enriching Field Map at the same time | Optimistic concurrency: `version` int + compare-and-swap with refresh+retry (≤3) |
| OneDrive sync delay / partial uploads | Atomic writes only (`os.replace`); on `PermissionError`, retry with backoff |
| Audit needs to track which user did what | `user` field on every history and run_history entry; source: `os.getlogin()` + machine name |
| OneDrive conflict copies (`state-Andrew.json`) | Detect `state-*.json` siblings on open; surface manual-pick UI; never auto-merge |
| `Working Library/debug/` artifact bloat | Auto-prune to last 5 runs per client |

**Field Map writeback path:** all writes through `field_map.update_field(name, patch)`; reads current → applies patch → increments `version` → writes atomically with retry on conflict.

---

## PySide6 GUI design

| Concern | Approach |
|---|---|
| Section tables (one tab per EPIC section) | `QTableView` + custom `QAbstractTableModel`. **Not** `QTableWidget`. |
| Editor widgets | `QStyledItemDelegate` subclasses: enum dropdowns from `enum_values`, date pickers, currency masks |
| Confidence highlighting | `Qt::BackgroundRole` on cells; red <0.6, yellow 0.6-0.85, no color ≥0.85 |
| Conflicts (multi-doc disagreement) | Inline expandable row; per-source value + click-to-pick |
| Repeatable groups (e.g., 40 vehicles) | List-on-left (`QListWidget`) + form-on-right pattern. **Do not** try to render nested repeatables in a single grid. |
| PDF preview | `QtPdf` + `QPdfView` (PySide6 6.6+); `pageNavigator().jump(page)` to deep-link Claude's cited page on cell focus. **Not** `QWebEngineView`. |
| Audit log | Read-only `QPlainTextEdit` bound to a JSON event stream; filterable by run_id |
| Run controls | "Begin Entry" button gated on at least one approved field; "Pause" / "Resume" surfaced as a modal during entry |

---

## Playwright entry — the hard parts

1. **Persistent context, not CDP attach.** `playwright.chromium.launch_persistent_context(user_data_dir=..., headless=False)`. User logs in *inside that browser*, navigates to the entry page, then clicks "Begin Entry" in the GUI.
2. **Selector resolution order:** (a) Field Map `data-automation-id`, (b) Field Map `name` attribute, (c) **label fallback** `page.get_by_label(field.label)` scoped to the active screen container. Successful fallbacks under `--debug` propose a Field Map update.
3. **Pause-for-human:** on any failure (timeout, validation, missing selector), `enter.py` writes `{ failed_action, selector_tried, intended_value, last_known_state }` into a `pending_pause` block in `state.json`, surfaces a PySide6 modal, and waits. On resume, **re-read the field via `locator.input_value()`** and accept the post-resume DOM as ground truth — do not diff against the intended value.
4. **Always trace under `--debug`.** EPIC will fail in ways you cannot reproduce without `trace.zip`.

---

## Roadmap

### Phase 1 — Foundation (no EPIC writes, no Claude calls)
- [ ] Project scaffold: package layout, `cli.py`, `config.py`, `logger.py`, env-driven secrets.
- [ ] `config.py` SharePoint path discovery: `%ONEDRIVE%` lookup + first-run picker; cache resolved path in `%APPDATA%/IGA Marketing Master/config.json`.
- [ ] `field_map.py`: load Epic Field Map; expose lookup by `domain_tag`/`name`/screen; helper to add metadata fields with safe defaults; `update_field()` with `version` compare-and-swap and retry.
- [ ] `state.py`: state.json schema, atomic write, backup, merge logic, conflict surfacing, repeatable-group helpers, per-client lock file with stale-lock breaker, conflict-copy detection (`state-*.json` siblings).
- [ ] `claude_client.py` skeleton: SDK init, cache-block builder, PDF preflight, tool-use schema (with regenerated `domain_tag` enum), per-field auto-escalation Sonnet→Opus.
- [ ] Bootstrap script (Windows): venv, deps, Playwright browsers.

### Phase 2 — Extraction loop
- [ ] `extract.py`: take a list of PDFs + client name → call Claude → merge tool_use results into state.json.
- [ ] Conflict detection across docs (same `domain_tag`, different values).
- [ ] JIT `domain_tag` proposal flow: when Claude proposes a new tag, queue it for GUI confirmation before commit.
- [ ] Tests: golden PDF → expected state.json snapshot.

### Phase 3 — Review GUI
- [ ] PySide6 main window: client picker, file drop zone, section tabs.
- [ ] `QTableView` + model per section; editor delegates; confidence colors.
- [ ] Repeatable-group list+form pane.
- [ ] PDF preview via `QtPdf`; deep-link on cell focus.
- [ ] Audit log pane.
- [ ] `domain_tag` confirmation dialogs; Field Map writeback.

### Phase 4 — EPIC entry
- [ ] `epic_session.py`: persistent-context launch, page detection, screen identification helpers.
- [ ] `enter.py`: walk approved state, resolve selectors with fallback, fill fields, validate.
- [ ] Pause-for-human modal + DOM re-read on resume.
- [ ] Selector-drift detection writes Field Map updates under `--debug`.
- [ ] `--debug` Playwright tracing.

### Phase 5 — Hardening
- [ ] Selector smoke test: walk Field Map against a stable EPIC test screen, report rot.
- [ ] Run history viewer / cost dashboard (Claude usage per run).
- [ ] Drift alerts when EPIC build version changes.
- [ ] (Future, not v1) Read-only Excel export; headless mode; client creation; EPIC navigation; non-PDF inputs.

---

## Known Edge Cases & Risks

| Risk | Severity | Mitigation |
|---|---|---|
| EPIC selector rot under `data-automation-id` | **High** | Label-fallback locator; selector smoke test; `last_verified_at` + `epic_build_version` per field |
| Claude hallucinates `domain_tag` not in Field Map | High | Tool schema `enum` regenerated per call from live Field Map |
| Same Named Insured worded differently across docs | Medium | Conflict surfacing in GUI; user picks canonical |
| 40-vehicle schedules blow up GUI tables | Medium | List+form pattern, virtualized list, pagination |
| Cache invalidation when Field Map edited mid-session | Low | Edits cluster per review session; one cache miss, not many |
| MFA / SSO disrupts Playwright session | Medium | Persistent context preserves cookies; user re-auths inline |
| Mid-entry crash leaves EPIC half-populated | Medium | `state.json` records `entered` per field; on resume, only un-entered fields attempted |
| PDF over Claude's 100-page / 32 MB limit | Low | `extract.py` pre-flight check; split-with-overlap fallback |
| state.json corruption mid-write | Low | Atomic `os.replace()` + rolling `.bak` |
| Sensitive PII handling | Low (approved by user) | API approval explicit; debug artifacts stay in shared SharePoint trust boundary |
| Two users open same client concurrently | Medium | Per-client lock file with stale-lock breaker |
| Field Map JIT writes collide between users | Low | `version` field + compare-and-swap with refresh+retry |
| OneDrive sync conflict copies | Low | Detect `state-*.json` siblings; surface manual merge UI |
| `Working Library/debug/` bloats SharePoint | Medium | Auto-prune debug artifacts to last 5 runs per client |

---

## Out of Scope (v1)

- Adding/creating clients in EPIC (user does this manually first).
- EPIC navigation (user navigates to the entry page first).
- Headless mode.
- Non-PDF inputs (Word, images, email). Convert to PDF upstream.
- Read-only Excel export of state.json (future feature).
- Anything that touches Marketing Submission Review, OCR, Quote Review (those are separate features in v1's backlog and not part of this rewrite).

---

## Open Questions (to resolve in /plan-review)

1. `domain_tag` **namespacing convention** — proposed: `submission.name`, `policy.named_insured`, `vehicle.vin` style. Confirm before Phase 2 begins.
2. Selector **smoke-test cadence** — proposed: manual + on-demand from GUI; auto-trigger if `last_verified_at` > 30 days.
3. **Auto-escalation confidence threshold** — proposed `< 0.7`; finalize during Phase 2 once we see real-world distributions.
4. **SharePoint sync path discovery** — does IGA already have a defined SharePoint library + path convention to use?
5. **Per-user identity** — proposed source: `os.getlogin()` + machine name. Confirm vs. AAD/Entra UPN via `whoami /upn`.

---

## Critical files (existing) to read before /build

- [Library/Epic Field Map.json](Library/Epic%20Field%20Map.json) — the authoritative field universe (~16K lines, ~1,631 fields)
- [Testing and Example Library/Extracted.xlsx](Testing%20and%20Example%20Library/Extracted.xlsx) — historical reference for what "good extraction" looks like; informs the GUI section layout, but no longer the editable contract
- [../IGA-Marketing-Master/docs/architecture.md](../IGA-Marketing-Master/docs/architecture.md) — what got tried and where it stalled
- [../IGA-Marketing-Master/docs/features/automated-data-entry.md](../IGA-Marketing-Master/docs/features/automated-data-entry.md) — workbook contract that we're replacing

No code is being reused from v1 — the conceptual reset is too deep.

---

## Self-Prompt for /build (DRAFT — do not use until /plan-review approves)

```
Your goal is to build IGA Marketing Master 2.0 — a Windows desktop tool that extracts insurance data from PDF documents using the Claude API and enters it into Applied EPIC via Playwright, with a stateful per-client review/edit GUI in between.

Input: 1-N user-selected PDF files for one client per run (drag-drop or browse via GUI).

Output: (1) Per-client `Working Library/<Client>/state.json` — the canonical extracted state, edited by the user via GUI. (2) Field-by-field data entry into Applied EPIC starting from a screen the user has navigated to manually.

Stack: Python 3.13, PySide6 (GUI + QtPdf), Anthropic SDK (Claude Sonnet 4.6 default; prompt caching with 2 breakpoints; tool use; PDF document blocks), Playwright (`launch_persistent_context`, headed). Windows-only.

Architecture (build in this order):
1. Foundation: cli, config, logger, field_map, state (atomic JSON), claude_client skeleton, bootstrap script.
2. Extraction loop: extract.py merges Claude tool_use output into state.json; conflict detection; JIT domain_tag proposal queue.
3. Review GUI: PySide6 with QTableView + QAbstractTableModel per section; QStyledItemDelegate editors; confidence highlighting; QtPdf preview with page deep-link; repeatable list+form pattern; audit log; domain_tag confirmation dialogs.
4. EPIC entry: epic_session.py + enter.py with persistent-context Playwright; selector fallback chain (data-automation-id → name → label); pause-for-human via GUI modal with DOM-re-read on resume; --debug Playwright tracing.
5. Hardening: selector smoke test, run history viewer, drift alerts.

Constraints and conventions:
- Epic Field Map.json is the authoritative source; enrich JIT with new metadata. Never break the existing structure.
- domain_tag is a namespaced stable business-concept ID; treat as a *cache* of Claude's mapping decisions, not a precondition.
- state.json: keyed by domain_tag; repeatables as arrays of records; per-field history[] + top-level run_history[]; atomic writes via os.replace + rolling .bak; `user` on every history entry.
- Tool use, not JSON-mode prompting. Single `record_extracted_field` tool with domain_tag enum regenerated per call. Optional `needs_review` bool.
- Two prompt-cache breakpoints: stable system+glossary+examples, then Field Map.
- Default Sonnet 4.6; auto-escalate to Opus 4.7 per-field on confidence < 0.7 OR `needs_review` OR required field missing. GUI badges Opus-sourced fields. Per-run "Force Opus" override available, off by default.
- Playwright: persistent_context, never CDP attach. Selector resolution: data-automation-id → name → get_by_label fallback. On error: pause, modal, on resume re-read DOM as ground truth.
- --debug saves raw Claude requests/responses, Playwright trace.zip, screenshots before/after each action, verbose logs. Auto-prune debug artifacts to last 5 runs per client.
- Working Library lives on a SharePoint-synced OneDrive path. Per-client lock file (`state.json.lock`) prevents concurrent edits. Field Map updates use compare-and-swap on a `version` int.
- Out of scope v1: client creation, EPIC navigation, headless, non-PDF inputs, Excel export.

Build this step by step. Start with Phase 1 — foundation and field_map.
```

---

*Next step: run `/plan-review`. /build is locked until plan-review sets Status to ✅ APPROVED.*

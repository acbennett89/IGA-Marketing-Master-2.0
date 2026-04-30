# IGA Marketing Master 2.0

A single-user Windows desktop tool that reads insurance PDFs, extracts the marketing-submission data with the Claude API, lets a human review and correct the result, and then types the values into Applied EPIC for you. The goal is to take the typing out of marketing submissions while keeping a human in the loop on every value EPIC actually receives.

---

## Prerequisites

- **Operating system:** Windows 10 or Windows 11. (The tool is Windows-only by design — it drives the EPIC web UI through Playwright + Chromium and relies on Windows Credential Manager to store the Anthropic API key.)
- **Python 3.13.** Earlier 3.x versions are not supported.
- **Applied EPIC web access** in your normal Chrome profile. The tool drives a separate Chromium profile so your daily browsing is untouched.
- **An Anthropic API key.** First-run setup will prompt you for one; you can also paste it later from the GUI.
- **OneDrive is optional.** v1 is local-first — the Working Library lives on your local disk by default. If you want it on a OneDrive-synced folder, see TROUBLESHOOTING.md "Working Library default location."

---

## Installation

From a PowerShell prompt in the project root:

```
.\scripts\bootstrap.ps1
```

This script:

1. Creates a Python virtual environment under `.venv\`.
2. Installs all dependencies from `requirements.txt`.
3. Runs `playwright install chromium` so the Chromium browser the tool drives is ready.
4. Prompts for your Anthropic API key (hidden input) and stores it in Windows Credential Manager.

If the script reports any errors, see TROUBLESHOOTING.md "How to install / first-time setup".

---

## Daily use

### Launching

After installation, launch the tool from a PowerShell prompt:

```
iga-marketing-master-2
```

(or `python -m iga_marketing_master_2.cli`, if the entry point isn't on your PATH).

The first launch will:

- Ask you where to put your **Working Library** folder (defaults to `Documents\IGA Marketing Master\Working Library`).
- Ask for your Anthropic API key if `bootstrap.ps1` didn't already store one.

### Extracting a client's submission

1. **Pick or create a client** from the toolbar. Each client is a folder under your Working Library.
2. **Drop PDFs onto the window** (or click "Add PDFs..."). The tool copies them into `<client>/inputs/` so review-time deep-links keep working even if you later delete the originals.
3. **Wait for extraction.** Claude reads each PDF, asks itself the right questions, and fills in the canonical state. The audit log at the bottom of the window streams progress.
4. **Review and edit.** Each tab (Submission / Account / Vehicles / etc.) shows the values Claude pulled, color-coded by confidence:
   - **Red:** low confidence — please double-check.
   - **Yellow:** medium confidence.
   - **No tint:** high confidence, or you've already reviewed it.
5. **Click a value's source cell** to jump the right-hand PDF preview to the exact page Claude pulled it from.
6. **If a field has alternative values** (multiple PDFs disagreed), a small chevron appears — click it to see the candidates and pick one.

### Entering into EPIC

1. **Open EPIC in the tool's Chromium window** and navigate to the screen you want to start entering on (typically the Account or Submission screen for the client).
2. **Click "Begin Entry."** The tool walks every approved field, types it into EPIC, and reads the value back to make sure EPIC accepted it.
3. **If EPIC rejects a value** (validation error) or **the field has moved** (selector drift), the tool pauses and shows you a dialog. Fix it manually in EPIC, then click **Resume**. The tool re-reads what you typed and treats your value as the new canonical answer.
4. **Cancel** or **Skip this field** are always available from the pause dialog if you want to bail out.

---

## Project layout

```
IGA-Marketing-Master-2.0/
├── src/iga_marketing_master_2/      # Python source
│   ├── cli.py                       # command-line entry point
│   ├── config.py                    # paths + Settings discovery
│   ├── logger.py                    # rotating file + per-client run logs
│   ├── secret_store.py              # keyring wrapper for the API key
│   ├── field_map.py                 # the EPIC Field Map (~1,631 fields)
│   ├── state.py                     # per-client state.json read/write
│   ├── claude_client.py             # Anthropic SDK wrapper
│   ├── extract.py                   # PDF → state orchestrator
│   ├── enter.py                     # state → EPIC driver (Playwright)
│   ├── epic_session.py              # browser session + selector resolution
│   └── gui/                         # PySide6 review/edit interface
├── Library/
│   └── Epic Field Map.json          # authoritative field universe
├── Working Library/                 # (default location, override via picker)
│   └── <Client Name>/
│       ├── inputs/                  # source PDFs
│       ├── state.json               # canonical extracted data + audit
│       ├── state.json.bak           # rollback copy
│       ├── snapshots/               # daily snapshots, 30-day retention
│       ├── runs.log                 # per-client audit log
│       └── debug/                   # only when --debug is on
├── tests/                           # pytest suite (224 tests)
├── scripts/bootstrap.ps1            # one-time setup
├── pyproject.toml
└── requirements.txt
```

---

## The `--debug` flag

```
iga-marketing-master-2 --debug
```

Turns on the noisy diagnostics:

- Console output at DEBUG level.
- Per-action Playwright tracing written to `<client>/debug/playwright/<run_id>/trace.zip` (replayable in Playwright's trace viewer).
- Before/after screenshots for every field entry.
- Full Claude request/response JSON dumps under `<client>/debug/claude/<run_id>/` (PDF base64 elided to a sha256 to keep files small).
- Last 5 run_id directories are retained per client; older ones are pruned automatically.

Without `--debug`, the only on-disk record of a session is the `runs.log` file in the client folder and the rotating global app log under `%LOCALAPPDATA%\IGA Marketing Master\logs\iga.log` (10 MB × 5 backups, ~50 MB total).

---

## Where to read next

- **PLAN-REVIEW.md** — the authoritative project plan (Path B, single-user, all 17 amendments folded in).
- **ARCHITECTURE.md** — the technical reference: every module's contract, data shape, and decision rationale.
- **TROUBLESHOOTING.md** — the operator-facing recovery guide. Read this first when something goes wrong.
- **DECISION-MAP-\<agent\>.md** — per-developer-agent decision logs from the build, useful for understanding why a piece of code is the way it is.

---

## Running the tests

From the project root with the virtualenv activated:

```
pytest
```

Expected: **224 tests passing**, all hermetic (no network, no filesystem outside tmp dirs, no PySide6 import unless explicitly testing the GUI). The full suite runs in well under a minute.

To run a single module's tests:

```
pytest tests/test_state.py -v
```

---

## Development notes

This project was built end-to-end by an orchestrated set of agents, summarized briefly here for future maintainers:

1. **Setup agent** — scaffolded the repo, branch, requirements, and bootstrap script.
2. **Architecture agent** — wrote ARCHITECTURE.md (the contract every developer agent then implemented against). Resolved the `domain_tag` grammar question.
3. **7 developer agents in parallel** — `field-map-agent`, `config-and-cli-agent`, `state-agent`, `claude-client-agent`, `extraction-agent`, `gui-agent`, `epic-driver-agent`. Each produced its module(s), tests, and a `DECISION-MAP-*.md` log.
4. **Documentation agent (Phase 2)** — added inline comments to every source file and wrote this README plus TROUBLESHOOTING.md.

All design choices are anchored to PLAN-REVIEW.md and ARCHITECTURE.md. Path B (single-user, local Working Library) is the explicit scope for v1; multi-user / SharePoint sync is deferred to v1.5 and not addressed by this code.

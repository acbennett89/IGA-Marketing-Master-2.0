# Troubleshooting — IGA Marketing Master 2.0

This guide is the operator-facing recovery reference for IGA Marketing Master 2.0. It is a starter file; the developer / documentation agents fill in each section during the build per the Breakage Analysis in [PLAN-REVIEW.md](PLAN-REVIEW.md).

## How to install / first-time setup

*To be written.* Will cover running `scripts/bootstrap.ps1`, the first-run Anthropic API key prompt, and the first-run Working Library folder picker.

## EPIC isn't accepting my values (selector drift)

*To be written.* Will cover the pre-flight selector smoke warning banner, the pause-for-human modal flow, and when to escalate selector drift to the admin.

## state.json corruption / recovery

*To be written.* Will cover the rolling `.bak` file, the daily `snapshots/state-YYYY-MM-DD.json` (30-day retention), and the manual restore procedure.

## Working Library on a synced folder (OneDrive / SharePoint)

*To be written — note only.* v1 is local-first (Path B). If the operator chooses to put their Working Library on a OneDrive / SharePoint-synced path, that's their choice — the app itself does not manage sync. Multi-user / SharePoint sync is deferred to v1.5.

## Where do logs and debug artifacts live

*To be written.* Will cover per-client `runs.log`, the global app log under `platformdirs.user_log_dir`, and the `debug/` folder produced under each client when running with `--debug`.

## When to ask Andrew (or the designated admin) for help

*To be written.* Will list the escalation path: persistent EPIC selector drift, Field Map structural issues, anything outside the GUI's pause-for-human path.

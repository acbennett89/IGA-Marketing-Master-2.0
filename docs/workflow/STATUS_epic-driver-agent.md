# STATUS — epic-driver-agent

**Status:** Complete
**Date:** 2026-04-30
**Owner:** epic-driver-agent (Playwright + browser-automation specialist)
**Scope:** EPIC entry layer — `epic_session.py` + `enter.py`

---

## Deliverables

| File | Path | State |
|---|---|---|
| Decision map | `DECISION-MAP-epic-driver-agent.md` | Written |
| Module: epic session lifecycle | `src/iga_marketing_master_2/epic_session.py` | Implemented |
| Module: entry driver | `src/iga_marketing_master_2/enter.py` | Implemented |
| Tests: epic_session | `tests/test_epic_session.py` | 20 tests, all passing |
| Tests: enter | `tests/test_enter.py` | 9 tests, all passing |
| This status | `STATUS_epic-driver-agent.md` | Written |

Test totals: **29/29 passing in scope**, **190/190 passing in repo** (no other agent's tests broken).

---

## Contract honored

### `epic_session.py`

- `cleanup_user_data_dir_lock(user_data_dir: Path) -> None` — inspects `SingletonLock` / `SingletonCookie` / `SingletonSocket` / `LOCK` / `lockfile` / `parent.lock`. Parses Chromium `<pid>-<host>` content; deletes when PID is dead/unparseable; raises `PlaywrightProfileInUseError` when a live PID owns the dir. PID liveness via `psutil` if installed, else `os.kill(pid, 0)` on POSIX or "treat as stale" on Windows without psutil. Idempotent.
- `launch_with_persistent_context(user_data_dir: Path, *, headed=True, debug=False) -> BrowserContext` — runs cleanup, requires absolute path (Playwright #34700), lazily imports `playwright.sync_api`, starts tracing in debug mode, stashes the `sync_playwright()` handle on the context for later teardown.
- `resolve_locator(page, field_entry, *, screen_container=None) -> tuple[Locator, str]` — chain `data-automation-id` → `name` → `get_by_label(label, exact=True)`; returns `(Locator, "automation_id" | "name" | "label_fallback")`. Raises `SelectorUnresolvedError(attempts=...)` when all three miss. Logs label-fallback hits at WARNING with full context (label, field name, automation_id, domain_tag, screen_code) so a future drift recorder can reconstruct what to patch.
- `preflight_selector_smoke(page, tags_to_enter, field_map, *, screen_container=None) -> SmokeReport` — touched-screens-only via `field_map.screens_touched_by_domain_tags`, then `fields_for_screen` per screen. Records strategy + elapsed_ms per field. `label_fallback` and unresolved are both flagged `stale=True`. Non-blocking — never raises. Logs a warning if elapsed > 30s.
- `SmokeReport` / `SmokeReportEntry` dataclasses; `STRATEGY_AUTOMATION_ID` / `STRATEGY_NAME` / `STRATEGY_LABEL_FALLBACK` constants exported.

### `enter.py`

- `run_entry_session(state, field_map, browser_context, *, on_pause_callback, on_progress_callback=None, settings=None, client_path=None, save_state_callback=None, screen_container=None) -> EntryResult` — full ARCHITECTURE §7.2 contract:
  - Walks `state.fields[]` (sorted by domain_tag) then `state.repeatables[group]` (sorted by group, items in array order, fields per item sorted by tag). Only `status="approved"` is processed; everything else skipped silently.
  - Pre-flight smoke runs first; stale entries surface as a warning string in `EntryResult.warnings`, never block.
  - For each field: lookup FieldEntry → `resolve_locator` → type-aware `_fill_field` (text / textarea / date / select / checkbox / currency) → 250 ms debounce → DOM read-back validation + inline-error probe.
  - On selector-unresolved / fill exception / validation-rejected: build a `PauseInfo` with operator-readable `reason_message` and engineering `technical_detail`; write `state.pending_pause` durably, save, call `on_pause_callback`.
  - Callback returns `"resume" | "skip" | "abort"`. On `"resume"`, DOM is re-read via `locator.input_value()` (or `is_checked()` for checkboxes); the read value is committed as canonical with `actor="user"`, `action="resumed_after_pause"`, confidence 1.0.
  - On run end: `pending_pause` cleared, `run_history` entry appended (`kind="entry"`).
- Repeatable-group iteration verified deterministic (vehicle item 0 then item 1, fields in tag-sorted order).
- Pause-for-human persistence verified: `pending_pause` is set on state and saved BEFORE the callback opens its modal, then cleared after resolution.
- `--debug`: trace.zip stopped + saved into `<client>/debug/playwright/<run_id>/trace.zip`; per-action screenshots `{n:04d}-{tag}-{phase}.png`; auto-prune to last 5 run_id directories by mtime.

---

## Decoupling discipline

- **No GUI import.** `enter.py` reaches the GUI exclusively through `on_pause_callback`. Verified by file-level imports: only `epic_session`, `config`, `logger`, lazy `field_map` and lazy `state`.
- **No `field_map` write coupling in `epic_session`.** `resolve_locator` logs label-fallback drift but does not call `field_map.update_field`. The orchestrator may wire that in a future pass; today the log entries carry enough context (`label`, `field_name`, `automation_id`, `domain_tag`, `screen_code`) to reconstruct what to patch.
- **State module decoupling.** `state.py` is still a stub at the time of this writing; `enter.py` accesses state through duck-typed reads + a lazy `from . import state` so when state.py lands the canonical `set_pending_pause` / `append_run_history` setters take over automatically. The `save_state_callback` injection point lets the GUI plug in `state.save_atomic` without `enter` importing it directly.

---

## Open items routed to this agent

ARCHITECTURE §14 listed no items routed *to* this agent, but the brief asked us to:

1. **Pin Playwright >= 1.55** — pinned in `requirements.txt`/`pyproject.toml` (already done by setup agent); reaffirmed in module-level requirements comment of `epic_session.py`.
2. **Use absolute paths only for `user_data_dir`** — both `cleanup_user_data_dir_lock` and `launch_with_persistent_context` raise `ValueError` on relative paths.
3. **Implement `cleanup_user_data_dir_lock()`** — done; cleans the documented set; raises on live PID.
4. **Document the exact list of lock files cleaned** — see `LOCK_FILES` in `epic_session.py` and section B of `DECISION-MAP-epic-driver-agent.md`. Final list: `SingletonLock`, `SingletonCookie`, `SingletonSocket`, `LOCK`, `lockfile`, `parent.lock`.

---

## Cross-agent dependencies (informational)

- **field-map-agent (`field_map.py`)** — relies on `lookup_by_domain_tag`, `screens_touched_by_domain_tags`, `fields_for_screen` and the `FieldEntry` API (`name`, `label`, `domain_tag`, `screen_code`, `type`, `hint`, `enum_values`, `raw["automation_id"]`). All present in the current implementation.
- **state-agent (`state.py`)** — currently a stub. `enter.py` is forward-compatible: when `state.set_pending_pause(state, pause)` and `state.append_run_history(state, entry)` land they will be picked up automatically via the lazy import inside `_set_pending_pause` / `_append_run_history`.
- **gui-agent (`gui.py`)** — supplies the `on_pause_callback`. The signature is `Callable[[PauseInfo], Literal["resume", "skip", "abort"]]`; the GUI will open `EpicValidationPauseDialog` / `SelectorUnresolvedPauseDialog` (`OperatorModal` subclasses per amendment #5).

---

## Risks + caveats

- **psutil is not in `requirements.txt`.** The brief did not ask us to add it. `_is_pid_alive` falls back to `os.kill(pid, 0)` on POSIX; on Windows without psutil it conservatively reports "stale" (the cleanup proceeds; if a real owner exists, Playwright's own "user data dir already in use" surfaces and we wrap it as `PlaywrightProfileInUseError`). Recommend the orchestrator add `psutil` to `requirements.txt` when next touching dependencies — the cost is tiny and the Windows-without-psutil branch becomes safer.
- **Screen container heuristic.** `enter.run_entry_session` accepts an optional `screen_container: Locator`. When not supplied it scopes against the bare page — fine for v1 because the operator navigates to a single screen before clicking Begin Entry. v1.5 multi-screen walks will likely want the GUI to compute a screen container from the field map's `screen_code` and pass it down.
- **EPIC inline error probe.** `_find_inline_error` looks for `:scope .error, :scope [role='alert']` — a generic heuristic. Specific EPIC error containers can be tightened later via field-map metadata (e.g., a future `error_selector` key) without changing this module's contract.

---

## How to run the tests

```pwsh
cd C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0
$env:PYTHONPATH = 'src'
python -m pytest tests/test_epic_session.py tests/test_enter.py -v
```

Expected: 29 tests pass. No real Chromium is launched — all Playwright APIs are mocked.

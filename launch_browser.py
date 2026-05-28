"""Standalone EPIC browser launcher for testing.

Opens Playwright Chromium with the same profile/CDP setup the GUI uses,
without starting the GUI app. After launch, run test scripts (e.g.
test_property_full.py) which connect via CDP port 9222.

Run from repo root:
    .venv/Scripts/python launch_browser.py
"""
import logging
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, r"src")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

from iga_marketing_master_2.epic_session import launch_with_persistent_context
from iga_marketing_master_2.epic_steps import (
    step_enterprise_id,
    step_login,
    step_database_select,
    step_session_conflict,
    step_account_lookup_nav,
    step_account_search,
    step_account_select_row,
    step_account_sidebar_nav,
    step_view_picker,
)

PROFILE = Path(os.path.expandvars(r"%LOCALAPPDATA%\IGA Marketing Master\playwright-profile"))
EPIC_URL = "https://insu621.appliedepic.com"
CDP_PORT = 9222

# Account to land on after login. Set to None to skip account navigation
# entirely (the launcher will leave the browser on the post-login screen).
TEST_LOOKUP_CODE: str | None = "GORBILL-01"
TEST_SIDEBAR_SECTION: str = "Policies"
TEST_VIEW: str = "Marketed"

# Stop-level levels controlling how deep the post-login navigation goes.
# 1 = Home dashboard only (no account nav)
# 2 = Account Locate screen open
# 3 = Account selected (search + select row complete)
# 4 = Policies section selected on the account
# 5 = Marketed Policies view (full chain) — default
_LEVEL_LABELS = {
    1: "Home dashboard",
    2: "Account Locate screen",
    3: "Account selected",
    4: "Policies section",
    5: "Marketed Policies view (full chain)",
}


def _prompt_level(default: int = 5) -> int:
    """Prompt the operator for how deep to drive the launcher. Falls back to
    *default* on empty input or non-interactive stdin (e.g. running under a
    harness that didn't attach a TTY)."""
    print("How deep should the launcher navigate?")
    for n in sorted(_LEVEL_LABELS):
        marker = " (default)" if n == default else ""
        print(f"  {n} — {_LEVEL_LABELS[n]}{marker}")
    try:
        raw = input(f"Choose 1-{max(_LEVEL_LABELS)} [Enter for {default}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        print(f"  Unrecognized input {raw!r}; using default ({default}).")
        return default
    if n not in _LEVEL_LABELS:
        print(f"  {n} is out of range; using default ({default}).")
        return default
    return n


print(f"Profile: {PROFILE}")
print(f"CDP port: {CDP_PORT}")
print(f"Navigating to: {EPIC_URL}")
print()
STOP_LEVEL = _prompt_level()
print(f"Stop level: {STOP_LEVEL} — {_LEVEL_LABELS[STOP_LEVEL]}\n")

try:
    ctx = launch_with_persistent_context(PROFILE, headed=True, debug=False, cdp_port=CDP_PORT)
except Exception as exc:
    print(f"ERROR: launch failed: {exc}")
    input("\nPress Enter to exit...")
    sys.exit(1)

page = ctx.pages[0] if ctx.pages else ctx.new_page()
page.goto(EPIC_URL, wait_until="domcontentloaded", timeout=30_000)

# ── Login chain: enterprise ID → login → database select → session conflict ──
print("\n--- Running login chain ---")
try:
    step_enterprise_id.run(page)
    step_login.run(ctx)
    step_database_select.run(page, debug=True)  # debug=True → selects _DEMO database
    step_session_conflict.run(page, on_waiting=lambda msg: print(f"  {msg}"))
    print("--- Login chain complete ---\n")
except Exception as exc:
    print(f"WARNING: login chain failed: {exc}")
    print("You may need to finish login manually.\n")

# ── Account navigation chain ──────────────────────────────────────────────
# Steps execute conditionally based on STOP_LEVEL (1-5). Each step short-
# circuits the chain on failure so the operator still ends up with a
# usable browser, just stopped earlier than asked.
if STOP_LEVEL == 1:
    print("--- Stop level 1: leaving you on the Home dashboard ---\n")
elif not TEST_LOOKUP_CODE:
    print("--- TEST_LOOKUP_CODE is None: skipping account navigation ---\n")
else:
    print(f"--- Navigating to account {TEST_LOOKUP_CODE} (stop level {STOP_LEVEL}) ---")
    try:
        # Level >= 2: Open Account Locate
        if not step_account_lookup_nav.run(page):
            print("  WARNING: could not open Account Locate screen")
        elif STOP_LEVEL >= 3:
            # Level >= 3: Search the lookup code
            if not step_account_search.run(page, lookup_code=TEST_LOOKUP_CODE):
                print(f"  WARNING: search for {TEST_LOOKUP_CODE} failed")
            else:
                # Level >= 3 still: select the row
                result = step_account_select_row.run(page, lookup_code=TEST_LOOKUP_CODE)
                outcome = getattr(result, "outcome", None)
                outcome_name = getattr(outcome, "name", str(outcome))
                if outcome_name != "SELECTED":
                    print(f"  WARNING: account row not selected ({outcome_name}); "
                          f"error={getattr(result, 'error', None)!r}")
                else:
                    print(f"  Selected: {result.matched_lookup_code} — "
                          f"{result.matched_account_name}")
                    # Level >= 4: navigate to Policies sidebar
                    if STOP_LEVEL >= 4 and TEST_SIDEBAR_SECTION:
                        if not step_account_sidebar_nav.run(page, section=TEST_SIDEBAR_SECTION):
                            print(f"  WARNING: sidebar nav to {TEST_SIDEBAR_SECTION!r} failed")
                        elif STOP_LEVEL >= 5 and TEST_VIEW:
                            # Level >= 5: switch view to Marketed
                            if not step_view_picker.run(page, view=TEST_VIEW):
                                print(f"  WARNING: view switch to {TEST_VIEW!r} failed")
        print(f"--- Stopped at level {STOP_LEVEL}: {_LEVEL_LABELS[STOP_LEVEL]} ---\n")
    except Exception as exc:
        print(f"WARNING: account navigation failed: {exc}\n")

print(f"Browser is open at {EPIC_URL}")
print(f"Test scripts can connect via")
print(f"  playwright.chromium.connect_over_cdp('http://localhost:{CDP_PORT}')")
print()
print("Press Enter here OR close the browser window to exit and release the profile lock.")

# Exit when EITHER the user presses Enter OR they close the browser window.
# Without this, X-ing the browser leaves the script waiting on input() — the
# profile lock stays held and the next launch fails.
shutdown = threading.Event()

def _stdin_watcher() -> None:
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        pass
    shutdown.set()

threading.Thread(target=_stdin_watcher, daemon=True).start()

# Playwright fires `close` on the context when its underlying browser
# disconnects (which happens when the operator X's the window) — and on
# the page when the last tab closes. Either signal trips shutdown.
try:
    ctx.on("close", lambda _ctx: shutdown.set())
    page.on("close", lambda _pg: shutdown.set())
except Exception:
    pass

shutdown.wait()

try:
    ctx.close()
except Exception:
    # Already closed (browser was X-ed) — that's fine.
    pass

print("Browser closed — exiting.")

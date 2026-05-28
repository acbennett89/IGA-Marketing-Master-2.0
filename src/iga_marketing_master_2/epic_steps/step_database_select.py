"""step_database_select.py — select the correct EPIC database after login.

Some EPIC tenants present a Database picker immediately after credentials are
accepted. The picker shows a dropdown with one row per available database —
typically one ending in _DEMO and one ending in _PROD.

Rules:
  - debug=True  → select the option whose name ends with _DEMO
  - debug=False → select the option whose name ends with _PROD

If the dialog is not present the step is a no-op. If the correct database is
already selected, the dropdown is left alone and Continue is clicked directly.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

_log = logging.getLogger("iga.epic_steps.database_select")

_DETECT_MS = 3_000
_CONTINUE_WAIT_MS = 15_000

# Settle parameters after the database picker is dismissed (or skipped).
# EPIC's Home dashboard loads activity feeds asynchronously, surfacing a
# "Retrieving..." indicator while it works. Clicking toolbar buttons before
# that completes silently no-ops (the click goes to the loading overlay).
#
#   _RETRIEVING_APPEAR_MS — how long to give "Retrieving..." to appear before
#                            we assume the dashboard isn't going to show it.
#   _RETRIEVING_DISAPPEAR_MS — max time to wait for "Retrieving..." to go away
#                              once it has appeared.
#   _POST_LOGIN_FINAL_MS — small safety pad after the indicator clears.
_RETRIEVING_APPEAR_MS    = 3_000
_RETRIEVING_DISAPPEAR_MS = 30_000
_POST_LOGIN_FINAL_MS     = 1_000


def run(page: Page, *, debug: bool = False) -> bool:
    """Select DEMO (debug) or PROD (non-debug) database if the picker is shown.

    Returns True when the step succeeded or was not needed.
    Returns False on an unrecoverable error.
    """
    target_suffix = "_DEMO" if debug else "_PROD"

    # Detect the picker via the Continue button — unique to this dialog.
    try:
        continue_btn = page.get_by_role("button", name="Continue")
        continue_btn.wait_for(state="visible", timeout=_DETECT_MS)
    except PlaywrightTimeout:
        _log.debug("Database picker not present — skipping")
        _post_login_settle(page)
        return True

    _log.info("Database picker detected — target suffix: %s", target_suffix)

    try:
        # The picker uses an <asi-combo-box> Angular component.
        combo = page.locator("asi-combo-box").first

        # Read the current value from the input inside the combo box.
        combo_input = combo.locator("input").first
        current = ""
        try:
            current = (combo_input.input_value(timeout=2_000) or "").strip()
        except Exception:  # noqa: BLE001
            pass
        _log.info("Current database: %r", current)

        if current.upper().endswith(target_suffix.upper()):
            _log.info("Database already correct (%s) — clicking Continue", current)
        else:
            # Open the dropdown by clicking the chevron <i> inside the combo.
            combo.locator("i").first.click(timeout=3_000)

            # Click the row whose text ends with the target suffix.
            # Rows are divs inside asi-combo-box — scoping to combo avoids
            # matching the current-database display in the page header.
            row = combo.get_by_text(target_suffix, exact=False).first
            row.click(timeout=5_000)

            selected = ""
            try:
                selected = (combo_input.input_value(timeout=1_000) or "").strip()
            except Exception:  # noqa: BLE001
                pass
            _log.info("Database selected: %r", selected)

        # Click Continue.
        continue_btn.click()

        # Wait for the dialog to disappear.
        try:
            continue_btn.wait_for(state="hidden", timeout=_CONTINUE_WAIT_MS)
        except PlaywrightTimeout:
            pass  # Full page navigation may have replaced it — that's fine.

        _log.info("Database picker dismissed")
        _post_login_settle(page)
        return True

    except PlaywrightTimeout as exc:
        _log.error("Database picker step timed out: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_database_select: %s", exc)
        return False


def _post_login_settle(page: Page) -> None:
    """Wait for the EPIC Home dashboard to finish loading before the caller
    starts clicking toolbar buttons.

    The dashboard surfaces a "Retrieving..." indicator while its activity
    feeds load asynchronously. Clicking before it clears silently no-ops
    (the click lands on the loading overlay). We:

      1. Give the indicator up to ``_RETRIEVING_APPEAR_MS`` to render.
      2. If it shows up, poll until it disappears (max ``_RETRIEVING_DISAPPEAR_MS``).
      3. Burn a small final pad so any lingering frames mount.
      4. If the indicator never appeared, treat that as "already settled" and
         skip straight to the final pad.
    """
    # Find any element on the page whose visible text contains "Retrieving".
    # EPIC doesn't tag the loading indicator with a stable data-automation-id,
    # so a text probe is the most portable signal across builds.
    appeared = False
    try:
        appeared = bool(page.evaluate(
            """(timeout) => new Promise((resolve) => {
                const deadline = Date.now() + timeout;
                const check = () => {
                    const txt = (document.body && document.body.innerText) || '';
                    if (/\\bRetrieving\\b/i.test(txt)) return resolve(true);
                    if (Date.now() >= deadline) return resolve(false);
                    setTimeout(check, 100);
                };
                check();
            })""",
            _RETRIEVING_APPEAR_MS,
        ))
    except Exception:  # noqa: BLE001
        appeared = False

    if appeared:
        _log.debug("post-login: 'Retrieving...' detected — waiting for it to clear")
        try:
            cleared = bool(page.evaluate(
                """(timeout) => new Promise((resolve) => {
                    const deadline = Date.now() + timeout;
                    const check = () => {
                        const txt = (document.body && document.body.innerText) || '';
                        if (!/\\bRetrieving\\b/i.test(txt)) return resolve(true);
                        if (Date.now() >= deadline) return resolve(false);
                        setTimeout(check, 250);
                    };
                    check();
                })""",
                _RETRIEVING_DISAPPEAR_MS,
            ))
        except Exception:  # noqa: BLE001
            cleared = False
        if cleared:
            _log.debug("post-login: 'Retrieving...' cleared")
        else:
            _log.warning(
                "post-login: 'Retrieving...' still showing after %d ms — proceeding anyway",
                _RETRIEVING_DISAPPEAR_MS,
            )
    else:
        _log.debug("post-login: no 'Retrieving...' indicator seen — dashboard already idle")

    page.wait_for_timeout(_POST_LOGIN_FINAL_MS)
    _log.debug("post-login settle complete")

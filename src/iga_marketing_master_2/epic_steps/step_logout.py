"""step_logout.py — cleanly log out of EPIC before closing the app.

Clicks the Logout element in the EPIC top navigation bar, handles the
"Do you wish to close all Epic windows?" confirmation dialog (Yes), then
waits for the session to return to the signon screen. This prevents the
"already logged in" conflict that appears when EPIC is closed mid-session
and then relaunched.

Selector notes (confirmed via live CDP inspection):
  - Logout is a div/generic element with title "Logout (Ctrl+Alt+F4)",
    NOT a <button> role.  Use page.get_by_title() to find it.
  - The confirmation dialog may be suppressed if the user previously
    checked "Don't show this again".  The step handles both cases.
  - Post-logout signon screen shows button[name="Login"].
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

_log = logging.getLogger("iga.epic_steps.logout")

# Time to detect the Logout element when probing for an active session.
_DETECT_MS = 3_000
# Time to wait for the "close all windows" confirmation dialog to appear.
_CONFIRM_DIALOG_MS = 3_000
# Time to wait for the signon/login screen after clicking Yes.
_SIGNON_WAIT_MS = 15_000


def is_logged_in(page: Page) -> bool:
    """Return True if the page appears to be an active EPIC session.

    Uses a fast probe for the Logout element in the top nav ribbon.
    Returns False on any error (browser closed, page navigating, etc.).
    """
    try:
        page.get_by_title("Logout (Ctrl+Alt+F4)").wait_for(
            state="visible", timeout=_DETECT_MS
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def run(page: Page) -> bool:
    """Click Logout, confirm the dialog, and wait for the EPIC session to end.

    Returns True when logout succeeded or was not needed.
    Returns False on timeout or unexpected error.
    """
    # Detect the Logout element — if not present the user isn't logged in.
    try:
        logout_el = page.get_by_title("Logout (Ctrl+Alt+F4)")
        logout_el.wait_for(state="visible", timeout=_DETECT_MS)
    except PlaywrightTimeout:
        _log.debug("Logout element not found — user may not be logged in")
        return True

    _log.info("Clicking EPIC Logout")

    try:
        logout_el.click()

        # EPIC may show "Do you wish to close all Epic windows?" — click Yes.
        try:
            confirm_text = page.get_by_text(
                "Do you wish to close all Epic windows?", exact=False
            )
            confirm_text.wait_for(state="visible", timeout=_CONFIRM_DIALOG_MS)
            _log.info("Logout confirmation dialog detected — clicking Yes")
            page.get_by_role("button", name="Yes").click()
        except PlaywrightTimeout:
            # Dialog suppressed (user checked "Don't show this again") — that's fine.
            _log.debug("No logout confirmation dialog — proceeding")

        # Wait for the signon screen (Login button).
        login_btn = page.get_by_role("button", name="Login")
        login_btn.wait_for(state="visible", timeout=_SIGNON_WAIT_MS)
        _log.info("Logout complete — back on signon screen")
        return True

    except PlaywrightTimeout:
        _log.warning(
            "Logout: signon screen did not appear within %d ms — "
            "proceeding anyway to avoid blocking app close",
            _SIGNON_WAIT_MS,
        )
        return True  # Best-effort — never block a close on a slow redirect

    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_logout: %s", exc)
        return False

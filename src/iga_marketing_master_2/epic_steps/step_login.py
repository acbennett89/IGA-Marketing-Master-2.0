"""step_login.py — handle the EPIC Identity Provider login flow.

After the Enterprise ID step completes, the EPIC home page shows a single
"Login" button. Clicking it opens a **new tab** at the Applied Identity
Provider (idpprva.appliedcloudservices.com) with a Usercode + Password form.

This step:
1. Checks whether the EPIC home tab already shows the Login button.
   If not (persistent profile has a live session) → no-op, return True.
2. Clicks Login on the home tab and waits for the IDP tab to open.
3. Fills Usercode and Password from Windows Credential Manager.
4. Clicks the IDP Login button and waits for the IDP tab to close.
5. Switches focus back to the EPIC home tab.

Credentials are stored via secret_store.set_epic_credentials(). If they are
not stored, this step returns False and logs an actionable message.
"""

from __future__ import annotations

import logging

from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeout

from ..secret_store import get_epic_credentials

_log = logging.getLogger("iga.epic_steps.login")

# URL fragment that identifies the Applied Identity Provider tab.
_IDP_URL_FRAGMENT = "appliedcloudservices.com/EpicIdentityProvider"

# How long to wait to see if the Login button is on the EPIC home page (ms).
_LOGIN_BTN_DETECT_MS = 3_000
# How long to wait for the IDP tab to appear after clicking Login (ms).
_IDP_TAB_WAIT_MS = 10_000
# How long to wait for the IDP tab to close after submitting credentials (ms).
_IDP_CLOSE_WAIT_MS = 15_000
# How long to wait for the EPIC home page to finish loading post-login (ms).
_HOME_LOAD_WAIT_MS = 30_000


def _find_epic_home(context: BrowserContext) -> Page | None:
    """Return the first page that is NOT the IDP tab, or None."""
    for p in context.pages:
        if _IDP_URL_FRAGMENT not in p.url:
            return p
    return None


def run(context: BrowserContext) -> bool:
    """Perform the EPIC login if the home page requires it.

    Returns True when login succeeded (or was not needed).
    Returns False on credential missing or unrecoverable error.
    """
    home = _find_epic_home(context)
    if home is None:
        _log.error("No EPIC home tab found in browser context")
        return False

    # Check whether the Login button is present — if not, we're already in.
    try:
        login_btn = home.get_by_role("button", name="Login")
        login_btn.wait_for(state="visible", timeout=_LOGIN_BTN_DETECT_MS)
    except PlaywrightTimeout:
        _log.debug("EPIC Login button not present — session already active, skipping")
        return True

    # Load credentials before doing anything in the browser.
    creds = get_epic_credentials()
    if creds is None:
        _log.error(
            "EPIC credentials not stored. Run secret_store.set_epic_credentials(usercode, password) "
            "once to save them to Windows Credential Manager."
        )
        return False
    usercode, password = creds

    _log.info("EPIC Login button detected — starting login flow")

    try:
        # Click Login on the home tab — this opens the IDP in a new tab.
        with context.expect_page(timeout=_IDP_TAB_WAIT_MS) as page_info:
            login_btn.click()
        idp_page = page_info.value

        _log.info("IDP tab opened: %s", idp_page.url)
        idp_page.wait_for_load_state("domcontentloaded", timeout=_IDP_TAB_WAIT_MS)

        # Fill credentials.
        # The IDP form uses div labels, not <label> elements, so get_by_label
        # is unreliable. Use accessible name via get_by_role first; fall back
        # to positional input selectors if the accessible name doesn't match.
        try:
            idp_page.get_by_role("textbox", name="Usercode").fill(usercode)
        except Exception:  # noqa: BLE001
            idp_page.locator("input").nth(0).fill(usercode)

        try:
            idp_page.get_by_role("textbox", name="Password").fill(password)
        except Exception:  # noqa: BLE001
            idp_page.locator("input[type='password'], input").nth(1).fill(password)

        idp_page.get_by_role("button", name="Login").click()

        # Wait for the IDP tab to close (redirect completes, tab shuts).
        idp_page.wait_for_event("close", timeout=_IDP_CLOSE_WAIT_MS)
        _log.info("IDP tab closed — login submitted")

    except PlaywrightTimeout as exc:
        _log.error("Login flow timed out: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_login: %s", exc)
        return False

    # Wait for the EPIC home to finish loading after the auth redirect.
    try:
        home.wait_for_load_state("domcontentloaded", timeout=_HOME_LOAD_WAIT_MS)
        home.bring_to_front()
        _log.info("EPIC home loaded — login complete")
        return True
    except PlaywrightTimeout:
        _log.warning(
            "EPIC home did not finish loading within %d ms post-login — "
            "continuing anyway (may need extra wait)",
            _HOME_LOAD_WAIT_MS,
        )
        return True  # Tab is open; partial load is better than aborting entry

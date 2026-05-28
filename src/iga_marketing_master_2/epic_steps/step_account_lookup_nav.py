"""step_account_lookup_nav.py — navigate to the EPIC Account Locate screen.

Pre-condition
-------------
The browser is attached to EPIC and the operator is logged in past the
Database picker. Any EPIC screen is acceptable as a starting point — the
Locate toolbar button is present in the global toolbar regardless of the
current area.

What this step does
-------------------
1.  If the Account Locate screen is already open, it is a no-op (idempotent).
2.  Otherwise, clicks the toolbar's Locate button (``title="Locate (F2)"``)
    and waits for the Account Locate screen to render.

Selector notes (confirmed via live CDP inspection 2026-05-22)
-------------------------------------------------------------
- Toolbar button: ``div.main-button[title="Locate (F2)"]`` — has no
  ``data-automation-id``; the ``title`` attribute is the most stable hook.
- Screen container: ``[data-automation-id="AccountLocate"]`` wraps the
  whole Account Locate ``<program>`` element. The presence of this node
  is the canonical signal that the screen is loaded.
- Sibling frames visible on the screen: ``fraCriteria``, ``fraAccountType``,
  ``fraLocateBy``, ``fraResults``. Any of these going visible also
  indicates a successful navigation; ``fraCriteria`` is used here as a
  belt-and-braces secondary check.
- Footer screen code is ``LOCATE`` and the document title becomes
  ``Account Locate`` — both are also reliable detection signals if the
  data-automation-id ever changes.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.account_lookup_nav")

_ALREADY_OPEN_MS = 500    # quick probe: are we already on Account Locate?
_CLICK_TIMEOUT   = 5_000  # click the Locate toolbar button
_LOAD_WAIT_MS    = 15_000 # wait for the Account Locate screen to render
_FINAL_SETTLE_MS = 1_000  # tail pause so the next step doesn't race EPIC

_SCREEN_SEL  = '[data-automation-id="AccountLocate"]'
_CRITERIA_SEL = '[data-automation-id="fraCriteria"]'
_LOCATE_BTN_SEL = 'div.main-button[title="Locate (F2)"]'


def run(page: Page) -> bool:
    """Open the EPIC Account Locate screen.

    Returns True when the screen is loaded (or was already open).
    Returns False on an unrecoverable error.
    """
    checkpoint(page, "Account: open Locate screen")
    # 1. Idempotent: skip the click if Account Locate is already open.
    # Use page title as the primary signal (rock-solid) and the data-automation-id
    # marker as a backup — see comment on the post-click wait below for why
    # ``is_visible()`` is unreliable for the AccountLocate wrapper element.
    try:
        if (page.title() or "").strip().lower() == "account locate":
            _log.debug("Account Locate already open (title match) — no navigation needed")
            page.wait_for_timeout(_FINAL_SETTLE_MS)
            return True
        if page.locator(_SCREEN_SEL).count() > 0:
            _log.debug("Account Locate already open (marker present) — no navigation needed")
            page.wait_for_timeout(_FINAL_SETTLE_MS)
            return True
    except Exception:  # noqa: BLE001
        # If the probe blew up, fall through to the click path.
        pass

    # 2. Click the toolbar's Locate button.
    try:
        locate_btn = page.locator(_LOCATE_BTN_SEL)
        locate_btn.wait_for(state="visible", timeout=_CLICK_TIMEOUT)
    except PlaywrightTimeout:
        _log.error(
            "Locate toolbar button not visible — is EPIC loaded and past "
            "the database picker?"
        )
        return False

    try:
        locate_btn.first.click(timeout=_CLICK_TIMEOUT)
    except EntryCancelled:
        raise
    except PlaywrightTimeout as exc:
        _log.error("Click on Locate toolbar button timed out: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error clicking Locate toolbar button: %s", exc)
        return False

    # 3. Wait for the Account Locate screen to render.
    # We check three independent signals — first hit wins:
    #   (a) page.title() == "Account Locate" — EPIC sets this per screen.
    #   (b) [data-automation-id="AccountLocate"] attached — the `<program>`
    #       wrapper for the screen. We use ``attached`` not ``visible`` because
    #       the wrapper itself often has 0 px CSS dimensions (only its
    #       children render content), so Playwright's visibility check rejects
    #       it even when the screen is fully rendered.
    #   (c) fraCriteria visible — final belt-and-braces fallback.
    try:
        page.wait_for_function(
            """([titleWant, screenSel, criteriaSel]) => {
                if ((document.title || '').trim().toLowerCase() === titleWant) return true;
                if (document.querySelector(screenSel)) return true;
                const c = document.querySelector(criteriaSel);
                if (c && c.offsetParent !== null) return true;
                return false;
            }""",
            arg=["account locate", _SCREEN_SEL, _CRITERIA_SEL],
            timeout=_LOAD_WAIT_MS,
        )
    except PlaywrightTimeout:
        _log.error(
            "Clicked Locate but the Account Locate screen did not render "
            "within %d ms", _LOAD_WAIT_MS,
        )
        return False

    _log.info("Account Locate screen open")
    page.wait_for_timeout(_FINAL_SETTLE_MS)
    return True

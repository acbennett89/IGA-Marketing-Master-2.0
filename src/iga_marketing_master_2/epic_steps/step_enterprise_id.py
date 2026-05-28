"""step_enterprise_id.py — dismiss the EPIC Enterprise ID prompt.

EPIC shows "Missing or Invalid Enterprise ID" on the very first load of a fresh
Playwright profile (or any time the browser loses its stored tenant cookie).
This step detects that dialog, fills in the tenant ID, and clicks Continue.

If the dialog is not present the step is a no-op and returns True immediately.
"""

from __future__ import annotations

import logging

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

_log = logging.getLogger("iga.epic_steps.enterprise_id")

ENTERPRISE_ID = "INSU621"

# How long to wait for the dialog to appear on page load (ms).
_DIALOG_DETECT_MS = 3_000
# How long to wait for the dialog to disappear after clicking Continue (ms).
_DISMISS_WAIT_MS = 10_000


def run(page: Page) -> bool:
    """Fill the Enterprise ID dialog if present, then wait for it to close.

    Returns True when the dialog was handled (or was never shown).
    Returns False if the dialog appeared but could not be dismissed.
    """
    # Quick presence check — the dialog title is a reliable anchor.
    try:
        dialog_heading = page.get_by_text("Missing or Invalid Enterprise ID", exact=True)
        dialog_heading.wait_for(state="visible", timeout=_DIALOG_DETECT_MS)
    except PlaywrightTimeout:
        _log.debug("Enterprise ID dialog not present — skipping")
        return True

    _log.info("Enterprise ID dialog detected — filling '%s'", ENTERPRISE_ID)

    try:
        field = page.get_by_label("Enterprise ID")
        field.fill(ENTERPRISE_ID)

        page.get_by_role("button", name="Continue").click()

        # Wait for the dialog to leave the DOM.
        dialog_heading.wait_for(state="hidden", timeout=_DISMISS_WAIT_MS)

        _log.info("Enterprise ID accepted — dialog dismissed")
        return True

    except PlaywrightTimeout:
        _log.error(
            "Enterprise ID dialog did not dismiss within %d ms after clicking Continue",
            _DISMISS_WAIT_MS,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_enterprise_id: %s", exc)
        return False

"""step_session_conflict.py — handle the EPIC "already logged in" dialog.

After login and database selection, EPIC may show:

    "User 'BENAN1' is already logged in. Do you wish to cancel that session
     and continue? Clicking 'Yes' may result in any unsaved work being lost."

This dialog requires a human decision — clicking Yes boots the existing
session, No aborts the login. The step detects it and waits indefinitely
(polling every 500 ms) until the user dismisses it, then continues.

An optional ``on_waiting`` callback is called once when the dialog is first
detected, allowing the caller to update the UI (e.g. sidebar status).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

_log = logging.getLogger("iga.epic_steps.session_conflict")

_DETECT_MS = 3_000
_POLL_INTERVAL_S = 0.5
# Maximum wait before giving up (30 minutes — effectively unlimited for a
# human-attended session).
_MAX_WAIT_S = 30 * 60


def run(
    page: Page,
    *,
    on_waiting: Callable[[str], None] | None = None,
) -> bool:
    """Wait for the operator to dismiss the session-conflict dialog if shown.

    Returns True once the dialog is gone (or was never there).
    Returns False only if the maximum wait is exceeded.
    """
    # Quick check — if the dialog isn't present, bail out fast.
    try:
        dialog_text = page.get_by_text("is already logged in", exact=False)
        dialog_text.wait_for(state="visible", timeout=_DETECT_MS)
    except PlaywrightTimeout:
        _log.debug("Session conflict dialog not present — skipping")
        return True

    _log.warning(
        "Session conflict dialog detected — waiting for operator to dismiss it"
    )

    if on_waiting is not None:
        try:
            on_waiting("⚠ Action needed in browser — dismiss the Sign On dialog")
        except Exception:  # noqa: BLE001
            pass

    # Poll until the dialog disappears or max wait is exceeded.
    deadline = time.monotonic() + _MAX_WAIT_S
    while time.monotonic() < deadline:
        try:
            dialog_text.wait_for(state="hidden", timeout=int(_POLL_INTERVAL_S * 1000))
            _log.info("Session conflict dialog dismissed — continuing")
            return True
        except PlaywrightTimeout:
            pass  # Still visible — keep waiting
        except Exception as exc:  # noqa: BLE001
            # Page navigated away — dialog is gone
            _log.info("Session conflict: page changed (%s) — continuing", exc)
            return True

    _log.error(
        "Session conflict dialog still present after %d minutes — giving up",
        _MAX_WAIT_S // 60,
    )
    return False

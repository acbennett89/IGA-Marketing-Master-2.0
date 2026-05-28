"""step_account_sidebar_nav.py — click a section in the EPIC left sidebar.

Pre-condition
-------------
The EPIC left sidebar is visible. This is true on the Account Locate
results screen (with a row highlighted) and on every per-account screen
once an account is open. The step is reusable in both contexts.

What this step does
-------------------
1.  Finds the level-1 sidebar button whose visible text matches *section*
    (case-insensitive, exact).
2.  If that button is already ``.selected`` (the section is already showing),
    the step is a no-op.
3.  Otherwise, clicks the button and waits for it to become ``.selected``
    (or the page title to change), whichever happens first.

The *section* parameter is the displayed label — e.g. ``"Account Detail"``,
``"Policies"``, ``"Quotes"``, ``"Activities"``, ``"Contacts"``. Matching by
text is deliberate: the underlying ``sidebar-button-N`` numeric IDs vary
across EPIC tenants and reorder over time, but the labels are stable.

Selector notes (confirmed via live CDP inspection 2026-05-22)
-------------------------------------------------------------
- Sidebar button: ``<a class="sidebar-button level-1">`` — text is in the
  anchor's ``innerText``. Direct host element is ``<sidebar-button>``, but
  the ``<a>`` is what receives clicks.
- Selected state: ``selected`` class added to both the ``<a>`` and the host
  ``<sidebar-button>``. We probe the ``<a>``.
- Some items show ``sidebar-button parent level-1`` when they have children
  (e.g. Contacts, Quotes, Policies). The ``parent`` modifier doesn't affect
  clickability — the anchor still navigates on click.
- ``data-automation-id`` is ``sidebar-button-N level-1`` where N differs per
  tenant — intentionally not used.
"""

from __future__ import annotations

import logging
import time

from playwright.sync_api import Page

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.account_sidebar_nav")

_SIDEBAR_BUTTON_SEL = 'a.sidebar-button.level-1'

_FIND_TIMEOUT_MS = 5_000
_CLICK_TIMEOUT   = 5_000
_NAV_SETTLE_MS   = 8_000
_FINAL_SETTLE_MS = 1_000  # tail pause so the next step doesn't race EPIC


def _find_button_handle(page: Page, section: str):
    """Return the JS handle (via Playwright Locator) for the matching button.

    Returns ``None`` if no visible sidebar button has matching text. Uses
    ``evaluate_handle`` so we can check ``.selected`` and click without
    re-querying.
    """
    return page.evaluate_handle(
        """([sel, want]) => {
            const btns = Array.from(document.querySelectorAll(sel))
                .filter(el => el.offsetParent !== null);
            for (const b of btns) {
                if ((b.innerText || '').trim().toLowerCase() === want.toLowerCase()) {
                    return b;
                }
            }
            return null;
        }""",
        [_SIDEBAR_BUTTON_SEL, section],
    )


def run(
    page: Page,
    *,
    section: str,
    settle_timeout_ms: int = _NAV_SETTLE_MS,
) -> bool:
    """Navigate to *section* via the EPIC left sidebar.

    Returns True when the section is showing (or was already).
    Returns False when the button could not be found, the click failed, or
    the section did not become active within *settle_timeout_ms*.
    """
    target = (section or "").strip()
    if not target:
        _log.error("step_account_sidebar_nav.run called with empty section")
        return False

    checkpoint(page, f"Account: sidebar navigate to {target!r}")

    # 1. Locate the button (poll briefly — sidebar can take a moment to render
    # after navigation).
    deadline = time.time() + _FIND_TIMEOUT_MS / 1000.0
    handle = None
    while time.time() < deadline:
        handle = _find_button_handle(page, target)
        try:
            if handle and handle.evaluate("el => el !== null"):
                break
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)
        handle = None

    if handle is None:
        _log.error(
            "Sidebar button %r not found. Is the left sidebar visible on "
            "this screen?", target,
        )
        return False

    # 2. Idempotent: skip click if already selected.
    try:
        already_selected = bool(
            handle.evaluate("el => el.classList.contains('selected')"),
        )
    except Exception:  # noqa: BLE001
        already_selected = False

    if already_selected:
        _log.debug("Sidebar %r already selected — no click needed", target)
        page.wait_for_timeout(_FINAL_SETTLE_MS)
        return True

    title_before = ""
    try:
        title_before = page.title()
    except Exception:  # noqa: BLE001
        pass

    # 3. Click via the live handle (avoids stale-locator races).
    try:
        handle.evaluate(
            """el => {
                el.scrollIntoView({block: 'nearest'});
                el.click();
            }""",
        )
    except EntryCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.error("Click on sidebar %r failed: %s", target, exc)
        return False

    # 4. Wait for the section to become active. Two signals — either is enough:
    #    (a) the button gains the .selected class
    #    (b) the page title changes (EPIC sets it per-screen)
    deadline = time.time() + settle_timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            sel_now = bool(
                handle.evaluate("el => el.classList.contains('selected')"),
            )
        except Exception:  # noqa: BLE001
            sel_now = False
        if sel_now:
            _log.info("Sidebar navigation complete: %r", target)
            page.wait_for_timeout(_FINAL_SETTLE_MS)
            return True

        try:
            if page.title() and page.title() != title_before:
                _log.info(
                    "Sidebar navigation complete (title changed): %r → %r",
                    title_before, page.title(),
                )
                page.wait_for_timeout(_FINAL_SETTLE_MS)
                return True
        except Exception:  # noqa: BLE001
            pass

        page.wait_for_timeout(150)

    _log.error(
        "Clicked sidebar %r but it never became active within %d ms",
        target, settle_timeout_ms,
    )
    return False

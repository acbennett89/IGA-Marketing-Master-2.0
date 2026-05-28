"""step_view_picker.py — switch the current EPIC frame's view via its title dropdown.

EPIC's standard screens have a frame title shaped like ``Policies - Current/Renewed``
where ``Policies`` is the screen and ``Current/Renewed`` is the active view. A
``▼`` caret next to the title opens a context menu of alternate views:
``All Except Marketed``, ``Current/Renewed``, ``Expired/History``, ``Marketed``,
``Marketed (History)`` on the Policies screen — and analogous lists on Contacts,
Quotes, and similar screens. This step drives that dropdown.

Typical composition
-------------------
To reach Marketed Policies for the currently-selected account::

    step_account_sidebar_nav.run(page, section="Policies")
    step_view_picker.run(page, view="Marketed")

Pre-condition
-------------
A frame with the title-dropdown pattern is visible. By default the step uses
the first visible ``.context-menu-link`` on the page; pass *frame_id* to scope
to a specific frame when multiple are visible.

What this step does
-------------------
1.  Resolves the title-dropdown trigger (``span.frame-title-text.context-menu-link``)
    on the page or inside *frame_id*.
2.  Reads the current view from ``.context-menu-title`` and skips the work if
    it already equals *view* (case-insensitive).
3.  Clicks the trigger to open the dropdown.
4.  Clicks the ``<li class="dropdown-menu-item">`` whose visible text equals
    *view* exactly (case-insensitive).
5.  Waits for the title's ``.context-menu-title`` to update to *view*.

Selector notes (confirmed via live CDP inspection 2026-05-22)
-------------------------------------------------------------
- Trigger:        ``span.frame-title-text.context-menu-link`` (clickable)
- Current value:  ``span.context-menu-title > span`` inside the trigger
- Open menu:      ``ul.dropdown-menu.visible.expand-right`` (portal-rendered)
- Menu items:     ``li.dropdown-menu-item`` with ``span.text`` content
- Item ``data-automation-id`` is ``dropdown-menu-item-N`` where N changes by
  tenant/state — we match by visible text, not numeric id.
"""

from __future__ import annotations

import logging
import time

from playwright.sync_api import Page

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.view_picker")

_TRIGGER_SEL    = 'span.frame-title-text.context-menu-link'
_CURRENT_SEL    = 'span.context-menu-title'
_MENU_SEL       = 'ul.dropdown-menu.visible'
_MENU_ITEM_SEL  = 'li.dropdown-menu-item'

_TRIGGER_WAIT_MS = 5_000
_MENU_WAIT_MS    = 3_000
_SETTLE_WAIT_MS  = 5_000
_FINAL_SETTLE_MS = 1_000  # tail pause so the next step doesn't race EPIC


def _scope(page: Page, frame_id: str | None):
    """Return a Locator for *page* or the *frame_id* frame container."""
    if frame_id:
        return page.locator(f'[data-automation-id="{frame_id}"]')
    return page


def _current_view(scope, trigger) -> str:
    """Read the active view text from the trigger's ``.context-menu-title``."""
    try:
        cur = trigger.locator(_CURRENT_SEL).first
        if cur.count() == 0:
            return ""
        return (cur.inner_text(timeout=1_000) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _find_trigger_handle(
    page: Page,
    *,
    frame_id: str | None,
    screen_name: str | None,
    timeout_ms: int,
):
    """Find a single ``.frame-title-text.context-menu-link`` trigger.

    With *screen_name* set, we require the trigger's ``.title`` child to
    say that name — this is the disambiguator when a frame contains
    multiple title-style dropdowns (we hit this on the Policies frame,
    which embeds a "scope" filter showing options ``Client/All/Line``
    that has the same trigger class as the view picker).

    Polls until a matching trigger is visible or *timeout_ms* elapses.
    Returns a Playwright ``ElementHandle`` (via ``evaluate_handle``) or
    ``None`` on timeout.
    """
    import time as _time

    deadline = _time.time() + timeout_ms / 1000.0
    while _time.time() < deadline:
        handle = page.evaluate_handle(
            """([frameId, screenName]) => {
                const root = frameId
                    ? document.querySelector(`[data-automation-id="${frameId}"]`)
                    : document;
                if (!root) return null;
                const triggers = Array.from(
                    root.querySelectorAll('span.frame-title-text.context-menu-link')
                ).filter(t => t.offsetParent !== null);
                if (!triggers.length) return null;
                if (!screenName) return triggers[0];
                const want = screenName.toLowerCase();
                for (const t of triggers) {
                    const titleEl = t.querySelector('.title');
                    const titleTxt = (titleEl?.innerText || '').trim().toLowerCase();
                    if (titleTxt === want) return t;
                }
                return null;
            }""",
            [frame_id, screen_name],
        )
        try:
            if handle and handle.evaluate("el => el !== null"):
                return handle
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)
    return None


def run(
    page: Page,
    *,
    view: str,
    frame_id: str | None = None,
    screen_name: str | None = None,
    settle_timeout_ms: int = _SETTLE_WAIT_MS,
) -> bool:
    """Switch the active frame's view to *view*.

    *frame_id* — optional ``data-automation-id`` of the surrounding frame
    (e.g. ``"fraPolicies"``) to scope the trigger lookup when multiple
    title-dropdowns are visible at once.
    *screen_name* — optional screen-title disambiguator (e.g. ``"Policies"``).
    When set we only consider triggers whose ``.title`` element matches.
    The combination of *frame_id* + *screen_name* is the strongest form of
    targeting; either alone may pick the wrong dropdown on busy screens.

    Returns True when the view is showing *view* (or already was).
    Returns False when the trigger or menu item could not be found, or the
    update did not settle in *settle_timeout_ms*.
    """
    target = (view or "").strip()
    if not target:
        _log.error("step_view_picker.run called with empty view")
        return False

    checkpoint(page, f"View picker: switch to {target!r}")

    # 1. Locate the title trigger with full disambiguation.
    trigger_handle = _find_trigger_handle(
        page,
        frame_id=frame_id,
        screen_name=screen_name,
        timeout_ms=_TRIGGER_WAIT_MS,
    )
    if trigger_handle is None:
        _log.error(
            "View dropdown trigger not found (frame_id=%r screen_name=%r) "
            "within %d ms",
            frame_id, screen_name, _TRIGGER_WAIT_MS,
        )
        return False

    # 2. Idempotent: skip if the resolved trigger already shows the target.
    try:
        current = (trigger_handle.evaluate(
            """el => {
                const cur = el.querySelector('span.context-menu-title');
                return (cur ? (cur.innerText || '') : '').trim();
            }"""
        ) or "").strip()
    except Exception:  # noqa: BLE001
        current = ""

    if current.casefold() == target.casefold():
        _log.debug("View already %r — no change", target)
        page.wait_for_timeout(_FINAL_SETTLE_MS)
        return True

    _log.info(
        "Switching view: %r → %r (frame_id=%r screen_name=%r)",
        current or "<unknown>", target, frame_id, screen_name,
    )

    # 3. Open the dropdown by clicking the resolved trigger handle directly
    # (NOT a generic locator that might re-match the wrong element).
    try:
        trigger_handle.evaluate("el => { el.scrollIntoView({block: 'nearest'}); el.click(); }")
    except EntryCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.error("Could not open view dropdown: %s", exc)
        return False

    # 4. Wait for the menu to appear (rendered as a portal — search page-wide).
    menu = page.locator(_MENU_SEL).first
    try:
        menu.wait_for(state="visible", timeout=_MENU_WAIT_MS)
    except Exception:  # noqa: BLE001
        _log.error("View dropdown menu did not appear")
        return False

    # 5. Click the item whose visible text matches *target* exactly.
    clicked = page.evaluate(
        """([menuSel, itemSel, want]) => {
            const menus = Array.from(document.querySelectorAll(menuSel))
                .filter(m => m.offsetParent !== null);
            if (!menus.length) return { ok: false, reason: 'no visible menu' };
            const menu = menus[menus.length - 1]; // latest opened
            const items = Array.from(menu.querySelectorAll(itemSel));
            const available = [];
            for (const item of items) {
                const txt = (item.innerText || '').trim();
                available.push(txt);
                if (txt.toLowerCase() === want.toLowerCase()) {
                    item.click();
                    return { ok: true };
                }
            }
            return { ok: false, reason: 'no match', available };
        }""",
        [_MENU_SEL, _MENU_ITEM_SEL, target],
    )
    if not clicked.get("ok"):
        _log.error(
            "View %r not in dropdown. Available: %s",
            target, clicked.get("available", []),
        )
        return False

    # 6. Wait for the title to reflect the new view.
    deadline = time.time() + settle_timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            cur = (trigger_handle.evaluate(
                """el => {
                    const cur = el.querySelector('span.context-menu-title');
                    return (cur ? (cur.innerText || '') : '').trim();
                }"""
            ) or "").strip()
        except Exception:  # noqa: BLE001
            cur = ""
        if cur.casefold() == target.casefold():
            _log.info("View switched: %r", target)
            page.wait_for_timeout(_FINAL_SETTLE_MS)
            return True
        page.wait_for_timeout(150)

    _log.error(
        "Clicked %r in dropdown but title never updated within %d ms",
        target, settle_timeout_ms,
    )
    return False

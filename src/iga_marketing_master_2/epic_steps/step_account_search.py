"""step_account_search.py — search for an account on the EPIC Account Locate screen.

Pre-condition
-------------
The browser is already on the EPIC Account Locate screen (``[data-automation-id=
"AccountLocate"]`` is present). Call :mod:`step_account_lookup_nav` first if
that is not guaranteed.

What this step does
-------------------
1.  Ensures the "Locate account" combo (``cboAccountType``) is set to ``Client``;
    changes it via the dropdown only if it is not already that value.
2.  Ensures the "Locate by" combo (``cboLocateBy``) is set to *search_by* (the
    displayed label — e.g. ``"Lookup Code"`` or ``"Account/Business Name"``);
    changes it via the dropdown only if it is not already that value.
3.  Fills the criteria input (``streCriteria1``) with *search_term*.
4.  Clicks the Locate (search) button (``btnLocate``).

The step does *not* read results, pick a row, or verify how many matches came
back — that is the job of :mod:`step_account_select_row` (lookup-code flow) or
:mod:`step_account_dup_check` (name-search / new-account preflight).

Common *search_by* values (full list lives in the cboLocateBy dropdown — match
by visible text)::

    Lookup Code              # default; exact-prefix match on the EPIC code
    Account/Business Name    # substring match on the displayed name
    Email Address
    Phone Number
    Last Name, First Name
    Street Address
    Submission ID
    ...

Selector notes (confirmed via live CDP inspection 2026-05-22)
-------------------------------------------------------------
- Both combos use the same EPIC ``asi-combo-box`` pattern:
    * Open via ``[data-automation-id="<id>"] .drop-btn`` (caret icon).
    * Options render as portal rows ``div[data-automation-id^="<id> body-row"]``
      somewhere in the DOM (not inside the combo itself).
    * The current value lives in ``[data-automation-id="<id>"] input``.
- ``cboAccountType`` defaults to ``Client``; other values include Customer,
  Suspect, Prospect — none of which we want for marketing lookup.
- ``cboLocateBy`` default is ``Account/Business Name``.
- The criteria input is always ``streCriteria1`` regardless of which
  ``cboLocateBy`` value is chosen — only the label above it changes (e.g.
  "Lookup code begins with" vs. "Account/Business name contains").
"""

from __future__ import annotations

import logging
import time

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.account_search")

_SCREEN_SEL       = '[data-automation-id="AccountLocate"]'
_ACCOUNT_TYPE_SEL = '[data-automation-id="cboAccountType"]'
_LOCATE_BY_SEL    = '[data-automation-id="cboLocateBy"]'
_CRITERIA_SEL     = '[data-automation-id="streCriteria1"]'
_BTN_LOCATE_SEL   = '[data-automation-id="btnLocate"]'

_COMBO_OPEN_TIMEOUT = 2_000
_ROW_POLL_TIMEOUT   = 5_000
_CLICK_TIMEOUT      = 5_000
_LOAD_TIMEOUT       = 10_000
_FINAL_SETTLE_MS    = 1_000  # tail pause so the next step doesn't race EPIC


def _current_combo_value(page: Page, combo_sel: str) -> str:
    """Return the current value shown in the combo's input, or ``""``."""
    try:
        inp = page.locator(f"{combo_sel} input").first
        return (inp.input_value(timeout=1_000) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _select_combo_value(page: Page, combo_sel: str, target: str) -> None:
    """Open *combo_sel* and click the portal row whose text equals *target*.

    Idempotent — if the combo already shows *target* (case-insensitive), no
    interaction occurs. Raises RuntimeError if the target row never appears.

    The EPIC ``asi-combo-box`` renders its option list in a top-level portal
    keyed by ``data-automation-id="<combo-id> body-row item-N"``. We poll for
    those rows after clicking ``.drop-btn``, then click the row whose visible
    text matches *target*.
    """
    if _current_combo_value(page, combo_sel).casefold() == target.casefold():
        _log.debug("combo %s already = %r — no change", combo_sel, target)
        return

    _log.info("combo %s → %r", combo_sel, target)

    # Extract bare automation-id so we can build the portal-row selector.
    # e.g. '[data-automation-id="cboLocateBy"]' → 'cboLocateBy'
    combo_id = combo_sel.split('"')[1] if '"' in combo_sel else combo_sel

    # 1. Open the dropdown via the caret button.
    try:
        page.locator(f"{combo_sel} .drop-btn").first.click(timeout=_COMBO_OPEN_TIMEOUT)
    except PlaywrightTimeout as exc:
        raise RuntimeError(f"Could not open {combo_id} dropdown: {exc}") from exc

    # 2. Poll for portal rows to appear.
    portal_sel = f'div[data-automation-id^="{combo_id} body-row"]'
    deadline = time.time() + _ROW_POLL_TIMEOUT / 1000.0
    rows_locator = page.locator(portal_sel)
    while time.time() < deadline:
        if rows_locator.count() > 0:
            break
        page.wait_for_timeout(120)
    else:
        raise RuntimeError(
            f"{combo_id} dropdown opened but no rows appeared within "
            f"{_ROW_POLL_TIMEOUT} ms"
        )

    # 3. Find the row whose text matches *target* exactly (case-insensitive),
    # then click it via Playwright. JS ``.click()`` doesn't trigger Angular's
    # selection handler for these combos — we observed cboLocateBy staying on
    # the old value despite a successful JS click. Playwright's :has-text is
    # substring-y and several Locate-by options overlap (e.g. "Account/Business
    # Name" vs "Account/Business name contains"), so we resolve the matching
    # index in JS and let Playwright click via ``.nth(idx)``.
    matched_index = page.evaluate(
        """([portalSel, want]) => {
            const rows = Array.from(document.querySelectorAll(portalSel));
            for (let i = 0; i < rows.length; i++) {
                if ((rows[i].innerText || '').trim().toLowerCase()
                    === want.toLowerCase()) {
                    return i;
                }
            }
            return -1;
        }""",
        [portal_sel, target],
    )
    if matched_index < 0:
        # Dump what we did see, to help the operator fix the input.
        available = page.evaluate(
            """(portalSel) => Array.from(
                document.querySelectorAll(portalSel)
            ).map(r => (r.innerText || '').trim())""",
            portal_sel,
        )
        raise RuntimeError(
            f"{combo_id} has no row matching {target!r}. "
            f"Available: {available}"
        )

    try:
        page.locator(portal_sel).nth(matched_index).click(timeout=_CLICK_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"{combo_id}: found row {target!r} at index {matched_index} "
            f"but Playwright click failed: {exc}"
        ) from exc

    # 4. Confirm the value stuck.
    page.wait_for_timeout(250)
    final = _current_combo_value(page, combo_sel)
    if final.casefold() != target.casefold():
        _log.warning(
            "combo %s click succeeded but value is %r (expected %r)",
            combo_id, final, target,
        )


def _fill_criteria(page: Page, value: str) -> None:
    """Set ``streCriteria1`` to *value* via the native input setter.

    Uses the same focus-fire-input/change pattern as ``step_mms_create._fill_text``
    so Angular's reactive forms see the change. ``.fill()`` alone sometimes
    leaves the bound value stale on EPIC inputs.
    """
    sel = f"{_CRITERIA_SEL} input"
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) throw new Error('criteria input not found: ' + sel);
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        [sel, value],
    )
    _log.debug("filled streCriteria1 = %r", value)


def run(
    page: Page,
    *,
    search_term: str | None = None,
    search_by: str = "Lookup Code",
    lookup_code: str | None = None,  # deprecated alias for search_term
) -> bool:
    """Search the Account Locate screen for *search_term*.

    Parameters
    ----------
    search_term
        Value to fill into the criteria input. Required.
    search_by
        Label of the "Locate by" dropdown option to use — e.g. ``"Lookup Code"``
        (default) or ``"Account/Business Name"``. Matched by visible text in the
        ``cboLocateBy`` portal rows.
    lookup_code
        Deprecated alias for ``search_term``. If provided and ``search_term`` is
        ``None``, it is used in place. Kept for backwards-compat with the
        original lookup-only API.

    Pre-condition: the browser is on the Account Locate screen.

    Returns True when the Locate button was clicked successfully.
    Returns False if a pre-condition was missing or a step failed.
    """
    # Backwards-compat: accept the old lookup_code= kwarg.
    if search_term is None and lookup_code is not None:
        search_term = lookup_code
    term = (search_term or "").strip()
    if not term:
        _log.error("step_account_search.run requires a non-empty search_term")
        return False

    by = (search_by or "").strip()
    if not by:
        _log.error("step_account_search.run requires a non-empty search_by")
        return False

    checkpoint(page, f"Account: search by {by!r} for {term!r}")

    # 0. Pre-condition: Account Locate screen must be open. Probe via page
    # title + marker presence — ``is_visible()`` on the AccountLocate wrapper
    # is unreliable because the ``<program>`` element has 0 px dimensions
    # itself (see step_account_lookup_nav for the same workaround).
    try:
        title_ok = (page.title() or "").strip().lower() == "account locate"
        marker_ok = page.locator(_SCREEN_SEL).count() > 0
        if not (title_ok or marker_ok):
            _log.error(
                "Account Locate screen is not open. Run "
                "step_account_lookup_nav before step_account_search."
            )
            return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Could not verify Account Locate screen state: %s", exc)
        return False

    try:
        # 1. Locate account → "Client"
        _select_combo_value(page, _ACCOUNT_TYPE_SEL, "Client")

        # 2. Locate by → search_by (e.g. "Lookup Code" or "Account/Business Name")
        _select_combo_value(page, _LOCATE_BY_SEL, by)

        # 3. Fill the criteria input.
        page.locator(_CRITERIA_SEL).first.wait_for(
            state="visible", timeout=_LOAD_TIMEOUT,
        )
        _fill_criteria(page, term)

        # 4. Click Locate.
        btn = page.locator(_BTN_LOCATE_SEL)
        btn.first.wait_for(state="visible", timeout=_CLICK_TIMEOUT)
        btn.first.click(timeout=_CLICK_TIMEOUT)

        _log.info("Account search submitted: %s = %r", by, term)
        page.wait_for_timeout(_FINAL_SETTLE_MS)
        return True

    except EntryCancelled:
        raise
    except PlaywrightTimeout as exc:
        _log.error("Account search timed out: %s", exc)
        return False
    except RuntimeError as exc:
        _log.error("Account search failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_account_search: %s", exc)
        return False

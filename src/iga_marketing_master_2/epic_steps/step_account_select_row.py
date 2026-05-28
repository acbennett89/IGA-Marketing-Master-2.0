"""step_account_select_row.py — highlight the result row matching a lookup code.

Pre-condition
-------------
The browser is on the EPIC Account Locate screen and a search has either
already been submitted (results may be present) or no results have rendered
yet (the step will wait briefly for the results grid to populate). Call
:mod:`step_account_search` first to fire the search.

What this step does
-------------------
1.  Waits up to ``settle_timeout_ms`` for the results grid to settle.
2.  Scans visible result rows (``[data-automation-id^="vlvwResults body-row"]``)
    and matches the row whose first cell (Lookup Code column) equals
    *lookup_code* exactly (case-insensitive).
3.  If found and the row is not already highlighted, single-clicks it so the
    EPIC sidebar acts on this account.
4.  Returns a :class:`SelectRowResult` describing the outcome.

The step is intentionally headless — it does **not** open the account, click
sidebar items, or surface popups. Composing it with :mod:`step_account_sidebar_nav`
is the caller's job (and lets the same primitives drive other flows: open
Quotes, Activities, Contacts, etc.).

Selector notes (confirmed via live CDP inspection 2026-05-22)
-------------------------------------------------------------
- Results grid:   ``[data-automation-id="fraResults"]``
- Result rows:    ``div[data-automation-id^="vlvwResults body-row"]`` —
                  e.g. ``vlvwResults body-row item-0``.
- Row text layout (one match, first line is Lookup Code, second is Account
  Name)::

      GORBILL-01\nBill Goran Widgets LLC\nInsured\nActive\n6640 Carothers Pkwy\n...

  We read the first non-empty line as the lookup code and the second as the
  account name. The first ``.body-cell.first`` child is the visually-first
  column and used as a fallback if the row text is empty.
- Highlight state: the row gains class ``highlighted`` when selected. EPIC
  auto-highlights the only row when the search returns a single match — the
  click is then a no-op (idempotent).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from enum import Enum

from playwright.sync_api import Page

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.account_select_row")

_SCREEN_SEL  = '[data-automation-id="AccountLocate"]'
_RESULTS_SEL = '[data-automation-id="fraResults"]'
_ROW_SEL     = 'div[data-automation-id^="vlvwResults body-row"]'

_DEFAULT_SETTLE_MS = 8_000
_CLICK_TIMEOUT     = 5_000
_FINAL_SETTLE_MS   = 1_000  # tail pause so the next step doesn't race EPIC


class SelectRowOutcome(Enum):
    """Outcome of a row-selection attempt."""

    SELECTED  = "selected"
    """A row matching *lookup_code* was found and highlighted."""

    NOT_FOUND = "not_found"
    """The results grid rendered (or the wait elapsed) and no row matched."""

    ERROR     = "error"
    """A pre-condition failed or the DOM was unexpectedly shaped."""


@dataclass(slots=True)
class SelectRowResult:
    """Structured outcome the caller can pattern-match on."""

    outcome: SelectRowOutcome
    matched_lookup_code: str | None = None
    """Echoed back from the row (preserves the casing EPIC stored)."""

    matched_account_name: str | None = None
    """Account/Business Name from the row, populated on SELECTED."""

    error: str | None = None
    """Free-text reason populated only when outcome is ERROR."""


def _read_row_summary(page: Page, row_locator) -> tuple[str, str]:
    """Return ``(lookup_code, account_name)`` from a result row, or ``("","")``.

    Reads ``innerText`` and splits on lines — fall back to ``""`` if the row
    is empty or hidden. Done via a single ``evaluate`` so we don't pay the
    locator round-trip per cell.
    """
    try:
        text = row_locator.evaluate("el => (el.innerText || '').trim()")
    except Exception:  # noqa: BLE001
        return "", ""
    if not text:
        return "", ""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    code = lines[0] if lines else ""
    name = lines[1] if len(lines) > 1 else ""
    return code, name


def run(
    page: Page,
    *,
    lookup_code: str,
    settle_timeout_ms: int = _DEFAULT_SETTLE_MS,
) -> SelectRowResult:
    """Find and highlight the row matching *lookup_code*.

    The step waits up to *settle_timeout_ms* for any row to appear before
    declaring NOT_FOUND. This gives EPIC time to render search results after
    :mod:`step_account_search` fires.
    """
    code = (lookup_code or "").strip()
    if not code:
        return SelectRowResult(
            outcome=SelectRowOutcome.ERROR,
            error="lookup_code is empty",
        )

    checkpoint(page, f"Account: select row for lookup code {code!r}")

    # 0. Pre-condition: Account Locate screen must be open. Probe via page
    # title + marker presence — ``is_visible()`` is unreliable on the
    # ``<program>`` wrapper element (see step_account_lookup_nav).
    try:
        title_ok = (page.title() or "").strip().lower() == "account locate"
        marker_ok = page.locator(_SCREEN_SEL).count() > 0
        if not (title_ok or marker_ok):
            return SelectRowResult(
                outcome=SelectRowOutcome.ERROR,
                error=("Account Locate screen is not open. Run "
                       "step_account_lookup_nav + step_account_search first."),
            )
    except Exception as exc:  # noqa: BLE001
        return SelectRowResult(
            outcome=SelectRowOutcome.ERROR,
            error=f"Could not verify Account Locate screen: {exc}",
        )

    # 1. Wait for the results grid to settle. We don't know the exact moment
    # EPIC finishes the search, so poll the row count until it's nonzero or
    # *settle_timeout_ms* elapses. A 0-row outcome after the timeout means
    # NOT_FOUND, not ERROR.
    rows = page.locator(_ROW_SEL)
    deadline = time.time() + settle_timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if rows.count() > 0:
                break
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)

    # 2. Scan rows for an exact (case-insensitive) lookup-code match.
    try:
        n_rows = rows.count()
    except Exception as exc:  # noqa: BLE001
        return SelectRowResult(
            outcome=SelectRowOutcome.ERROR,
            error=f"Could not enumerate result rows: {exc}",
        )

    if n_rows == 0:
        _log.info("No result rows for lookup_code=%r", code)
        return SelectRowResult(outcome=SelectRowOutcome.NOT_FOUND)

    matched_index: int | None = None
    matched_code = matched_name = ""
    for i in range(n_rows):
        row = rows.nth(i)
        row_code, row_name = _read_row_summary(page, row)
        if row_code.casefold() == code.casefold():
            matched_index = i
            matched_code = row_code
            matched_name = row_name
            break

    if matched_index is None:
        _log.info("No row matched lookup_code=%r among %d result(s)", code, n_rows)
        return SelectRowResult(outcome=SelectRowOutcome.NOT_FOUND)

    matched_row = rows.nth(matched_index)

    # 3. Idempotent highlight: skip the click if the row already has it.
    try:
        already = matched_row.evaluate(
            "el => el.classList.contains('highlighted')",
        )
    except Exception:  # noqa: BLE001
        already = False

    if already:
        _log.debug("Row %r already highlighted — no click needed", matched_code)
    else:
        try:
            matched_row.click(timeout=_CLICK_TIMEOUT)
            page.wait_for_timeout(200)
        except Exception as exc:  # noqa: BLE001
            return SelectRowResult(
                outcome=SelectRowOutcome.ERROR,
                error=f"Found row {matched_code!r} but failed to click it: {exc}",
                matched_lookup_code=matched_code,
                matched_account_name=matched_name,
            )

    _log.info("Selected account: %s — %s", matched_code, matched_name)
    page.wait_for_timeout(_FINAL_SETTLE_MS)
    return SelectRowResult(
        outcome=SelectRowOutcome.SELECTED,
        matched_lookup_code=matched_code,
        matched_account_name=matched_name,
    )

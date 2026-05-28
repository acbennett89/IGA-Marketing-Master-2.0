"""step_mms_create.py — create a Master Marketing Submission (MMS) in EPIC.

Pre-condition
-------------
The operator has navigated to Policies > Add a Master Marketing Submission so that
the MKADMSTR creation form is open and visible on the page.

What this step does
-------------------
1.  Fills the MMS header: Name, Effective, Expiration, Agency (verify), Branch,
    Department.
2.  Sets Type of business = "Commercial Lines" in the Policies to Market section.
3.  For each LineSpec in *lines*, clicks the Add icon-button to open the MMKADNEW
    dialog, picks "Add new line", fills Line code (every row), Profit center / Line
    status / Issuing location (first row only — EPIC auto-propagates to subsequent
    rows), then clicks Finish (last line) or Add (all others).
4.  Clicks Finish on the outer MKADMSTR form to save.

Selector notes (confirmed via live CDP inspection 2026-05-18 + v1 reference)
-----------------------------------------------------------------------------
- Screen code  : MKADMSTR  (creation form), MMKADNEW  (Add New Line dialog)
- MKADMSTR Structure section has Agency, Branch, Department — NO Profit Center.
- MMKADNEW lives inside a ``modal-screen`` Angular element. All field fills and
  action buttons inside it are scoped to ``modal-screen`` (the innermost one) to
  avoid matching identically-named elements on the outer MKADMSTR form.
- cboLine is a 44-entry virtualized list rendered as a portal. Typing into its
  input via .fill() triggers Angular's filter; matching rows appear page-wide as
  ``div[data-automation-id^="cboLine body-row"]`` elements. No manual scroll needed.
- The universal _fill_combo() handles all combos — code-keyed (e.g. "GLIA"),
  description-keyed (e.g. "Commercial Lines"), and virtual-list — identically.
  It opens the drop-down, fills the input, polls for portal rows, and clicks the
  matching row using a Playwright locator with has_text filtering.
- Readonly inputs (EPIC marks some) get keyboard.type(delay=40) instead of .fill().
- "Add new line" radio: ``input[value="rbtnNew"]``
- Continue button: ``[data-test="btnContinue"]``  (uses data-test, not data-automation-id)
- Add/Finish in MMKADNEW: ``[data-automation-id="btnAdd"]`` / ``[data-automation-id="btnFinish"]``
- Profit center, Line status, Issuing location auto-populate from the first line;
  only filled on is_first=True rows.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

_log = logging.getLogger("iga.epic_steps.mms_create")

# ── Timing constants ────────────────────────────────────────────────────────
_DETECT_MS     = 5_000   # wait for MKADMSTR form to be visible
_FILL_TIMEOUT  = 8_000   # per-field combo fill / portal-rows poll (ms)
_CLICK_TIMEOUT = 5_000   # button clicks
_SAVE_WAIT_MS  = 10_000  # wait for MKADMSTR to disappear after Finish


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass
class LineSpec:
    """One policy line to add inside the MMKADNEW dialog.

    line_code:    LOB code from cboLine (e.g. ``"GLIA"``, ``"BAUT"``, ``"WCOM"``).
    profit_center: Code for cboProfitCenter. Default ``"MM"`` (Middle Market).
    line_status:  Code for cboLineStatus. ``"AOR"`` = First Year AOR.
    issuing_state: Two-letter state code for cboIssuingLocation, e.g. ``"TN"``.
    """

    line_code: str
    profit_center: str = "MM"
    line_status: str = "New"
    issuing_state: str = "TN"


@dataclass
class MmsSetup:
    """Header data for the MKADMSTR creation form.

    name:       MMS name, e.g. ``"2025 New Business MMS - Acme Corp"``.
    effective:  Effective date as ``"M/D/YYYY"``.
    expiration: Expiration date as ``"M/D/YYYY"``.
    agency:     Agency code — ``"IGA"``, ``"IEB"``, or ``"VA"``.
    branch:     Branch code, e.g. ``"002"`` (Franklin TN).
    department: Department code — always ``"CL"`` for IGA.
    lines:      LineSpec objects to add in the Policies to Market grid.
    """

    name: str
    effective: str
    expiration: str
    agency: str = "IGA"
    branch: str = "002"
    department: str = "CL"
    lines: list[LineSpec] = field(default_factory=list)


# ── Private helpers ──────────────────────────────────────────────────────────

def _fill_text(page: Page, automation_id: str, value: str) -> None:
    """Set *value* on an asi-string-edit or asi-date-edit input via nativeInputSetter."""
    sel = f'[data-automation-id="{automation_id}"] input'
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) throw new Error('field not found: ' + sel);
            el.focus();
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        [sel, value],
    )
    _log.debug("filled text %s = %r", automation_id, value)


def _fill_combo(
    page: Page,
    automation_id: str,
    value: str,
    *,
    ctx: "Page | Any | None" = None,
    timeout_ms: int = _FILL_TIMEOUT,
) -> None:
    """Fill any EPIC ASI combo — code-keyed, description-keyed, or virtual list.

    Strategy (mirrors v1 marketing_application._fill_combo):
      1. Open the drop-down via the ``.drop-btn`` toggle.
      2. Triple-click the input to select all, then .fill(value).
         Readonly inputs fall back to keyboard.type(delay=40).
      3. Poll the full *page* for portal-rendered rows:
           ``div[data-automation-id^="{id} body-row"]``
         or static option boxes inside the combo container.
      4. Click the exact-text match; fall back to first row if none.

    ``ctx`` scopes the input/drop-btn locator to a container (e.g. a
    ``modal-screen``).  Portal rows are always searched on the full page.
    """
    scope = ctx if ctx is not None else page

    drop_sel   = f'[data-automation-id="{automation_id}"] .drop-btn'
    input_sel  = f'[data-automation-id="{automation_id}"] input'
    portal_sel = f'div[data-automation-id^="{automation_id} body-row"]'
    option_sel = f'[data-automation-id="{automation_id}"] .options-box .option'

    # 1. Open the dropdown.
    try:
        drop = scope.locator(drop_sel)
        if drop.count() > 0 and drop.first.is_visible():
            drop.first.click(timeout=1_500)
            page.wait_for_timeout(400)
    except Exception:
        pass

    # 2. Fill the input.
    try:
        inp = scope.locator(input_sel)
        inp.first.click(timeout=1_500)
        inp.first.click(click_count=3, timeout=1_500)
        is_readonly = inp.first.get_attribute("readonly") is not None
        if is_readonly:
            page.keyboard.type(value, delay=40)
        else:
            inp.first.fill(value, timeout=1_500)
        page.wait_for_timeout(350)
    except Exception as exc:
        raise RuntimeError(
            f"Could not interact with combo '{automation_id}': {exc}"
        ) from exc

    # 3. Poll for portal rows or static options.
    options_appeared = False
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        if page.locator(portal_sel).count() > 0:
            options_appeared = True
            break
        if page.locator(option_sel).count() > 0:
            options_appeared = True
            break
        page.wait_for_timeout(150)

    if not options_appeared:
        _log.warning("combo %s: no dropdown rows appeared for %r", automation_id, value)
        return

    page.wait_for_timeout(150)

    # 4. Click best match.
    try:
        rows = page.locator(portal_sel)
        if rows.count() > 0:
            exact = rows.filter(has=page.locator("span.text", has_text=value))
            target = exact if exact.count() > 0 else rows
            target.first.click(timeout=1_500)
            page.wait_for_timeout(400)
            _log.debug("combo %s = %r (portal rows)", automation_id, value)
            return
    except Exception:
        pass

    try:
        options = page.locator(option_sel)
        if options.count() > 0:
            exact = options.filter(has_text=value)
            target = exact if exact.count() > 0 else options
            target.first.click(timeout=1_500)
            page.wait_for_timeout(400)
            _log.debug("combo %s = %r (option box)", automation_id, value)
    except Exception:
        pass


def _innermost_modal(page: Page):
    """Return the last (innermost) open ``modal-screen`` locator, or *page* if none."""
    try:
        modals = page.locator("modal-screen")
        if modals.count() > 0:
            return modals.last
    except Exception:
        pass
    return page


def _wait_visible(page: Page, locator, timeout_ms: int) -> bool:
    """Poll *locator* until visible or timeout. Returns True if it appeared."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if locator.count() > 0 and locator.first.is_visible():
                return True
        except Exception:
            pass
        page.wait_for_timeout(150)
    return False


# ── Public API ───────────────────────────────────────────────────────────────

def _open_add_submission(page: Page) -> None:
    """Click the Add icon on the Policies-Marketed screen and wait for MKADMSTR.

    Raises RuntimeError if the Policies-Marketed screen is not visible or the
    MKADMSTR creation form does not appear after clicking Add.
    """
    marketed = page.locator('[data-automation-id="fraMasterMarketingSubmissions"]')
    if not _wait_visible(page, marketed, 10_000):
        raise RuntimeError(
            "Policies-Marketed screen not visible. "
            "Navigate to the target account → Policies → Marketed first."
        )

    add_btn = page.locator(
        '[data-automation-id="vlvwMasterSubmissions"] .icon-button[title="Add"]'
    )
    if not _wait_visible(page, add_btn, 5_000):
        raise RuntimeError(
            "Policies-Marketed screen visible but the Add button was not found."
        )
    add_btn.first.click(timeout=_CLICK_TIMEOUT)

    form = page.locator('[data-automation-id="fraMasterMarketingSubmission"]')
    if not _wait_visible(page, form, 20_000):
        raise RuntimeError(
            "Clicked Add on Policies-Marketed but the MKADMSTR form did not open."
        )


def run(page: Page, setup: MmsSetup) -> bool:
    """Create a Master Marketing Submission from *setup*.

    Pre-condition: the browser is on the Policies-Marketed screen for the target
    account (``fraMasterMarketingSubmissions`` is visible).  This function clicks
    the Add button, waits for MKADMSTR to open, then fills and saves the form.
    Returns True on success, False on any unrecoverable error.
    """
    try:
        _open_add_submission(page)
    except Exception as exc:
        _log.error("Could not open Add MMS form: %s", exc)
        return False

    _log.info("MKADMSTR form open — filling header")

    try:
        # ── Name ─────────────────────────────────────────────────────────────
        _fill_text(page, "streName", setup.name)

        # ── Effective / Expiration dates ─────────────────────────────────────
        if setup.effective:
            _fill_text(page, "dteEffective", setup.effective)
        if setup.expiration:
            _fill_text(page, "dteExpiration", setup.expiration)

        # ── Structure: Agency, Branch, Department ────────────────────────────
        _fill_combo(page, "cboAgency", setup.agency)
        _fill_combo(page, "cboBranch", setup.branch)
        _fill_combo(page, "cboDepartment", setup.department)

        # ── Type of business ─────────────────────────────────────────────────
        _fill_combo(page, "cboTypeOfBusiness", "Commercial Lines")

        _log.info("Header filled — adding %d policy line(s)", len(setup.lines))

        # ── Add policy lines ─────────────────────────────────────────────────
        # Only open the Add Line dialog if we have lines to add.  Opening it
        # with 0 lines leaves the MMKADNEW modal open, which blocks all subsequent
        # clicks (including btnDetail) via its modal-screen-wrap overlay.
        if setup.lines:
            _click_add_line_btn(page)
            _handle_add_line_picker(page)

            if not _wait_visible(page, page.locator('[data-automation-id="fraDetail"]'), 15_000):
                raise RuntimeError(
                    "Add Line picker handled but MMKADNEW form did not appear."
                )

            for i, line in enumerate(setup.lines):
                _fill_add_new_line_row(
                    page, line,
                    is_first=(i == 0),
                    is_last=(i == len(setup.lines) - 1),
                )
                if i < len(setup.lines) - 1:
                    page.wait_for_timeout(500)

            # Brief pause after the last line is added so EPIC can settle before
            # the outer Finish click commits the whole submission.
            page.wait_for_timeout(1_500)
        else:
            _log.warning(
                "No policy lines in setup — skipping Add New Line dialog. "
                "The MMS will be saved with no lines."
            )

        # ── Safety net: dismiss any open modal before clicking Detail ─────────
        # If MMKADNEW is still open (e.g. from a failed previous attempt or any
        # other modal), its modal-screen-wrap will intercept the Detail click.
        try:
            modal_cancel = page.evaluate("""() => {
                const modal = document.querySelector('modal-screen');
                if (!modal) return false;
                const cancel = Array.from(modal.querySelectorAll('button'))
                    .find(b => b.textContent.trim() === 'Cancel');
                if (cancel) { cancel.click(); return true; }
                return false;
            }""")
            if modal_cancel:
                _log.warning("Dismissed an open modal before clicking Detail")
                page.wait_for_timeout(500)
        except Exception:
            pass

        # ── Open Submission Detail ────────────────────────────────────────────
        _log.info("Clicking Detail to open Submission Detail sidebar")
        detail = page.locator('[data-automation-id="btnDetail"]')
        if not _wait_visible(page, detail, 5_000):
            _log.warning("Detail button not found on MKADMSTR form")
            return False

        # Use Playwright's real click (fires Angular event handlers).
        # The modal safety net above ensures no overlay blocks it.
        detail.first.click(timeout=_CLICK_TIMEOUT)

        # Confirm the Submission Detail sidebar appeared.
        detail_sidebar = page.locator('[data-automation-id*="MasterMarketingSubmissionDetail"]')
        if _wait_visible(page, detail_sidebar, _SAVE_WAIT_MS):
            _log.info("MMS created and Detail open: %r", setup.name)
            return True

        _log.error(
            "Clicked Detail but Submission Detail sidebar did not appear. "
            "Browser may still be on MKADMSTR form."
        )
        return False

    except PlaywrightTimeout as exc:
        _log.error("MMS create timed out: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_mms_create: %s", exc)
        return False


def _click_add_line_btn(page: Page) -> None:
    """Click the Add icon-button inside the vlvwLines grid."""
    add_btn = page.locator('[data-automation-id="vlvwLines"] .icon-button[title="Add"]')
    if not _wait_visible(page, add_btn, 10_000):
        raise RuntimeError(
            "Add Master Marketing Submission form is open but the Lines table "
            "Add button was not found."
        )
    add_btn.first.click(timeout=_CLICK_TIMEOUT)


def _handle_add_line_picker(page: Page) -> None:
    """In the Add Line picker: select 'Add new line' radio and click Continue."""
    radio = page.locator('input[value="rbtnNew"]')
    if not _wait_visible(page, radio, 10_000):
        raise RuntimeError(
            "Add Line popup did not appear (could not find the 'Add new line' radio)."
        )
    try:
        radio.first.check(timeout=_CLICK_TIMEOUT)
    except Exception:
        radio.first.click(timeout=_CLICK_TIMEOUT)

    continue_btn = page.locator('[data-test="btnContinue"]')
    if not _wait_visible(page, continue_btn, 5_000):
        raise RuntimeError(
            "'Add new line' radio selected but the Continue button was not visible."
        )
    continue_btn.first.click(timeout=_CLICK_TIMEOUT)


def _fill_add_new_line_row(
    page: Page,
    line: LineSpec,
    *,
    is_first: bool,
    is_last: bool,
) -> None:
    """Fill one row in the MMKADNEW Add New Line form and click Add or Finish.

    Scoped to the innermost ``modal-screen`` element so fills don't accidentally
    hit identically-named fields on the outer MKADMSTR form.

    Profit center, Line status, and Issuing location are only filled on the first
    row — EPIC auto-propagates those values to subsequent rows.
    """
    _log.info(
        "Adding line: code=%r profit_center=%r status=%r state=%r",
        line.line_code, line.profit_center, line.line_status, line.issuing_state,
    )

    modal = _innermost_modal(page)

    # Line code — always filled on every row.
    _fill_combo(page, "cboLine", line.line_code, ctx=modal)

    if is_first:
        _fill_combo(page, "cboProfitCenter", line.profit_center, ctx=modal)
        _fill_combo(page, "cboLineStatus", line.line_status, ctx=modal)
        # cboIssuingLocation auto-fills from the branch state — no manual fill needed.

    # Add (more lines follow) or Finish (last line).
    btn_sel = (
        '[data-automation-id="btnFinish"]' if is_last
        else '[data-automation-id="btnAdd"]'
    )
    btn = modal.locator(btn_sel)
    if not _wait_visible(page, btn, 5_000):
        # Fall back to Finish if Add is not available.
        btn = modal.locator('[data-automation-id="btnFinish"]')
    btn.first.click(timeout=_CLICK_TIMEOUT)

    _log.info("Line %r submitted (action=%s)", line.line_code, "Finish" if is_last else "Add")

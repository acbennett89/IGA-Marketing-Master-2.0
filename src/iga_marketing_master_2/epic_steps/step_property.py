"""step_property.py — fill the Commercial Property (CP) screens inside a submission.

Pre-condition
-------------
The browser is on the Submission Detail sidebar tree for the target MMS, with the
Property subtree visible and a blank Property line already created by the user.

Sections filled (in order)
---------------------------
1. **Premises** (CHM-PRPREMSE)
   Opens the Location/Building Lookup dialog and selects all account locations.
   Loc 1 / Bldg 1 is typically pre-seeded from the account; this step adds all
   remaining buildings.

2. **Subjects** (CHM-PRSUBJCT)
   Adds each Subject of Insurance row: loc/bldg, subject type, description, amount,
   valuation, inflation guard, blanket, form number.  Cause of Loss is set per-subject.

3. **Additional Interests** (CHM-PRADLINT)
   Adds mortgage holders, loss payees, and other named interests.

4. **Forms & Endorsements** (PRFRMEND)
   Old Angular proxy pattern.  Adds each form by number/name lookup.

5. **Additional Coverages** (PRADDCOV)
   Old Angular proxy pattern (identical layout to GL's GLADDCOV).
   Adds each additional coverage row.

Skipped sections (per IGA spec)
--------------------------------
Business Income, Value Reporting, Remarks, Statement of Values,
Additional Attachments, Supplemental Screens, Document View.

Selector notes (confirmed via live CDP inspection 2026-05-19)
--------------------------------------------------------------
- Sidebar: ``data-automation-id^="sidebar-button-Policy.Property.{key}"`` prefix match.
- Premises Add: ``data-test="vlvwPremise_add"`` → Location/Building Lookup dialog;
  header checkbox selects all; Save = ``[data-test="btnFinish"]`` or ``text=Save``.
- Subjects Add: ``data-test="vlvwSubject_add"``; detail fields use React ``__textField``
  suffix pattern and ``data-test`` comboboxes.
- Additional Interests Add: ``data-test="vlvwInterest_add"``; same React pattern.
- F&E: old proxy; Add = ``[data-automation-id="fraFormsEnd"] .icon-button[title="Add"]``.
  Form number lookup field = ``streNumber``.
- Additional Coverages: old proxy; Add = ``[name="vlvwCoverage"] .icon-button[title="Add"]``.
  Field names differ from GL: ``streLimit1``/``streLimit2``, ``streDedType`` (not streDeductibleType).
  ``streDedType`` is readonly — EPIC auto-populates it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled, check_cancel
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

from iga_marketing_master_2.epic_steps.step_mms_create import (
    _fill_combo,
    _fill_text,
    _wait_visible,
)
from iga_marketing_master_2.epic_steps._page_health import (
    safe_action,
    assert_screen_code,
)
from iga_marketing_master_2.epic_steps._grid_helpers import (
    fill_field_verified,
    grid_row_count,
    wait_grid_grew,
    fill_validated_address_then_manual,
    abbreviate_if_too_long,
    AC_DESC_MAX_CHARS,
)

_log = logging.getLogger("iga.epic_steps.property")

_CLICK_TIMEOUT = 5_000
_NAV_WAIT_MS   = 1_200   # settle after sidebar navigation
_FILL_WAIT_MS  = 150     # brief pause after each field fill


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class SubjectSpec:
    """One Subject of Insurance row on CHM-PRSUBJCT (confirmed via CDP 2026-05-19).

    Field mapping:
      location_number → inteLocationNumber__textField
      building_number → inteBuildingNumber__textField
      subject_number  → inteSubject__textField  (auto-assigned; leave blank to accept EPIC default)
      subject_type    → cboSubject              (combo: "Building", "Personal Property of Insured", …)
      description     → streDescription
      amount          → deceAmount__textField
      valuation       → cboValuation1           (combo: "Replacement Cost", "ACV", …)
      inflation_guard → pereInflationGuard__textField  (e.g. "4" for 4%)
      form_number     → streFormNumber
      cause_of_loss   → vlvwCauseOfLoss_add dialog (combo inside dialog)
    """
    subject_type:    str        # required — "Building", "Personal Property of Insured", etc.
    location_number: str = "1"
    building_number: str = "1"
    subject_number:  str = ""  # leave blank; EPIC assigns
    description:     str = ""
    amount:          str = ""  # e.g. "227000" or "227,000"
    valuation:       str = ""  # e.g. "Replacement Cost", "ACV"
    inflation_guard: str = ""  # percent, e.g. "4"
    form_number:     str = ""
    cause_of_loss:   str = ""  # e.g. "Special", "Broad", "Basic"
    coinsurance:     str = ""  # e.g. "80" for 80% — entered in Cause of Loss dialog Coins % field
    deductible:      str = ""  # e.g. "2500" or "2,500" — entered in Cause of Loss dialog


@dataclass
class AdditionalInterestSpec:
    """One Additional Interest row on CHM-PRADLINT (confirmed via CDP 2026-05-19).

    Core fields:
      name            → streName
      interest_type   → cboInterest  (combo: "Mortgagee", "Loss Payee", "Additional Insured", …)
      street          → adePrimary-streetLine
      city            → adePrimary-city
      state           → adePrimary-state  (2-letter code)
      zip_code        → adePrimary-zipCode
      location_number → inteLocationNumber__textField
      building_number → inteBuildingNumber__textField
      subject_number  → inteSubjectNumber__textField
      loan_number     → streReferenceNumber
      rank            → inteRank__textField
    """
    name:            str
    interest_type:   str = ""   # "Mortgagee", "Loss Payee", etc.
    street:          str = ""
    city:            str = ""
    state:           str = ""
    zip_code:        str = ""
    location_number: str = ""
    building_number: str = ""
    subject_number:  str = ""
    loan_number:     str = ""
    rank:            str = ""


@dataclass
class PropertyFormSpec:
    """One Forms & Endorsements row on PRFRMEND (old Angular proxy pattern).

    Two lookup modes:
    - ``number`` set → fill ``streNumber`` and Tab; EPIC auto-fills the name.
    - ``number`` blank, ``name`` set → fill ``streName`` directly (for IGA
      standard forms that we add by name, e.g. "Property Extension Endorsement").
    Provide at least one of ``number`` or ``name``.
    """
    number: str = ""    # form number, e.g. "CP 00 10"
    name:   str = ""    # form name — used as fallback or when number is blank
    edition_date: str = ""  # MM/YYYY, e.g. "10-12"


@dataclass
class PropertyCoverageSpec:
    """One Additional Coverage row on PRADDCOV (old Angular proxy pattern).

    Field mapping (differs slightly from GL GLADDCOV):
      description → streDescription
      code        → streCode          (N/A if no specific code; required field)
      limit1      → streLimit1        (GL uses streLimit)
      limit2      → streLimit2        (aggregate)
      deductible  → streDeductible
      form_number → streFormNumber
      loc_number  → inteLocationNumber
      bldg_number → inteBuildingNumber
      subj_number → inteSubject

    streDedType (Deductible type) is readonly — EPIC auto-populates it.
    """
    description:     str
    code:            str = ""    # required by EPIC; defaults to "N/A"
    limit1:          str = ""    # each claim / per occurrence limit
    limit2:          str = ""    # aggregate limit
    deductible:      str = ""
    form_number:     str = ""
    location_number: str = ""
    building_number: str = ""
    subject_number:  str = ""


@dataclass
class PropertySetup:
    """Full input for the Property entry step.

    subjects             — subjects of insurance (CP Subjects screen)
    additional_interests — mortgage holders, loss payees, etc.
    forms                — forms & endorsements
    coverages            — additional coverages
    add_all_premises     — if True, open Location/Building Lookup and select all
                           available account locations (default True)
    """
    subjects:             list[SubjectSpec]           = field(default_factory=list)
    additional_interests: list[AdditionalInterestSpec] = field(default_factory=list)
    forms:                list[PropertyFormSpec]       = field(default_factory=list)
    coverages:            list[PropertyCoverageSpec]   = field(default_factory=list)
    add_all_premises:     bool                         = True


# ── IGA standard Property forms added on every submission ────────────────────

PROPERTY_STANDARD_FORMS: list[PropertyFormSpec] = [
    PropertyFormSpec(name="Property Extension Endorsement"),
]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _dismiss_modal(page: Page) -> bool:
    """Dismiss all open modals/dialogs that are blocking interactions.

    Loops until no more modals are found (up to 5 iterations).

    Strategy:
    1. Alert banners (``data-test="common-message-modal"``) → click OK button
    2. Dialogs with ``data-modal-id`` shroud → JS-click all close buttons
       (``[data-test$="-close-button"]`` or ``[class*="CloseButton_module"]``).
       Do NOT use Escape (doesn't work for EPIC dialogs) or Cancel (triggers
       validation which reopens a new alert immediately).

    Returns True if at least one modal was dismissed.
    """
    dismissed_any = False
    for _ in range(5):
        try:
            result = page.evaluate("""() => {
                // 1. Alert banners
                const alert = document.querySelector('[data-test="common-message-modal"]');
                if (alert) {
                    const btn = alert.querySelector('button');
                    if (btn) { btn.click(); return 'alert'; }
                }
                // 2. Modal dialogs — click all × close buttons via JS
                const hasShroud = !!document.querySelector('[data-modal-id]');
                if (hasShroud) {
                    const closeBtns = document.querySelectorAll(
                        '[data-test$="-close-button"], [class*="CloseButton_module_closeIcon"]'
                    );
                    if (closeBtns.length > 0) {
                        closeBtns.forEach(b => b.click());
                        return `dialog-close(${closeBtns.length})`;
                    }
                }
                return false;
            }""")
        except Exception:
            break
        if not result:
            break
        page.wait_for_timeout(400)
        _log.debug("_dismiss_modal: %s", result)
        dismissed_any = True
    return dismissed_any


# ── Validated 2026-05-28: pre-Add gate helpers for subject rows ────────────

def _install_dom_observer(page: Page) -> None:
    """Install a MutationObserver to timestamp DOM activity. Idempotent."""
    try:
        page.evaluate(
            """() => {
                if (window.__igaSubjMo) return;
                window.__igaSubjLastMut = Date.now();
                const mo = new MutationObserver(() => { window.__igaSubjLastMut = Date.now(); });
                mo.observe(document.documentElement, {
                    childList: true, subtree: true, attributes: true, characterData: true,
                });
                window.__igaSubjMo = mo;
            }"""
        )
    except Exception:
        pass


def _check_popups(page: Page, *, where: str = "") -> int:
    """Dismiss every visible ``common-message-modal`` validation alert.

    Targets EPIC's "Required information is missing" / confirmation-style
    alerts only — does NOT close ``data-modal-id`` dialogs (CoL etc.).
    Call this before AND after every click/fill on the Subjects screen:
    a single missed alert pulls up a shroud that intercepts every
    subsequent pointer click for ~5 s, cascading into Playwright
    pointer-events failures and silently dropping form data.

    Returns count dismissed.
    """
    dismissed = 0
    for _ in range(5):
        try:
            n = page.evaluate(
                """() => {
                    const alerts = document.querySelectorAll('[data-test="common-message-modal"]');
                    let count = 0;
                    for (const a of alerts) {
                        if (!(a.innerText || '').trim()) continue;
                        count++;
                        const btn = a.querySelector('[data-test="dialog-ok-btn"]')
                            || a.querySelector('button[data-test$="-close-button"]')
                            || a.querySelector('button');
                        if (btn) btn.click();
                    }
                    return count;
                }"""
            )
        except Exception:
            break
        if not n:
            break
        if where:
            _log.warning("popup dismissed at %s (%d)", where, n)
        page.wait_for_timeout(300)
        dismissed += n
    return dismissed


def _read_subject_count(page: Page) -> int:
    """Return the integer at the front of the Subjects footer (`'N Items'`)."""
    try:
        text = page.evaluate(
            "() => { const f = document.querySelector('[data-test=\"vlvwSubject-footer\"]');"
            " return f ? f.innerText.trim() : ''; }"
        )
        return int(text.split(" ")[0]) if text else 0
    except Exception:
        return 0


def _wait_pre_add_gate(page: Page, *, quiet_ms: int = 600, timeout_ms: int = 8_000) -> bool:
    """Block until the Subjects grid is ready for a clean Add click.

    Gate conditions (all required):
    - ``vlvwSubject-footer`` text ends with ``"Items"`` (grid finished rendering)
    - ``vlvwSubject_add`` is present + visible + not ``disabled`` + ``aria-disabled != "true"``
    - DOM mutations have been quiet for ``quiet_ms`` (default 600 ms)

    Returns True when satisfied; False on timeout. Popups are drained
    continuously while waiting — see :func:`_check_popups`.
    """
    _install_dom_observer(page)
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        _check_popups(page, where="pre-add-gate")
        try:
            st = page.evaluate(
                """() => {
                    const footer = document.querySelector('[data-test="vlvwSubject-footer"]');
                    const addBtn = document.querySelector('[data-test="vlvwSubject_add"]');
                    return {
                        footerText: footer ? footer.innerText.trim() : null,
                        addOk: addBtn ? (!addBtn.disabled
                            && addBtn.offsetParent !== null
                            && addBtn.getAttribute('aria-disabled') !== 'true') : false,
                    };
                }"""
            )
        except Exception:
            page.wait_for_timeout(100)
            continue
        footer_ok = bool(st.get("footerText")) and st["footerText"].endswith("Items")
        if footer_ok and st.get("addOk"):
            try:
                idle = int(page.evaluate("() => Date.now() - (window.__igaSubjLastMut || 0)"))
            except Exception:
                idle = 0
            if idle >= quiet_ms:
                _log.debug("Subjects pre-Add gate satisfied: footer=%r idle=%dms", st["footerText"], idle)
                return True
        page.wait_for_timeout(100)
    _log.warning("Subjects pre-Add gate timed out after %dms", timeout_ms)
    return False


def _wait_for_subject_form_active(page: Page, timeout_ms: int = 5_000) -> bool:
    """Poll ``#inteLocationNumber__textField`` until it is enabled.

    This is the canonical "subject form ready" signal — when this field is
    enabled, all other subject fields will also be editable.
    Returns True if the form activated within *timeout_ms*, False otherwise.
    """
    deadline = time.time() + timeout_ms / 1000
    while time.time() < deadline:
        try:
            loc = page.locator('#inteLocationNumber__textField').first
            if loc.is_enabled(timeout=200):
                return True
        except Exception:
            pass
        page.wait_for_timeout(150)
    return False


def _activate_subject_row(page: Page) -> bool:
    """Wait for screen settle, click Add Subject once, verify form is editable.

    Validated 2026-05-28: pre-Add gate (footer ``'X Items'`` + Add enabled +
    DOM-mutation-quiet 600 ms) → single click → form-active poll →
    **750 ms post-activate settle** + popup check.

    The 750 ms hold is critical: ``#inteLocationNumber__textField`` becomes
    enabled within a few ms of the Add click, but EPIC's backend hasn't
    finished wiring up the inline form's field handlers. Without the hold,
    the very next field fill landed in a stale form and Row 2+ silently
    dropped all data (manifested as "Required information is missing"
    validation popups dismissed mid-flow and an empty placeholder ``-``
    row in the grid). With the hold, every row lands first try.

    Replaces the prior 4-attempt retry + Premises round-trip recovery —
    those are no longer needed when the gate + settle are honored.
    """
    if not _wait_pre_add_gate(page):
        _log.error("Subject Add: pre-Add gate timed out")
        return False
    _check_popups(page, where="pre-add-click")
    try:
        page.locator('[data-test="vlvwSubject_add"]').first.click(timeout=_CLICK_TIMEOUT)
    except Exception as exc:
        _log.error("Subject Add: click failed: %s", exc)
        _check_popups(page, where="add-click-fail")
        return False
    _check_popups(page, where="post-add-click")
    if not _wait_for_subject_form_active(page, timeout_ms=5_000):
        _log.error("Subject Add: form did not activate after click")
        _check_popups(page, where="form-inactive")
        return False
    # CRITICAL settle — see docstring.
    page.wait_for_timeout(750)
    _check_popups(page, where="post-activate-settle")
    return True


def _nav_to_property(page: Page, max_attempts: int = 4) -> bool:
    """Click the Property entry in the submission sidebar (level-3).

    Retries up to max_attempts times — the sidebar can render slowly after
    the submission detail page loads.  Returns True once clicked.
    """
    btn = page.locator('[data-automation-id*="level-3"]').filter(has_text="Property")
    for attempt in range(1, max_attempts + 1):
        if _wait_visible(page, btn, 3_000):
            btn.first.click(timeout=_CLICK_TIMEOUT)
            page.wait_for_timeout(_NAV_WAIT_MS)
            _log.debug("Clicked Property sidebar parent (attempt %d/%d)", attempt, max_attempts)
            return True
        _log.warning(
            "Property sidebar entry not visible — attempt %d/%d; waiting 2 s",
            attempt, max_attempts,
        )
        page.wait_for_timeout(2_000)
    _log.error("Property sidebar entry not found after %d attempts", max_attempts)
    return False


def _nav_section(page: Page, section_key: str, screen_code: str) -> bool:
    """Navigate to a Property sub-section via its sidebar link.

    Uses prefix-match on ``sidebar-button-Policy.Property.{section_key}``.
    Confirms arrival by checking for *screen_code* in the page text.
    """
    _dismiss_modal(page)  # clear any blocking alert before navigating
    sidebar_sel = f'[data-automation-id^="sidebar-button-Policy.Property.{section_key}"]'
    btn = page.locator(sidebar_sel)
    if not _wait_visible(page, btn, 5_000):
        _log.error("Property sidebar link for %s not found", section_key)
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    # Confirm screen code appears in the page text or status bar.
    # Old proxy screens (PRFRMEND, PRADDCOV) show code only in the status bar,
    # NOT in document.body.innerText — check both locations.
    try:
        page.wait_for_function(
            f"""() => {{
                const text = document.body.innerText;
                if (text.includes("{screen_code}")) return true;
                // Also check status bar at bottom (old proxy screens)
                const bars = document.querySelectorAll('[class*="statusBar"], [class*="status-bar"], .status-bar');
                for (const b of bars) {{ if (b.textContent.includes("{screen_code}")) return true; }}
                // Check last item in the last footer-like element
                const allEls = document.querySelectorAll('[data-automation-id="{screen_code}"]');
                return allEls.length > 0;
            }}""",
            timeout=8_000,
        )
        _log.debug("Navigated to Property.%s (%s) OK", section_key, screen_code)
        return True
    except PlaywrightTimeout:
        _log.warning("Property.%s navigated but screen code %s not confirmed", section_key, screen_code)
        return True  # best-effort; continue anyway


def _fill_proxy_field(page: Page, field_id: str, value: str) -> None:
    """Fill an old-format proxy field by its data-automation-id."""
    if not value:
        _log.debug("fill_proxy %s: skipped (blank)", field_id)
        return
    try:
        inp = page.locator(f'[data-automation-id="{field_id}"] input')
        inp.first.click(timeout=2_000)
        inp.first.fill(value, timeout=2_000)
        page.wait_for_timeout(_FILL_WAIT_MS)
        _log.debug("fill_proxy %s = %r  OK", field_id, value)
    except Exception as exc:
        _log.warning("fill_proxy %s = %r  FAILED: %s", field_id, value, exc)


def _fill_id_field(page: Page, field_id: str, value: str) -> None:
    """Fill a React form field by its HTML id (``__textField`` suffix pattern)."""
    if not value:
        _log.debug("fill_id #%s: skipped (blank)", field_id)
        return
    try:
        inp = page.locator(f'#{field_id}')
        inp.first.click(timeout=2_000)
        inp.first.fill(value, timeout=2_000)
        page.wait_for_timeout(_FILL_WAIT_MS)
        _log.debug("fill_id #%s = %r  OK", field_id, value)
    except Exception as exc:
        _log.warning("fill_id #%s = %r  FAILED: %s", field_id, value, exc)


def _fill_react_combo(page: Page, combo_id: str, value: str) -> None:
    """Fill a React combobox field by its data-test id.

    Matching strategy (best-first):
    1. Exact full text match  (row text stripped == value)
    2. Exact code match       (uppercase prefix == value, e.g. "P" for "PPercent")
    3. Exact label match      (text after the code prefix == value, e.g. "BBuilding" → "Building")
    4. Contains match         (value appears anywhere in row text)
    5. First available row    (fallback)

    This prevents "Building" from matching "Additional Flood Building" (AFBPP)
    before the exact "Building" (B) option, and prevents single-letter code
    searches like "P" (Percent) from being captured by "OCPer Occurrence" via
    the contains-anywhere fallback.
    """
    if not value:
        _log.debug("fill_combo %s: skipped (blank)", combo_id)
        return
    try:
        inp = page.locator(f'[data-test="{combo_id}-combobox-input"]')
        inp.first.click(timeout=2_000)
        inp.first.fill(value, timeout=2_000)
        page.wait_for_timeout(700)  # allow EPIC's virtualised dropdown to render
        rows = page.locator('[data-test="dropdown-row"]')
        count = min(rows.count(), 20)
        val_lower = value.lower()

        import re as _re
        # Non-greedy: match shortest uppercase/digit prefix before a TitleCase word.
        # "BBuilding" → code="B", label="Building"
        # "BPPBusiness Personal Property" → code="BPP", label="Business Personal Property"
        # "ABLDPAdditional Building Property" → code="ABLPD", label="Additional Building Property"
        _label_re = _re.compile(r'^[A-Z0-9]+?([A-Z][a-z].*)')

        best_idx = None
        best_priority = 99

        for n in range(count):
            rt = (rows.nth(n).text_content() or "").strip()
            rt_lower = rt.lower()
            # Priority 1: exact full-text match
            if rt_lower == val_lower:
                best_idx, best_priority = n, 1
                break
            m = _label_re.match(rt)
            label = m.group(1).strip() if m else rt
            # Code = everything before the label starts (e.g. "P" in "PPercent",
            # "OC" in "OCPer Occurrence"). Empty string if regex didn't match.
            code = rt[: len(rt) - len(label)].strip() if m else ""
            # Priority 2: exact code match (case-insensitive)
            if code and code.lower() == val_lower and best_priority > 2:
                best_idx, best_priority = n, 2
            # Priority 3: exact label match
            elif label.lower() == val_lower and best_priority > 3:
                best_idx, best_priority = n, 3
            # Priority 4: contains anywhere
            elif val_lower in rt_lower and best_priority > 4:
                best_idx, best_priority = n, 4

        if best_idx is not None:
            rows.nth(best_idx).click(timeout=2_000)
            _log.debug("fill_combo %s = %r  matched row %d (priority %d)", combo_id, value, best_idx, best_priority)
        elif count > 0:
            rows.first.click(timeout=2_000)
            _log.warning("fill_combo %s = %r  no good match — picked first row", combo_id, value)
        else:
            _log.warning("fill_combo %s = %r  no dropdown rows appeared", combo_id, value)
        page.wait_for_timeout(_FILL_WAIT_MS)
    except Exception as exc:
        _log.warning("fill_combo %s = %r  FAILED: %s", combo_id, value, exc)


# ── Premises ──────────────────────────────────────────────────────────────────

def _fill_premises(page: Page) -> None:
    """Open Location/Building Lookup dialog and select all available buildings.

    The Premises screen (CHM-PRPREMSE) shows a grid of buildings linked to this
    property line.  Clicking Add opens a dialog that lists all buildings defined
    on the account.  We check-all and save to link every building at once.

    If no buildings appear in the dialog (blank account), we log a warning and
    return without error.
    """
    _nav_section(page, "Premise", "CHM-PRPREMSE")

    add_btn = page.locator('[data-test="vlvwPremise_add"]')
    if not _wait_visible(page, add_btn, 5_000):
        _log.error("Premises Add button not found on CHM-PRPREMSE")
        return

    _log.info("Opening Location/Building Lookup dialog")
    add_btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(800)

    # Confirm dialog opened
    dialog = page.get_by_role("heading", name="Location/Building Lookup")
    if not _wait_visible(page, dialog, 8_000):
        _log.error("Location/Building Lookup dialog did not open")
        return

    # Row checkboxes use data-test="row-{n}-checkbox" (React custom checkbox divs).
    # Scope to the ads-portal where the dialog renders.
    # Rows load asynchronously — retry for up to ~6 s before giving up.
    portal = page.locator('[data-test="ads-portal"]')
    row_chks = portal.locator('[data-test^="row-"][data-test$="-checkbox"]')

    _ROW_RETRIES, _ROW_WAIT_MS = 6, 1_000
    count = 0
    for _attempt in range(_ROW_RETRIES):
        page.wait_for_timeout(_ROW_WAIT_MS)
        count = row_chks.count()
        _log.debug("Premises dialog: row poll %d/%d → %d row(s)", _attempt + 1, _ROW_RETRIES, count)
        if count > 0:
            break
    _log.info("Premises dialog: %d row(s) available (after %d poll(s))", count, _attempt + 1)

    if count == 0:
        _log.warning("Premises: no rows found in Location/Building Lookup dialog after %d retries", _ROW_RETRIES)
    else:
        for n in range(count):
            try:
                row_chks.nth(n).click(timeout=2_000)
                page.wait_for_timeout(200)
                _log.debug("Premises: checked row %d", n)
            except Exception as exc:
                _log.warning("Premises: could not check row %d: %s", n, exc)

    # Log checked count
    page.wait_for_timeout(300)
    try:
        checked_str = portal.locator('[data-test="total-rows-checked"]').first.text_content(timeout=1_000) or ""
        _log.info("Premises dialog: %s", checked_str.strip())
    except Exception:
        pass

    # Save — exact=True prevents matching "Save & Add Another".
    # Scope to the portal to avoid background page Save buttons.
    save_btn = page.get_by_role("button", name="Save", exact=True)
    if not _wait_visible(page, save_btn, 3_000):
        _log.error("Premises: Save button not found in dialog")
        return
    save_btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(1_200)
    _log.info("Premises: saved Location/Building Lookup selection (%d row(s))", count)

    # Navigate away from Premises and back — ensures EPIC processes the
    # saved premises before subjects are opened (subjects fields are disabled
    # until the premise link is confirmed by a page reload).
    _log.debug("Premises: navigating away and back to confirm premise link")
    try:
        # Navigate to Subjects briefly
        _nav_section(page, "Subject", "CHM-PRSUBJCT")
        page.wait_for_timeout(500)
        # Navigate back to Premises to confirm
        _nav_section(page, "Premise", "CHM-PRPREMSE")
        page.wait_for_timeout(500)
    except Exception as exc:
        _log.warning("Premises: nav-away-and-back failed: %s", exc)


# ── Subjects of Insurance ─────────────────────────────────────────────────────

def _add_cause_of_loss(page: Page, cause: str, coinsurance: str = "", deductible: str = "") -> None:
    """Edit the first existing Cause of Loss row, or add a new one if the grid is empty.

    EPIC copies the previous subject's CoL when a new subject row is created.
    We therefore click the first existing row to open its edit dialog and overwrite
    all fields, rather than clicking Add (which would stack a duplicate row on top of
    the already-copied one).

    Deductible routing (confirmed via CDP 2026-05-20):
    - Value containing '%' → 'Deductible percent' field; clears 'Deductible amount'
    - Dollar figure (digits / $ / ,) → 'Deductible amount' field; clears 'Deductible percent'

    The dialog uses ``data-modal-id`` (NOT ``<dialog>``).
    """
    if not cause:
        return
    try:
        modal = page.locator('[data-modal-id]')

        # --- Step 1: open the dialog ---
        # Try clicking the first existing row in the CoL grid.  EPIC copies the
        # previous subject's CoL on every new subject Add, so for subjects 2-N
        # there is always at least one row.  The JS walker finds the first leaf
        # element whose text is an all-uppercase CoL code (2–5 chars, e.g. SPC).
        clicked_code = page.evaluate("""
            () => {
                const addBtn = document.querySelector('[data-test="vlvwCauseOfLoss_add"]');
                if (!addBtn) return null;
                let container = addBtn;
                for (let i = 0; i < 5; i++) {
                    container = container.parentElement;
                    if (!container) return null;
                }
                const walker = document.createTreeWalker(container, NodeFilter.SHOW_ELEMENT);
                let node;
                while ((node = walker.nextNode())) {
                    if (node.tagName === 'BUTTON' || node.tagName === 'INPUT') continue;
                    if (node.children.length > 0) continue;
                    const text = node.textContent.trim();
                    if (/^[A-Z]{2,5}$/.test(text)) {
                        node.click();
                        return text;
                    }
                }
                return null;
            }
        """)

        if clicked_code:
            _log.debug("Cause of Loss: selected existing row (code=%r) — clicking Edit button", clicked_code)
            page.wait_for_timeout(300)
            edit_btn = page.locator('[data-test="vlvwCauseOfLoss_edit"]')
            if _wait_visible(page, edit_btn, 1_500):
                edit_btn.first.click(timeout=_CLICK_TIMEOUT)
                page.wait_for_timeout(400)
            if not _wait_visible(page, modal, 2_500):
                _log.debug("Cause of Loss: edit button did not open dialog — falling back to Add")
                clicked_code = None

        if not clicked_code:
            add_btn = page.locator('[data-test="vlvwCauseOfLoss_add"]')
            if not _wait_visible(page, add_btn, 3_000):
                _log.warning("Cause of Loss Add button not visible; skipping cause %r", cause)
                return
            add_btn.first.click(timeout=_CLICK_TIMEOUT)
            page.wait_for_timeout(400)
            if not _wait_visible(page, modal, 4_000):
                _log.warning("Cause of Loss: dialog did not open; skipping cause %r", cause)
                return

        _log.debug("Cause of Loss: dialog open")

        # --- Step 2: Cause of Loss combo ---
        cause_combo = modal.locator('[data-test="cboCauseOfLoss-combobox-input"]').first
        try:
            current_val = cause_combo.input_value(timeout=1_000)
        except Exception:
            current_val = ""
        if current_val.upper() != cause.upper():
            try:
                cause_combo.click(timeout=2_000)
                cause_combo.fill(cause, timeout=2_000)
                page.wait_for_timeout(500)
                drop_rows = page.locator('[data-test="dropdown-row"]')
                for n in range(min(drop_rows.count(), 10)):
                    if cause.lower() in (drop_rows.nth(n).text_content() or "").lower():
                        drop_rows.nth(n).click(timeout=2_000)
                        break
                else:
                    if drop_rows.count() > 0:
                        drop_rows.first.click(timeout=2_000)
            except Exception as exc:
                _log.warning("Cause of Loss: could not fill combo: %s", exc)

        # --- Step 3: Coins % (id=inteCoinsurance__textField, required; default 80) ---
        _coins = (coinsurance or "80").replace('%', '').strip() or "80"
        _fill_id_field(page, "inteCoinsurance__textField", _coins)

        # --- Step 4: Deductible + type ---
        # Dollar amount → inteDeductible1__textField + cboDedType1=FL (Flat)
        # Percentage    → pereDeductible1__textField + cboDedType1=P  (Percent)
        # Always clear the unused deductible field so edits don't leave stale data.
        if deductible:
            _ded_raw = deductible.strip()
            _is_pct = '%' in _ded_raw
            _ded_val = _ded_raw.replace('%', '').replace('$', '').replace(',', '').strip()
            if _is_pct:
                _fill_id_field(page, "pereDeductible1__textField", _ded_val)
                try:
                    page.locator('#inteDeductible1__textField').first.fill("", timeout=500)
                except Exception:
                    pass
                _fill_react_combo(page, "cboDedType1", "P")
            else:
                _fill_id_field(page, "inteDeductible1__textField", _ded_val)
                try:
                    page.locator('#pereDeductible1__textField').first.fill("", timeout=500)
                except Exception:
                    pass
                _fill_react_combo(page, "cboDedType1", "FL")

        # --- Step 5: Save ---
        save = page.get_by_role("button", name="Save", exact=True)
        if _wait_visible(page, save, 3_000):
            save.first.click(timeout=_CLICK_TIMEOUT)
            page.wait_for_timeout(500)
            _log.debug("Cause of Loss: Save clicked")
            try:
                err_ok = page.locator('[data-test="common-message-modal"] button')
                if err_ok.first.is_visible(timeout=500):
                    err_ok.first.click(timeout=1_000)
                    _log.warning("Cause of Loss: dismissed post-save validation")
            except Exception:
                pass
            try:
                if modal.is_visible(timeout=500):
                    page.evaluate(
                        "() => { document.querySelectorAll('[class*=CloseButton_module_closeIcon]')"
                        ".forEach(b => b.click()); }"
                    )
                    page.wait_for_timeout(300)
                    _log.warning("Cause of Loss: closed lingering dialog")
            except Exception:
                pass
            _log.debug("Cause of Loss: done")
        else:
            _log.warning("Cause of Loss: Save button not found after selecting %r", cause)
    except Exception as exc:
        _log.warning("_add_cause_of_loss(%r) failed: %s", cause, exc)


# Maps state.json extracted subject type labels → EPIC combo search strings.
# EPIC's Subject of Insurance combo uses code+label (e.g. "BBuilding").
# Confirmed via live CDP inspection 2026-05-19.
_SUBJECT_TYPE_SEARCH: dict[str, str] = {
    "building":                     "Building",                          # B
    "personal property of insured": "Personal Property",                 # PP
    "earthquake":                   "Earthquake",                        # EQ
    "business income":              "Business Income with Extra Expense", # BUSIN
}


def _fill_subjects(page: Page, subjects: list[SubjectSpec]) -> None:
    """Add each Subject of Insurance row on CHM-PRSUBJCT.

    Uses the React ``__textField`` / combobox pattern.
    Add button: ``data-test="vlvwSubject_add"``
    Cause of Loss: ``data-test="vlvwCauseOfLoss_add"``

    Subject type mapping: state.json labels are mapped to EPIC combo search
    strings via ``_SUBJECT_TYPE_SEARCH``.  Unknown types fall back to the
    raw label (case-insensitive contains matching).
    """
    if not subjects:
        _log.info("No subjects to add; skipping Subjects section")
        return

    _nav_section(page, "Subject", "CHM-PRSUBJCT")

    for i, subj in enumerate(subjects):
        check_cancel()
        _log.info(
            "Adding subject %d/%d: type=%r loc=%s bldg=%s desc=%r amount=%r",
            i + 1, len(subjects),
            subj.subject_type, subj.location_number, subj.building_number,
            subj.description, subj.amount,
        )

        # Skip rows with no amount AND no valuation — EPIC requires at least Amount
        # and Valuation to save; blank rows trigger "Required information is missing"
        # on nav-away which blocks subsequent sections.
        if not subj.amount and not subj.valuation:
            _log.warning(
                "Subject %d: skipping — both amount and valuation are blank (type=%r desc=%r)",
                i + 1, subj.subject_type, subj.description,
            )
            continue

        # Snapshot before-count for commit verification.
        before_n = _read_subject_count(page)

        # Pre-Add gate + click + form-active + 750 ms post-activate settle.
        # Validated 2026-05-28 — replaces the prior 4-attempt retry + Premises
        # round-trip. See :func:`_activate_subject_row` docstring.
        if not _activate_subject_row(page):
            _log.error("Subject %d: form did not activate; skipping", i + 1)
            continue

        # Location / Building — Tab after each to trigger EPIC's lookup resolver.
        _check_popups(page, where=f"row{i+1}/pre-loc")
        _fill_id_field(page, "inteLocationNumber__textField", subj.location_number)
        _check_popups(page, where=f"row{i+1}/post-loc")
        page.keyboard.press("Tab")
        _check_popups(page, where=f"row{i+1}/post-loc-tab")
        _fill_id_field(page, "inteBuildingNumber__textField", subj.building_number)
        _check_popups(page, where=f"row{i+1}/post-bldg")
        page.keyboard.press("Tab")
        _check_popups(page, where=f"row{i+1}/post-bldg-tab")
        page.wait_for_timeout(400)

        # Subject type
        _subj_search = _SUBJECT_TYPE_SEARCH.get(subj.subject_type.lower(), subj.subject_type)
        _fill_react_combo(page, "cboSubject", _subj_search)
        _check_popups(page, where=f"row{i+1}/post-type")

        _fill_id_field(page, "streDescription", subj.description)
        _check_popups(page, where=f"row{i+1}/post-desc")

        _fill_id_field(page, "deceAmount__textField", subj.amount)
        _check_popups(page, where=f"row{i+1}/post-amt")

        if subj.valuation:
            _fill_react_combo(page, "cboValuation1", subj.valuation)
            _check_popups(page, where=f"row{i+1}/post-val")

        _fill_id_field(page, "pereInflationGuard__textField", subj.inflation_guard)
        _check_popups(page, where=f"row{i+1}/post-infl")

        # Form number intentionally NOT filled on the Subjects screen.
        # (User instruction 2026-05-20.)

        page.wait_for_timeout(300)

        # Cause of Loss (also contains Coins % and Deductible). CoL Save
        # commits the subject row — no nav-away required.
        if subj.cause_of_loss:
            _check_popups(page, where=f"row{i+1}/pre-col")
            _add_cause_of_loss(
                page, subj.cause_of_loss,
                coinsurance=subj.coinsurance,
                deductible=subj.deductible,
            )
            _check_popups(page, where=f"row{i+1}/post-col")

        # Verify the row actually committed (footer ticked + new row contains
        # the expected amount digits — guards against the empty "-" placeholder
        # case where footer ticks but the data was lost to a validation popup).
        # Falls back to a Premises round-trip if the fast path didn't commit
        # (e.g. rows without a CoL).
        if not _verify_subject_committed(page, before_n, subj, timeout_ms=5_000):
            _log.warning(
                "Subject %d: fast-path commit failed — falling back to Premises round-trip",
                i + 1,
            )
            try:
                _nav_section(page, "Premise", "CHM-PRPREMSE")
                page.wait_for_timeout(400)
                _nav_section(page, "Subject", "CHM-PRSUBJCT")
                page.wait_for_timeout(400)
                _check_popups(page, where=f"row{i+1}/fallback-nav")
            except Exception as exc:
                _log.warning("Subject %d: fallback nav failed: %s", i + 1, exc)

        _log.info(
            "Subject %d: type=%r amount=%r  OK",
            i + 1, subj.subject_type, subj.amount,
        )

    _log.info("Subjects section complete (%d rows)", len(subjects))


def _verify_subject_committed(
    page: Page,
    before_n: int,
    subj: SubjectSpec,
    *,
    timeout_ms: int = 5_000,
) -> bool:
    """Confirm the row at index ``before_n`` appears in the grid with data.

    Requires BOTH: the footer count bumps past ``before_n`` AND the new
    row's text contains the expected amount digits. The amount check
    guards against EPIC's "-" placeholder rows that bump the counter
    even when the data was dropped (e.g. mid-flow validation popup).
    Rows with no expected amount fall back to footer-bump only.
    """
    expected_digits = "".join(ch for ch in (subj.amount or "") if ch.isdigit())
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        _check_popups(page, where="commit-verify")
        now_n = _read_subject_count(page)
        if now_n > before_n:
            try:
                row_text = page.evaluate(
                    """(idx) => {
                        const grid = document.querySelector('[data-test="vlvwSubject"]');
                        if (!grid) return '';
                        const rows = grid.querySelectorAll('[data-test="row"]');
                        return rows[idx] ? rows[idx].innerText.replace(/\\s+/g, ' ').trim() : '';
                    }""",
                    before_n,
                )
            except Exception:
                row_text = ""
            if not expected_digits:
                return True  # no amount given — trust the footer
            if expected_digits in (row_text or "").replace(",", ""):
                return True
            _log.warning(
                "Commit verify: footer bumped but new row missing %s — row=%r",
                expected_digits, row_text[:80],
            )
            return False
        time.sleep(0.15)
    return False


# ── Additional Interests ──────────────────────────────────────────────────────

def _fill_additional_interests(page: Page, interests: list[AdditionalInterestSpec]) -> None:
    """Add each Additional Interest row on CHM-PRADLINT (React vlvwInterest pattern).

    Rewritten 2026-05-26 to mirror ``step_inland_marine._fill_im_additional_interests``:
    safe_action + verify_input_value, pre-Add settle, _wait_grid_grew with
    retry, lenient validated-address path with manual-fallback + tail
    extraction onto address line 2.

    The validated-lookup helper lives in step_business_auto. Imported
    lazily so step_property doesn't pull in step_business_auto at
    module-load time (avoids the circular: business_auto → property).
    """
    # Lazy imports — see docstring.
    from iga_marketing_master_2.epic_steps.step_business_auto import (
        _split_us_address,
        _fill_validated_address,
        _resolve_ai_interest,
    )

    if not interests:
        _log.info("No additional interests; skipping section")
        return

    if not _nav_section(page, "AdditionalInterest", "CHM-PRADLINT"):
        _log.error("Additional Interests: nav failed — aborting section")
        return

    _log.info("=== Additional Interests: %d item(s) ===", len(interests))
    assert_screen_code(page, "CHM-PRADLINT", timeout_ms=8_000)

    add_btn_sel = '[data-test="vlvwInterest_add"]'

    for i, ai in enumerate(interests, 1):
        check_cancel()
        name = (ai.name or "").strip()
        if not name:
            _log.warning("AI #%d: blank name — skipping (EPIC requires name)", i)
            continue

        _log.info(
            "--- Row %d/%d: name=%r type=%r loc/bldg/subj=%s/%s/%s",
            i, len(interests), name, ai.interest_type or "-",
            ai.location_number or "-",
            ai.building_number or "-",
            ai.subject_number or "-",
        )

        _dismiss_modal(page)
        rows_before = grid_row_count(page, "vlvwInterest")
        _log.debug("AI #%d: rows_before=%d", i, rows_before)

        page.wait_for_timeout(750)

        with safe_action(page, context=f"AI #{i}: click Add"):
            page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)

        grew = wait_grid_grew(page, "vlvwInterest", rows_before, timeout_ms=3_000)
        if not grew:
            _log.warning("AI #%d: row did not grow after Add — retrying with longer settle", i)
            page.wait_for_timeout(2_500)
            with safe_action(page, context=f"AI #{i}: RETRY Add"):
                page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)
            grew = wait_grid_grew(page, "vlvwInterest", rows_before, timeout_ms=5_000)
            if not grew:
                _log.error("AI #%d: row still did not grow after retry — halting run.", i)
                raise EntryCancelled(
                    f"AI #{i}: EPIC rejected Add (twice); halting"
                )
            _log.info("AI #%d: retry succeeded", i)

        fill_field_verified(
            page, "streName", name, i, "name",
            section="AI", logger=_log,
        )

        if ai.interest_type:
            # Property's combo accepts the human-readable label directly,
            # but we try the 2-char resolver first for consistency with
            # the IM / BAUT / Umbrella codepaths. If resolution fails,
            # fall back to the raw value.
            code = _resolve_ai_interest(ai.interest_type) or ai.interest_type
            with safe_action(page, context=f"AI #{i}: combo cboInterest={code}"):
                _fill_react_combo(page, "cboInterest", code)
            _log.debug("AI #%d: cboInterest = %r", i, code)

        if ai.street or ai.city or ai.state or ai.zip_code:
            full = ", ".join(
                [s for s in (ai.street, ai.city,
                             f"{ai.state} {ai.zip_code}".strip())
                 if s]
            )
            fill_validated_address_then_manual(
                page,
                street=ai.street, city=ai.city, state_code=ai.state,
                zip_code=ai.zip_code, full_address=full or ai.street,
                row_idx=i, section="AI",
                fill_validated_fn=_fill_validated_address,
                fill_combo_fn=_fill_react_combo,
                logger=_log,
            )

        # EPIC validation: "A location number must be accompanied by a building
        # number." If loc# is set but bldg# is blank, EPIC pops a Validation
        # Error modal and refuses to save. Default bldg# to "1" so the row
        # still saves with the loc reference intact.
        ai_loc = ai.location_number
        ai_bldg = ai.building_number
        if ai_loc and not ai_bldg:
            _log.warning(
                "AI #%d: loc=%s with blank bldg — defaulting bldg='1' to satisfy "
                "EPIC validation (\"location must be accompanied by a building\")",
                i, ai_loc,
            )
            ai_bldg = "1"

        if ai_loc:
            fill_field_verified(
                page, "inteLocationNumber__textField", ai_loc,
                i, "loc#", section="AI", logger=_log,
            )
        if ai_bldg:
            fill_field_verified(
                page, "inteBuildingNumber__textField", ai_bldg,
                i, "bldg#", section="AI", logger=_log,
            )
        if ai.subject_number:
            fill_field_verified(
                page, "inteSubjectNumber__textField", ai.subject_number,
                i, "subj#", section="AI", logger=_log,
            )
        if ai.loan_number:
            fill_field_verified(
                page, "streReferenceNumber", ai.loan_number,
                i, "loan/ref#", section="AI", logger=_log,
            )
        if ai.rank:
            fill_field_verified(
                page, "inteRank__textField", ai.rank,
                i, "rank", section="AI", logger=_log,
            )

        page.wait_for_timeout(300)
        _log.debug("AI #%d: row complete", i)

    _log.info("=== Additional Interests: complete (%d row(s) processed) ===", len(interests))


# ── Forms & Endorsements ──────────────────────────────────────────────────────

def _fill_forms_endorsements(page: Page, forms: list[PropertyFormSpec]) -> None:
    """Add forms and endorsements on PRFRMEND (old Angular proxy pattern).

    Add button: ``[data-automation-id="fraFormsEnd"] .icon-button[title="Add"]``

    Form lookup: type the form number into ``streNumber`` (has a search icon),
    Tab to trigger lookup.  EPIC auto-fills Name and Edition Date on a successful
    lookup.  If the number has no match, we fill Name directly via ``streName``.
    """
    if not forms:
        _log.info("No forms to add; skipping Forms & Endorsements section")
        return

    _nav_section(page, "FormEndorsement", "PRFRMEND")

    add_btn = page.locator('[data-automation-id="fraFormsEnd"] .icon-button[title="Add"]')

    for i, fm in enumerate(forms):
        check_cancel()
        _log.info(
            "Adding form %d/%d: number=%r name=%r",
            i + 1, len(forms), fm.number, fm.name,
        )

        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Forms & Endorsements Add button not visible at row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(500)

        # Form number lookup
        if fm.number:
            _fill_proxy_field(page, "streNumber", fm.number)
            page.keyboard.press("Tab")
            page.wait_for_timeout(600)
            # Check if EPIC auto-filled the name
            try:
                name_inp = page.locator('[data-automation-id="streName"] input')
                name_val = name_inp.first.input_value(timeout=1_000)
                if name_val:
                    _log.debug("Form %r: EPIC auto-filled name = %r", fm.number, name_val)
                elif fm.name:
                    _fill_proxy_field(page, "streName", fm.name)
            except Exception:
                if fm.name:
                    _fill_proxy_field(page, "streName", fm.name)
        elif fm.name:
            _fill_proxy_field(page, "streName", fm.name)

        # Edition date (if provided and EPIC didn't auto-fill)
        if fm.edition_date:
            try:
                dte_inp = page.locator('[data-automation-id="dteEdition"] input')
                existing = dte_inp.first.input_value(timeout=500)
                if not existing:
                    dte_inp.first.click(timeout=2_000)
                    dte_inp.first.fill(fm.edition_date, timeout=2_000)
                    page.wait_for_timeout(_FILL_WAIT_MS)
                    _log.debug("Form edition date = %r  OK", fm.edition_date)
                else:
                    _log.debug("Form edition date already set to %r by EPIC", existing)
            except Exception as exc:
                _log.warning("Could not fill edition date %r: %s", fm.edition_date, exc)

        page.wait_for_timeout(300)
        _log.info("Form %d (%r) added OK", i + 1, fm.number or fm.name)

    _log.info("Forms & Endorsements section complete (%d rows)", len(forms))


# ── Additional Coverages ──────────────────────────────────────────────────────

def _fill_additional_coverages(page: Page, coverages: list[PropertyCoverageSpec]) -> None:
    """Add each Additional Coverage row on PRADDCOV (old Angular proxy pattern).

    Layout is almost identical to GL's GLADDCOV.  Key differences:
    - Limit fields: ``streLimit1`` / ``streLimit2`` (GL uses ``streLimit`` / ``streLimit2``)
    - Deductible type: ``streDedType`` (GL uses ``streDeductibleType``)
    - ``streDedType`` is readonly — do NOT fill it; EPIC auto-populates.
    - Has subject-level linkage: ``inteSubject``

    Add button: ``[name="vlvwCoverage"] .icon-button[title="Add"]``
    """
    if not coverages:
        _log.info("No additional coverages; skipping section")
        return

    _nav_section(page, "AdditionalCoverage", "PRADDCOV")

    add_btn = page.locator('[name="vlvwCoverage"] .icon-button[title="Add"]')

    for i, cov in enumerate(coverages):
        check_cancel()
        _log.info(
            "Adding additional coverage %d/%d: desc=%r code=%r limit1=%r deductible=%r",
            i + 1, len(coverages),
            cov.description, cov.code, cov.limit1, cov.deductible,
        )

        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Additional Coverage Add button not visible at row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)

        # Location / Building / Subject linkage
        _fill_proxy_field(page, "inteLocationNumber", cov.location_number)
        _fill_proxy_field(page, "inteBuildingNumber", cov.building_number)
        _fill_proxy_field(page, "inteSubject",        cov.subject_number)

        # Core coverage fields
        short_desc = abbreviate_if_too_long(
            cov.description, max_chars=AC_DESC_MAX_CHARS,
            row_idx=i + 1, label="description", section="AC", logger=_log,
        )
        _fill_proxy_field(page, "streDescription", short_desc)
        _fill_proxy_field(page, "streCode",        cov.code or "N/A")
        _fill_proxy_field(page, "streLimit1",      cov.limit1)
        _fill_proxy_field(page, "streLimit2",      cov.limit2)
        _fill_proxy_field(page, "streDeductible",  cov.deductible)
        # streDedType is readonly — EPIC auto-populates from coverage code
        # streFormNumber intentionally NOT filled on Additional Coverages screen.
        # (User instruction 2026-05-20: leave Additional Coverage form_number blank.)

        page.wait_for_timeout(300)
        _log.info(
            "Additional coverage %d (%r) filled OK — code=%r limit1=%r",
            i + 1, cov.description, cov.code or "N/A", cov.limit1,
        )

    _log.info("Additional Coverages section complete (%d rows)", len(coverages))


# ── Public API ────────────────────────────────────────────────────────────────

def run(page: Page, setup: PropertySetup) -> bool:
    """Fill the Property sections of the open submission.

    Expects the browser to be on the Submission Detail page (MMS detail tree visible
    in the left sidebar) with a Property line already created.

    EPIC dependency (confirmed 2026-05-19): Premises MUST be added before Subjects.
    EPIC blocks subject creation with "A premises must be added to the policy before
    a subject can be added."  The step order below enforces this.

    Order:
      1. Premises — link account locations to this property line  ← MUST be first
      2. Subjects — add all subjects of insurance
      3. Additional Interests — add mortgagees, loss payees, etc.
      4. Forms & Endorsements — standard + submission-specific forms
      5. Additional Coverages — additional coverage endorsements

    Returns True on success, False if a fatal error prevented completion.
    """
    _log.info("=== Property entry step starting ===")
    _log.info(
        "Setup: %d subject(s), %d AI(s), %d form(s), %d coverage(s), add_all_premises=%s",
        len(setup.subjects),
        len(setup.additional_interests),
        len(setup.forms),
        len(setup.coverages),
        setup.add_all_premises,
    )

    try:
        # Navigate to Property parent in sidebar
        _log.info("Navigating to Property in submission sidebar")
        _nav_to_property(page)
        checkpoint(page, "Property: navigation")

        # 1. Premises
        if setup.add_all_premises:
            _log.info("=== Step 1: Premises ===")
            checkpoint(page, "Property: Premises")
            _fill_premises(page)
        else:
            _log.info("Premises: add_all_premises=False — skipping")

        # 2. Subjects
        if setup.subjects:
            _log.info("=== Step 2: Subjects (%d) ===", len(setup.subjects))
            checkpoint(page, "Property: Subjects")
            _fill_subjects(page, setup.subjects)
        else:
            _log.info("Subjects: none provided — skipping")

        # 3. Additional Interests — rewritten 2026-05-26 to mirror the IM AI
        # pattern (safe_action + verify, _wait_grid_grew, lenient validated
        # address with manual fallback + tail extraction).
        if setup.additional_interests:
            _log.info(
                "=== Step 3: Additional Interests (%d) ===",
                len(setup.additional_interests),
            )
            checkpoint(page, "Property: Additional Interests")
            _fill_additional_interests(page, setup.additional_interests)
        else:
            _log.info("Additional Interests: none provided — skipping")

        # 4. Forms & Endorsements (always: standard + submission-specific)
        all_forms = list(PROPERTY_STANDARD_FORMS) + list(setup.forms)
        _log.info("=== Step 4: Forms & Endorsements (%d) ===", len(all_forms))
        checkpoint(page, "Property: Forms & Endorsements")
        _fill_forms_endorsements(page, all_forms)

        # 5. Additional Coverages
        if setup.coverages:
            _log.info("=== Step 5: Additional Coverages (%d) ===", len(setup.coverages))
            checkpoint(page, "Property: Additional Coverages")
            _fill_additional_coverages(page, setup.coverages)
        else:
            _log.info("Additional Coverages: none provided — skipping")

        _log.info("=== Property entry step complete ===")
        return True

    except EntryCancelled:
        raise
    except PlaywrightTimeout as exc:
        _log.error("Property step timed out: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_property: %s", exc, exc_info=True)
        return False

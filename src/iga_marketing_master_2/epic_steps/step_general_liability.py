"""step_general_liability.py — fill the General Liability application form inside a submission.

Pre-condition
-------------
The browser is on the Submission Detail sidebar with the General Liability subtree
visible (GL line already opened).

Sections covered
----------------
1.  **Coverages** (GLCOVER) — limit / deductible fields, CGL checkbox, and the
    "Policy" aggregate-applies checkbox (always checked — IGA fixed default).
2.  **Hazards** (CHM-GLHAZARD) — add one hazard row per ``HazardSpec`` via the
    inline detail form.  Rows auto-save when navigation moves away.
3.  **Contractors** (CHM-GLCNTRCT) — "Default All Questions to No" (always applied),
    then fill Type-of-Work subcontracted fields.

Sections covered
----------------
1.  **Coverages** (GLCOVER)
2.  **Hazards** (CHM-GLHAZARD)
3.  **Claims Made / Employee Benefits** (CHM-GLCLMEMP) — always default all questions
    to No; fill EBL fields if provided.
4.  **Contractors** (CHM-GLCNTRCT) — always default all questions to No; fill
    Type-of-Work fields if provided.
5.  **Products/Completed Operations** (CHM-GLPRODUC) — always default all 10 questions
    to No.
6.  **Additional Interests** (CHM-GLADDINT) — add each AdditionalInterestSpec row.
    Screen code unconfirmed; adjust if EPIC rejects navigation.
7.  **Forms & Endorsements** (CHM-GLFRMEND) — add each FormSpec row by form number.
    User-provided specific instructions apply here; extend FormSpec as needed.
8.  **Additional Coverages** (GLADDCOV) — add each AdditionalCoverageSpec row.
9.  **General Information** (GLGENIF) — default all questions to No.
    Screen code is tentative; sidebar key ``GeneralInformation`` assumed.

Skipped sections
----------------
Remark, InternationalLiability, SupplementalScreen, DocumentView.

Selector notes (confirmed via live CDP inspection 2026-05-18)
-------------------------------------------------------------
Coverages (GLCOVER) — "old-format" Angular proxy pattern:
  Text/numeric fields: ``[data-automation-id="{name}"] input``
    (the bare <input> has NO name/id; the proxy div carries the field id)
  Checkboxes: ``id`` attribute only — ``GLCOVERchkCommGenLiab``, ``GLCOVERchkAppPolicy``
  cboBasis combo: old proxy → use _fill_combo(page, "cboBasis", value)

Hazards (CHM-GLHAZARD) — React inline detail form (no modal, no Save button):
  Grid Add:    ``[data-test="vlvwHazards_add"]``
  Grid Delete: ``[data-test="vlvwHazards_delete"]``  (confirm: ``[data-test="dialog-yes-btn"]``)
  All detail form fields have real ``id`` attributes (React __textField suffix):
    ``#inteLocationNumber__textField``  Loc #
    ``#inteBuildingNumber__textField``  Bldg #
    ``#streClassCode``                  Class code  (Tab after fill triggers lookup)
    ``#streClassification``             Classification (auto-filled after class code)
    ``#cboPremiumBasis``                Premium basis combo (React ComboboxBase)
    ``#streExposure``                   Exposure
    ``#streTerritory``                  Territory
  Premium basis dropdown rows: ``[data-test="dropdown-row"]``, single-letter codes:
    A=Area  C=Total Cost  F=Frontage  M=Admissions  P=Payroll
    R=Receipts  S=Gross Sales  T=Other  U=Unit
  Save mechanism: inline auto-save; navigating to next section commits the last row.

Contractors (CHM-GLCNTRCT) — React form with proper <label for=...> associations:
  Default button: ``button`` text "Default All Questions to \"No\""
  Type of Work fields addressable by get_by_label():
    "Dollars paid to subcontractors:"   id=cureDollarsPaid__textField
    "Percent of work subcontracted:"    id=intePercentofWork__textField
    "Number of full-time staff:"        id=inteNumFullTime__textField
    "Number of part-time staff:"        id=inteNumPartTime__textField
    "Remarks"                           id=streRemarks
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled, check_cancel
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

from iga_marketing_master_2.epic_steps.step_mms_create import (
    _fill_combo,
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
    AI_REASON_MAX_CHARS,
    AC_DESC_MAX_CHARS,
)

_log = logging.getLogger("iga.epic_steps.general_liability")

_CLICK_TIMEOUT = 5_000
_NAV_WAIT_MS   = 1_000


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class GlCoveragesSpec:
    """Limit and deductible values for GLCOVER.

    All fields optional — leave blank to skip.  Pass numeric strings without
    currency symbols or commas (e.g. ``"1000000"``).

    Fixed defaults always applied on every submission:
      - CGL checkbox (``GLCOVERchkCommGenLiab``) always checked.
      - Policy aggregate applies (``GLCOVERchkAppPolicy``) always checked.
      - Project (``GLCOVERchkAppProject``) and Location (``GLCOVERchkAppLocation``) always checked.
      - Deductibles are never filled — pass-through is intentionally omitted.

    ``occurrence_type`` controls the Claims Made / Occurrence radio; pass the
    value extracted from state (e.g. ``"Claims Made"`` or ``"Occurrence"``).
    Defaults to ``"Occurrence"`` when blank.
    """
    each_occ_limit:     str = ""          # streEachOccLimit
    gen_aggr_limit:     str = ""          # streGenAggrAppLimit
    pers_adv_inj_limit: str = ""          # strePersAdvInjLimit
    prod_oper_limit:    str = ""          # streProdOperLimit
    med_limit:          str = ""          # streMedLimit
    dam_prem_limit:     str = ""          # streDamPremLimit
    emp_ben_limit:      str = ""          # streEmpBenLimit
    occurrence_type:    str = "Occurrence"  # rbtnOccurrence / rbtnClaimsMade


@dataclass
class HazardSpec:
    """One hazard row on CHM-GLHAZARD.

    ``class_code`` is required.  ``exposure`` is required.
    ``premium_basis`` is the single-letter EPIC code:
      A=Area  C=Total Cost  F=Frontage  M=Admissions  P=Payroll
      R=Receipts  S=Gross Sales  T=Other  U=Unit
    ``loc_num`` / ``bldg_num`` default to 1 (primary location/building).
    """
    class_code:     str
    exposure:       str        # numeric string, e.g. "250000"
    premium_basis:  str = ""   # single-letter code; blank = leave default
    classification: str = ""   # human description; filled manually if EPIC lookup leaves it blank
    loc_num:        int = 1
    bldg_num:       int = 1


@dataclass
class ContractorsSpec:
    """Values for CHM-GLCNTRCT Type-of-Work section.

    "Default All Questions to No" is always applied when navigating to this screen.
    All fields optional.
    """
    dollars_subcontract: str = ""   # cureDollarsPaid__textField
    percent_subcontract: str = ""   # intePercentofWork__textField
    num_full_time:       str = ""   # inteNumFullTime__textField
    num_part_time:       str = ""   # inteNumPartTime__textField
    remarks:             str = ""   # streRemarks


@dataclass
class EblSpec:
    """Values for the Claims Made / Employee Benefits Liability screen (CHM-GLCLMEMP).

    The Claims Made Yes/No questions are always defaulted to No.
    All fields optional — leave blank to skip.
    """
    retroactive_date:      str = ""   # EBL retroactive date  → dteRetroactiveDate-mask
    deductible_per_claim:  str = ""   # streDeductiblePerClaim__textField
    num_employees:         str = ""   # inteNumberOfEmployees__textField
    num_employees_covered: str = ""   # inteNumberOfEmployeesCovered__textField


@dataclass
class GeneralLiabilitySetup:
    """Input for the General Liability entry step."""
    coverages:            GlCoveragesSpec              = field(default_factory=GlCoveragesSpec)
    hazards:              list[HazardSpec]             = field(default_factory=list)
    ebl:                  EblSpec                      = field(default_factory=EblSpec)
    contractors:          ContractorsSpec              = field(default_factory=ContractorsSpec)
    additional_interests: list[AdditionalInterestSpec] = field(default_factory=list)
    additional_coverages: list[AdditionalCoverageSpec] = field(default_factory=list)
    forms:                list[FormSpec]               = field(default_factory=list)
    fill_general_info:    bool                         = True   # default all GL Gen Info Qs to No


# ── Sidebar navigation ────────────────────────────────────────────────────────

def _nav_to_general_liability(page: Page) -> bool:
    """Click the 'General Liability' entry in the submission sidebar tree.

    The parent entry has a dynamic automation-id (e.g.
    ``sidebar-button-163;225889;TN level-3``) so we locate it by level-3 +
    exact text.  Returns True once the Coverages screen (GLCOVER) is
    visible, False on timeout.
    """
    btn = page.locator('[data-automation-id*="level-3"]').filter(has_text="General Liability")
    if not _wait_visible(page, btn, 5_000):
        _log.error("General Liability sidebar entry not found")
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    status = page.locator('[data-automation-id="GLCOVER"]')
    if _wait_visible(page, status, 8_000):
        return True
    _log.warning("Clicked General Liability sidebar but GLCOVER not confirmed")
    return True  # best-effort


def _nav_section(page: Page, section_key: str, screen_code: str) -> bool:
    """Click the GL sidebar link for *section_key* and confirm *screen_code* in status bar."""
    sidebar_sel = f'[data-automation-id^="sidebar-button-Policy.GeneralLiability.{section_key}"]'
    btn = page.locator(sidebar_sel)
    if not _wait_visible(page, btn, 5_000):
        _log.error("GL sidebar link for %s not found", section_key)
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    status = page.locator(f'[data-automation-id="{screen_code}"]')
    if _wait_visible(page, status, 8_000):
        return True
    _log.warning("Navigated to GL %s but screen code %s not confirmed", section_key, screen_code)
    return True  # best-effort


# ── Field helpers ─────────────────────────────────────────────────────────────

def _fill_proxy_field(page: Page, field_id: str, value: str) -> None:
    """Fill a Coverages old-format field via its proxy div's data-automation-id."""
    if not value:
        _log.debug("fill_proxy %s: skipped (blank)", field_id)
        return
    try:
        inp = page.locator(f'[data-automation-id="{field_id}"] input')
        inp.first.click(timeout=2_000)
        inp.first.fill(value, timeout=2_000)
        page.wait_for_timeout(150)
        _log.debug("fill_proxy %s = %r  OK", field_id, value)
    except Exception as exc:
        _log.warning("fill_proxy %s = %r  FAILED: %s", field_id, value, exc)


def _fill_id_field(page: Page, field_id: str, value: str) -> None:
    """Fill a React form field by its HTML id attribute."""
    if not value:
        _log.debug("fill_id #%s: skipped (blank)", field_id)
        return
    try:
        inp = page.locator(f'#{field_id}')
        inp.first.click(timeout=2_000)
        inp.first.fill(value, timeout=2_000)
        page.wait_for_timeout(150)
        _log.debug("fill_id #%s = %r  OK", field_id, value)
    except Exception as exc:
        _log.warning("fill_id #%s = %r  FAILED: %s", field_id, value, exc)


def _fill_react_combo(page: Page, combo_id: str, value: str) -> None:
    """Select a value in a React ComboboxBase by clicking the dropdown rows.

    Opens the combo by clicking its input, then clicks the first ``dropdown-row``
    whose text starts with or contains *value* (case-insensitive).
    """
    if not value:
        return
    try:
        inp = page.locator(f'#{combo_id}')
        inp.first.click(timeout=2_000)
        page.wait_for_timeout(400)
        # Find the matching row in the open dropdown.
        rows = page.locator('[data-test="dropdown-row"]')
        count = rows.count()
        if count == 0:
            _log.warning("No dropdown rows for combo %s after click", combo_id)
            return
        val_lower = value.strip().lower()
        for n in range(count):
            row = rows.nth(n)
            text = (row.text_content() or "").strip()
            # Match by single-letter code prefix or anywhere in text.
            if text.lower().startswith(val_lower) or val_lower in text.lower():
                row.click(timeout=2_000)
                page.wait_for_timeout(300)
                return
        # Fallback: click first row.
        _log.warning("No dropdown-row match for %r in combo %s; clicking first row", value, combo_id)
        rows.first.click(timeout=2_000)
        page.wait_for_timeout(300)
    except Exception as exc:
        _log.warning("Could not fill react combo #%s=%r: %s", combo_id, value, exc)


def _check_by_id(page: Page, checkbox_id: str) -> None:
    """Ensure a checkbox is checked via JS label-click (avoids overlap issues)."""
    result = page.evaluate(f"""() => {{
        const cb = document.getElementById('{checkbox_id}');
        if (!cb) return 'not_found';
        if (cb.checked) return 'already_checked';
        const label = document.querySelector('label[for="{checkbox_id}"]');
        if (label) {{ label.click(); return 'clicked_label'; }}
        cb.click(); return 'clicked_input';
    }}""")
    _log.info("Checkbox %s: %s", checkbox_id, result)


# ── Coverages ─────────────────────────────────────────────────────────────────

def _fill_coverages(page: Page, spec: GlCoveragesSpec) -> None:
    """Fill GLCOVER limit fields and apply IGA fixed defaults."""
    _nav_section(page, "Coverages", "GLCOVER")

    _log.debug(
        "GL Coverages spec: occurrence=%r each_occ=%r gen_aggr=%r pers_adv=%r "
        "prod_oper=%r med=%r dam_prem=%r emp_ben=%r",
        spec.occurrence_type,
        spec.each_occ_limit or "(blank)",
        spec.gen_aggr_limit or "(blank)",
        spec.pers_adv_inj_limit or "(blank)",
        spec.prod_oper_limit or "(blank)",
        spec.med_limit or "(blank)",
        spec.dam_prem_limit or "(blank)",
        spec.emp_ben_limit or "(blank)",
    )

    # Claims Made / Occurrence radio — the data-automation-id sits on a proxy DIV;
    # the actual <input type="radio"> is nested inside.  Click the input directly.
    occ_id = "rbtnClaimsMade" if "claims" in spec.occurrence_type.lower() else "rbtnOccurrence"
    result = page.evaluate(f"""() => {{
        const container = document.querySelector('[data-automation-id="{occ_id}"]');
        if (!container) return 'not_found';
        const radio = container.tagName === 'INPUT'
            ? container
            : container.querySelector('input[type="radio"]');
        if (!radio) return 'no_radio_input';
        radio.click();
        return 'clicked';
    }}""")
    _log.info("Occurrence type radio %s (%r): %s", occ_id, spec.occurrence_type, result)

    _fill_proxy_field(page, "streEachOccLimit",    spec.each_occ_limit)
    _fill_proxy_field(page, "streGenAggrAppLimit",  spec.gen_aggr_limit)
    _fill_proxy_field(page, "strePersAdvInjLimit",  spec.pers_adv_inj_limit)
    _fill_proxy_field(page, "streProdOperLimit",    spec.prod_oper_limit)
    _fill_proxy_field(page, "streMedLimit",         spec.med_limit)
    _fill_proxy_field(page, "streDamPremLimit",     spec.dam_prem_limit)
    _fill_proxy_field(page, "streEmpBenLimit",      spec.emp_ben_limit)
    # Deductibles intentionally not filled — IGA submits without deductibles.

    _log.info("Checking CGL, Policy/Project/Location aggregate applies")
    _check_by_id(page, "GLCOVERchkCommGenLiab")
    _check_by_id(page, "GLCOVERchkAppPolicy")
    _check_by_id(page, "GLCOVERchkAppProject")
    _check_by_id(page, "GLCOVERchkAppLocation")


# ── Hazards ───────────────────────────────────────────────────────────────────

def _fill_hazard_class_code(page: Page, class_code: str, classification: str, row_num: int) -> None:
    """Fill Class code + Classification for one hazard row via the search modal.

    Flow:
    1. Type the class code into #streClassCode.
    2. Click the search icon (real Playwright click — React ignores JS-only events).
       EPIC opens an in-page modal "General Liability Class Codes".
    3. Click "Locate" to run the search (code is pre-filled in the dialog).
    4. Handle results:
       a. One row    → click it → click Save → both fields populate.
       b. Multiple   → filter rows by classification text; click best match; Save.
                       If no description match, click first row and warn.
       c. No rows    → click Cancel → fall back to typing both fields directly.
    5. On any exception (modal never opened, timeout) → fall back to direct fill.
    """
    try:
        cc = page.locator('#streClassCode')
        if not _wait_visible(page, cc, 5_000):
            _log.error("Class code field not visible for hazard %d", row_num)
            return
        cc.first.click(timeout=2_000)
        cc.first.fill(class_code, timeout=2_000)
        page.wait_for_timeout(200)
    except Exception as exc:
        _log.error("Could not fill class code input for hazard %d: %s", row_num, exc)
        return

    # Click the search icon inside the class code field wrapper.
    search_btn = page.locator(
        '[data-test="textField-wrapper"]:has(#streClassCode) [data-test="end-icon-wrapper"]'
    )

    try:
        search_btn.first.click(timeout=3_000)

        # Wait for the "General Liability Class Codes" modal to appear.
        locate_btn = page.get_by_role("button", name="Locate")
        if not _wait_visible(page, locate_btn, 6_000):
            _log.warning("Class code modal did not open for hazard %d; filling directly", row_num)
            _fill_class_direct(page, class_code, classification)
            return

        # Trigger the search (code is already pre-filled in the dialog input).
        locate_btn.first.click(timeout=3_000)
        page.wait_for_timeout(600)

        # Count results in the modal grid.
        rows = page.locator('[data-automation-id*="body-row"]')
        count = rows.count()
        _log.info("Class code %r lookup: %d row(s) found", class_code, count)

        if count == 0:
            _log.warning("Class code %r not in EPIC database; cancelling modal and filling directly", class_code)
            page.get_by_role("button", name="Cancel").first.click(timeout=3_000)
            page.wait_for_timeout(300)
            _fill_class_direct(page, class_code, classification)
            return

        if count == 1:
            rows.first.click(timeout=3_000)
        else:
            # Multiple results — match on classification description.
            if classification:
                matched = rows.filter(has_text=classification)
                if matched.count() > 0:
                    matched.first.click(timeout=3_000)
                    _log.info("Class code %r: matched on classification %r", class_code, classification)
                else:
                    rows.first.click(timeout=3_000)
                    _log.warning(
                        "Class code %r: %d results, no match for %r — picked first row",
                        class_code, count, classification,
                    )
            else:
                rows.first.click(timeout=3_000)
                _log.warning("Class code %r: %d results, no classification to filter on — picked first row", class_code, count)

        # Save the selection.
        page.get_by_role("button", name="Save").first.click(timeout=3_000)
        page.wait_for_timeout(500)
        _log.debug("Class code %r lookup complete", class_code)

    except Exception as exc:
        _log.warning("Class code %r modal failed for hazard %d: %s — filling directly", class_code, row_num, exc)
        # Dismiss modal if still open.
        try:
            page.get_by_role("button", name="Cancel").first.click(timeout=1_500)
        except Exception:
            pass
        _fill_class_direct(page, class_code, classification)


def _fill_class_direct(page: Page, class_code: str, classification: str) -> None:
    """Fallback: type class code and classification directly without the popup."""
    _fill_id_field(page, "streClassCode", class_code)
    if classification:
        _fill_id_field(page, "streClassification", classification)


def _fill_hazards(page: Page, hazards: list[HazardSpec]) -> None:
    """Add hazard rows on CHM-GLHAZARD via the inline detail form.

    Each click of the Add button creates a new blank row and its detail form
    below the grid.  Rows auto-save when navigation moves away; for multiple
    rows the Add click itself commits the previous row and opens the next.
    """
    if not hazards:
        return

    _nav_section(page, "Hazards", "CHM-GLHAZARD")

    add_btn = page.locator('[data-test="vlvwHazards_add"]')

    for i, hz in enumerate(hazards):
        check_cancel()
        _log.info("Adding hazard %d: class_code=%r exposure=%r", i + 1, hz.class_code, hz.exposure)

        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Hazards Add button not visible for row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)

        # Location and building numbers.
        _fill_id_field(page, "inteLocationNumber__textField", str(hz.loc_num))
        _fill_id_field(page, "inteBuildingNumber__textField", str(hz.bldg_num))

        # Class code — fill then use the search button to open the lookup popup.
        # The popup is a new browser window; Playwright catches it via expect_page().
        _fill_hazard_class_code(page, hz.class_code, hz.classification, i + 1)

        # Premium basis combo (React dropdown).
        if hz.premium_basis:
            _fill_react_combo(page, "cboPremiumBasis", hz.premium_basis)

        # Exposure.
        _fill_id_field(page, "streExposure", hz.exposure)

        page.wait_for_timeout(300)
    # Navigating to Contractors (or any next section) commits the last row.


# ── Contractors ───────────────────────────────────────────────────────────────

def _fill_contractors(page: Page, spec: ContractorsSpec) -> None:
    """Navigate to CHM-GLCNTRCT, default all questions to No, fill Type-of-Work fields."""
    _nav_section(page, "Contractors", "CHM-GLCNTRCT")

    # Always default all Yes/No questions to No.
    # Click via JS to bypass the sticky-note overlay that intercepts pointer events.
    _log.info("Defaulting all Contractors questions to No")
    try:
        default_btn = page.locator('button').filter(has_text="Default All Questions")
        if not _wait_visible(page, default_btn, 5_000):
            _log.warning("'Default All Questions to No' button not found on CHM-GLCNTRCT")
        else:
            page.evaluate("""() => {
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.textContent.includes('Default All Questions'));
                if (btn) btn.click();
            }""")
            page.wait_for_timeout(500)
    except Exception as exc:
        _log.warning("Could not click Default All Questions: %s", exc)

    # Type of Work Subcontracted — fill only non-blank values.
    def _lbl(label: str, value: str) -> None:
        if not value:
            _log.debug("contractors %r: skipped (blank)", label)
            return
        try:
            inp = page.get_by_label(label)
            inp.first.click(timeout=2_000)
            inp.first.fill(value, timeout=2_000)
            page.wait_for_timeout(150)
            _log.debug("contractors %r = %r  OK", label, value)
        except Exception as exc:
            _log.warning("contractors %r = %r  FAILED: %s", label, value, exc)

    _lbl("Dollars paid to subcontractors:", spec.dollars_subcontract)
    _lbl("Percent of work subcontracted:", spec.percent_subcontract)
    _lbl("Number of full-time staff:",     spec.num_full_time)
    _lbl("Number of part-time staff:",     spec.num_part_time)
    _lbl("Remarks",                        spec.remarks)


# ── Claims Made / Employee Benefits Liability ─────────────────────────────────

def _fill_masked_date(page: Page, field_id_prefix: str, value: str) -> None:
    """Fill a date masked-input field.

    EPIC date pickers expose an ``<input id="{prefix}-mask">`` element, but
    the prefix can include query-table suffixes (e.g. ``dteRetroactiveDate-mask``
    or ``dteProposedRetroDate;qtf_...-mask``).  We match by ``id*=`` containment
    on the prefix so we don't have to hard-code the full dynamic id.
    """
    if not value:
        _log.debug("fill_date %s: skipped (blank)", field_id_prefix)
        return
    try:
        inp = page.locator(f'input[id*="{field_id_prefix}"]').first
        inp.click(timeout=2_000)
        inp.fill(value, timeout=2_000)
        page.wait_for_timeout(200)
        page.keyboard.press("Tab")
        _log.debug("fill_date %s = %r  OK", field_id_prefix, value)
    except Exception as exc:
        _log.warning("fill_date %s = %r  FAILED: %s", field_id_prefix, value, exc)


def _fill_claims_made_ebl(page: Page, spec: EblSpec) -> None:
    """Navigate to CHM-GLCLMEMP and fill Claims Made / Employee Benefits Liability.

    Always defaults the Claims Made Yes/No questions to No.
    Then fills any non-blank EBL fields (deductible, retroactive date, employee counts).
    """
    _nav_section(page, "ClaimsMadeEmployeeBenefits", "CHM-GLCLMEMP")

    # Default all Claims Made Yes/No questions to No via JS (same sticky-note risk).
    _log.info("Defaulting Claims Made questions to No")
    page.evaluate("""() => {
        const btn = Array.from(document.querySelectorAll('button'))
            .find(b => b.textContent.includes('Default All Questions'));
        if (btn) btn.click();
    }""")
    page.wait_for_timeout(500)

    # Employee Benefits Liability fields.
    _fill_id_field(page, "streDeductiblePerClaim__textField", spec.deductible_per_claim)
    _fill_id_field(page, "inteNumberOfEmployees__textField",         spec.num_employees)
    _fill_id_field(page, "inteNumberOfEmployeesCovered__textField",  spec.num_employees_covered)
    _fill_masked_date(page, "dteRetroactiveDate", spec.retroactive_date)


# ── Products / Completed Operations ──────────────────────────────────────────

def _fill_products(page: Page) -> None:
    """Navigate to CHM-GLPRODUC and default all 10 questions to No.

    The "Default All Questions to 'No'" button is overlapped by the sticky-note
    panel at some scroll positions, so we JS-click it (same pattern as Contractors
    and Claims Made screens).  No per-submission fields are filled here — this
    section is always the fixed default.
    """
    _nav_section(page, "Products", "CHM-GLPRODUC")

    _log.info("Defaulting all Products/Completed Operations questions to No")
    result = page.evaluate("""() => {
        const btn = Array.from(document.querySelectorAll('button'))
            .find(b => b.textContent.includes('Default All Questions'));
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not result:
        _log.warning("'Default All Questions to No' button not found on CHM-GLPRODUC")
    page.wait_for_timeout(500)


# ── Additional Interests ──────────────────────────────────────────────────────

@dataclass
class AdditionalInterestSpec:
    """One Additional Interest entry on CHM-GLADLINT (confirmed via CDP 2026-05-19).

    ``interest_type`` is the EPIC combo value — e.g. "Additional Insured",
    "Certificate Holder", "Loss Payee".  ``name`` is required; address fields
    are optional.
    """
    name:          str
    interest_type: str = ""   # cboInterest combo
    lookup_code:   str = ""   # streLookupCode — EPIC account lookup shortcut
    street:        str = ""   # adeInterest-streetLine
    city:          str = ""   # adeInterest-city
    state:         str = ""   # adeInterest-state  (2-letter)
    zip_code:      str = ""   # adeInterest-zipCode
    reference_no:  str = ""   # streRefNo
    reason:        str = ""   # streReasonForInt
    loc_num:       str = "1"  # inteLocationNumber__textField
    bldg_num:      str = "1"  # inteBuildingNumber__textField


def _fill_additional_interests(page: Page, interests: list[AdditionalInterestSpec]) -> None:
    """Add each Additional Interest row via the inline grid on CHM-GLADLINT.

    Rewritten 2026-05-26 to mirror ``step_inland_marine._fill_im_additional_interests``:
    safe_action + verify_input_value, pre-Add settle, _wait_grid_grew with
    retry, lenient address fields, Claude-abbreviation of reason on overflow.

    GL field IDs differ from IM/Property/BA/Umbrella:
      * Add button is ``vlvwAdditionalInterests_add`` (vs ``vlvwInterest_add``).
      * Address fields are ``adeInterest-*`` (vs ``adePrimary-*``); state is a
        combo on the same prefix.  No SmartyStreets validator on this widget,
        so we go straight to the manual lenient path.
      * Reference number is ``streRefNo`` (vs ``streReferenceNumber``).
    """
    if not interests:
        _log.info("No additional interests; skipping section")
        return

    if not _nav_section(page, "AdditionalInterest", "CHM-GLADLINT"):
        _log.error("Additional Interests: nav failed — aborting section")
        return

    _log.info("=== Additional Interests: %d item(s) ===", len(interests))
    assert_screen_code(page, "CHM-GLADLINT", timeout_ms=8_000)

    add_btn_sel = '[data-test="vlvwAdditionalInterests_add"]'

    for i, ai in enumerate(interests, 1):
        check_cancel()
        name = (ai.name or "").strip()
        if not name:
            _log.warning("AI #%d: blank name — skipping (EPIC requires name)", i)
            continue

        _log.info(
            "--- Row %d/%d: name=%r type=%r loc/bldg=%s/%s",
            i, len(interests), name, ai.interest_type or "-",
            ai.loc_num or "-", ai.bldg_num or "-",
        )

        rows_before = grid_row_count(page, "vlvwAdditionalInterests")
        _log.debug("AI #%d: rows_before=%d", i, rows_before)

        page.wait_for_timeout(750)

        with safe_action(page, context=f"AI #{i}: click Add"):
            page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)

        grew = wait_grid_grew(page, "vlvwAdditionalInterests", rows_before, timeout_ms=3_000)
        if not grew:
            _log.warning("AI #%d: row did not grow after Add — retrying with longer settle", i)
            page.wait_for_timeout(2_500)
            with safe_action(page, context=f"AI #{i}: RETRY Add"):
                page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)
            grew = wait_grid_grew(page, "vlvwAdditionalInterests", rows_before, timeout_ms=5_000)
            if not grew:
                _log.error("AI #%d: row still did not grow after retry — halting run.", i)
                raise EntryCancelled(
                    f"AI #{i}: EPIC rejected Add (twice); halting"
                )
            _log.info("AI #%d: retry succeeded", i)

        if ai.interest_type:
            with safe_action(page, context=f"AI #{i}: combo cboInterest={ai.interest_type!r}"):
                _fill_combo(page, "cboInterest", ai.interest_type)
            _log.debug("AI #%d: cboInterest = %r", i, ai.interest_type)

        if ai.lookup_code:
            fill_field_verified(
                page, "streLookupCode", ai.lookup_code,
                i, "lookup_code", section="AI", logger=_log, lenient=True,
            )
            page.wait_for_timeout(500)

        fill_field_verified(
            page, "streName", name, i, "name",
            section="AI", logger=_log,
        )

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
                street_id="adeInterest-streetLine",
                city_id="adeInterest-city",
                zip_id="adeInterest-zipCode",
                address2_id="adeInterest-address2",
                state_combo_id="adeInterest-state",
                fill_validated_fn=None,  # GL widget has no validator dropdown
                fill_combo_fn=_fill_combo,
                logger=_log,
            )

        if ai.reference_no:
            fill_field_verified(
                page, "streRefNo", ai.reference_no,
                i, "ref#", section="AI", logger=_log,
            )

        if ai.reason:
            short_reason = abbreviate_if_too_long(
                ai.reason, max_chars=AI_REASON_MAX_CHARS,
                row_idx=i, label="reason", section="AI", logger=_log,
            )
            fill_field_verified(
                page, "streReasonForInt", short_reason,
                i, "reason", section="AI", logger=_log,
            )

        if ai.loc_num:
            fill_field_verified(
                page, "inteLocationNumber__textField", ai.loc_num,
                i, "loc#", section="AI", logger=_log,
            )
        if ai.bldg_num:
            fill_field_verified(
                page, "inteBuildingNumber__textField", ai.bldg_num,
                i, "bldg#", section="AI", logger=_log,
            )

        page.wait_for_timeout(300)
        _log.debug("AI #%d: row complete", i)

    _log.info("=== Additional Interests: complete (%d row(s) processed) ===", len(interests))


# ── General Information ───────────────────────────────────────────────────────

def _fill_general_information(page: Page) -> None:
    """Navigate to CHM-GLGI2014 and default all 22 questions to No.

    Sidebar key ``GeneralInfo201404`` and screen code ``CHM-GLGI2014`` confirmed
    via CDP 2026-05-19.  The "Default All Questions to 'No'" is a real <button>
    (not overlapped), so JS click is used for consistency with other screens.
    """
    _nav_section(page, "GeneralInfo201404", "CHM-GLGI2014")

    _log.info("Defaulting all GL General Information questions to No")
    result = page.evaluate("""() => {
        const btn = Array.from(document.querySelectorAll('button'))
            .find(b => b.textContent.includes('Default All Questions'));
        if (btn) { btn.click(); return true; }
        return false;
    }""")
    if not result:
        _log.warning("'Default All Questions to No' button not found on CHM-GLGI2014")
    page.wait_for_timeout(500)

    # IGA fixed override: Q21 "formal, written safety and security policy" → Yes.
    # Field ID uses qtf_ suffix: cboQuestion21;qtf_qtblQuestions_P21_Answer.
    # Use id*= containment to match regardless of the dynamic suffix.
    _log.info("Setting GL General Information Q21 (formal safety policy) to Yes")
    try:
        inp = page.locator('input[id*="cboQuestion21"]').first
        inp.click(timeout=2_000)
        page.wait_for_timeout(300)
        rows = page.locator('[data-test="dropdown-row"]')
        matched = False
        for n in range(rows.count()):
            if rows.nth(n).text_content().strip().lower() == "yes":
                rows.nth(n).click(timeout=2_000)
                matched = True
                break
        if not matched and rows.count() > 0:
            # Fallback: fill directly.
            inp.fill("Yes", timeout=1_500)
        _log.debug("GL General Information Q21 set to Yes")
    except Exception as exc:
        _log.warning("Could not set GL General Information Q21 to Yes: %s", exc)


# ── Forms & Endorsements ──────────────────────────────────────────────────────

@dataclass
class FormSpec:
    """One form / endorsement row on CHM-GLFRMEND (confirmed via CDP 2026-05-19).

    ``name`` is the endorsement name as it appears in EPIC.
    ``form_number`` is optional — leave blank if the form has no standard number.
    Tab after Number triggers EPIC's library lookup which auto-populates Name.
    ``edition_date`` format: MM/DD/YYYY (EPIC masked-input ``dteEdition-mask``).

    Confirmed field IDs (React, ``__textField`` suffix on loc/bldg/premium):
      streNumber, streName, dteEdition-mask, cboCopyrightType, streCopyrightCode,
      curePremium__textField, inteLocationNumber__textField, inteBuildingNumber__textField
    """
    name:           str
    form_number:    str = ""   # streNumber (blank = name-only entry)
    edition_date:   str = ""   # dteEdition-mask
    copyright_code: str = ""   # streCopyrightCode
    copyright_type: str = ""   # cboCopyrightType
    premium:        str = ""   # curePremium__textField


# IGA fixed GL endorsements — added on every GL submission regardless of data.
GL_STANDARD_FORMS: list[FormSpec] = [
    FormSpec(name="Blanket by Written Contract - Additional Insured"),
    FormSpec(name="Blanket by Written Contract - Waiver of Subrogation"),
    FormSpec(name="Blanket by Written Contract - Primary and Non-Contributory"),
    FormSpec(name="General Liability Extension Endorsement"),
]


def _fill_forms_endorsements(page: Page, forms: list[FormSpec]) -> None:
    """Add each form / endorsement row on CHM-GLFRMEND.

    Always called with ``GL_STANDARD_FORMS`` merged with any submission-specific
    forms.  Clicking Add opens the inline detail form; navigating away auto-saves.

    If ``form_number`` is blank, fills Name directly without Tab lookup.
    If ``form_number`` is provided, fills Number → Tab (triggers EPIC library
    lookup → auto-populates Name) → then fills remaining fields.
    """
    if not forms:
        return

    _nav_section(page, "FormEndorsement", "CHM-GLFRMEND")

    add_btn = page.locator('[data-test="vlvwFormsEnd_add"]')

    for i, frm in enumerate(forms):
        check_cancel()
        _log.info("Adding form %d: %r (number=%r)", i + 1, frm.name, frm.form_number or "(none)")

        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Forms & Endorsements Add button not visible for row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)

        if frm.form_number:
            # Number → Tab triggers EPIC library lookup → auto-populates Name.
            _fill_id_field(page, "streNumber", frm.form_number)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)

        # Name — fill always (required; library lookup may not find custom endorsements).
        _fill_id_field(page, "streName", frm.name)

        _fill_masked_date(page, "dteEdition",      frm.edition_date)
        _fill_id_field(page, "streCopyrightCode",  frm.copyright_code)
        if frm.copyright_type:
            try:
                _fill_combo(page, "cboCopyrightType", frm.copyright_type)
            except Exception as exc:
                _log.warning("Could not set copyright type %r for form %d: %s", frm.copyright_type, i + 1, exc)
        _fill_id_field(page, "curePremium__textField", frm.premium)

        page.wait_for_timeout(300)


# ── Additional Coverages ──────────────────────────────────────────────────────

@dataclass
class AdditionalCoverageSpec:
    """One Additional Coverage row on GLADDCOV (confirmed via CDP 2026-05-19).

    Column mapping (grid display name → field):
      Coverage Name  → description  (streDescription)
      Each Claim     → each_claim   (streLimit)
      Aggregate      → aggregate    (streLimit2)
      Deductible     → deductible   (streDeductible)
      Ded Type       → readonly; EPIC auto-populates from coverage code
      Retro Date     → retro_date   (dteCoverageEffectiveDate)

    GLADDCOV uses the old Angular proxy pattern: all fields are proxy DIVs with
    ``data-automation-id``; the bare ``<input>`` inside has no id/name.
    Use ``_fill_proxy_field`` (not ``_fill_id_field``) for all fills.
    Add button: ``[name="vlvwCoverage"] .icon-button[title="Add"]``
    streDeductibleType is readonly in EPIC (A-Z/0-9 coded); do not fill it.
    """
    description: str
    code:        str = ""   # streCode
    each_claim:  str = ""   # streLimit   (Limit 1 / Each Claim)
    aggregate:   str = ""   # streLimit2  (Aggregate)
    deductible:  str = ""   # streDeductible
    retro_date:  str = ""   # dteCoverageEffectiveDate  MM/DD/YYYY
    form_number: str = ""   # streFormNumber


def _fill_additional_coverages(page: Page, coverages: list[AdditionalCoverageSpec]) -> None:
    """Add each Additional Coverage row on GLADDCOV.

    GLADDCOV uses the old Angular proxy pattern — NOT the React ``__textField``
    pattern.  All fills go through ``_fill_proxy_field`` which targets
    ``[data-automation-id="{name}"] input``.

    Add button has no ``data-test``; targeted via ``[name="vlvwCoverage"] .icon-button[title="Add"]``.
    Must use real Playwright ``.click()`` (React/Angular event handlers).
    Rows auto-save on navigation away from the section.
    """
    if not coverages:
        return

    _nav_section(page, "AdditionalCoverage", "GLADDCOV")

    add_btn = page.locator('[name="vlvwCoverage"] .icon-button[title="Add"]')

    for i, cov in enumerate(coverages):
        check_cancel()
        _log.info("Adding additional coverage %d: %r", i + 1, cov.description)

        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Additional Coverage Add button not visible for row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)

        short_desc = abbreviate_if_too_long(
            cov.description, max_chars=AC_DESC_MAX_CHARS,
            row_idx=i + 1, label="description", section="AC", logger=_log,
        )
        _fill_proxy_field(page, "streDescription",         short_desc)
        _fill_proxy_field(page, "streCode",                cov.code or "N/A")
        _fill_proxy_field(page, "streLimit",               cov.each_claim)
        _fill_proxy_field(page, "streLimit2",              cov.aggregate)
        _fill_proxy_field(page, "streDeductible",          cov.deductible)
        # streDeductibleType is readonly — EPIC auto-populates it from the coverage code
        _fill_proxy_field(page, "streFormNumber",          cov.form_number)

        # Date fields are also proxy DIVs; click the inner input and type.
        if cov.retro_date:
            try:
                inp = page.locator('[data-automation-id="dteCoverageEffectiveDate"] input')
                inp.first.click(timeout=2_000)
                inp.first.fill(cov.retro_date, timeout=2_000)
                page.keyboard.press("Tab")
                page.wait_for_timeout(200)
                _log.debug("fill_proxy dteCoverageEffectiveDate = %r  OK", cov.retro_date)
            except Exception as exc:
                _log.warning("Could not fill retro_date %r for coverage %d: %s", cov.retro_date, i + 1, exc)

        page.wait_for_timeout(300)


# ── Public API ────────────────────────────────────────────────────────────────

def run(page: Page, setup: GeneralLiabilitySetup) -> bool:
    """Fill the General Liability sections of the open submission.

    Section order follows the EPIC GL sidebar (top to bottom):
      Coverages → Hazards → Claims Made/EBL → Contractors → Products →
      Additional Interest → Form Endorsement → Additional Coverage →
      General Information

    Claims Made/EBL navigation commits the last pending hazard row (auto-save
    trigger), so that ordering must be preserved.
    """
    try:
        _log.info("Navigating to General Liability in submission sidebar")
        _nav_to_general_liability(page)
        checkpoint(page, "General Liability: navigation")

        _log.info("Filling GL Coverages")
        checkpoint(page, "General Liability: Coverages")
        _fill_coverages(page, setup.coverages)

        if setup.hazards:
            _log.info("Filling GL Hazards (%d row(s))", len(setup.hazards))
            checkpoint(page, "General Liability: Hazards")
            _fill_hazards(page, setup.hazards)

        # Always navigate to Claims Made/EBL — also commits any pending hazard row.
        _log.info("Filling Claims Made / Employee Benefits Liability")
        checkpoint(page, "General Liability: Claims Made / EBL")
        _fill_claims_made_ebl(page, setup.ebl)

        _log.info("Filling GL Contractors")
        checkpoint(page, "General Liability: Contractors")
        _fill_contractors(page, setup.contractors)

        _log.info("Defaulting Products/Completed Operations questions to No")
        checkpoint(page, "General Liability: Products/Completed Operations")
        _fill_products(page)

        # Additional Interests — rewritten 2026-05-26 to mirror the IM AI
        # pattern (safe_action + verify, _wait_grid_grew, lenient address
        # fields, Claude reason abbreviation).  GL uses adeInterest-* IDs
        # and the vlvwAdditionalInterests grid (no SmartyStreets validator).
        if setup.additional_interests:
            _log.info("Adding %d Additional Interest(s)", len(setup.additional_interests))
            checkpoint(page, "General Liability: Additional Interests")
            _fill_additional_interests(page, setup.additional_interests)

        # Always include the 4 IGA standard GL endorsements; merge with any
        # submission-specific forms passed in setup.forms.
        all_forms = GL_STANDARD_FORMS + list(setup.forms)
        _log.info("Adding %d Form(s) & Endorsement(s) (%d IGA standard + %d submission-specific)",
                  len(all_forms), len(GL_STANDARD_FORMS), len(setup.forms))
        checkpoint(page, "General Liability: Forms & Endorsements")
        _fill_forms_endorsements(page, all_forms)

        if setup.additional_coverages:
            _log.info("Adding %d Additional Coverage(s)", len(setup.additional_coverages))
            checkpoint(page, "General Liability: Additional Coverages")
            _fill_additional_coverages(page, setup.additional_coverages)

        if setup.fill_general_info:
            _log.info("Defaulting GL General Information questions to No")
            checkpoint(page, "General Liability: General Information")
            _fill_general_information(page)

        _log.info("General Liability step complete")
        return True

    except EntryCancelled:
        raise
    except PlaywrightTimeout as exc:
        _log.error("General Liability timed out: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_general_liability: %s", exc)
        return False

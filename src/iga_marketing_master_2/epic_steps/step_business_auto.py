"""step_business_auto.py — fill the Business Auto (BAUT) screens inside a submission.

Pre-condition
-------------
The browser is on the Submission Detail sidebar tree for the target MMS, with at
least one BAUT line already created by the user.  Each BAUT line is state-specific
(one per state of garaging).

Sections filled (in order)
---------------------------
1. **Coverages** (CHM-BAPCOV{STATE})
   State-specific screen: TN → CHM-BAPCOVTN, KY → CHM-BAPCOVKY, etc.
   Mix of:
     - Symbol checkboxes per coverage (Liability, Medical, Uninsured, Towing,
       Comprehensive, Specified Causes of Loss, Collision)
     - Text limit fields (CSL / BI limits / PD limit / Med limit / etc.)

2. **Vehicles** (BAVEHICL) — OLD Angular proxy
   Per-vehicle screen with THREE sub-tabs:
     - tpgVehicleTab (Description) — year, make, model, VIN, body type, garage
     - tpgRatingTab  (Rating)      — GVW/age (not populated from current state.json)
     - tpgCoveragesTab (Coverages) — comprehensive_deductible, collision_deductible

3. **Additional Interests** (BAUT AdditionalInterest) — React vlvwInterest pattern.

4. **Additional Coverages** (BAUT AdditionalCoverage) — Old proxy vlvwCoverage pattern.

Skipped sections (per IGA spec)
--------------------------------
Drivers (no state.json data), General Information, Remarks, Supplemental Screens,
Additional Attachments, Document View.  Forms & Endorsements: only IGA standard
forms are added on every submission (none defined yet for BAUT).

Selector notes (confirmed via live CDP inspection 2026-05-20)
--------------------------------------------------------------
- BAUT line (level-3 sidebar): ``[data-automation-id$=";{STATE} level-3"]``
  Example: ``sidebar-button-163;225908;TN level-3``
- BAUT sub-sections (level-4): ``[data-automation-id^="sidebar-button-Policy.BusinessAuto.{section}"]``
  After clicking a BAUT level-3 entry, only THAT state's level-4 entries are
  expanded — so prefix-match is sufficient.
  Exception: Coverages is state-suffixed (``CoverageTN``, ``CoverageKY``, …).
- Coverages: symbol checkboxes ``chkLiability1..9``, ``chkMedical2..8``, etc.
  Limit fields: ``streLiabilityCSLLimit1``, ``streLiabilityBILimit1``, ``streLiabilityBILimit2``, …
- Vehicles list: ``[data-automation-id="vlvwVehicles"]`` with toolbar Add =
  ``[name="vlvwVehicles"] .icon-button[title="Add"]``.
- Vehicle tabs: ``[data-automation-id="tpgVehicleTab|tpgRatingTab|tpgCoveragesTab"]``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled, check_cancel
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

from iga_marketing_master_2.epic_steps.step_mms_create import (
    _fill_combo,
    _fill_text,
    _wait_visible,
)
from iga_marketing_master_2.epic_steps.step_property import (
    _dismiss_modal,
    _fill_id_field,
    _fill_proxy_field,
    _fill_react_combo,
)
from iga_marketing_master_2.epic_steps.step_general_liability import FormSpec as _GLFormSpec
from iga_marketing_master_2.epic_steps.step_general_liability import (
    _fill_masked_date as _gl_fill_masked_date,
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

_log = logging.getLogger("iga.epic_steps.business_auto")

_CLICK_TIMEOUT = 5_000
_NAV_WAIT_MS   = 1_200   # settle after sidebar navigation
_FILL_WAIT_MS  = 150     # brief pause after each field fill
_COV_FILL_WAIT_MS = 400  # longer pause on Coverages screen — EPIC's blur-commit cycle is slow there
_COV_COMMIT_MS    = 1_800  # final settle before navigating away from Coverages


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class VehicleSpec:
    """One vehicle row on BAVEHICL.

    Description tab (tpgVehicleTab):
      vehicle_num   → inteVehicleNum
      year          → inteYear
      make          → streMake
      model         → streModel
      vin           → streVIN
      body_type     → cboBodyType (combo) / streBodyTypeIfOther (free-text override)
      garage_address→ chkDefaultAddress (checkbox — auto-fills from account address)

    Rating tab (tpgRatingTab):
      cost_new      → inteCostNew (integer)
      class_code    → streClass   (free text; optional, often blank)

    Coverages tab (tpgCoveragesTab):
      valuation_type           → cboValuationType ("ACV", "Stated Amount", …)
      comprehensive_deductible → streComprehensiveDeductible
      collision_deductible     → streCollisionDeductible
    """
    vehicle_num: str = ""
    year:        str = ""
    make:        str = ""
    model:       str = ""
    vin:         str = ""
    body_type:   str = ""
    garage_address: str = ""
    # Rating tab
    cost_new:    str = ""
    class_code:  str = ""
    # Coverages tab
    valuation_type: str = ""
    comprehensive_deductible: str = ""
    collision_deductible:     str = ""


@dataclass
class AutoCoverageSpec:
    """Coverages screen (CHM-BAPCOV{STATE}) field set.

    Limit fields are text inputs.  Symbols fields are comma-separated digits
    ("1, 7, 8, 9") that map to per-coverage checkboxes (chkLiability1..9, etc.).
    All values are optional — blank values are skipped.
    """
    # Liability
    liability_symbols:      str = ""   # csv → chkLiability1..9
    liability_csl_limit1:   str = ""
    liability_bi_limit1:    str = ""   # per accident (larger)
    liability_bi_limit2:    str = ""   # per person  (smaller)
    liability_pd_limit1:    str = ""
    # Medical payments
    medical_symbols:        str = ""   # csv → chkMedical2..8
    medical_limit1:         str = ""
    # Uninsured / underinsured
    uninsured_symbols:      str = ""   # csv → chkUninsured2..7
    uninsured_csl_limit1:   str = ""
    uninsured_bi_limit1:    str = ""
    uninsured_bi_limit2:    str = ""
    uninsured_pd_each_accident: str = ""
    uninsured_pd_deductible:    str = ""
    # Towing & labor
    towing_symbols:         str = ""   # csv → chkTowing3,7
    towing_limit1:          str = ""
    # Comprehensive
    comprehensive_symbols:  str = ""   # csv → chkComprehensive2..8
    comprehensive_deductible1: str = ""
    # Specified causes of loss
    cause_of_loss_symbols:  str = ""   # csv → chkCauseOfLoss2..8
    cause_of_loss_deductible1: str = ""
    # Collision
    collision_symbols:      str = ""   # csv → chkCollision2..8
    collision_deductible1:  str = ""


@dataclass
class AutoAdditionalInterestSpec:
    """One Additional Interest row on BAUT > AdditionalInterest.

    Mirrors policy.auto.additional_interest.* domain tags in state.json.
    """
    name:           str
    interest_type:  str = ""   # "Additional Insured - Owner of Auto", "Loss Payee", etc.
    vehicle_number: str = ""   # which vehicle this AI is attached to
    address_line_1: str = ""


@dataclass
class AutoAdditionalCoverageSpec:
    """One Additional Coverage row on BAUT > AdditionalCoverage."""
    description:    str
    code:           str = ""
    limit1:         str = ""
    deductible:     str = ""


@dataclass
class BusinessAutoSetup:
    """Top-level container for one BAUT line entry.

    Attributes
    ----------
    state                 — 2-letter state code matching the BAUT line (e.g. "TN")
    coverages             — Coverages screen values
    vehicles              — list of vehicles (filled in order)
    additional_interests  — list of AIs (filled in order)
    additional_coverages  — list of ACs (filled in order)
    """
    state: str
    coverages:            AutoCoverageSpec                = field(default_factory=AutoCoverageSpec)
    vehicles:             list[VehicleSpec]               = field(default_factory=list)
    additional_interests: list[AutoAdditionalInterestSpec]= field(default_factory=list)
    additional_coverages: list[AutoAdditionalCoverageSpec]= field(default_factory=list)


# ── Address helpers ───────────────────────────────────────────────────────────

_US_STATE_ZIP_RE = re.compile(r"^\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")


def _fill_validated_address(page: Page, street: str, full_address: str) -> bool:
    """Fill an EPIC validated-address field via its suggestion dropdown.

    EPIC's address widget (``[data-test="streetLine"]``) is a SuggestionField:
    type into the street input and a dropdown of validated USPS addresses
    appears (``[data-test="streetLine"] [data-test="dropdown-row"]``).
    Clicking the right row auto-populates street + city + state + zip+4 +
    county + country in one go.

    When multiple rows appear (common for streets like "Main St"), we score
    each candidate by how many components of *full_address* it contains
    (city, state, zip) and pick the highest-scoring row.  Ties broken by
    first-appearance order.

    Returns True if a row was picked, False if the dropdown never opened.
    """
    if not street:
        return False
    try:
        inp = page.locator('#adePrimary-streetLine')
        if not _wait_visible(page, inp, 3_000):
            _log.warning("adePrimary-streetLine not visible")
            return False
        inp.first.click(timeout=2_000)
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.keyboard.type(street, delay=25)
        page.wait_for_timeout(800)  # let the validator query return

        # Wait for any dropdown row to appear inside the streetLine widget
        rows_loc = page.locator('[data-test="streetLine"] [data-test="dropdown-row"]')
        if not _wait_visible(page, rows_loc, 2_500):
            _log.debug("AI address: no suggestion rows appeared for street=%r", street)
            return False

        # Score every row against the full address
        rows_text: list[str] = []
        try:
            count = rows_loc.count()
            for i in range(min(count, 25)):
                t = (rows_loc.nth(i).text_content() or "").strip()
                rows_text.append(t)
        except Exception as exc:
            _log.warning("AI address: row enumeration failed: %s", exc)
            return False
        if not rows_text:
            return False

        addr_low = (full_address or "").lower()
        # Pull the city/state/zip out of the full address for component scoring
        _street, city, state, zip_code = _split_us_address(full_address)
        components = [c.lower() for c in (city, state, zip_code) if c]

        best_idx = 0
        best_score = -1
        for i, t in enumerate(rows_text):
            t_low = t.lower()
            score = 0
            for c in components:
                if c and c in t_low:
                    score += 1
            # Bonus if the entire address string substring-matches the row
            if addr_low and addr_low.replace(",", "").replace("  ", " ") in t_low.replace(",", "").replace("  ", " "):
                score += 2
            if score > best_score:
                best_score = score
                best_idx = i

        if len(rows_text) > 1:
            _log.info(
                "AI address: %d suggestions — picked [%d] %r (score=%d) out of %r",
                len(rows_text), best_idx, rows_text[best_idx], best_score, rows_text,
            )
        else:
            _log.debug("AI address: 1 suggestion → %r", rows_text[0])

        rows_loc.nth(best_idx).click(timeout=2_000)
        page.wait_for_timeout(600)  # let EPIC populate the other fields
        return True
    except Exception as exc:
        _log.warning("AI address validator failed: %s", exc)
        return False


def _split_us_address(line: str) -> "tuple[str, str, str, str]":
    """Parse a free-form US address into (street, city, state, zip).

    Accepts the comma-separated format that state.json's
    ``policy.auto.additional_interest.primary_address.line_1`` provides, e.g.:
        "2960 Bunn School Rd, Henry, TN 38231"
        "123 Main St, Apt 4B, Nashville, TN 37201-1234"

    Strategy:
      - Split on commas.
      - Last segment is "STATE ZIP" (2 letters + 5 or 9 digits).
      - Second-to-last segment is the city.
      - All earlier segments are joined back into the street (preserves any
        "Suite N" / "Apt 4" segments that legitimately use commas).

    If the line doesn't match the expected pattern, returns (line, "", "", "")
    so callers can fall back to filling the whole thing into the street field.
    """
    if not line or "," not in line:
        return (line.strip(), "", "", "")
    parts = [p.strip() for p in line.split(",") if p.strip()]
    if len(parts) < 3:
        return (line.strip(), "", "", "")
    state_zip_match = _US_STATE_ZIP_RE.match(parts[-1])
    if not state_zip_match:
        return (line.strip(), "", "", "")
    state, zip_code = state_zip_match.group(1).upper(), state_zip_match.group(2)
    city = parts[-2]
    street = ", ".join(parts[:-2])
    return (street, city, state, zip_code)


# ── Additional Interest (cboInterest) directory ───────────────────────────────
#
# EPIC's Additional Interest combo uses a 2-character code that maps to a
# longer description.  The combo input stores the CODE; the description appears
# in the dropdown row as `{CODE}{DESCRIPTION}`.  When state.json provides a
# description (e.g. "Additional Insured - Owner of Auto"), we look up the
# matching code and feed it to the combo — far more reliable than typing the
# description and hoping the autocomplete picks the right row.
#
# Captured via live CDP inspection 2026-05-20 (114 entries).  Keep in sync with
# EPIC: re-scroll the dropdown after EPIC version upgrades.

_AI_INTEREST_CODES: dict[str, str] = {
    "A1": "OLT - Architects, Engineers, or Surveyors (use AG instead)",
    "A2": "OLT - Bicycle Liability",
    "A3": "Members of Faculties and Teaching Staffs",
    "A4": "OLT - States, Counties, Cities, or other Governmental Units (use SC instead)",
    "A5": "OLT - Employees other than executive officers (use EO instead)",
    "A6": "OLT - All other (use OT instead)",
    "A7": "Additional Insured - State or Political Sub-divisions - Permits",
    "A8": "OLT - Additional Insured - Volunteers (use VO instead)",
    "AB": "Additional Bill Party",
    "AC": "Accounting Contact",
    "AD": "Additional Interest",
    "AE": "Applicant's Employer",
    "AF": "Attorney in Fact",
    "AG": "Architects, Engineers or Surveyors",
    "AH": "Additional Insured -- Other Members of Household",
    "AI": "Additional Insured",
    "AL": "Additional Interest Lessor",
    "AM": "Additional Insured and Mortgagee",
    "AN": "Additional Named Insured",
    "AP": "Alternate Bill Party",
    "AS": "Additional Insured Lessor",
    "AT": "Additional Insured Lessor and Loss Payee",
    "AU": "Audit Contact",
    "B1": "M&C - Architects, Engineers, or Surveyors (use AG instead)",
    "B2": "M&C - Employees other than executive officers (use EO instead)",
    "B3": "Gasoline or Oil Dealers",
    "B4": "M&C - Owners, managers, or Operators of Premises (use OM instead)",
    "B5": "Owners or Contractors",
    "B6": "M&C - States, Counties, Cities or other Governmental Units - issued to owner or lease (use SC)",
    "B7": "M&C - States, Counties, Cities, or other Governmental Units - issued to contractors (use SC instead)",
    "B8": "M&C - Volunteer Workers (use VO instead)",
    "B9": "M&C - All Other (use OT instead)",
    "BB": "M&C - Additional Insured - Volunteers (use VO instead)",
    "BN": "Beneficiary",
    "BO": "Beneficial Owner",
    "BW": "Breach of warranty",
    "C1": "OCP - Architects, Engineers, or Surveyors (use AG instead)",
    "C2": "OCP - Employees other than Executive Officers (use EO instead)",
    "C3": "OCP - States, Counties, Cities, or other Governmental Units (use SC instead)",
    "CC": "Claim Contact",
    "CD": "Contract Holder",
    "CE": "Co-Applicant's Employer",
    "CH": "Certificate Holder",
    "CI": "Controlling Interest",
    "CN": "Co-Owner/Non-Occupant",
    "CO": "Corporate Owner",
    "CP": "Contract Purchaser",
    "CR": "Co-Owner of Insured Premises",
    "CS": "Contract Seller",
    "CT": "Additional Insured - Concessionaires Trading Under Your Name",
    "CW": "Co-Owner/Occupant",
    "CZ": "Co-Owner",
    "DA": "Disaster Agency",
    "DI": "Designated Insured",
    "DP": "Designated Person or Organization",
    "E1": "Concessionaires of premises on policies covering lessors",
    "E2": "Lessors on policies covering concessionaires on their premises",
    "E3": "Vendors - Limited Form",
    "E4": "Vendors - Broad Form",
    "EC": "Equity Line of Credit",
    "EL": "Employee as Lessor",
    "EO": "Employees Other than Executive Officers",
    "ES": "Escrow Corporation",
    "EX": "Executor",
    "F1": "Blanket Contract",
    "FC": "Finance Company",
    "FO": "Fee Owner",
    "FR": "Franchisor",
    "G1": "Specific Contract",
    "GR": "Grantor",
    "GS": "Governmental Subdivisions",
    "IC": "Inspection Contact",
    "IL": "Additional Insured and Lienholder",
    "LA": "Loss Payee and Additional Interest",
    "LB": "Leaseback owner",
    "LC": "Additional Interest -- Loss Payable Conditions",
    "LF": "Assisted Living Care Facility",
    "LH": "Lienholder",
    "LI": "Loss Payee and Additional Insured",
    "LL": "Land Lease Interests",
    "LO": "Lenders Loss Payable",
    "LP": "Loss Payee",
    "LS": "Lessor of Leased Equipment",
    "M1": "Storekeepers - Owners, Managers or operators of Premises (use OM instead)",
    "MA": "Mortgagee, Assignee or Receiver",
    "MG": "Mortgagee",
    "ML": "Manager/Lessor",
    "MP": "Mortgagee and Loss Payee",
    "MS": "Mortgage Servicing Agency",
    "MU": "Municipality",
    "OC": "Owner, Lessee, Contractor (Form B)",
    "OL": "Owner, Lessee, Contractor (Form A)",
    "OM": "Owners, Managers or Operators of Premises",
    "ON": "Owner",
    "OO": "Owner/Operator",
    "OT": "Other",
    "PA": "Political Agency",
    "PI": "Loss payee, Additional Insured, and Additional Interest",
    "PL": "Personal Liability",
    "PS": "State or Political Subdivision Permits Relating to Premises",
    "RG": "Registrant",
    "RO": "Registered Owner",
    "SA": "Contract of Sale",
    "SC": "States, Counties, Cities or Other Governmental Units",
    "SL": "Secured Loss Payee",
    "ST": "Additional Interest - Student Away from Home",
    "TA": "Townhouse Association",
    "TE": "Trustee",
    "TH": "Title Holder",
    "TR": "Trust",
    "UO": "Condominium Unit Owner",
    "VN": "Vendor",
    "VO": "Additional Insured - Volunteers",
    "VR": "Vehicle Registrant",
}

# Common state.json description → EPIC code aliases.  These cover the
# inconsistent phrasing Claude/extractors produce for the same underlying role.
_AI_INTEREST_ALIASES: dict[str, str] = {
    # Auto-specific phrasings that don't have an exact dropdown entry
    "additional insured - owner of auto":            "AI",
    "additional insured - owner of vehicle":         "AI",
    "owner of auto":                                 "AI",
    # Common shorthand
    "additional insured":                            "AI",
    "loss payee":                                    "LP",
    "lienholder":                                    "LH",
    "lien holder":                                   "LH",
    "mortgagee":                                     "MG",
    "vendor":                                        "VN",
    "owner":                                         "ON",
    "registered owner":                              "RO",
    "vehicle registrant":                            "VR",
    "designated insured":                            "DI",
    "additional named insured":                      "AN",
    "trustee":                                       "TE",
    "trust":                                         "TR",
    "certificate holder":                            "CH",
    "loss payee and additional insured":             "LI",
    "additional insured and mortgagee":              "AM",
    "additional insured and lienholder":             "IL",
    "mortgagee and loss payee":                      "MP",
}


def _resolve_ai_interest(value: str) -> str:
    """Map a state.json interest description to the 2-char EPIC code.

    Resolution order:
      1. Empty → return ""
      2. 2-char uppercase that exists in directory → return as-is
      3. Lowercased value matches an alias → return alias code
      4. Lowercased value equals a directory description (exact) → return that code
      5. Lowercased value is a substring of a directory description (or vice versa) → return that code
      6. No match → return original value (let the combo do its best) + log warning
    """
    if not value:
        return ""
    raw = value.strip()
    # (2) direct code
    if len(raw) == 2 and raw.upper() in _AI_INTEREST_CODES:
        return raw.upper()
    # (3) alias
    key = raw.lower()
    if key in _AI_INTEREST_ALIASES:
        code = _AI_INTEREST_ALIASES[key]
        _log.debug("AI interest %r → alias → %s (%s)", value, code, _AI_INTEREST_CODES.get(code, ""))
        return code
    # (4) exact description
    for code, desc in _AI_INTEREST_CODES.items():
        if desc.lower() == key:
            _log.debug("AI interest %r → exact desc → %s", value, code)
            return code
    # (5) substring fuzzy
    for code, desc in _AI_INTEREST_CODES.items():
        desc_low = desc.lower()
        if key in desc_low or desc_low in key:
            _log.debug("AI interest %r → fuzzy desc → %s (%s)", value, code, desc)
            return code
    _log.warning("AI interest %r — no directory match; passing raw to combo", value)
    return raw


# ── Symbol-string parsing ─────────────────────────────────────────────────────

_SYMBOL_RE = re.compile(r"[0-9]+")


def _parse_symbols(csv: str) -> set[int]:
    """Parse a comma-separated symbol string like '1, 7, 8, 9' → {1, 7, 8, 9}.

    Tolerates extra whitespace, missing commas, and non-numeric junk.
    Non-numeric input (e.g. '2A' from a buggy extraction) skips the letter
    suffix and only keeps the digits ('2A' → {2}).
    """
    if not csv:
        return set()
    return {int(m) for m in _SYMBOL_RE.findall(csv)}


# ── Navigation ────────────────────────────────────────────────────────────────

def _nav_to_business_auto_state(page: Page, state: str, max_attempts: int = 4) -> bool:
    """Expand the BAUT line in the sidebar for the given 2-letter state code.

    The BAUT level-3 sidebar buttons all have label 'Business Auto' but their
    ``data-automation-id`` ends with ``;{STATE} level-3``.  **Critical**: the
    same ``;{STATE} level-3`` suffix is shared by GL, WC, and any other
    state-suffixed LOB — so we MUST also filter by ``textContent === "Business
    Auto"`` to avoid clicking GL/WC by accident.  Mirrors the pattern in
    :func:`step_workers_comp._nav_to_workers_comp_state` and the IM / Umbrella
    nav functions.

    Robustness layers (added 2026-05-20 after a KY run failed to find the
    element on first try):
      1. Before searching for level-3, ensure the Policies (level-1) tree and
         the active MMS (level-2) are both expanded.  EPIC collapses these
         when navigating away from the Policies area.
      2. JS-click the matching element — bypasses Playwright's actionability
         check when the row is in the DOM but not yet fully visible.
    """
    state_upper = state.upper()

    for attempt in range(1, max_attempts + 1):
        # Step 1: ensure tree is expanded before searching.  Cheap to repeat.
        try:
            _expand_policy_tree(page)
        except Exception as exc:
            _log.debug("BAUT %s: tree-expand pre-step failed: %s", state_upper, exc)

        # Step 2: iterate every level-3 entry whose suffix matches the target
        # state, and click the FIRST one whose visible label is exactly
        # "Business Auto".  Skips GL TN / WC TN / etc. that share the suffix.
        clicked = page.evaluate(
            """(s) => {
                const candidates = document.querySelectorAll(
                    `[data-automation-id$=";${s} level-3"]`
                );
                for (const el of candidates) {
                    const txt = (el.textContent || '').trim();
                    if (txt === 'Business Auto') {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return true;
                    }
                }
                return false;
            }""",
            state_upper,
        )
        if clicked:
            page.wait_for_timeout(_NAV_WAIT_MS)
            _log.debug(
                "Clicked BAUT %s sidebar parent (attempt %d/%d)",
                state_upper, attempt, max_attempts,
            )
            return True

        _log.warning(
            "BAUT %s sidebar entry not found — attempt %d/%d; waiting 2 s",
            state_upper, attempt, max_attempts,
        )
        page.wait_for_timeout(2_000)

    # Final failure: list the BAUT states that ARE present on this submission
    # so the operator can fix the input rather than guess what went wrong.
    available = list_available_baut_states(page)
    if available:
        _log.error(
            "BAUT %s sidebar entry not found after %d attempts. "
            "Available BAUT lines on this submission: %s",
            state_upper, max_attempts, available,
        )
    else:
        _log.error(
            "BAUT %s sidebar entry not found after %d attempts. "
            "NO BAUT lines exist on this submission — create one in EPIC first.",
            state_upper, max_attempts,
        )
    return False


def list_available_baut_states(page: Page) -> list:
    """Return the 2-letter state suffixes of every BAUT line on the current submission.

    Reads ``data-automation-id`` of every level-3 sidebar entry, filtering for
    those whose text is "Business Auto" and whose aid ends with ``;{XX} level-3``.
    Expands the Policies tree first if collapsed, so this is safe to call from
    fresh entry points (e.g. test scripts) where the tree may not be open yet.
    """
    try:
        _expand_policy_tree(page)
    except Exception:
        pass
    try:
        return list(page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('[data-automation-id*="level-3"]')) {
                    const aid = el.getAttribute('data-automation-id') || '';
                    const m = aid.match(/;([A-Z]{2}) level-3$/);
                    if (m && (el.textContent || '').trim() === 'Business Auto') {
                        out.push(m[1]);
                    }
                }
                return out;
            }"""
        ))
    except Exception:
        return []


def _expand_policy_tree(page: Page) -> None:
    """Ensure Policies (level-1) and the active MMS (level-2) are expanded.

    Only acts when the tree appears collapsed (no level-3 entries present).
    Avoids accidentally toggling an already-expanded node closed.
    """
    page.evaluate(
        """() => {
            const hasLevel3 = document.querySelectorAll('[data-automation-id*="level-3"]').length > 0;
            if (hasLevel3) return;  // already expanded
            // Find and click Policies (level-1)
            const policies = Array.from(document.querySelectorAll('[data-automation-id*="level-1"]'))
                                .find(el => (el.textContent || '').trim() === 'Policies');
            if (policies) policies.click();
        }"""
    )
    page.wait_for_timeout(400)
    # If level-2 (MMS) exists now but no level-3 yet, click it to expand
    page.evaluate(
        """() => {
            const hasLevel3 = document.querySelectorAll('[data-automation-id*="level-3"]').length > 0;
            if (hasLevel3) return;
            const mms = document.querySelector('[data-automation-id*="level-2"]');
            if (mms) mms.click();
        }"""
    )
    page.wait_for_timeout(400)


def _nav_baut_section(page: Page, section_key: str, screen_code: str) -> bool:
    """Navigate to a BAUT sub-section via its sidebar link.

    Uses prefix-match on ``sidebar-button-Policy.BusinessAuto.{section_key}``.
    Confirms arrival by checking for *screen_code* in the page text or status bar.
    """
    _dismiss_modal(page)
    sidebar_sel = f'[data-automation-id^="sidebar-button-Policy.BusinessAuto.{section_key}"]'
    btn = page.locator(sidebar_sel)
    if not _wait_visible(page, btn, 5_000):
        _log.error("BAUT sidebar link for %s not found", section_key)
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    try:
        page.wait_for_function(
            f"""() => {{
                const text = document.body.innerText;
                if (text.includes("{screen_code}")) return true;
                const bars = document.querySelectorAll('[class*="statusBar"], [class*="status-bar"], .status-bar');
                for (const b of bars) {{ if (b.textContent.includes("{screen_code}")) return true; }}
                const allEls = document.querySelectorAll('[data-automation-id="{screen_code}"]');
                return allEls.length > 0;
            }}""",
            timeout=8_000,
        )
        _log.debug("Navigated to BusinessAuto.%s (%s) OK", section_key, screen_code)
        return True
    except PlaywrightTimeout:
        _log.warning("BusinessAuto.%s navigated but %s not confirmed", section_key, screen_code)
        return True  # best-effort; continue anyway


def _switch_vehicle_subtab(page: Page, tab_id: str) -> bool:
    """Click one of the per-vehicle tab headers: tpgVehicleTab / tpgRatingTab / tpgCoveragesTab.

    Returns True if click succeeded; False otherwise.  Caller should wait for
    the corresponding tab body to render.
    """
    sel = f'[data-automation-id="{tab_id}"]'
    tab = page.locator(sel)
    if not _wait_visible(page, tab, 2_500):
        _log.warning("Vehicle sub-tab %s not visible", tab_id)
        return False
    try:
        tab.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(200)
        _log.debug("Switched to vehicle sub-tab %s", tab_id)
        return True
    except Exception as exc:
        _log.warning("Failed to click vehicle sub-tab %s: %s", tab_id, exc)
        return False


# ── Coverages screen helpers ──────────────────────────────────────────────────

def _toggle_symbol_checkbox(page: Page, cov_prefix: str, n: int, checked: bool) -> None:
    """Set a coverage-symbol checkbox to the desired state.

    cov_prefix is the field-map name prefix: 'Liability', 'Medical', 'Uninsured',
    'Towing', 'Comprehensive', 'CauseOfLoss', 'Collision'.  n is the ACORD symbol.

    Approach (mirrors v1's working pattern):
      - Click the visible wrapper DIV ``[data-test="chk{Cov}{n}"]`` — NOT the hidden
        input.  The underlying ``<input type=checkbox>`` is 0px and visually hidden
        behind a styled switch; clicking the wrapper triggers EPIC's full event
        chain (mousedown → mouseup → change → React onChange), which sets the
        form's dirty flag.  Force-clicking the hidden input often fails to commit.
    """
    target = f"chk{cov_prefix}{n}"
    try:
        wrapper = page.locator(f'[data-test="{target}"]')
        if not wrapper.count():
            _log.warning("Symbol checkbox wrapper [data-test=%s] not found", target)
            return
        # Read current state via the inner input
        try:
            inner = wrapper.first.locator('input[type="checkbox"]')
            current = bool(inner.first.evaluate("el => el.checked")) if inner.count() else False
        except Exception:
            current = False
        if current == checked:
            _log.debug("Symbol %s%d already %s", cov_prefix, n, checked)
            return
        wrapper.first.click(timeout=2_000)
        page.wait_for_timeout(_COV_FILL_WAIT_MS)
        _log.debug("Symbol %s%d clicked via wrapper", cov_prefix, n)
    except Exception as exc:
        _log.warning("Symbol checkbox %s toggle failed: %s", target, exc)


def _apply_symbols(page: Page, cov_prefix: str, csv: str, valid_symbols: set[int]) -> None:
    """For each valid symbol on a coverage row, check it if in csv else leave alone.

    Note: we deliberately do NOT un-check symbols not in csv — EPIC may have
    pre-checked defaults the user wants to keep.  Only ADD what the data says.
    """
    if not csv:
        _log.debug("Symbols %s: skipped (blank)", cov_prefix)
        return
    wanted = _parse_symbols(csv)
    overlap = wanted & valid_symbols
    if not overlap:
        _log.warning("Symbols %s: no valid symbols in %r (valid=%s)", cov_prefix, csv, sorted(valid_symbols))
        return
    _log.info("Symbols %s: applying %s", cov_prefix, sorted(overlap))
    for n in sorted(overlap):
        _toggle_symbol_checkbox(page, cov_prefix, n, checked=True)


# Valid symbol sets per coverage (mirrors the field map's checkbox list).
_LIABILITY_SYMS     = {1, 2, 3, 4, 7, 8, 9}
_MEDICAL_SYMS       = {2, 3, 4, 7, 8}
_UNINSURED_SYMS     = {2, 3, 4, 6, 7}
_TOWING_SYMS        = {3, 7}
_COMPREHENSIVE_SYMS = {2, 3, 4, 7, 8}
_CAUSE_OF_LOSS_SYMS = {2, 3, 4, 7, 8}
_COLLISION_SYMS     = {2, 3, 4, 7, 8}


def _fill_coverages(page: Page, state: str, cov: AutoCoverageSpec) -> None:
    """Fill the BAUT Coverages screen (CHM-BAPCOV{STATE})."""
    screen_code = f"CHM-BAPCOV{state.upper()}"
    if not _nav_baut_section(page, f"PolicyCoverage.Coverage{state.upper()}", screen_code):
        _log.error("Coverages: could not navigate to %s", screen_code)
        return

    _log.info("=== Coverages (%s) ===", screen_code)

    # --- Symbol checkboxes ---
    _apply_symbols(page, "Liability",     cov.liability_symbols,     _LIABILITY_SYMS)
    _apply_symbols(page, "Medical",       cov.medical_symbols,       _MEDICAL_SYMS)
    _apply_symbols(page, "Uninsured",     cov.uninsured_symbols,     _UNINSURED_SYMS)
    _apply_symbols(page, "Towing",        cov.towing_symbols,        _TOWING_SYMS)
    _apply_symbols(page, "Comprehensive", cov.comprehensive_symbols, _COMPREHENSIVE_SYMS)
    _apply_symbols(page, "CauseOfLoss",   cov.cause_of_loss_symbols, _CAUSE_OF_LOSS_SYMS)
    _apply_symbols(page, "Collision",     cov.collision_symbols,     _COLLISION_SYMS)

    # --- Limit text fields ---
    # Try React id, fall back to old-proxy data-automation-id.
    _limit_field(page, "streLiabilityCSLLimit1",  cov.liability_csl_limit1)
    _limit_field(page, "streLiabilityBILimit1",   cov.liability_bi_limit1)
    _limit_field(page, "streLiabilityBILimit2",   cov.liability_bi_limit2)
    _limit_field(page, "streLiabilityPDLimit1",   cov.liability_pd_limit1)
    _limit_field(page, "streMedicalLimit1",       cov.medical_limit1)
    _limit_field(page, "streUninsuredCSLLimit1",  cov.uninsured_csl_limit1)
    _limit_field(page, "streUninsuredBILimit1",   cov.uninsured_bi_limit1)
    _limit_field(page, "streUninsuredBILimit2",   cov.uninsured_bi_limit2)
    _limit_field(page, "streUninsuredPDEachAccident", cov.uninsured_pd_each_accident)
    _limit_field(page, "streUninsuredPDDeductible",   cov.uninsured_pd_deductible)
    _limit_field(page, "streTowingLimit1",            cov.towing_limit1)
    _limit_field(page, "streComprehensiveDeductible1", cov.comprehensive_deductible1)
    _limit_field(page, "streCauseOfLossDeductible1",   cov.cause_of_loss_deductible1)
    _limit_field(page, "streCollisionDeductible1",     cov.collision_deductible1)

    # Final commit: blur whichever field still has focus and give EPIC time to
    # persist before we navigate away.  Without this, values entered into the
    # last few fields can be discarded when the user/script changes screens
    # (observed 2026-05-20: filled values disappeared on nav-back).
    try:
        page.keyboard.press("Tab")
        page.wait_for_timeout(_COV_COMMIT_MS)
        _log.debug("Coverages: final blur + %d ms commit wait done", _COV_COMMIT_MS)
    except Exception as exc:
        _log.warning("Coverages: final blur failed: %s", exc)

    _log.info("Coverages: filled %s", screen_code)


def _limit_field(page: Page, field_name: str, value: str) -> None:
    """Fill a Coverages limit text field using v1's confirmed-working pattern.

    Use real keystrokes (``keyboard.type(value, delay=30)``) rather than
    ``inp.fill()``.  EPIC's React form only marks the field as dirty when it
    sees per-character ``input`` events from real keyboard activity; ``fill()``
    sets the value in one shot and the dirty flag never trips, so the value is
    discarded on screen navigation.  Tab afterwards to blur and commit.
    """
    if not value:
        _log.debug("limit %s: skipped (blank)", field_name)
        return
    selectors = [
        f'#{field_name}__textField',                   # React id with suffix
        f'input[name="{field_name}__textField"]',      # name attr with suffix
        f'#{field_name}',                              # bare id (fallback)
        f'input[name="{field_name}"]',                 # form name fallback
        f'[data-automation-id="{field_name}"] input',  # old-proxy fallback
    ]
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if not loc.count():
                continue
            inp = loc.first
            if not inp.is_visible(timeout=500):
                continue
            inp.click(timeout=1_500)
            # Clear via Ctrl+A + Delete (so existing content is wiped) then
            # type the new value as real keystrokes.
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.keyboard.type(value, delay=30)
            page.keyboard.press("Tab")
            page.wait_for_timeout(_COV_FILL_WAIT_MS)
            _log.debug("limit %s = %r  OK (via %s)", field_name, value, sel)
            return
        except Exception:
            continue
    _log.warning("limit %s = %r  FAILED (no selector matched)", field_name, value)


# ── Vehicle helpers ──────────────────────────────────────────────────────────

def _force_check_proxy(page: Page, field_id: str, checked: bool = True) -> None:
    """Toggle a checkbox via the strategy that works for its wrapper type.

    Two wrapper variants exist in EPIC and need different click strategies:

    1. **React Checkbox** — wrapper is ``[data-test="{field_id}"]``, narrow
       (~30px wide), styled switch DIV right next to the hidden input.
       Clicking the wrapper center hits the switch → React onChange fires.
       Used on the policy-level Coverages screen (chkLiability1, etc.).

    2. **Old Angular proxy** — wrapper is ``[data-automation-id="{field_id}"]``
       (NO data-test), often very wide (chkLiability is 153px wide).  Clicking
       the wrapper center misses the actual checkbox at the left edge.  Must
       click the inner ``<input>`` directly with ``force=True``.  Used on the
       per-vehicle Coverages tab (chkLiability, chkComprehensive, etc.).

    v1's `_ba_set_coverage` documents this exact failure mode: "The proxy div
    for some coverages (e.g. Liability) is very wide (152px+); a center click
    misses the actual checkbox box.  Target the inner input[type='checkbox']
    directly to get a reliable click regardless of width."
    """
    # Prefer the React wrapper if present (newer screens), fall back to the
    # Angular proxy.  Track which selector matched so we click correctly.
    react_loc = page.locator(f'[data-test="{field_id}"]')
    proxy_loc = page.locator(f'[data-automation-id="{field_id}"]')
    is_react = False
    wrapper = None
    if _wait_visible(page, react_loc, 800):
        wrapper = react_loc.first
        is_react = True
    elif _wait_visible(page, proxy_loc, 800):
        wrapper = proxy_loc.first
    if wrapper is None:
        _log.warning("Checkbox wrapper %s not visible after wait", field_id)
        return
    try:
        inner = wrapper.locator('input[type="checkbox"]')
        current = bool(inner.first.evaluate("el => el.checked")) if inner.count() else False
        if current == checked:
            _log.debug("Checkbox %s already %s", field_id, checked)
            return
        try:
            wrapper.scroll_into_view_if_needed(timeout=1_000)
        except Exception:
            pass
        if is_react:
            # Narrow wrapper — center-click hits the switch.
            wrapper.click(timeout=2_500)
        else:
            # Wide old-proxy wrapper — center-click misses; click the hidden
            # input directly with force=True (v1's working pattern).
            inner.first.click(force=True, timeout=2_500)
        page.wait_for_timeout(_FILL_WAIT_MS)
        # Verify it actually toggled.  If not, swap strategies and retry once.
        after = bool(inner.first.evaluate("el => el.checked")) if inner.count() else False
        if after != checked:
            _log.debug("Checkbox %s first attempt didn't toggle — swapping strategy", field_id)
            try:
                if is_react:
                    inner.first.click(force=True, timeout=2_000)
                else:
                    wrapper.click(timeout=2_000)
                page.wait_for_timeout(_FILL_WAIT_MS)
            except Exception as exc:
                _log.warning("Checkbox %s swap-strategy retry failed: %s", field_id, exc)
        _log.debug("Checkbox %s clicked (%s)", field_id, "wrapper" if is_react else "inner-force")
    except Exception as exc:
        _log.warning("Checkbox %s toggle failed: %s", field_id, exc)


def _apply_vehicle_coverage(page: Page, checkbox_aid: str, fields: "list[tuple[str, str]]") -> bool:
    """Check a per-vehicle coverage box and fill its limit/deductible fields.

    Returns True if at least one field had a value (i.e. coverage was applied);
    False if all fields blank (skips the checkbox entirely).
    """
    has_any = any(v for _, v in fields)
    if not has_any:
        _log.debug("Per-vehicle coverage %s: no values to fill — skipping", checkbox_aid)
        return False
    _force_check_proxy(page, checkbox_aid, checked=True)
    for field_id, value in fields:
        if value:
            _fill_proxy_field(page, field_id, value)
    _log.debug("Per-vehicle coverage %s applied", checkbox_aid)
    return True


def _check_default_address(page: Page) -> None:
    """Check the 'Default account address' checkbox on the Vehicle Description tab.

    Confirmed via CDP 2026-05-20:
      - Wrapper DIV: ``[data-automation-id="chkDefaultAddress"]``
      - Inner input: ``#BAVEHICLchkDefaultAddress`` (type=checkbox, visually hidden)
    Use force-check because the underlying input is hidden behind a styled switch.
    """
    try:
        inp = page.locator('#BAVEHICLchkDefaultAddress')
        if not inp.count():
            _log.warning("Default account address checkbox not found")
            return
        if inp.first.is_checked(timeout=600):
            _log.debug("Default account address already checked")
            return
        try:
            inp.first.check(force=True, timeout=1_500)
            _log.debug("Default account address checked (force)")
        except Exception:
            page.evaluate(
                "() => { const el = document.querySelector('#BAVEHICLchkDefaultAddress'); if (el) el.click(); }"
            )
            _log.debug("Default account address checked (JS click)")
        page.wait_for_timeout(_FILL_WAIT_MS)
    except Exception as exc:
        _log.warning("Default account address check failed: %s", exc)


# ── Vehicles ──────────────────────────────────────────────────────────────────

def _fill_vehicles(page: Page, vehicles: list[VehicleSpec], cov: AutoCoverageSpec) -> None:
    """Add each vehicle row on BAVEHICL (old Angular proxy).

    Per-vehicle Coverages tab: for every policy-level coverage with a non-empty
    symbol set, check the corresponding checkbox and copy its limits/deductibles
    down to the vehicle so it shows the same limits as the parent policy
    Coverages screen.  VehicleSpec.comprehensive_deductible / collision_deductible
    override the policy-level deductibles when present.
    """
    if not vehicles:
        _log.info("No vehicles to add; skipping section")
        return

    if not _nav_baut_section(page, "Vehicle", "BAVEHICL"):
        return

    _log.info("=== Vehicles (%d) ===", len(vehicles))

    # Old-proxy Add button: inside the vlvwVehicles section.
    add_btn = page.locator('[name="vlvwVehicles"] .icon-button[title="Add"]')

    for i, veh in enumerate(vehicles, 1):
        check_cancel()
        _log.info(
            "Adding vehicle %d/%d: #%s %s %s %s VIN=%s",
            i, len(vehicles), veh.vehicle_num, veh.year, veh.make, veh.model, veh.vin,
        )

        _dismiss_modal(page)
        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Vehicles Add button not visible at row %d", i)
            break
        try:
            add_btn.first.click(timeout=_CLICK_TIMEOUT)
            page.wait_for_timeout(400)
            _dismiss_modal(page)
        except Exception as exc:
            _log.error("Vehicles: failed to click Add at row %d: %s", i, exc)
            break

        # --- Tab 1: Description (tpgVehicleTab) ---
        # Fields confirmed on this tab via CDP 2026-05-20:
        #   inteVehicleNum, inteYear, streMake, streModel, streVIN, cboBodyType,
        #   chkDefaultAddress (checkbox — auto-fills garage from account address).
        # NOT on this tab: cboVehicleType, cboUse, inteRadius, inteCostNew → Rating tab.
        # NOT on this tab: cboValuationType, streComprehensiveDeductible,
        #                  streCollisionDeductible → Coverages tab.
        _switch_vehicle_subtab(page, "tpgVehicleTab")
        _fill_proxy_field(page, "inteVehicleNum", veh.vehicle_num)
        _fill_proxy_field(page, "inteYear",       veh.year)
        _fill_proxy_field(page, "streMake",       veh.make)
        _fill_proxy_field(page, "streModel",      veh.model)
        # IGA rule: VIN must never be left blank in EPIC — when extraction
        # didn't surface one (older units, trailers without legible plates,
        # newly-acquired equipment), file "TBD" as a placeholder so the
        # marketing rep can fix it later. Empty/whitespace-only treated the
        # same as missing.
        _fill_proxy_field(
            page, "streVIN",
            (veh.vin or "").strip() or "TBD",
        )
        if veh.body_type:
            _fill_combo_proxy(page, "cboBodyType", veh.body_type)
        # Garage: check the "Default account address" box rather than typing
        # into the validated address widget (per user instruction 2026-05-20).
        # The checkbox is hidden behind a styled switch — use force-click.
        if veh.garage_address:
            _check_default_address(page)

        # --- Tab 2: Rating (tpgRatingTab) ---
        # Per IGA spec 2026-05-20: only Cost New and Class Code (when available)
        # are entered on this tab.  All other rating fields (VehicleType, Use,
        # Radius, Territory, GVW, etc.) are left for EPIC/underwriter to fill.
        if veh.cost_new or veh.class_code:
            if _switch_vehicle_subtab(page, "tpgRatingTab"):
                page.wait_for_timeout(150)
                _fill_proxy_field(page, "inteCostNew", veh.cost_new)
                _fill_proxy_field(page, "streClass",   veh.class_code)

        # --- Tab 3: Coverages (tpgCoveragesTab) ---
        # For each policy-level coverage with applicable symbols, check the
        # corresponding chkXxx box and copy the limits down.  Per-vehicle
        # deductibles in VehicleSpec override the policy-level deductibles.
        # Field IDs confirmed via CDP 2026-05-20.
        if _switch_vehicle_subtab(page, "tpgCoveragesTab"):
            page.wait_for_timeout(200)

            # Valuation type — combo on this tab
            _fill_combo_proxy(page, "cboValuationType", veh.valuation_type)

            # Liability — non-empty symbols means applies (1=Any Auto covers all)
            if _parse_symbols(cov.liability_symbols):
                _apply_vehicle_coverage(page, "chkLiability", [
                    ("streLiabCSLEachAccident", cov.liability_csl_limit1),
                    ("streLiabBIEachPerson",    cov.liability_bi_limit2),  # BI per-person (smaller)
                    ("streLiabBIEachAccident",  cov.liability_bi_limit1),  # BI per-accident (larger)
                    ("streLiabPDEachAccident",  cov.liability_pd_limit1),
                ])
                # Re-verify the checkbox stuck — Liability is the first coverage
                # on the tab and the click sometimes lands before EPIC is ready.
                # If still unchecked after the limit fills, click again.
                try:
                    inner = page.locator('[data-test="chkLiability"] input[type="checkbox"]')
                    if inner.count() and not bool(inner.first.evaluate("el => el.checked")):
                        _log.info("Per-vehicle Liability: checkbox still unchecked — retrying")
                        _force_check_proxy(page, "chkLiability", checked=True)
                except Exception as exc:
                    _log.debug("Per-vehicle Liability re-verify failed: %s", exc)

            # Medical payments
            if _parse_symbols(cov.medical_symbols):
                _apply_vehicle_coverage(page, "chkMedical", [
                    ("streMedicalEachPerson", cov.medical_limit1),
                ])

            # Uninsured Motorist
            if _parse_symbols(cov.uninsured_symbols):
                _apply_vehicle_coverage(page, "chkUninsured", [
                    ("streUMCSLEachAccident", cov.uninsured_csl_limit1),
                    ("streUMBIEachPerson",    cov.uninsured_bi_limit2),
                    ("streUMBIEachAccident",  cov.uninsured_bi_limit1),
                    ("streUMPDEachAccident",  cov.uninsured_pd_each_accident),
                ])

            # Towing & Labor
            if _parse_symbols(cov.towing_symbols):
                _apply_vehicle_coverage(page, "chkTowingLabor", [
                    ("streTowingLaborEachPerson", cov.towing_limit1),
                ])

            # Specified Causes of Loss
            if _parse_symbols(cov.cause_of_loss_symbols):
                _apply_vehicle_coverage(page, "chkCauseOfLoss", [
                    ("streCauseOfLossDeductible", cov.cause_of_loss_deductible1),
                ])

            # Comprehensive — per-vehicle deductible overrides policy-level
            comp_ded = veh.comprehensive_deductible or cov.comprehensive_deductible1
            if _parse_symbols(cov.comprehensive_symbols) or comp_ded:
                _apply_vehicle_coverage(page, "chkComprehensive", [
                    ("streComprehensiveDeductible", comp_ded),
                ])

            # Collision — per-vehicle deductible overrides policy-level
            coll_ded = veh.collision_deductible or cov.collision_deductible1
            if _parse_symbols(cov.collision_symbols) or coll_ded:
                _apply_vehicle_coverage(page, "chkCollision", [
                    ("streCollisionDeductible", coll_ded),
                ])

        # Switch back to Description tab so the next vehicle's Add starts cleanly.
        _switch_vehicle_subtab(page, "tpgVehicleTab")
        page.wait_for_timeout(150)

        _log.info("Vehicle %d (#%s) OK", i, veh.vehicle_num or "?")

    _log.info("Vehicles section complete (%d rows)", len(vehicles))


def _fill_combo_proxy(page: Page, combo_id: str, value: str, fallback_text: str = "") -> None:
    """Fill an old-proxy combo by typing into its input.

    Old-proxy combos render as a DIV with the combo_id as data-automation-id,
    containing an <input>.  Typing + Tab usually triggers EPIC's auto-resolve.
    Falls back to the React combo helper if proxy selector doesn't match.
    """
    if not value:
        _log.debug("combo_proxy %s: skipped (blank)", combo_id)
        return
    # Try old-proxy first
    try:
        proxy = page.locator(f'[data-automation-id="{combo_id}"] input')
        if proxy.count() and proxy.first.is_visible(timeout=500):
            proxy.first.click(timeout=1_500)
            proxy.first.fill(value, timeout=1_500)
            page.keyboard.press("Tab")
            page.wait_for_timeout(_FILL_WAIT_MS)
            _log.debug("combo_proxy %s = %r  OK", combo_id, value)
            return
    except Exception as exc:
        _log.debug("combo_proxy %s old-proxy attempt failed: %s", combo_id, exc)
    # Fall back to React combo
    try:
        _fill_react_combo(page, combo_id, value)
    except Exception as exc:
        _log.warning("combo_proxy %s = %r  FAILED via both patterns: %s", combo_id, value, exc)


# ── Additional Interests ──────────────────────────────────────────────────────

def _fill_auto_additional_interests(page: Page, interests: list[AutoAdditionalInterestSpec]) -> None:
    """Add each AI row on BAUT > AdditionalInterest (React vlvwInterest pattern).

    Rewritten 2026-05-26 to mirror ``step_inland_marine._fill_im_additional_interests``:
    safe_action + verify_input_value, pre-Add settle, _wait_grid_grew with
    retry, lenient validated-address path with manual-fallback + tail
    extraction onto address line 2.
    """
    if not interests:
        _log.info("No additional interests; skipping section")
        return

    if not _nav_baut_section(page, "AdditionalInterest", "CHM-BAADDINT"):
        _log.error("Additional Interests: nav failed — aborting section")
        return

    _log.info("=== Additional Interests: %d item(s) ===", len(interests))
    assert_screen_code(page, "CHM-BAADDINT", timeout_ms=8_000)

    add_btn_sel = '[data-test="vlvwInterest_add"]'

    for i, ai in enumerate(interests, 1):
        check_cancel()
        name = (ai.name or "").strip()
        if not name:
            _log.warning("AI #%d: blank name — skipping (EPIC requires name)", i)
            continue

        _log.info(
            "--- Row %d/%d: name=%r type=%r veh#=%s addr=%r",
            i, len(interests), name, ai.interest_type or "-",
            ai.vehicle_number or "-",
            (ai.address_line_1 or "-")[:60],
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

        if ai.vehicle_number:
            fill_field_verified(
                page, "inteVehicleNumber__textField", ai.vehicle_number,
                i, "vehicle#", section="AI", logger=_log,
            )

        if ai.interest_type:
            code = _resolve_ai_interest(ai.interest_type)
            if code:
                with safe_action(page, context=f"AI #{i}: combo cboInterest={code}"):
                    _fill_react_combo(page, "cboInterest", code)
                _log.debug("AI #%d: cboInterest = %r (resolved from %r)", i, code, ai.interest_type)
            else:
                _log.warning(
                    "AI #%d: interest_type %r did not resolve — leaving combo blank",
                    i, ai.interest_type,
                )

        if ai.address_line_1:
            street, city, state_code, zip_code = _split_us_address(ai.address_line_1)
            _log.debug(
                "AI #%d address split: street=%r city=%r state=%r zip=%r",
                i, street, city, state_code, zip_code,
            )
            fill_validated_address_then_manual(
                page,
                street=street, city=city, state_code=state_code,
                zip_code=zip_code, full_address=ai.address_line_1,
                row_idx=i, section="AI",
                fill_validated_fn=_fill_validated_address,
                fill_combo_fn=_fill_react_combo,
                logger=_log,
            )

        page.wait_for_timeout(300)
        _log.debug("AI #%d: row complete", i)

    _log.info("=== Additional Interests: complete (%d row(s) processed) ===", len(interests))


# ── Additional Coverages ──────────────────────────────────────────────────────

# ── Forms & Endorsements ──────────────────────────────────────────────────────

# IGA fixed BAUT endorsements — added on every Business Auto submission regardless
# of data.  Matches GL_STANDARD_FORMS but with the LOB-specific extension renamed.
BA_STANDARD_FORMS: list[_GLFormSpec] = [
    _GLFormSpec(name="Blanket by Written Contract - Additional Insured"),
    _GLFormSpec(name="Blanket by Written Contract - Waiver of Subrogation"),
    _GLFormSpec(name="Blanket by Written Contract - Primary and Non-Contributory"),
    _GLFormSpec(name="Business Auto Extension Endorsement"),
]


def _fill_ba_forms_endorsements(page: Page, forms: list[_GLFormSpec]) -> None:
    """Add each form / endorsement row on the BAUT Forms & Endorsements screen.

    Confirmed via CDP 2026-05-20: BAUT F&E is the OLD Angular proxy pattern
    (NOT the React pattern GL uses).  Selectors:
      - Add button: ``[name="vlvwFormsEnd"] .icon-button[title="Add"]``
      - Form fields are proxy DIVs with ``data-automation-id``:
          streNumber, streName, dteEdition, cboCopyrightType, streCopyrightCode,
          curePremium, streRemarks, inteVehicleNumber
        (inputs inside have NO id/name — must use ``_fill_proxy_field``)
    """
    if not forms:
        return

    _nav_baut_section(page, "FormEndorsement", "BAFRMEND")

    add_btn = page.locator('[name="vlvwFormsEnd"] .icon-button[title="Add"]')

    for i, frm in enumerate(forms):
        check_cancel()
        _log.info("Adding form %d: %r (number=%r)", i + 1, frm.name, frm.form_number or "(none)")

        _dismiss_modal(page)
        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Forms & Endorsements Add button not visible for row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)

        if frm.form_number:
            # Number → Tab triggers EPIC library lookup → auto-populates Name.
            _fill_proxy_field(page, "streNumber", frm.form_number)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)

        # Name is required; library lookup may not resolve IGA custom endorsements,
        # so fill it directly via the proxy input.
        _fill_proxy_field(page, "streName", frm.name)

        if frm.edition_date:
            _fill_proxy_field(page, "dteEdition", frm.edition_date)
        _fill_proxy_field(page, "streCopyrightCode", frm.copyright_code)
        if frm.copyright_type:
            try:
                _fill_combo(page, "cboCopyrightType", frm.copyright_type)
            except Exception as exc:
                _log.warning("Could not set copyright type %r for form %d: %s", frm.copyright_type, i + 1, exc)
        _fill_proxy_field(page, "curePremium", frm.premium)

        page.wait_for_timeout(300)

    _log.info("Forms & Endorsements section complete (%d rows)", len(forms))


# ── Additional Coverages ──────────────────────────────────────────────────────

def _ac_row_count(page: Page) -> int:
    """Return the current row count in the BAUT Additional Coverage grid."""
    try:
        return int(page.evaluate(
            """() => document.querySelectorAll('[data-test^="vlvwCoverage-focusable-row-"]').length"""
        ))
    except Exception:
        return 0


def _wait_for_ac_form_active(page: Page, timeout_ms: int = 2_500) -> bool:
    """Poll until the AC form's streDescription input becomes enabled.

    After clicking Add on the BAUT Additional Coverages screen, EPIC normally
    enables the form panel within ~500 ms.  But if the previous Add didn't
    fully commit (e.g. a screen-nav interrupted it), the form stays disabled
    and subsequent fill attempts fail with "element is not enabled".
    """
    end_ms = 0
    step_ms = 150
    while end_ms < timeout_ms:
        try:
            state = page.evaluate(
                """() => {
                    const el = document.getElementById('streDescription');
                    if (!el) return { exists: false };
                    return { exists: true, disabled: el.disabled, readonly: el.readOnly };
                }"""
            )
            if state and state.get("exists") and not state.get("disabled") and not state.get("readonly"):
                return True
        except Exception:
            pass
        page.wait_for_timeout(step_ms)
        end_ms += step_ms
    return False


def _fill_auto_additional_coverages(page: Page, coverages: list[AutoAdditionalCoverageSpec]) -> None:
    """Add each AC row on BAUT > AdditionalCoverage.

    Confirmed via CDP 2026-05-20: BAUT AdditionalCoverage is the React pattern.
    Selectors:
      - Add button: ``[data-test="vlvwCoverage_add"]``
      - Form fields use direct HTML id (mix of bare and ``__textField`` suffix):
          streDescription    (bare)
          streCode           (bare)
          streLimit1__textField, streLimit2__textField
          streDeductible__textField
          inteVehicleNumber__textField
          curePremium__textField
    """
    if not coverages:
        _log.info("No additional coverages; skipping section")
        return

    _nav_baut_section(page, "AdditionalCoverage", "BAADDCOV")

    _log.info("=== Additional Coverages (%d) ===", len(coverages))

    add_btn = page.locator('[data-test="vlvwCoverage_add"]')
    for i, ac in enumerate(coverages, 1):
        check_cancel()
        _log.info("Adding AC %d/%d: desc=%r code=%r ded=%r", i, len(coverages), ac.description, ac.code, ac.deductible)

        _dismiss_modal(page)
        if not _wait_visible(page, add_btn, 5_000):
            _log.error("AC Add button not visible at row %d", i)
            break

        # Click Add with row-count verification + retry.  The button can swallow
        # a click silently if EPIC isn't quite ready (race after a screen nav
        # or a prior incomplete row).  Confirming the grid count increased
        # tells us the Add actually fired.
        rows_before = _ac_row_count(page)
        clicked = False
        for attempt in range(1, 4):
            try:
                add_btn.first.click(timeout=_CLICK_TIMEOUT)
            except Exception as exc:
                _log.warning("AC %d: Add click attempt %d failed: %s", i, attempt, exc)
                page.wait_for_timeout(400)
                continue
            # Poll for the row count to increase (up to ~1.5s per attempt).
            grew = False
            for _ in range(10):
                page.wait_for_timeout(150)
                if _ac_row_count(page) > rows_before:
                    grew = True
                    break
            if grew:
                clicked = True
                _log.debug("AC %d: Add fired on attempt %d (rows %d→%d)",
                           i, attempt, rows_before, rows_before + 1)
                break
            _log.warning("AC %d: Add click attempt %d didn't add a row — retrying", i, attempt)
            _dismiss_modal(page)
        if not clicked:
            _log.error("AC %d: Add never produced a new row — skipping", i)
            continue

        # Verify the form panel is active.  If the row was added but the form
        # didn't auto-activate, click the new row to focus it.
        if not _wait_for_ac_form_active(page, timeout_ms=2_500):
            _log.warning("AC %d: form not active after Add — trying row click", i)
            try:
                rows = page.locator('[data-test^="vlvwCoverage-focusable-row-"]')
                if rows.count():
                    rows.last.click(timeout=2_000)
                    page.wait_for_timeout(500)
            except Exception as exc:
                _log.debug("AC %d: fallback row click failed: %s", i, exc)
            if not _wait_for_ac_form_active(page, timeout_ms=2_000):
                _log.error("AC %d: form never activated — skipping", i)
                continue

        short_desc = abbreviate_if_too_long(
            ac.description, max_chars=AC_DESC_MAX_CHARS,
            row_idx=i, label="description", section="AC", logger=_log,
        )
        _fill_id_field(page, "streDescription", short_desc)
        # Code is required by EPIC — fall back to N/A if blank (same pattern as property)
        _fill_id_field(page, "streCode", ac.code or "N/A")
        _fill_id_field(page, "streLimit1__textField",     ac.limit1)
        _fill_id_field(page, "streDeductible__textField", ac.deductible)
        _log.info("AC %d (%r) filled", i, short_desc)

        page.wait_for_timeout(300)

    _log.info("Additional Coverages section complete (%d rows)", len(coverages))


# ── Entry point ───────────────────────────────────────────────────────────────

def run(page: Page, setup: BusinessAutoSetup) -> bool:
    """Walk all BAUT screens for the given state, filling from `setup`.

    Pre-conditions
    --------------
    - Browser is on the Submission Detail sidebar tree.
    - A BAUT line for `setup.state` already exists (created via step_mms_create).

    Returns True on completion (best-effort; per-field failures are logged but
    do not abort the run).
    """
    _log.info("=== Business Auto entry step starting (state=%s) ===", setup.state)
    _log.info(
        "Setup: state=%s, %d vehicle(s), %d AI(s), %d AC(s)",
        setup.state, len(setup.vehicles), len(setup.additional_interests), len(setup.additional_coverages),
    )

    try:
        if not _nav_to_business_auto_state(page, setup.state):
            _log.error("Aborting — could not open BAUT line for state %s", setup.state)
            return False

        # 1. Coverages (state-specific screen)
        _log.info("=== Step 1: Coverages ===")
        checkpoint(page, f"Business Auto ({setup.state}): Coverages")
        _fill_coverages(page, setup.state, setup.coverages)

        # 2. Vehicles (passes cov down so per-vehicle Coverages tab gets
        #    the same limits as the policy-level Coverages screen)
        _log.info("=== Step 2: Vehicles ===")
        checkpoint(page, f"Business Auto ({setup.state}): Vehicles")
        _fill_vehicles(page, setup.vehicles, setup.coverages)

        # 3. Additional Interests — rewritten 2026-05-26 to match the IM AI
        # pattern (safe_action + verify, _wait_grid_grew, lenient validated
        # address with manual fallback + tail extraction).
        _log.info("=== Step 3: Additional Interests ===")
        checkpoint(page, f"Business Auto ({setup.state}): Additional Interests")
        _fill_auto_additional_interests(page, setup.additional_interests)

        # 4. Forms & Endorsements (IGA standard set; skips submission-specific
        #    scraped forms — only the fixed BA_STANDARD_FORMS list is entered)
        _log.info("=== Step 4: Forms & Endorsements ===")
        checkpoint(page, f"Business Auto ({setup.state}): Forms & Endorsements")
        _fill_ba_forms_endorsements(page, BA_STANDARD_FORMS)

        # 5. Additional Coverages
        _log.info("=== Step 5: Additional Coverages ===")
        checkpoint(page, f"Business Auto ({setup.state}): Additional Coverages")
        _fill_auto_additional_coverages(page, setup.additional_coverages)

        _log.info("=== Business Auto entry step complete (state=%s) ===", setup.state)
        return True
    except EntryCancelled:
        raise
    except Exception as exc:
        _log.error("Business Auto run aborted: %s", exc, exc_info=True)
        return False

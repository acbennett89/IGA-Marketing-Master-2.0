"""step_workers_comp.py — fill the Workers' Compensation (WCOM) screens.

Pre-condition
-------------
The browser is on the Submission Detail sidebar tree for the target MMS, with
at least one WCOM line already created by the user.  Like BAUT, WCOM lines are
state-specific (one per state of risk).

Sections filled (in order)
---------------------------
1. **Policy Information / Total Premiums** (CHM-WCPLCYIN — Part 1/3 states, each-
   accident + disease limits, deductible)

2. **Total Premium Calculations** (CHM-WCRATING — per-state experience mod and
   deductible factor from ``policy.workers_comp.rating_info``)

3. **Class Codes** (TBD — TODO: selectors not yet verified live; logged + skipped)

4. **Forms & Endorsements** (CHM-WCFRMEND — IGA standard endorsement set)

Skipped sections (per IGA spec)
--------------------------------
Applicant (account-level data, not policy), Individuals, Prior Carriers,
General Information, Assigned Risk, Remarks, Supplemental Screens.

Selector notes (confirmed via live CDP inspection 2026-05-20)
--------------------------------------------------------------
- WCOM line (level-3 sidebar): same pattern as BAUT, ``;{STATE} level-3`` suffix.
  Filter by text 'Worker' to distinguish from BAUT.
- WCOM sub-sections (level-4): ``Policy.WorkersComp.{section_key}``.
- Policy Info screen uses React ``__textField`` suffix pattern + bare-id text
  fields.  No `data-test` on the inputs — only on combo wrappers.
- Total Premium Calc Add: ``[data-test="vlvwStates_add"]`` (React pattern).
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
from iga_marketing_master_2.epic_steps.step_property import (
    _dismiss_modal,
    _fill_id_field,
    _fill_proxy_field,
    _fill_react_combo,
)
from iga_marketing_master_2.epic_steps.step_business_auto import (
    _expand_policy_tree,
)
from iga_marketing_master_2.epic_steps.step_general_liability import FormSpec as _GLFormSpec

_log = logging.getLogger("iga.epic_steps.workers_comp")

_CLICK_TIMEOUT  = 5_000
_NAV_WAIT_MS    = 1_200
_FILL_WAIT_MS   = 150


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class WcLocationSpec:
    """One row on the WC Locations grid.

    Mirrors the per-location address fields the operator fills before adding
    class-code (Rating) rows for that location.  At minimum needs ``loc_num``
    and a state — the address fields are best-effort (EPIC will save with a
    blank address but flag it later in the validation pass).
    """
    loc_num:       str
    state:         str        # 2-letter state code (drives the address widget's cboState)
    street:        str = ""
    city:          str = ""
    zip_code:      str = ""
    site_id:       str = ""
    highest_floor: str = ""


@dataclass
class WcRatingInfoSpec:
    """One per-state rating row on the Total Premium Calculations screen.

    Mirrors ``policy.workers_comp.rating_info.*`` in state.json:
      state           — 2-letter state code (used to drive the cboState combo)
      experience_mod  — pereFactorExperience__textField (e.g. "0.910")
      deductible      — deductible (currency or "No Deductible"); routed to the
                        Policy Info screen's amount field on the first row
    """
    state:          str
    experience_mod: str = ""
    deductible:     str = ""


@dataclass
class WcClassCodeSpec:
    """One class-code row.  Field selectors NOT yet verified live."""
    state:        str
    class_code:   str = ""
    description:  str = ""
    payroll:      str = ""


@dataclass
class WorkersCompSetup:
    """Top-level container for one WCOM line entry.

    state               — 2-letter state code matching the WCOM line
    each_accident       — Part 1 each-accident limit
    disease_each_employee
    disease_policy_limit
    part1_states        — comma-separated states (e.g. "TN,KY")
    part3_states        — comma-separated states or "ALL OTHER"
    rating_info         — per-state rating rows (experience mod + deductible)
    class_codes         — per-state class codes (TBD)
    """
    state:                str
    each_accident:        str = ""
    disease_each_employee: str = ""
    disease_policy_limit: str = ""
    part1_states:         str = ""
    part3_states:         str = ""
    locations:            list[WcLocationSpec]   = field(default_factory=list)
    rating_info:          list[WcRatingInfoSpec] = field(default_factory=list)
    class_codes:          list[WcClassCodeSpec]  = field(default_factory=list)


# ── Navigation ────────────────────────────────────────────────────────────────

def _nav_to_workers_comp_state(page: Page, state: str, max_attempts: int = 4) -> bool:
    """Expand the WCOM line in the sidebar for the given 2-letter state code.

    Like BAUT, WCOM lines share the ``;{STATE} level-3`` suffix.  The label is
    'Worker's Compensation' (apostrophe variant — match by text contains
    'Compensation' to be tolerant of label changes).
    """
    state_upper = state.upper()
    for attempt in range(1, max_attempts + 1):
        try:
            _expand_policy_tree(page)
        except Exception as exc:
            _log.debug("WCOM %s: tree-expand failed: %s", state_upper, exc)

        # Find via JS — there can be multiple ;{STATE} level-3 entries (e.g. BAUT
        # + WCOM both for TN); filter by label text containing "Compensation".
        clicked = page.evaluate(
            """(s) => {
                const candidates = document.querySelectorAll(`[data-automation-id$=";${s} level-3"]`);
                for (const el of candidates) {
                    const txt = (el.textContent || '').trim();
                    if (txt.includes('Compensation')) {
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
            _log.debug("Clicked WCOM %s sidebar parent (attempt %d/%d)",
                       state_upper, attempt, max_attempts)
            return True
        _log.warning(
            "WCOM %s sidebar entry not found — attempt %d/%d; waiting 2 s",
            state_upper, attempt, max_attempts,
        )
        page.wait_for_timeout(2_000)

    available = list_available_wcom_states(page)
    if available:
        _log.error(
            "WCOM %s sidebar entry not found after %d attempts. "
            "Available WCOM lines on this submission: %s",
            state_upper, max_attempts, available,
        )
    else:
        _log.error(
            "WCOM %s sidebar entry not found after %d attempts. "
            "NO WCOM lines exist on this submission — create one in EPIC first.",
            state_upper, max_attempts,
        )
    return False


def list_available_wcom_states(page: Page) -> list:
    """Return the 2-letter state suffixes of every WCOM line on the open submission."""
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
                    if (m && (el.textContent || '').includes('Compensation')) {
                        out.push(m[1]);
                    }
                }
                return out;
            }"""
        ))
    except Exception:
        return []


def _nav_wc_section(page: Page, section_key: str, screen_code: str) -> bool:
    """Navigate to a WCOM sub-section.

    Uses prefix-match on ``sidebar-button-Policy.WorkersComp.{section_key}``.
    Confirms via screen_code in the page text or status bar (best-effort).
    """
    _dismiss_modal(page)
    sidebar_sel = f'[data-automation-id^="sidebar-button-Policy.WorkersComp.{section_key}"]'
    btn = page.locator(sidebar_sel)
    if not _wait_visible(page, btn, 5_000):
        _log.error("WCOM sidebar link for %s not found", section_key)
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
                return document.querySelectorAll('[data-automation-id="{screen_code}"]').length > 0;
            }}""",
            timeout=6_000,
        )
        _log.debug("Navigated to WorkersComp.%s (%s) OK", section_key, screen_code)
        return True
    except PlaywrightTimeout:
        _log.warning("WorkersComp.%s navigated but %s not confirmed", section_key, screen_code)
        return True  # best-effort


# ── Policy Information / Total Premiums ──────────────────────────────────────

def _fill_policy_info(page: Page, setup: WorkersCompSetup) -> None:
    """Fill the WC Policy Information / Total Premiums screen.

    Required sequence (confirmed live 2026-05-20):
      1. strePart1States / strePart3States — comma-separated state codes
      2. cboEmpLiab = "WCEL" (Worker's Comp and Employers Liability) — REQUIRED
         field; until this is set, the each-accident / disease / deductible
         fields stay DISABLED and EPIC throws a "Required information is
         missing" validation modal on nav-away.
      3. streEachAccident / streDiseasePolicyLimit / streDiseaseEachEmployee
      4. cboDeductibles = "B" (Both Medical and Indemnity) — required before
         streAmount can be filled
      5. streAmount — deductible dollar amount (from matching rating_info)

    Combo options confirmed live:
      cboEmpLiab:    INEL | WCEL
      cboDeductibles: B | COPC | I | M
    """
    if not _nav_wc_section(page, "PolicyInfoTotalPremium", "CHM-WCPLCYIN"):
        return

    _log.info("=== Policy Information / Total Premiums ===")

    _fill_id_field(page, "strePart1States", setup.part1_states)
    _fill_id_field(page, "strePart3States", setup.part3_states)

    # cboEmpLiab is the gatekeeper combo — set it BEFORE the limit fields so
    # they become editable.  WCEL is the standard WC + EL coverage choice.
    try:
        _fill_react_combo(page, "cboEmpLiab", "WCEL")
        page.wait_for_timeout(400)
    except Exception as exc:
        _log.warning("cboEmpLiab=WCEL fill failed: %s", exc)

    _fill_id_field(page, "streEachAccident__textField",        setup.each_accident)
    _fill_id_field(page, "streDiseasePolicyLimit__textField",  setup.disease_policy_limit)
    _fill_id_field(page, "streDiseaseEachEmployee__textField", setup.disease_each_employee)

    # Deductible — pulled from the rating row matching this WCOM line's state.
    # Skip "No Deductible" / blank text; only fill if there's a real amount.
    if setup.rating_info:
        match = next(
            (r for r in setup.rating_info if r.state.upper() == setup.state.upper()),
            setup.rating_info[0],
        )
        ded_raw = (match.deductible or "").strip()
        if ded_raw and "no" not in ded_raw.lower():
            ded_val = ded_raw.replace("$", "").replace(",", "").strip()
            # cboDeductibles must be set first to enable streAmount
            try:
                _fill_react_combo(page, "cboDeductibles", "B")
                page.wait_for_timeout(400)
            except Exception as exc:
                _log.warning("cboDeductibles=B fill failed: %s", exc)
            _fill_id_field(page, "streAmount__textField", ded_val)

    page.keyboard.press("Tab")  # blur to commit
    page.wait_for_timeout(800)

    # Dismiss any lingering "Required information is missing" validation modal
    # (best-effort — if the form is complete it won't appear).
    try:
        ok = page.locator('[data-test="common-message-modal"] button')
        if ok.first.is_visible(timeout=400):
            ok.first.click(timeout=1_000)
            _log.warning("Policy Information: dismissed validation modal after fill")
    except Exception:
        pass

    _log.info("Policy Information: filled")


# ── Total Premium Calculations ───────────────────────────────────────────────

def _fill_total_prem_calc(page: Page, setup: WorkersCompSetup) -> None:
    """Add a per-state rating row for each entry in ``setup.rating_info``.

    Each row needs a state (cboState) and an experience mod
    (pereFactorExperience__textField).  Other premium factors are left for EPIC
    to compute or for the underwriter to fill manually.
    """
    if not setup.rating_info:
        _log.info("No rating info; skipping Total Premium Calculations")
        return

    if not _nav_wc_section(page, "TotalPremCalc", "CHM-WCRATING"):
        return

    _log.info("=== Total Premium Calculations (%d state row(s)) ===", len(setup.rating_info))

    add_btn = page.locator('[data-test="vlvwStates_add"]')
    for i, rating in enumerate(setup.rating_info, 1):
        check_cancel()
        _log.info("Adding rating row %d/%d: state=%r exp_mod=%r",
                  i, len(setup.rating_info), rating.state, rating.experience_mod)

        _dismiss_modal(page)
        if not _wait_visible(page, add_btn, 5_000):
            _log.error("vlvwStates_add not visible at row %d", i)
            break

        rows_before = _wc_state_row_count(page)
        clicked = False
        for attempt in range(1, 4):
            try:
                add_btn.first.click(timeout=_CLICK_TIMEOUT)
            except Exception as exc:
                _log.warning("Rating %d: Add click attempt %d failed: %s", i, attempt, exc)
                page.wait_for_timeout(400)
                continue
            grew = False
            for _ in range(10):
                page.wait_for_timeout(150)
                if _wc_state_row_count(page) > rows_before:
                    grew = True
                    break
            if grew:
                clicked = True
                break
            _log.warning("Rating %d: Add didn't add a row — retrying", i)
            _dismiss_modal(page)
        if not clicked:
            _log.error("Rating %d: Add never produced a new row — skipping", i)
            continue

        # Fill State combo + Experience Mod
        if rating.state:
            try:
                _fill_react_combo(page, "cboState", rating.state.upper())
            except Exception as exc:
                _log.warning("Rating %d: cboState fill failed: %s", i, exc)
        _fill_id_field(page, "pereFactorExperience__textField", rating.experience_mod)

        page.keyboard.press("Tab")
        page.wait_for_timeout(500)

    _log.info("Total Premium Calculations: filled (%d row(s))", len(setup.rating_info))


def _wc_state_row_count(page: Page) -> int:
    """Return the current row count in the WC Total Premium Calc states grid."""
    try:
        return int(page.evaluate(
            """() => document.querySelectorAll('[data-test^="vlvwStates-focusable-row-"]').length"""
        ))
    except Exception:
        return 0


# ── Locations + Class Codes (Locations screen) ────────────────────────────────
#
# Confirmed via live CDP inspection 2026-05-20: the WC "Locations" sidebar tab
# is a TWO-grid screen (CHM-WCLOCRTG):
#   - Top grid:    vlvwLocations (Loc # / Address / Site ID / Highest Floor)
#   - Bottom grid: vlvwRating    (State / Class code / Description / Payroll)
# The Rating Info grid is scoped to the currently-selected Location.

def _fill_wc_locations(page: Page, locations: list) -> None:
    """Add each WC Location row via the "Add Location" modal.

    Confirmed live 2026-05-20: clicking the vlvwLocations Add button opens a
    MODAL (not an inline form) containing the fields:
      Loc #, Address (compound adeAddress widget), Site ID, Highest Floor,
      Max # of emps
    The modal has Add / Finish / Cancel buttons.  Click **Finish** to commit
    the row + close the modal.  Forgetting Finish leaves a transparent modal
    layer that intercepts all subsequent clicks (including vlvwRating_add,
    which stays ``disabled`` until a Location row is committed).
    """
    if not locations:
        _log.info("No WC locations to add; skipping")
        return

    if not _nav_wc_section(page, "Location", "CHM-WCLOCRTG"):
        return

    _log.info("=== Locations (%d) ===", len(locations))

    add_btn = page.locator('[name="vlvwLocations"] .icon-button[title="Add"]')
    for i, loc in enumerate(locations, 1):
        check_cancel()
        _log.info("Adding location %d/%d: loc#=%r state=%r city=%r",
                  i, len(locations), loc.loc_num, loc.state, loc.city)
        _dismiss_modal(page)
        if not _wait_visible(page, add_btn, 5_000):
            _log.error("vlvwLocations Add button not visible at row %d", i)
            break
        rows_before = _wc_location_row_count(page)
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(700)  # let modal open

        # Basic fields (inside the modal)
        _fill_proxy_field(page, "inteLocation",     loc.loc_num)
        _fill_proxy_field(page, "streSiteID",       loc.site_id)
        _fill_proxy_field(page, "inteHighestFloor", loc.highest_floor)

        # Address widget — only attempt fill when there's actual address data.
        # An empty fill leaves the widget in an expanded state which can
        # interfere with the Finish click.
        if loc.street or loc.city or loc.zip_code:
            _fill_wc_address(page, loc.street, loc.city, loc.state, loc.zip_code)

        # Click Finish to commit the row and close the modal
        if not _click_modal_finish(page):
            _log.error("Location %d: Finish button not clickable — cancelling instead", i)
            _click_modal_cancel(page)
            continue

        # Verify the new row landed in the grid
        for _ in range(20):
            page.wait_for_timeout(150)
            if _wc_location_row_count(page) > rows_before:
                break
        else:
            _log.warning("Location %d: row count did not increase after Finish", i)

    _log.info("Locations: complete (%d row(s))", len(locations))


def _wc_location_row_count(page: Page) -> int:
    """Return the current row count in the WC Locations grid."""
    try:
        return int(page.evaluate(
            """() => {
                const grid = document.querySelector('[name="vlvwLocations"]');
                if (!grid) return 0;
                // Look for rendered rows
                const rows = grid.querySelectorAll('[data-test^="vlvwLocations-focusable-row-"]');
                if (rows.length) return rows.length;
                // Fallback: parse "N Items" from the footer
                const footer = document.querySelector('[data-test="vlvwLocations-footer"]');
                if (footer) {
                    const m = (footer.textContent || '').match(/(\\d+)\\s+Items?/i);
                    if (m) return parseInt(m[1], 10);
                }
                return 0;
            }"""
        ))
    except Exception:
        return 0


def _click_modal_finish(page: Page) -> bool:
    """Click the modal's Finish button. Returns True on success."""
    try:
        btn = page.get_by_role("button", name="Finish", exact=True)
        if not _wait_visible(page, btn, 3_000):
            return False
        btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(700)
        _log.debug("Clicked modal Finish button")
        return True
    except Exception as exc:
        _log.warning("Finish click failed: %s", exc)
        return False


def _click_modal_cancel(page: Page) -> None:
    """Click the modal's Cancel button as a recovery step."""
    try:
        btn = page.get_by_role("button", name="Cancel", exact=True)
        if btn.first.is_visible(timeout=600):
            btn.first.click(timeout=2_000)
            page.wait_for_timeout(400)
    except Exception:
        pass


def _norm_city(s: str) -> str:
    """Normalize a city name for comparison: lowercase, collapse whitespace,
    drop punctuation.  ``"St. Louis"`` → ``"st louis"``."""
    import re as _re
    s = (s or "").lower()
    s = _re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _wc_addr_keys(page: Page, field_id: str, value: str) -> None:
    """Type *value* into a field scoped under the WC address widget using real
    keystrokes (click → clear → type → Tab).

    EPIC's old-proxy address fields (``streStreet`` textarea, ``streCity`` /
    ``strePostalCode`` inputs) and the ``cboState`` Angular asi-combo-box all
    ignore value-set fills, so we drive them with the keyboard the way an
    operator would.  ``cboState`` takes the 2-letter code and holds it.
    """
    if not value:
        return
    sel = (
        f'[data-automation-id="adeAddress"] [data-automation-id="{field_id}"] input, '
        f'[data-automation-id="adeAddress"] [data-automation-id="{field_id}"] textarea'
    )
    try:
        loc = page.locator(sel).first
        loc.click(timeout=2_000)
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.keyboard.type(value, delay=20)
        page.keyboard.press("Tab")
        page.wait_for_timeout(_FILL_WAIT_MS)
        _log.debug("WC addr %s = %r OK", field_id, value)
    except Exception as exc:  # noqa: BLE001
        _log.warning("WC addr %s = %r FAILED: %s", field_id, value, exc)


def _wc_zip_picker_rows(page: Page) -> "list[dict] | None":
    """Return the ZIP Codes picker rows if the disambiguation modal is open.

    EPIC pops the ``vlvwZipPostCodes`` picker (status ZIPPOST) inside a sized
    ``modal-screen`` when a typed ZIP maps to more than one city.  Returns a
    list of ``{aid, text, cells}`` dicts (one per row) or ``None`` when no
    picker is open.

    The page always carries an empty, zero-size ``<message-box>`` host; it is
    ignored here — only a *sized* ``modal-screen`` actually carrying the picker
    grid counts as a real dialog.
    """
    try:
        return page.evaluate(
            """() => {
                const sized = el => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return el.offsetParent !== null && r.width > 0 && r.height > 0;
                };
                // Find the picker grid inside a real (sized) modal-screen.
                let scope = null;
                for (const m of document.querySelectorAll('modal-screen')) {
                    if (!sized(m)) continue;
                    if (m.querySelector('[data-automation-id^="vlvwZipPostCodes body-row item-"]')) {
                        scope = m; break;
                    }
                }
                if (!scope) {
                    if (!document.querySelector('[data-automation-id^="vlvwZipPostCodes body-row item-"]'))
                        return null;
                    scope = document;  // loose rows without a sized modal — still treat as picker
                }
                const rows = Array.from(
                    scope.querySelectorAll('[data-automation-id^="vlvwZipPostCodes body-row item-"]')
                ).map(r => ({
                    aid: r.getAttribute('data-automation-id'),
                    text: (r.textContent || '').trim(),
                    cells: Array.from(r.querySelectorAll('.body-cell,[role="gridcell"]'))
                        .map(c => (c.textContent || '').trim()).filter(Boolean),
                }));
                return rows.length ? rows : null;
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("WC ZIP picker probe failed: %s", exc)
        return None


def _wait_wc_zip_picker(page: Page, attempts: int = 16) -> "list[dict] | None":
    """Poll for the ZIP Codes picker (≈ ``attempts`` × 150 ms); None if it never opens."""
    for _ in range(attempts):
        rows = _wc_zip_picker_rows(page)
        if rows:
            return rows
        page.wait_for_timeout(150)
    return None


def _wc_pick_zip_row(page: Page, aid: str) -> None:
    """Click the matching ZIP picker row, then OK to commit it (City/State
    auto-fill from the row)."""
    try:
        page.locator(f'[data-automation-id="{aid}"]').first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(300)
    except Exception as exc:  # noqa: BLE001
        _log.warning("WC ZIP picker: row click failed (%s): %s", aid, exc)
    for sel in ('[data-automation-id="btnOK"] button', 'modal-screen button.button.accept'):
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible(timeout=600):
                loc.first.click(timeout=_CLICK_TIMEOUT)
                page.wait_for_timeout(500)
                _log.debug("WC ZIP picker: clicked OK via %s", sel)
                return
        except Exception:  # noqa: BLE001
            continue
    _log.warning("WC ZIP picker: OK button not clickable")


def _wc_cancel_zip_picker(page: Page) -> None:
    """Cancel the ZIP Codes picker modal.

    There are two ``btnCancel`` controls on the page (the picker's and the
    Location modal's), so every selector here is scoped to ``modal-screen`` —
    clicking the wrong Cancel would discard the whole Location row.
    """
    for sel in ('modal-screen button.button.cancel',
                'modal-screen [data-automation-id="btnCancel"] button'):
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible(timeout=600):
                loc.first.click(timeout=_CLICK_TIMEOUT)
                page.wait_for_timeout(400)
                _log.debug("WC ZIP picker: cancelled via %s", sel)
                return
        except Exception:  # noqa: BLE001
            continue
    _log.warning("WC ZIP picker: Cancel button not found in modal")


def _fill_wc_address(
    page: Page,
    street: str,
    city: str,
    state: str,
    zip_code: str,
) -> None:
    """Expand the adeAddress widget and fill the address, handling EPIC's
    ambiguous-ZIP picker.

    Same old-proxy address control as the Add-a-Contact form (``streStreet``
    textarea, ``streCity`` input, ``cboState`` asi-combo-box, ``strePostalCode``
    input).  Widget tab order is Street → ZIP and City → State, so we fill
    Street then ZIP and let EPIC resolve City/State from the ZIP:

    * **unambiguous ZIP** → City/State auto-fill on the ZIP Tab; nothing extra.
    * **ambiguous ZIP** → the ZIP Codes picker modal (``vlvwZipPostCodes``,
      status ZIPPOST) pops.  If a row's City matches the location's city we pick
      it and OK (City/State auto-fill from the row); otherwise we Cancel the
      modal and type City + State by hand.
    * **no ZIP** → can't resolve from a ZIP, so type City + State directly.

    All fields take real keystrokes (click → type → Tab); these proxy/Angular
    controls ignore value-set fills.  Picker flow mapped live via CDP 2026-05-27.

    The widget collapses back to display mode when focus moves away, so we
    expand it first by dispatching a real mouse chain on its ``.preview`` div
    (the underlying proxy DIV ignores a bare ``.click()``).
    """
    try:
        page.evaluate(
            """() => {
                const preview = document.querySelector('[data-automation-id="adeAddress"] .preview');
                if (!preview) return;
                const r = preview.getBoundingClientRect();
                const opts = { bubbles: true, cancelable: true, view: window,
                               clientX: r.x + r.width/2, clientY: r.y + r.height/2, button: 0 };
                preview.dispatchEvent(new MouseEvent('mousedown', opts));
                preview.dispatchEvent(new MouseEvent('mouseup', opts));
                preview.dispatchEvent(new MouseEvent('click', opts));
            }"""
        )
        page.wait_for_timeout(500)
    except Exception as exc:
        _log.warning("WC address widget expand failed: %s", exc)
        return

    # Street → Tab, then resolve City/State from the ZIP.
    _wc_addr_keys(page, "streStreet", street)

    if zip_code:
        _wc_addr_keys(page, "strePostalCode", zip_code)
        rows = _wait_wc_zip_picker(page)
        if rows:
            target = _norm_city(city)
            match_aid = None
            if target:
                # Prefer a discrete cell equal to the city; else substring of the row.
                for r in rows:
                    if any(_norm_city(c) == target for c in r.get("cells", [])):
                        match_aid = r["aid"]
                        break
                if not match_aid:
                    for r in rows:
                        if target in _norm_city(r.get("text", "")):
                            match_aid = r["aid"]
                            break
            if match_aid:
                _log.info("WC ZIP picker: matched city %r → %s", city, match_aid)
                _wc_pick_zip_row(page, match_aid)
            else:
                _log.info("WC ZIP picker: no row matched city %r — cancelling, typing City/State", city)
                _wc_cancel_zip_picker(page)
                _wc_addr_keys(page, "streCity", city)
                _wc_addr_keys(page, "cboState", (state or "").upper())
        else:
            _log.debug("WC ZIP %r unambiguous — City/State auto-filled", zip_code)
    else:
        # No ZIP to resolve from — type City + State directly.
        _wc_addr_keys(page, "streCity", city)
        _wc_addr_keys(page, "cboState", (state or "").upper())

    # Collapse the widget by clicking elsewhere (the Loc # field) so the
    # next Rating row's cboState doesn't get confused with the address state.
    try:
        loc_field = page.locator('[data-automation-id="inteLocation"] input')
        if loc_field.count():
            loc_field.first.click(timeout=1_500)
            page.wait_for_timeout(300)
    except Exception:
        pass


def _fill_class_codes(page: Page, setup: WorkersCompSetup) -> None:
    """Add class-code rows in the vlvwRating sub-grid on the Locations screen.

    EPIC scopes the Rating grid to the currently-selected Location row.  We
    expect the operator (or _fill_wc_locations above) to have left a Location
    selected when this runs — if no location exists yet, the Add button is
    disabled and we abort cleanly.

    Field IDs confirmed via CDP 2026-05-20: streClassCode, streDescriptionCode,
    cboState, cureEstimatedAnnualPayroll, plus optional streCategories /
    inteFullTime / intePartTime / streSIC / streNAICS.
    """
    if not setup.class_codes:
        _log.info("No class codes; skipping")
        return

    # Already on Locations screen — don't re-nav.
    _log.info("=== Class Codes / Rating Information (%d row(s)) ===", len(setup.class_codes))

    add_btn = page.locator('[name="vlvwRating"] .icon-button[title="Add"]')
    if not _wait_visible(page, add_btn, 3_000):
        _log.error("vlvwRating Add not visible — was a Location selected?")
        return

    for i, cc in enumerate(setup.class_codes, 1):
        check_cancel()
        _log.info("Adding class code %d/%d: state=%r code=%r payroll=%r",
                  i, len(setup.class_codes), cc.state, cc.class_code, cc.payroll)
        # Dismiss any leftover validation modal from the previous iteration
        # (e.g. "Required information missing" if a prior row's Categories
        # didn't get filled before the Add click).  _dismiss_modal already
        # closes common-message-modal alerts; this also clicks OK on
        # validation-error message-boxes.
        _dismiss_modal(page)
        try:
            err_ok = page.locator('message-box button')
            if err_ok.first.is_visible(timeout=400):
                err_ok.first.click(timeout=1_500)
                page.wait_for_timeout(300)
                _log.warning("Class codes: dismissed validation error before row %d", i)
        except Exception:
            pass
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(500)

        # State combo (rating row's, NOT the address widget's)
        if cc.state:
            _fill_proxy_field(page, "cboState", cc.state.upper())

        # Class code — type the 4-digit code, then click the lookup button
        # (icon-Locate) to open the WC Class Codes lookup modal.  Picking a
        # row + clicking Finish lets EPIC auto-fill streDescriptionCode AND
        # streCategories from its class-code library.  Description Code is
        # skipped per user instruction (filled automatically by the lookup).
        if cc.class_code:
            try:
                inp = page.locator('[data-automation-id="streClassCode"] input')
                if inp.count():
                    inp.first.click(timeout=2_000)
                    inp.first.fill(cc.class_code, timeout=2_000)
                    _log.debug("Class code typed = %r", cc.class_code)
                    _open_class_code_lookup(page, cc.class_code, cc.description)
            except Exception as exc:
                _log.warning("Class code lookup failed: %s", exc)

        # Categories is REQUIRED.  If the lookup didn't fill it (e.g. modal
        # never rendered for this code, or no match against the description),
        # fall back to typing the state.json description directly so EPIC
        # won't reject the row on save.
        _ensure_categories_filled(page, cc.description)

        # Payroll
        if cc.payroll:
            _fill_proxy_field(page, "cureEstimatedAnnualPayroll",
                              cc.payroll.replace("$", "").replace(",", ""))

        page.wait_for_timeout(300)

    _log.info("Class Codes: complete (%d row(s))", len(setup.class_codes))


def _ensure_categories_filled(page: Page, description: str) -> None:
    """If the Categories textarea is empty, fill it from state.json's description.

    The Class Code lookup auto-fills Categories when it picks a row, but if
    the lookup modal never rendered or no row matched, Categories stays blank
    and EPIC rejects the Rating row with "Required information is missing".
    """
    try:
        ta = page.locator('[data-automation-id="streCategories"] textarea')
        if not ta.count():
            return
        current = (ta.first.input_value(timeout=600) or "").strip()
        if current:
            return  # already filled (probably by the lookup)
        if not description:
            _log.warning("streCategories blank and no description in state.json — row will fail validation")
            return
        ta.first.click(timeout=2_000)
        ta.first.fill(description, timeout=2_000)
        page.keyboard.press("Tab")
        page.wait_for_timeout(_FILL_WAIT_MS)
        _log.info("streCategories: manually filled with %r (lookup didn't populate)", description)
    except Exception as exc:
        _log.warning("Could not manually fill streCategories: %s", exc)


def _open_class_code_lookup(page: Page, code: str, description: str) -> None:
    """Click the streClassCode lookup button, pick the best matching result,
    and click Finish.  If no rows match, Cancels the modal and logs a warning
    so the rating row is still saved (with whatever fields filled to that
    point — Categories will be blank, which the operator can correct later).

    Confirmed via live CDP inspection 2026-05-20:
      - Lookup button: ``[data-automation-id="streClassCode"] button`` (icon-Locate)
      - Modal screen code: WCCLSLUP
      - Result grid: ``[data-automation-id^="vlvwClassCode body-row item-"]``
      - Result row text format: ``{code}{description}`` (e.g. "6217Excavation & Drivers")
      - Modal close: Finish (commits) or Cancel (discards)

    Selection strategy:
      1. If state.json provides ``description``, find the row whose text
         contains that description (case-insensitive substring).
      2. Else, if only one result row exists, pick it.
      3. Else, log a warning and Cancel (don't guess between siblings).
    """
    try:
        btn = page.locator('[data-automation-id="streClassCode"] button')
        if not btn.count():
            _log.warning("Class code lookup button not found")
            return
        btn.first.click(timeout=2_000)
        page.wait_for_timeout(1_000)
    except Exception as exc:
        _log.warning("Class code lookup button click failed: %s", exc)
        return

    # Wait for the modal grid to render.  Some codes take noticeably longer
    # for EPIC's library to query — give it up to ~6 s before giving up.
    # First check if the modal itself opened (header present); if it did, click
    # the modal's "Locate" button to trigger the search explicitly.
    modal_loc = page.locator('modal-screen')
    modal_opened = False
    for _ in range(20):
        page.wait_for_timeout(150)
        if modal_loc.count() and modal_loc.first.is_visible():
            modal_opened = True
            break
    if modal_opened:
        # Ensure the Locate (Search) button has been pressed.  Some EPIC
        # builds auto-search on open; others wait for the explicit click.
        try:
            locate = page.get_by_role("button", name="Locate", exact=True)
            if locate.first.is_visible(timeout=400):
                locate.first.click(timeout=1_500)
                page.wait_for_timeout(800)
        except Exception:
            pass

    rows_loc = page.locator('[data-automation-id^="vlvwClassCode body-row item-"]')
    appeared = False
    for _ in range(40):  # ~6 s total
        page.wait_for_timeout(150)
        if rows_loc.count() > 0:
            appeared = True
            break
    if not appeared:
        _log.warning("Class code %r: lookup modal grid did not render — cancelling", code)
        _click_modal_cancel(page)
        return

    # Collect row texts for matching
    rows_text: list = []
    try:
        n = rows_loc.count()
        for i in range(min(n, 50)):
            t = (rows_loc.nth(i).text_content() or "").strip()
            rows_text.append(t)
    except Exception as exc:
        _log.warning("Class code %r: row enumeration failed: %s", code, exc)

    if not rows_text:
        _log.warning("Class code %r: no matching results — cancelling lookup, Categories will be blank", code)
        _click_modal_cancel(page)
        return

    # Pick the best match
    desc_low = (description or "").strip().lower()
    pick_idx = -1
    if desc_low:
        # Strip trailing period/punctuation for friendlier matching
        desc_clean = desc_low.rstrip(".").strip()
        for idx, t in enumerate(rows_text):
            if desc_clean and desc_clean in t.lower():
                pick_idx = idx
                break
        if pick_idx == -1:
            # Try the first significant word from the description
            key = desc_clean.split()[0] if desc_clean else ""
            if key:
                for idx, t in enumerate(rows_text):
                    if key in t.lower():
                        pick_idx = idx
                        break
    if pick_idx == -1:
        if len(rows_text) == 1:
            pick_idx = 0
        else:
            _log.warning(
                "Class code %r: %d candidates, none match description %r — cancelling",
                code, len(rows_text), description,
            )
            _click_modal_cancel(page)
            return

    _log.info("Class code %r: picked row [%d] %r (from %d candidates)",
              code, pick_idx, rows_text[pick_idx], len(rows_text))
    try:
        rows_loc.nth(pick_idx).click(timeout=2_000)
        page.wait_for_timeout(400)
    except Exception as exc:
        _log.warning("Class code %r: row click failed: %s — cancelling", code, exc)
        _click_modal_cancel(page)
        return

    if not _click_modal_finish(page):
        _log.warning("Class code %r: Finish click failed — cancelling", code)
        _click_modal_cancel(page)
        return

    # Give EPIC a moment to populate Description Code + Categories
    page.wait_for_timeout(500)
    _log.debug("Class code %r: lookup committed (Categories should be auto-filled)", code)


# ── Forms & Endorsements ──────────────────────────────────────────────────────

WC_STANDARD_FORMS: list[_GLFormSpec] = [
    _GLFormSpec(name="Blanket by Written Contract - Waiver of Subrogation"),
]


def _fill_wc_forms_endorsements(page: Page, forms: list[_GLFormSpec]) -> None:
    """Add each form / endorsement row on the WC Forms & Endorsements screen.

    Assumes the same OLD Angular proxy pattern as BAUT F&E (vlvwFormsEnd +
    proxy DIVs).  Falls back to React vlvwFormsEnd_add if the proxy pattern
    isn't present.
    """
    if not forms:
        return
    if not _nav_wc_section(page, "FormEndorsement", "CHM-WCFRMEND"):
        return

    _log.info("=== Forms & Endorsements (%d) ===", len(forms))

    proxy_add = page.locator('[name="vlvwFormsEnd"] .icon-button[title="Add"]')
    react_add = page.locator('[data-test="vlvwFormsEnd_add"]')
    use_proxy = proxy_add.count() > 0
    add_btn = proxy_add if use_proxy else react_add
    fill_fn = _fill_proxy_field if use_proxy else _fill_id_field

    for i, frm in enumerate(forms):
        check_cancel()
        _log.info("Adding form %d: %r", i + 1, frm.name)
        _dismiss_modal(page)
        if not _wait_visible(page, add_btn, 5_000):
            _log.error("F&E Add button not visible for row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)

        if frm.form_number:
            fill_fn(page, "streNumber", frm.form_number)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
        fill_fn(page, "streName", frm.name)
        if frm.edition_date:
            fill_fn(page, "dteEdition", frm.edition_date)
        if frm.copyright_type:
            try:
                _fill_combo(page, "cboCopyrightType", frm.copyright_type)
            except Exception as exc:
                _log.warning("Could not set copyright type %r for form %d: %s",
                             frm.copyright_type, i + 1, exc)
        fill_fn(page, "streCopyrightCode", frm.copyright_code)
        fill_fn(page, "curePremium" if use_proxy else "curePremium__textField", frm.premium)
        page.wait_for_timeout(300)

    _log.info("Forms & Endorsements: complete (%d rows)", len(forms))


# ── Entry point ───────────────────────────────────────────────────────────────

def run(page: Page, setup: WorkersCompSetup) -> bool:
    """Walk all WCOM screens for the given state, filling from ``setup``."""
    _log.info("=== Workers' Compensation entry step starting (state=%s) ===", setup.state)
    _log.info(
        "Setup: state=%s, %d rating row(s), %d class code(s), part1=%r part3=%r",
        setup.state, len(setup.rating_info), len(setup.class_codes),
        setup.part1_states, setup.part3_states,
    )

    try:
        if not _nav_to_workers_comp_state(page, setup.state):
            _log.error("Aborting — could not open WCOM line for state %s", setup.state)
            return False

        _log.info("=== Step 1: Policy Information / Total Premiums ===")
        checkpoint(page, f"Workers Comp ({setup.state}): Policy Information")
        _fill_policy_info(page, setup)

        # Locations + Class Codes live on the SAME screen (CHM-WCLOCRTG):
        # add the location first so a row is selected, then add class-code
        # Rating rows scoped to that selected location.
        _log.info("=== Step 2: Locations ===")
        checkpoint(page, f"Workers Comp ({setup.state}): Locations")
        _fill_wc_locations(page, setup.locations)

        _log.info("=== Step 3: Class Codes (Rating Information) ===")
        checkpoint(page, f"Workers Comp ({setup.state}): Class Codes")
        _fill_class_codes(page, setup)

        _log.info("=== Step 4: Total Premium Calculations ===")
        checkpoint(page, f"Workers Comp ({setup.state}): Total Premium Calculations")
        _fill_total_prem_calc(page, setup)

        _log.info("=== Step 5: Forms & Endorsements ===")
        checkpoint(page, f"Workers Comp ({setup.state}): Forms & Endorsements")
        _fill_wc_forms_endorsements(page, WC_STANDARD_FORMS)

        _log.info("=== Workers' Compensation entry step complete (state=%s) ===", setup.state)
        return True
    except EntryCancelled:
        raise
    except Exception as exc:
        _log.error("Workers' Comp run aborted: %s", exc, exc_info=True)
        return False

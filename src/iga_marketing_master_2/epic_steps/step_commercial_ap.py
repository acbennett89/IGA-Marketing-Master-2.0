"""step_commercial_ap.py — fill the Commercial AP application form inside a submission.

Pre-condition
-------------
The browser is on the Submission Detail sidebar tree for the target MMS, with the
Commercial AP subtree visible (General Liability line opened or AP already navigated to).
The user will have already landed here via step_mms_create → Detail button.

What this step does
-------------------
1.  **Other Named Insureds** (CHM-APOTHINS) — adds any additional named insureds
    supplied in *named_insureds*.  Loc 1 / primary named insured already comes from
    the client account; this list should contain only the *additional* names.

2.  **Premises** (CHM-APPREMSE) — adds Location 2+ from *premises*.  Loc 1 / Bldg 1
    is pre-populated from the client account; this step does not modify it.

3.  **General Information** (APGENIF3) — always applied regardless of extracted data:
    • Click "Default All Questions to 'No'" to set all 15 questions to No.
    • Set Question 2 (formal safety plan) to Yes.
    • Check the OSHA checkbox.

Skipped sections (per IGA workflow spec)
-----------------------------------------
Status, Applicant, Prior Carrier, Loss History, Forms & Endorsements, Remarks,
Attachments.  Applicant is auto-filled from the client page.

Selector notes (confirmed via live CDP inspection 2026-05-18)
--------------------------------------------------------------
- Sidebar navigation uses ``data-automation-id^="sidebar-button-Policy.CommercialAP.*"``
  prefix matching because the suffix contains dynamic policy IDs.
- Other Named Insureds Add/Delete use ``data-test`` attributes.
  The inline detail form auto-saves on navigation away from the section.
- Add Location dialog fields use ``name`` attributes (no ``data-automation-id``).
  Save = ``[data-test="btnFinish"]``, Save & Add Another = ``[data-test="btnAdd"]``.
- General Info Yes/No combos use ``data-automation-id="cboQuestion{N}"`` where N is
  the question number (1a, 1b, 2 … 14, 15).  "Default All to No" is a plain anchor
  with class ``link-label``.  Stickynote overlaps it, so we click via JS.
- OSHA checkbox id = ``APGENIF3chkOSHA``; click via label to bypass overlap issues.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled, check_cancel
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

# Re-use combo / text helpers from step_mms_create.
from iga_marketing_master_2.epic_steps.step_mms_create import (
    _fill_combo,
    _fill_text,
    _wait_visible,
)

_log = logging.getLogger("iga.epic_steps.commercial_ap")

_CLICK_TIMEOUT = 5_000
_NAV_WAIT_MS   = 1_000   # settle after sidebar navigation


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class NamedInsuredSpec:
    """One additional named insured to add in CHM-APOTHINS.

    Only *name* is required; the rest are optional and left blank unless provided.
    """
    name: str
    name_type: str = ""  # EPIC combo code, e.g. "LLC", "IND" — leave blank if unknown


@dataclass
class PremiseSpec:
    """One location/building row from the state.json ``location`` repeatable.

    All rows are passed in — including Loc 1 (which EPIC pre-creates from the
    account address and we EDIT) and Loc 2+ (which we ADD).  Multiple buildings
    at the same location_number are added via ``vlvwBuilding_add``.

    location_number  — state.json ``location.location_number``
    building_number  — state.json ``location.building_number``
    street           — parsed from ``location.building_description``
    """
    location_number: int
    building_number: int
    street: str
    city: str
    state: str      # 2-letter code, e.g. "TN"
    zip_code: str
    street2: str = ""
    county: str = ""


@dataclass
class CommercialApSetup:
    """Input for the Commercial AP entry step.

    named_insureds  — additional named insureds beyond the primary (already on account).
    premises        — locations to add as Loc 2, 3, … (Loc 1 is already present).
    General Information defaults (all No / Q2 Yes / OSHA) are always applied.
    """
    named_insureds: list[NamedInsuredSpec] = field(default_factory=list)
    premises: list[PremiseSpec] = field(default_factory=list)


# ── Sidebar navigation ────────────────────────────────────────────────────────

def _nav_to_commercial_ap(page: Page) -> bool:
    """Click the 'Commercial AP' entry in the submission sidebar tree.

    The parent entry has a dynamic automation-id (e.g.
    ``sidebar-button-17;106356 level-3``) so we locate it by level-3 +
    exact text.  Returns True once the Status screen (CHM-APSTATUS) is
    visible, False on timeout.
    """
    btn = page.locator('[data-automation-id*="level-3"]').filter(has_text="Commercial AP")
    if not _wait_visible(page, btn, 5_000):
        _log.error("Commercial AP sidebar entry not found")
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    status = page.locator('[data-automation-id="CHM-APSTATUS"]')
    if _wait_visible(page, status, 8_000):
        return True
    _log.warning("Clicked Commercial AP sidebar but CHM-APSTATUS not confirmed")
    return True  # best-effort


def _nav_section(page: Page, section_key: str, screen_code: str) -> bool:
    """Click the Commercial AP sidebar link whose automation-id contains *section_key*.

    Returns True when the target screen (identified by *screen_code* in the status bar)
    is visible, False on timeout.
    """
    sidebar_sel = f'[data-automation-id^="sidebar-button-Policy.CommercialAP.{section_key}"]'
    btn = page.locator(sidebar_sel)
    if not _wait_visible(page, btn, 5_000):
        _log.error("Sidebar link for %s not found", section_key)
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    # Confirm we landed on the right screen via the status bar text.
    status = page.locator(f'[data-automation-id="{screen_code}"]')
    if _wait_visible(page, status, 8_000):
        return True
    _log.warning("Navigated to %s but screen code %s not confirmed", section_key, screen_code)
    return True   # best-effort; continue anyway


# ── Other Named Insureds ──────────────────────────────────────────────────────

def _fill_other_named_insureds(page: Page, named_insureds: list[NamedInsuredSpec]) -> None:
    """Add each entry in *named_insureds* to the CHM-APOTHINS inline form.

    Clicking Add creates a blank inline row; filling Name then navigating away
    auto-saves the row (EPIC's inline-edit auto-save behaviour).  For multiple
    entries we click Add again after each fill — EPIC saves the current row and
    opens a new blank one.
    """
    if not named_insureds:
        return

    _nav_section(page, "OtherNamedInsureds", "CHM-APOTHINS")

    add_btn = page.locator('[data-test="vlvwOtherNamedInsureds_add"]')

    for i, ni in enumerate(named_insureds):
        check_cancel()
        _log.info("Adding named insured %d: %r", i + 1, ni.name)

        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Add button not visible for named insured row %d", i + 1)
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)

        # Fill Name (required).  The inline detail form exposes it as role=textbox name="Name".
        try:
            name_inp = page.get_by_role("textbox", name="Name").first
            name_inp.wait_for(state="visible", timeout=5_000)
            name_inp.click(timeout=2_000)
            name_inp.fill(ni.name, timeout=3_000)
        except Exception as exc:
            _log.error("Could not fill Name for named insured %d: %s", i + 1, exc)
            continue

        # Optional: Name type combo.
        if ni.name_type:
            try:
                _fill_combo(page, "cboNameType", ni.name_type)
            except Exception:
                pass

        page.wait_for_timeout(400)

    # Navigate to Premises to trigger auto-save on the last inline row.
    # (The navigation itself is the save trigger in EPIC's inline-edit pattern.)


# ── Premises ──────────────────────────────────────────────────────────────────

def _fill_location_modal(page: Page, loc: PremiseSpec) -> None:
    """Fill the Add/Update Location modal and click Save (data-test='btnFinish').

    Street field (data-test='streetLine') uses address autocomplete — type, wait
    for [data-test='row'] dropdown, click with force=True to bypass tooltip overlay.
    City/zip overwrite with [data-test='{field}'] input.
    State is a React combobox (id='adePrimary-state', data-test='state-combobox-input')
    — NOT a native <select>; fill by typing then clicking the dropdown row.
    """
    # ── Street (autocomplete) ──────────────────────────────────────────────
    search_str = loc.street
    if loc.street2:
        search_str = f"{loc.street} {loc.street2}".strip()

    try:
        street_inp = page.locator('[data-test="streetLine"] input').first
        street_inp.click(timeout=2_000)
        street_inp.fill(search_str, timeout=2_000)
        _log.debug("Location modal: filled streetLine = %r", search_str)
        page.wait_for_timeout(500)
        # Press Tab to commit the typed value and dismiss the autocomplete dropdown.
        # Do NOT click autocomplete rows — the tooltip overlay intercepts clicks
        # and may select a wrong address variant (e.g. "Ste 160").
        # City, ZIP, and State are always filled explicitly below.
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)
        _log.debug("Location modal: streetLine committed via Tab")
    except Exception as exc:
        _log.warning("Location modal: could not fill streetLine %r: %s", search_str, exc)

    # ── City / ZIP / County ────────────────────────────────────────────────
    for data_test, value in [
        ("city",    loc.city),
        ("zipCode", loc.zip_code),
        ("county",  loc.county),
    ]:
        if not value:
            continue
        try:
            inp = page.locator(f'[data-test="{data_test}"] input').first
            inp.click(click_count=3, timeout=1_500)
            inp.fill(value, timeout=1_500)
            page.wait_for_timeout(150)
            _log.debug("Location modal: %s = %r  OK", data_test, value)
        except Exception as exc:
            _log.warning("Location modal: could not fill %s=%r: %s", data_test, value, exc)

    # ── State — React combobox (id='adePrimary-state', data-test='state-combobox-input')
    # NOT a native <select> — type the 2-letter code, click the matching dropdown row.
    # Brief wait ensures the city/zip autocomplete has fully settled before we click state.
    if loc.state:
        page.wait_for_timeout(300)
        try:
            state_inp = page.locator(
                'input[id="adePrimary-state"], [data-test="state-combobox-input"]'
            ).first
            state_inp.click(timeout=2_000)
            state_inp.fill(loc.state, timeout=1_500)
            page.wait_for_timeout(400)
            rows = page.locator('[data-test="dropdown-row"]')
            matched = False
            for n in range(min(rows.count(), 10)):
                if loc.state in (rows.nth(n).text_content() or ""):
                    rows.nth(n).click(timeout=2_000)
                    matched = True
                    break
            if not matched and rows.count() > 0:
                rows.first.click(timeout=2_000)
            _log.debug("Location modal: state = %r  OK", loc.state)
        except Exception as exc:
            _log.warning("Location modal: could not set state %r: %s", loc.state, exc)

    # ── Save ───────────────────────────────────────────────────────────────
    save_btn = page.locator('[data-test="btnFinish"]')
    if _wait_visible(page, save_btn, 5_000):
        save_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)
        _log.debug("Location modal: saved OK")
    else:
        _log.error("Location modal: btnFinish not found — modal may still be open")

    # Safety net: dismiss modal if still visible after save
    modal = page.locator('[data-test="Policy.CommercialAP.Location.Add"]')
    try:
        if modal.is_visible(timeout=500):
            _log.warning("Location modal: still open after save — pressing Escape")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
    except Exception:
        pass


def _fill_building_form(page: Page, bldg: PremiseSpec) -> None:
    """Fill the inline building detail form that opens after vlvwBuilding_add.

    Unlike location ADD (which opens a modal dialog), vlvwBuilding_add activates
    an inline form on the right side of the Premises screen.  Fields:
      streSiteID              — site label (optional)
      streBuildingDescription — street address (required)

    Navigation away from the Premises section auto-saves inline edits.
    """
    try:
        # Building description is required by EPIC
        desc_inp = page.locator('input[id="streBuildingDescription"]')
        if _wait_visible(page, desc_inp, 3_000):
            desc_inp.first.click(timeout=2_000)
            desc_inp.first.fill(bldg.street, timeout=2_000)
            page.wait_for_timeout(150)
            _log.debug("Building form: streBuildingDescription = %r  OK", bldg.street)
        else:
            _log.warning("Building form: streBuildingDescription not visible for Bldg %d", bldg.building_number)
    except Exception as exc:
        _log.warning("Building form: could not fill description for Bldg %d: %s", bldg.building_number, exc)


def _fill_premises(page: Page, premises: list[PremiseSpec]) -> None:
    """Fill CHM-APPREMSE using v1's grouping strategy (mirrored from marketing_application.py).

    All location rows (including Loc 1) are passed in.  They are grouped by
    ``location_number`` and sorted by ``building_number`` within each group.

    - Loc 1  → EDIT the row EPIC pre-created from the account address
    - Loc 2+ → ADD via ``vlvwLocation_add``
    - Extra buildings at the same location (Bldg 2, 3, …) → ``vlvwBuilding_add``

    Street autocomplete: type the street, click the matching dropdown row,
    then overwrite city/state/zip for accuracy.
    """
    if not premises:
        _log.info("No premises; skipping Premises section")
        return

    _nav_section(page, "Premise", "CHM-APPREMSE")

    # Group by location_number, sort buildings within each group.
    locs_by_num: dict[int, list[PremiseSpec]] = {}
    for loc in premises:
        locs_by_num.setdefault(loc.location_number, []).append(loc)

    for loc_num in sorted(locs_by_num.keys()):
        check_cancel()
        buildings = sorted(locs_by_num[loc_num], key=lambda r: r.building_number)
        first = buildings[0]

        _log.info(
            "Premises: loc_num=%d — %s, %s %s (%d building(s))",
            loc_num, first.street, first.city, first.state, len(buildings),
        )

        if loc_num == 1:
            # EDIT the pre-existing Loc 1 (account address).
            row0 = page.locator('[data-test="vlvwLocation-focusable-row-0"]')
            if _wait_visible(page, row0, 3_000):
                try:
                    row0.click(timeout=2_000)
                    page.wait_for_timeout(300)
                except Exception:
                    pass
            edit_btn = page.locator('[data-test="vlvwLocation_edit"]')
            if _wait_visible(page, edit_btn, 3_000):
                edit_btn.first.click(timeout=_CLICK_TIMEOUT)
                page.wait_for_timeout(500)
                _log.debug("Premises: editing Loc 1")
                _fill_location_modal(page, first)
            else:
                _log.warning("Premises: vlvwLocation_edit not found — Loc 1 left unchanged")
        else:
            # ADD a new location row for Loc 2+.
            add_btn = page.locator('[data-test="vlvwLocation_add"]')
            if not _wait_visible(page, add_btn, 5_000):
                _log.error("Premises: vlvwLocation_add not visible for Loc %d", loc_num)
                continue
            add_btn.first.click(timeout=_CLICK_TIMEOUT)
            page.wait_for_timeout(500)
            _log.debug("Premises: adding Loc %d", loc_num)
            _fill_location_modal(page, first)

        # Add extra buildings (Bldg 2, 3, …) for this location.
        for bldg in buildings[1:]:
            _log.info(
                "Premises: adding Bldg %d for Loc %d — %s",
                bldg.building_number, loc_num, bldg.street,
            )
            bldg_add = page.locator('[data-test="vlvwBuilding_add"]')
            if not _wait_visible(page, bldg_add, 3_000):
                _log.error(
                    "Premises: vlvwBuilding_add not visible for Loc %d Bldg %d",
                    loc_num, bldg.building_number,
                )
                continue
            bldg_add.first.click(timeout=_CLICK_TIMEOUT)
            page.wait_for_timeout(500)
            _fill_building_form(page, bldg)

    _log.info(
        "Premises section complete: %d unique location(s), %d total row(s)",
        len(locs_by_num), len(premises),
    )


# ── General Information ───────────────────────────────────────────────────────

def _fill_general_information(page: Page) -> None:
    """Apply the fixed General Information defaults on APGENIF3.

    Always performed regardless of extraction:
      • Default all 15 questions to No via the "Default All Questions" link.
      • Set Question 2 (formal safety plan) to Yes.
      • Check the OSHA checkbox.

    Notes:
      - The "Default All Questions to 'No'" anchor is overlapped by the sticky note
        panel at certain scroll positions; we JS-click it to bypass z-index.
      - OSHA checkbox (id=APGENIF3chkOSHA) is clicked via its label for the same reason.
    """
    _nav_section(page, "GeneralInformation", "APGENIF3")

    # 1. Default all questions to No via JS (stickynote overlaps the link).
    result = page.evaluate("""() => {
        const link = Array.from(document.querySelectorAll('a.link-label'))
            .find(el => el.textContent.includes('Default All Questions'));
        if (link) { link.click(); return true; }
        return false;
    }""")
    if not result:
        _log.warning("'Default All Questions to No' link not found on APGENIF3")
    page.wait_for_timeout(600)

    # 2. Set Q2 (cboQuestion2) to "Yes".
    _log.info("Setting General Information Q2 to Yes")
    try:
        _fill_combo(page, "cboQuestion2", "Yes")
    except Exception as exc:
        _log.error("Could not set Q2 to Yes: %s", exc)

    page.wait_for_timeout(300)

    # 3. Check the OSHA checkbox via its label (label click bypasses overlap issues).
    result = page.evaluate("""() => {
        const osha = document.getElementById('APGENIF3chkOSHA');
        if (!osha) return 'not_found';
        if (osha.checked) return 'already_checked';
        const label = document.querySelector('label[for="APGENIF3chkOSHA"]');
        if (label) { label.click(); return 'clicked_label'; }
        osha.click();
        return 'clicked_input';
    }""")
    _log.info("OSHA checkbox: %s", result)


# ── Public API ────────────────────────────────────────────────────────────────

def run(page: Page, setup: CommercialApSetup) -> bool:
    """Fill the Commercial AP sections of the open submission.

    Expects the browser to be on the Submission Detail page (MMS detail tree visible
    in the left sidebar).  Navigates to each required section, fills the data, and
    returns True on success.
    """
    try:
        _log.info("Navigating to Commercial AP in submission sidebar")
        _nav_to_commercial_ap(page)
        checkpoint(page, "Commercial AP: navigation")

        if setup.named_insureds:
            _log.info("Filling %d other named insured(s)", len(setup.named_insureds))
            checkpoint(page, "Commercial AP: Other Named Insureds")
            _fill_other_named_insureds(page, setup.named_insureds)

        _log.info("Filling premises (%d additional location(s))", len(setup.premises))
        checkpoint(page, "Commercial AP: Premises")
        _fill_premises(page, setup.premises)

        _log.info("Filling General Information defaults")
        checkpoint(page, "Commercial AP: General Information")
        _fill_general_information(page)

        _log.info("Commercial AP step complete")
        return True

    except EntryCancelled:
        raise
    except PlaywrightTimeout as exc:
        _log.error("Commercial AP timed out: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error in step_commercial_ap: %s", exc)
        return False

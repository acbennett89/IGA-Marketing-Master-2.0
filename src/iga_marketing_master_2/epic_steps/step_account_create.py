"""step_account_create.py — create a new EPIC client account (Prospect by default).

Pre-condition
-------------
The browser is on the EPIC Account Locate screen. Best practice is to run the
duplicate-check pre-flight first::

    step_account_lookup_nav.run(page)
    step_account_search.run(page, search_by="Account/Business Name", search_term=setup.account_name)
    dup = step_account_dup_check.run(page, target_name=setup.account_name)
    if dup.outcome is DupCheckOutcome.NO_MATCH:
        step_account_create.run(page, setup=AccountSetup(...))

The create step is intentionally separate from the dup-check so the caller
(GUI) can surface NO_MATCH / EXACT / FUZZY_MATCHES decisions before committing.

What this step does
-------------------
1.  Clicks the Add icon on ``vlvwResults`` and waits for the Add Account form
    (a new React UI, **not** the legacy Angular EPIC screens).
2.  Fills the form from *setup*:
        - Required: format, name, client type, agency, branch, phone types,
          contact prefs.
        - Optional: address (via SmartyStreets autocomplete), phones, emails,
          website, NAICS / SIC, primary contact info, lines of business,
          comments.
3.  Clicks "Save Account".
4.  Watches for either:
        (a) Successful creation — title transitions away from "Add Client
            Account", new account screen renders.
        (b) **EPIC's built-in duplicate-check popup** — surfaced to the
            operator via :func:`runtime.on_validation_halt`. On Proceed the
            step looks for a Continue / Save-anyway button; on Cancel it backs
            out via the form's Cancel + Discard-changes confirmation.
        (c) Validation error — form refuses to submit (required fields
            blank). Returns ``VALIDATION_FAILED`` with whatever feedback is
            visible.

Form framework
--------------
This screen is a brand-new React UI — completely different from the Angular
EPIC screens that drive Account Locate, Policies, MMS, etc. None of the
``data-automation-id`` / ``cbo*`` / ``stre*`` conventions apply. Inputs are
addressed by their HTML ``name`` attribute (matching the form path), e.g.
``input[name="accountInfo.accountName"]``. Combos have an additional stable
hook: ``[data-test="<name>-combobox-selections"]``.

Selector inventory (confirmed via live CDP inspection 2026-05-22)
-----------------------------------------------------------------
- Screen marker:   ``document.title === "Add Client Account"``
- Submit button:   ``button[type="submit"]`` containing text "Save Account"
- Cancel button:   ``button[type="button"]`` containing text "Cancel"
- Discard dialog:  shows after Cancel — buttons "Yes" / "No"
- Combo dropdown:  ``.ComboboxDropdown_module_dropdown__94e367dc`` (portal)
- Combo row:       ``.DropdownRow_module_row__4b41325c`` inside dropdown
- Address suggestion list:
                   ``.SuggestionFieldResults_module_result__d8e3be64``
                   (multi-suite buildings carry ``...badgeExists...``)

Behaviour notes
---------------
- **EPIC pre-populates 5 of 6 required combos** with reasonable defaults
  (Business / Mobile / Email / Mail / Mail). We only re-select them when the
  *setup* asks for something different — saves clicks and keeps logs clean.
- **Combo input shows the *Name* column after selection**, not the *Code*.
  The step picks rows by Code (first cell, stable) and verifies by matching
  the Name afterwards.
- **Address field is a SmartyStreets autocomplete**. We type slowly via
  ``pressSequentially`` so the suggestion API fires, then click the first
  non-badged suggestion. Suggestions populate city / state / ZIP / county
  automatically — those fields are *not* filled manually.
- **Lines of Business** are checkboxes addressed by their uppercase ``name``
  (``AGRICULTURE``, ``BENEFITS``, ``BONDS``, ``COMMERCIAL``,
  ``FINANCIAL_SERVICES``, ``LIFE_AND_HEALTH``, ``PERSONAL``, ``OTHER``).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

from iga_marketing_master_2.epic_steps import runtime
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.account_create")


# ── Reference data (mirror in the GUI for dropdown population) ──────────────

AGENCY_BRANCHES: dict[str, list[str]] = {
    "IEB": ["002", "003", "004", "005", "006"],
    "IGA": ["001", "002", "003", "004", "005", "006"],
    "MEM": ["004"],
    "MS":  ["002"],
    "VA":  ["007"],
}
"""Branch codes available per agency code. MEM / MS / VA are single-branch."""

AGENCY_NAMES: dict[str, str] = {
    "IEB": "IGAEB LLC",
    "IGA": "Insurance Group of America LLC",
    "MEM": "IGA Memphis LLC",
    "MS":  "Meridian Safety LLC",
    "VA":  "VA",  # Display name TBD via live inspection — placeholder
}
"""The display name EPIC shows in the combo input after selection (not the code)."""

# Agencies the IGA Marketing Master GUI should expose in its dropdown.
# (Other agencies are kept in AGENCY_BRANCHES so the step accepts them if
# they ever appear in operator input, but they don't show in the GUI menu.)
GUI_AGENCY_CHOICES: tuple[str, ...] = ("IGA", "VA")

BRANCH_NAMES: dict[str, str] = {
    "001": "Corporate",
    "002": "Franklin TN",
    "003": "Chattanooga TN",
    "004": "Memphis",
    "005": "Knoxville TN",
    "006": "Campbellsville",
    "007": "Richmond",
}

VALID_CLIENT_FORMATS = {"BUSINESS", "INDIVIDUAL"}
VALID_CLIENT_TYPES   = {"PROSPECT", "INSURED"}
VALID_PHONE_TYPES    = {"Business", "Fax", "Mobile", "Other", "Residence"}
VALID_CONTACT_VIA    = {"Email", "Fax", "Mail", "Phone", "SMS"}
VALID_BILLING_PREFS  = {"Email", "Fax", "Mail"}
VALID_SERVICING_PREFS = {"Email", "Fax", "Mail"}

VALID_LINES_OF_BUSINESS = {
    "AGRICULTURE", "BENEFITS", "BONDS", "COMMERCIAL",
    "FINANCIAL_SERVICES", "LIFE_AND_HEALTH", "PERSONAL", "OTHER",
}

# EPIC's "Business Type" combo on the contact Business tab
# (businessDetail.type). Exact labels captured via live CDP 2026-05-27 —
# the GUI Client-page dropdown offers these verbatim so the value always
# matches a real EPIC row. Keep alphabetical (EPIC sorts the combo so).
BUSINESS_TYPES: tuple[str, ...] = (
    "Association",
    "C Corporation",
    "Church",
    "City Commission",
    "City Department",
    "Closely Held Corporation",
    "Co-employers",
    "Common Ownership",
    "Condo Association",
    "Condo Owners",
    "Corp Non-Profit Organization",
    "Corporation",
    "Corporations (more than one)",
    "County",
    "Estate of",
    "Farm Corporation",
    "Fraternity",
    "General Partnership",
    "Government Agency",
    "Hospital",
    "Individual",
    "Individuals (more than one)",
    "Joint Venture",
    "Labor Union",
    "Limited Corporation",
    "Limited Liability Company",
    "Limited Liability Partnership",
    "Limited Partnership",
    "Multiple Status",
    "Municipality",
    "Not for Profit Organization",
    "Partnership",
    "Political Subdivision",
    "Professional Corporation",
    "Proprietorship",
    "S Corporation",
    "School",
    "School Board",
    "Sorority",
    "Subchapter S Corporation",
    "Township",
    "Trust",
    "Unincorporated Association",
)


# ── Data types ───────────────────────────────────────────────────────────────

@dataclass(slots=True)
class AccountSetup:
    """Operator-supplied data for a new account.

    ``__post_init__`` validates the cross-field constraint that ``branch`` is
    valid for ``agency``. Other fields are checked at use-time so the GUI can
    populate them defensively without pre-validation roundtrips.
    """

    # Required
    account_name: str

    # Required with sensible defaults (EPIC pre-populates 5/6 of these too).
    agency: str = "IGA"
    branch: str = "002"
    client_format: str = "BUSINESS"
    client_type:   str = "PROSPECT"
    business_phone_type: str = "Business"
    primary_phone_type:  str = "Mobile"
    contact_via:    str = "Email"
    billing_pref:   str = "Email"
    servicing_pref: str = "Email"

    # Optional — fill when the GUI has the data, skip otherwise.
    fein:               str | None = None   # insured's FEIN → Main Business Contact
    business_type:      str | None = None   # EPIC Business Type → Main Business Contact
    account_source:     str | None = None
    street_address:     str | None = None
    address_2:          str | None = None
    business_phone:     str | None = None
    business_email:     str | None = None
    business_website:   str | None = None
    naics:              str | None = None
    sic:                str | None = None
    primary_first_name: str | None = None
    primary_last_name:  str | None = None
    primary_phone:      str | None = None
    primary_email:      str | None = None
    lines_of_business:  list[str] = field(default_factory=list)
    comments:           str | None = None

    def __post_init__(self) -> None:
        if not self.account_name or not self.account_name.strip():
            raise ValueError("AccountSetup.account_name is required")

        if self.agency not in AGENCY_BRANCHES:
            raise ValueError(
                f"Unknown agency {self.agency!r}. "
                f"Valid: {sorted(AGENCY_BRANCHES)}"
            )

        valid_branches = AGENCY_BRANCHES[self.agency]
        if self.branch not in valid_branches:
            raise ValueError(
                f"Branch {self.branch!r} is not valid for agency "
                f"{self.agency!r}. Valid: {valid_branches}"
            )

        if self.client_format not in VALID_CLIENT_FORMATS:
            raise ValueError(
                f"client_format {self.client_format!r} invalid; "
                f"choose from {sorted(VALID_CLIENT_FORMATS)}"
            )

        if self.client_type not in VALID_CLIENT_TYPES:
            raise ValueError(
                f"client_type {self.client_type!r} invalid; "
                f"choose from {sorted(VALID_CLIENT_TYPES)}"
            )

        for label, value, valid in (
            ("business_phone_type",  self.business_phone_type,  VALID_PHONE_TYPES),
            ("primary_phone_type",   self.primary_phone_type,   VALID_PHONE_TYPES),
            ("contact_via",          self.contact_via,          VALID_CONTACT_VIA),
            ("billing_pref",         self.billing_pref,         VALID_BILLING_PREFS),
            ("servicing_pref",       self.servicing_pref,       VALID_SERVICING_PREFS),
        ):
            if value not in valid:
                raise ValueError(
                    f"{label}={value!r} invalid; choose from {sorted(valid)}"
                )

        for lob in self.lines_of_business:
            if lob not in VALID_LINES_OF_BUSINESS:
                raise ValueError(
                    f"Unknown line of business {lob!r}; "
                    f"valid options: {sorted(VALID_LINES_OF_BUSINESS)}"
                )


class AccountCreateOutcome(Enum):
    """Outcome of an account-create attempt."""

    CREATED           = "created"
    """Account saved. ``lookup_code`` is populated when EPIC reports it."""

    DUP_CANCELLED     = "dup_cancelled"
    """EPIC's built-in duplicate popup appeared; operator chose Cancel."""

    VALIDATION_FAILED = "validation_failed"
    """Form refused to submit (missing required field, address mismatch)."""

    ERROR             = "error"
    """Pre-condition failed, DOM unexpected, or step couldn't recover."""


@dataclass(slots=True)
class AccountCreateResult:
    outcome: AccountCreateOutcome
    lookup_code: str | None = None
    account_name: str | None = None
    error: str | None = None


# ── Timing constants ─────────────────────────────────────────────────────────

_FORM_WAIT_MS         = 15_000   # wait for Add Client Account to render
_COMBO_OPEN_TIMEOUT   = 3_000
_COMBO_SETTLE_MS      = 400
_CLICK_TIMEOUT        = 5_000
_ADDRESS_SUGGEST_MS   = 4_000    # SmartyStreets debounce window
_POST_SAVE_WAIT_MS    = 20_000   # wait for either success or dup popup
_LOOKUP_CODE_SETTLE_MS = 8_000   # wait for "<CODE> - <Name>" title after save
_FINAL_SETTLE_MS      = 1_000


# ── Private helpers ──────────────────────────────────────────────────────────

def _is_on_add_form(page: Page) -> bool:
    """True when the React Add-Account form is the active screen."""
    try:
        return (page.title() or "").strip().lower() == "add client account"
    except Exception:  # noqa: BLE001
        return False


def _open_add_form(page: Page) -> None:
    """Click the Add icon on ``vlvwResults`` and wait for the form to render.

    Waits for both the title flip (``Add Client Account``) **and** at least
    one form input to exist in the DOM (the Account Name field). Title-only
    wait isn't enough — React mounts the title before the form body, and
    early calls into :func:`_set_radio` / :func:`_fill_text` would race the
    inputs into existence.
    """
    add_btn = page.locator('[data-automation-id="vlvwResults"] .icon-button[title="Add"]')
    add_btn.first.wait_for(state="visible", timeout=_CLICK_TIMEOUT)
    add_btn.first.click(timeout=_CLICK_TIMEOUT)

    deadline = time.time() + _FORM_WAIT_MS / 1000.0
    while time.time() < deadline:
        if _is_on_add_form(page):
            # Belt-and-braces: confirm a known form input is in the DOM
            # before we declare success.
            try:
                if page.locator('input[name="accountInfo.accountName"]').count() > 0:
                    page.wait_for_timeout(300)  # let React finish mounting siblings
                    return
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(150)
    raise RuntimeError("Clicked Add but the Add Account form did not render in time.")


def _fill_text(page: Page, name: str, value: str | None) -> None:
    """Set the form field named ``name`` to *value* via the native value setter.

    Matches ``<input>``, ``<textarea>``, or anything else with the matching
    ``name`` attribute — the Add Account form has both (e.g. ``agencyInfo.
    comments`` is a textarea, almost everything else is an input). The native
    setter on the *element's own prototype* is needed because React's
    controlled inputs ignore plain ``.value = ...`` mutations.

    No-op when *value* is None or empty.
    """
    if value is None or value == "":
        return
    sel = f'[name="{name}"]'
    page.evaluate(
        """([sel, val]) => {
            const el = document.querySelector(sel);
            if (!el) throw new Error('field not found: ' + sel);
            el.focus();
            // Pick the value setter that matches the element's actual class
            // (HTMLInputElement, HTMLTextAreaElement, or HTMLSelectElement).
            // React's onChange listens via the prototype's setter; using the
            // wrong one is silently ignored.
            const proto = (
                el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype :
                el.tagName === 'SELECT'   ? window.HTMLSelectElement.prototype   :
                                            window.HTMLInputElement.prototype
            );
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, val);
            el.dispatchEvent(new Event('input',  {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        [sel, value],
    )
    _log.debug("filled %s = %r", name, value)


def _set_radio(page: Page, name: str, value: str) -> None:
    """Click the radio matching ``name`` + ``value``, if not already checked.

    Same dance as :func:`_set_checkbox`: the actual ``<input type="radio">``
    is hidden behind a React ``Radio_module_*`` wrapper, so Playwright
    refuses to click it directly (the associated ``<label>`` intercepts
    pointer events). We resolve the input's per-form-open ``id`` and
    click the matching ``label[for=<id>]`` instead.

    Polls briefly for the input to mount — the form's React tree finishes
    populating slightly after the title flips to "Add Client Account",
    and the radios are the first thing we touch.
    """
    state = None
    deadline = time.time() + 3.0
    while time.time() < deadline:
        state = page.evaluate(
            """([name, value]) => {
                const inp = document.querySelector(
                    `input[name="${name}"][value="${value}"]`
                );
                if (!inp) return null;
                return { id: inp.id, checked: inp.checked };
            }""",
            [name, value],
        )
        if state is not None:
            break
        page.wait_for_timeout(150)

    if state is None:
        raise RuntimeError(f"Radio {name}={value!r}: input not found")

    if state["checked"]:
        _log.debug("radio %s already = %r", name, value)
        return

    label_sel = f'label[for="{state["id"]}"]'
    page.locator(label_sel).first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(150)
    _log.debug("set radio %s = %r", name, value)


def _set_checkbox(page: Page, name: str, want_checked: bool) -> None:
    """Toggle ``input[name=name]`` to match *want_checked* (no-op if matched).

    The React form decorates checkboxes with a styled wrapper; the actual
    ``<input>`` is hidden, so Playwright's click on the input itself is
    refused. We resolve the input's ``id`` and click the matching
    ``label[for=<id>]`` instead. The ``id`` is a per-form-open UUID, so the
    lookup has to happen at call time.
    """
    # Read current state directly from the input — it's still queryable
    # even though Playwright won't *click* it.
    state = page.evaluate(
        """(name) => {
            const inp = document.querySelector(`input[name="${name}"]`);
            if (!inp) return null;
            return { id: inp.id, checked: inp.checked };
        }""",
        name,
    )
    if state is None:
        raise RuntimeError(f"Checkbox {name!r}: input not found")

    if state["checked"] == want_checked:
        _log.debug("checkbox %s already %s", name, want_checked)
        return

    label_sel = f'label[for="{state["id"]}"]'
    page.locator(label_sel).first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(150)
    _log.debug("set checkbox %s = %s", name, want_checked)


def _combo_current_value(page: Page, name: str) -> str:
    """Read the displayed value from a React combo."""
    try:
        return page.locator(f'input[name="{name}"]').first.input_value(timeout=1_000) or ""
    except Exception:  # noqa: BLE001
        return ""


def _set_react_combo(
    page: Page,
    name: str,
    code: str,
    *,
    display_name: str | None = None,
) -> None:
    """Pick the row whose first cell text == *code* in a React combo.

    *display_name* — optional Name-column text we expect to appear in the
    combo input after selection. When provided we treat that as the "already
    set" signal too (since EPIC shows the Name, not the Code).

    Idempotent — skips the click if the input already shows *code* or
    *display_name*.
    """
    current = _combo_current_value(page, name).strip()
    if current.casefold() == code.casefold() or (
        display_name and current.casefold() == display_name.casefold()
    ):
        _log.debug("combo %s already shows %r — no change", name, current)
        return

    # Open the dropdown — clicking the input is enough for these combos.
    page.locator(f'input[name="{name}"]').first.click(timeout=_COMBO_OPEN_TIMEOUT)
    page.wait_for_timeout(_COMBO_SETTLE_MS)

    # Wait for the dropdown rows to render.
    dropdown_sel = '.ComboboxDropdown_module_dropdown__94e367dc'
    row_sel      = '.DropdownRow_module_row__4b41325c'
    deadline = time.time() + _COMBO_OPEN_TIMEOUT / 1000.0
    while time.time() < deadline:
        if page.locator(dropdown_sel).count() > 0:
            break
        page.wait_for_timeout(120)
    else:
        raise RuntimeError(f"Combo {name!r}: dropdown did not open")

    # Click the row whose first cell text matches *code* (case-insensitive).
    # We use evaluate to get a precise text match — Playwright's :has-text is
    # substring-y and the cell text often has trailing whitespace.
    matched = page.evaluate(
        """([dropdownSel, rowSel, want]) => {
            const dropdowns = Array.from(document.querySelectorAll(dropdownSel))
                .filter(d => d.offsetParent !== null);
            if (!dropdowns.length) return { ok: false, reason: 'no dropdown' };
            const dropdown = dropdowns[dropdowns.length - 1];
            const rows = Array.from(dropdown.querySelectorAll(rowSel));
            const tried = [];
            for (const r of rows) {
                const firstCell = r.querySelector('.DropdownCell_module_cell__c3a0c414');
                const code = (firstCell?.innerText || '').trim();
                tried.push(code);
                if (code.toLowerCase() === want.toLowerCase()) {
                    r.click();
                    return { ok: true, clicked: code };
                }
            }
            return { ok: false, reason: 'no match', tried };
        }""",
        [dropdown_sel, row_sel, code],
    )
    if not matched.get("ok"):
        raise RuntimeError(
            f"Combo {name!r}: no row matched {code!r}. "
            f"Available codes: {matched.get('tried', [])}"
        )

    page.wait_for_timeout(_COMBO_SETTLE_MS)


def _set_simple_combo_by_text(page: Page, name: str, text: str) -> None:
    """Pick a row by its visible (single-cell) text in a React combo.

    Used for combos that don't have a Code / Name split — phone types,
    contact-prefs, etc.
    """
    current = _combo_current_value(page, name).strip()
    if current.casefold() == text.casefold():
        _log.debug("combo %s already = %r — no change", name, text)
        return

    page.locator(f'input[name="{name}"]').first.click(timeout=_COMBO_OPEN_TIMEOUT)
    page.wait_for_timeout(_COMBO_SETTLE_MS)

    dropdown_sel = '.ComboboxDropdown_module_dropdown__94e367dc'
    row_sel      = '.DropdownRow_module_row__4b41325c'

    deadline = time.time() + _COMBO_OPEN_TIMEOUT / 1000.0
    while time.time() < deadline:
        if page.locator(dropdown_sel).count() > 0:
            break
        page.wait_for_timeout(120)
    else:
        raise RuntimeError(f"Combo {name!r}: dropdown did not open")

    matched = page.evaluate(
        """([dropdownSel, rowSel, want]) => {
            const dropdowns = Array.from(document.querySelectorAll(dropdownSel))
                .filter(d => d.offsetParent !== null);
            if (!dropdowns.length) return { ok: false, reason: 'no dropdown' };
            const dropdown = dropdowns[dropdowns.length - 1];
            const rows = Array.from(dropdown.querySelectorAll(rowSel));
            const tried = [];
            for (const r of rows) {
                const txt = (r.innerText || '').trim();
                tried.push(txt);
                if (txt.toLowerCase() === want.toLowerCase()) {
                    r.click();
                    return { ok: true };
                }
            }
            return { ok: false, reason: 'no match', tried };
        }""",
        [dropdown_sel, row_sel, text],
    )
    if not matched.get("ok"):
        raise RuntimeError(
            f"Combo {name!r}: no row matched {text!r}. "
            f"Available: {matched.get('tried', [])}"
        )

    page.wait_for_timeout(_COMBO_SETTLE_MS)


def _fill_code_combo(page: Page, name: str, code: str) -> bool:
    """Fill an autocomplete-style code combo (NAICS, SIC, ...) by typing.

    Different from :func:`_set_react_combo` — those combos have a fixed
    short option list (Agency, Branch, etc.). NAICS / SIC use the same
    ``ComboboxBase`` widget but are backed by a long industry-code lookup,
    so the operator-supplied code has to filter the dropdown first.

    Behaviour:
      1. Click the input to focus + open the dropdown.
      2. Type *code* slowly via ``pressSequentially`` so React's filter
         fires per keystroke.
      3. Wait briefly for the dropdown to populate.
      4. Click the row whose first cell text equals *code* exactly (case
         insensitive). If no exact match but rows exist, click the first
         row — for code lookups the dropdown usually narrows to a single
         match by the time the full code is typed.
      5. If no dropdown rows appear at all, press Tab so React commits
         the typed value (matches the address-autocomplete fallback).

    Returns True when a dropdown row was clicked, False when we fell back
    to Tab-to-blur.
    """
    if not code:
        return True

    inp = page.locator(f'input[name="{name}"]').first
    inp.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(150)
    inp.press_sequentially(code, delay=40)

    dropdown_sel = '.ComboboxDropdown_module_dropdown__94e367dc'
    row_sel      = '.DropdownRow_module_row__4b41325c'

    deadline = time.time() + 2.5
    while time.time() < deadline:
        if page.locator(dropdown_sel).count() > 0:
            rows = page.locator(f'{dropdown_sel} {row_sel}')
            if rows.count() > 0:
                break
        page.wait_for_timeout(120)
    else:
        # No dropdown rendered — commit the typed value via Tab so React
        # blurs the field with whatever's in the input box.
        try:
            inp.press("Tab", timeout=1_000)
        except Exception:  # noqa: BLE001
            pass
        _log.info(
            "Code combo %s: no dropdown rows for %r — kept typed value",
            name, code,
        )
        return False

    # Pick the row matching the code exactly; fall back to first row.
    matched_idx = page.evaluate(
        """([dropdownSel, rowSel, want]) => {
            const drops = Array.from(document.querySelectorAll(dropdownSel))
                .filter(d => d.offsetParent !== null);
            if (!drops.length) return -1;
            const drop = drops[drops.length - 1];
            const rows = Array.from(drop.querySelectorAll(rowSel));
            for (let i = 0; i < rows.length; i++) {
                const firstCell = rows[i].querySelector(
                    '.DropdownCell_module_cell__c3a0c414'
                );
                const cellTxt = (firstCell?.innerText || '').trim();
                if (cellTxt.toLowerCase() === want.toLowerCase()) {
                    return i;
                }
            }
            return rows.length > 0 ? 0 : -1;
        }""",
        [dropdown_sel, row_sel, code],
    )
    if matched_idx < 0:
        try:
            inp.press("Tab", timeout=1_000)
        except Exception:  # noqa: BLE001
            pass
        return False

    try:
        page.locator(f'{dropdown_sel} {row_sel}').nth(matched_idx).click(
            timeout=_CLICK_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        _log.warning("Code combo %s: row click failed: %s", name, exc)
        return False
    page.wait_for_timeout(150)
    _log.debug("code combo %s = %r (picked dropdown row)", name, code)
    return True


def _fill_address(page: Page, street: str) -> bool:
    """Type *street* into the SmartyStreets autocomplete and pick the best match.

    Returns True when a suggestion was picked, False when none appeared (in
    which case the input keeps the literal text the operator typed — we
    explicitly blur the field so React commits the value to form state, and
    EPIC accepts the address as-is rather than rejecting on save).

    Strategy:
      1. Focus + clear via the native setter.
      2. ``pressSequentially`` the address slowly so the debounce-fed
         suggestion API fires.
      3. Wait briefly for ``.SuggestionFieldResults_module_result__d8e3be64``.
      4. If suggestions appeared: click the first **non-badged** result
         (badged = multi-suite building, which opens a sub-picker we don't
         want); suggestion-click auto-fills city / state / ZIP.
      5. If none appeared: press Tab so the field blurs and React commits
         the typed value. EPIC keeps the literal address; city / state / ZIP
         stay empty unless filled separately.
    """
    sel = 'input[name="clientAddress.address-streetLine"]'
    inp = page.locator(sel).first

    # Clear, then type slowly to trigger autocomplete.
    inp.click(timeout=_CLICK_TIMEOUT)
    page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return;
            const setter = Object.getOwnPropertyDescriptor(
                window.HTMLInputElement.prototype, 'value').set;
            setter.call(el, '');
            el.dispatchEvent(new Event('input', {bubbles: true}));
        }""",
        sel,
    )
    inp.press_sequentially(street, delay=40)

    # Poll for suggestions.
    suggest_sel = '.SuggestionFieldResults_module_result__d8e3be64'
    deadline = time.time() + _ADDRESS_SUGGEST_MS / 1000.0
    while time.time() < deadline:
        if page.locator(suggest_sel).count() > 0:
            break
        page.wait_for_timeout(150)
    else:
        # No suggestion — commit the typed value by blurring so React's
        # onBlur handler fires and the field keeps the literal text.
        try:
            inp.press("Tab", timeout=1_000)
        except Exception:  # noqa: BLE001
            pass
        _log.info(
            "Address autocomplete returned no suggestions for %r — "
            "keeping typed value as-is", street,
        )
        return False

    # Click the first non-badged suggestion (badged = multi-suite expansion).
    clicked = page.evaluate(
        """(resultSel) => {
            const results = Array.from(document.querySelectorAll(resultSel))
                .filter(r => r.offsetParent !== null);
            if (!results.length) return { ok: false };
            // Prefer non-badged (street-level) results.
            const nonBadged = results.filter(r => !r.className.includes('badgeExists'));
            const target = nonBadged.length > 0 ? nonBadged[0] : results[0];
            target.click();
            return { ok: true, text: (target.innerText || '').trim() };
        }""",
        suggest_sel,
    )
    if not clicked.get("ok"):
        return False

    _log.info("Address picked: %s", clicked.get("text"))
    page.wait_for_timeout(_FINAL_SETTLE_MS)
    return True


_DUP_PANEL_SEL = '[data-test="top4Duplicates"]'
_DUP_DISMISS_SEL = 'button[data-test="btnDismissDAC"]'


def _inline_dup_panel_text(page: Page) -> str:
    """If the inline 'Possible Duplicates' panel is visible, return its text.

    EPIC fires this panel **before** Save once the form has enough data
    (typically address + phone match an existing account) — it lives inline
    in the form between the input fields and the Save button (NOT a modal
    overlay), so it can scroll off-screen.

    Returns ``""`` when the panel isn't shown.
    """
    try:
        loc = page.locator(_DUP_PANEL_SEL)
        if loc.count() == 0 or not loc.first.is_visible():
            return ""
        return (loc.first.inner_text(timeout=1_000) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _dismiss_inline_dup_panel(page: Page) -> bool:
    """Click the panel's Dismiss button so we can proceed to Save."""
    try:
        page.locator(_DUP_DISMISS_SEL).first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(400)
    except Exception as exc:  # noqa: BLE001
        _log.warning("Could not dismiss inline dup panel: %s", exc)
        return False
    return True


def _handle_inline_dup_panel(
    page: Page,
    account_name: str,
    panel_text: str,
) -> str:
    """Surface the inline dup panel to the operator via the dedicated callback.

    The operator dismisses (or actions) the EPIC panel themselves in the
    browser — our popup is the "ready to continue?" gate. Returns
    ``"proceed"`` if the operator clicks OK (panel dismissed, ready to
    Save) or ``"cancel"`` if they backed out.
    """
    rt = runtime.get_runtime()
    if rt is None:
        # Test-script context (no GUI wired) — default to cancel so iterating
        # against a real duplicate doesn't create unwanted records.
        _log.warning(
            "Inline dup panel showed for %r but no runtime registered — "
            "defaulting to cancel. Panel text:\n%s",
            account_name, panel_text,
        )
        return "cancel"

    try:
        return rt.on_inline_dup_prompt(account_name, panel_text)
    except EntryCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.error("on_inline_dup_prompt callback failed: %s", exc)
        return "cancel"


def _click_save(page: Page) -> None:
    """Click 'Save Account'. Caller handles whatever appears next."""
    save = page.locator('button[type="submit"]').filter(has_text="Save Account").first
    save.wait_for(state="visible", timeout=_CLICK_TIMEOUT)
    save.click(timeout=_CLICK_TIMEOUT)
    _log.info("Clicked Save Account")


def _wait_for_save_outcome(page: Page, account_name: str) -> AccountCreateResult:
    """Watch for save success, EPIC's dup popup, or a validation failure.

    Outcomes:
      - Form unmounts (title changes away from "Add Client Account") →
        CREATED. We then poll the title for up to *_LOOKUP_CODE_SETTLE_MS*
        looking for the canonical ``<CODE> - <Name>`` shape so we can
        capture the new lookup code. Falling out of that poll without a
        code is still CREATED — just with ``lookup_code=None``.
      - A modal/dialog appears whose text mentions "duplicate" or
        "potential match" → halt for operator. If they Proceed, click any
        visible Continue / Save / Yes button and re-watch. If Cancel,
        back out.
      - Form remains mounted past the timeout with no dialog → check for
        visible validation banners. Otherwise return ERROR.
    """
    deadline = time.time() + _POST_SAVE_WAIT_MS / 1000.0

    while time.time() < deadline:
        if not _is_on_add_form(page):
            new_title = (page.title() or "").strip()
            _log.info(
                "Account saved — first post-form title: %r (polling for lookup code...)",
                new_title,
            )
            lookup_code = _wait_for_lookup_code_in_title(page)
            final_title = (page.title() or "").strip()
            if lookup_code:
                _log.info(
                    "Captured lookup code %r from title %r",
                    lookup_code, final_title,
                )
            else:
                _log.warning(
                    "Save completed (title=%r) but no lookup code could be "
                    "extracted within %d ms — returning CREATED with empty code",
                    final_title, _LOOKUP_CODE_SETTLE_MS,
                )
            return AccountCreateResult(
                outcome=AccountCreateOutcome.CREATED,
                account_name=account_name,
                lookup_code=lookup_code,
            )

        # Look for a modal/dialog overlay.
        dialog_text = _probe_visible_dialog(page)
        if dialog_text:
            _log.info("Save dialog appeared: %r", dialog_text[:200])
            return _handle_save_dialog(page, account_name, dialog_text)

        page.wait_for_timeout(200)

    # Timed out without unmounting and without a dialog — probably a
    # validation banner is up.  Capture anything visible for the operator.
    banners = _collect_validation_messages(page)
    if banners:
        return AccountCreateResult(
            outcome=AccountCreateOutcome.VALIDATION_FAILED,
            account_name=account_name,
            error=" | ".join(banners),
        )

    return AccountCreateResult(
        outcome=AccountCreateOutcome.ERROR,
        account_name=account_name,
        error="Save did not complete and no dialog or validation message appeared",
    )


def _probe_visible_dialog(page: Page) -> str:
    """Return the text of any visible dialog/modal, or ``''`` if none."""
    try:
        return page.evaluate(
            """() => {
                const sels = ['[role="dialog"]', '[class*="Dialog"]', '[class*="Modal"]'];
                for (const sel of sels) {
                    const els = Array.from(document.querySelectorAll(sel))
                        .filter(el => el.offsetParent !== null);
                    if (els.length) return (els[els.length-1].innerText || '').trim();
                }
                return '';
            }"""
        ) or ""
    except Exception:  # noqa: BLE001
        return ""


def _handle_save_dialog(
    page: Page,
    account_name: str,
    dialog_text: str,
) -> AccountCreateResult:
    """Halt for operator on EPIC's duplicate / confirmation popup.

    Uses :attr:`runtime.EntryRuntime.on_validation_halt` so the GUI can show
    its own modal with a screenshot, defaulting to operator-controlled
    Proceed / Cancel.

    *dialog_text* — the visible text of the EPIC modal (so the runtime
    callback can pass it to the operator).
    """
    rt = runtime.get_runtime()
    choice: str = "cancel"
    if rt is not None:
        # Capture a screenshot to ship to the operator's halt dialog.
        screenshot_path = None
        try:
            screenshot_path = rt.artifacts_dir / "account_create_dup_popup.png"
            page.screenshot(path=str(screenshot_path), full_page=False)
        except Exception:  # noqa: BLE001
            screenshot_path = None

        finding = runtime.ValidationFinding(
            error_text=dialog_text,
            surface="portal_modal",
            screen_code="ADD-ACCOUNT",
            expecting=(
                "Save new account "
                f"{account_name!r} — EPIC surfaced a duplicate-check popup"
            ),
            screenshot_path=screenshot_path,
            sequence=len(rt.findings) + 1,
        )
        rt.findings.append(finding)
        try:
            choice = rt.on_validation_halt(finding)
        except EntryCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.error("on_validation_halt callback failed: %s", exc)
            choice = "cancel"
    else:
        # No GUI runtime — log + default to cancel so we don't accidentally
        # create duplicates in standalone test runs.
        _log.warning(
            "No runtime registered; defaulting to CANCEL on EPIC dup popup "
            "for account %r", account_name,
        )
        choice = "cancel"

    if choice == "proceed":
        return _click_dialog_continue(page, account_name)
    return _click_dialog_cancel(page, account_name)


def _click_dialog_continue(page: Page, account_name: str) -> AccountCreateResult:
    """Click a Continue / Save / Yes / OK button on the open dialog and re-watch."""
    clicked = page.evaluate(
        """() => {
            const labels = ['continue', 'save anyway', 'save', 'yes', 'ok', 'proceed'];
            const dialogSels = ['[role="dialog"]', '[class*="Dialog"]', '[class*="Modal"]'];
            for (const ds of dialogSels) {
                const dialogs = Array.from(document.querySelectorAll(ds))
                    .filter(d => d.offsetParent !== null);
                for (const d of dialogs.slice().reverse()) {
                    const btns = Array.from(d.querySelectorAll('button'))
                        .filter(b => b.offsetParent !== null);
                    for (const want of labels) {
                        const hit = btns.find(b => (b.innerText || '').trim().toLowerCase() === want);
                        if (hit) { hit.click(); return { ok: true, label: want }; }
                    }
                }
            }
            return { ok: false };
        }"""
    )
    if not clicked.get("ok"):
        return AccountCreateResult(
            outcome=AccountCreateOutcome.ERROR,
            account_name=account_name,
            error=(
                "Operator chose Proceed but no Continue/Save/Yes/OK button "
                "could be found on the dialog."
            ),
        )

    _log.info("Operator chose Proceed; clicked dialog button %r", clicked.get("label"))
    # Re-watch — could navigate to success, raise another dialog, or fail.
    return _wait_for_save_outcome(page, account_name)


def _click_dialog_cancel(page: Page, account_name: str) -> AccountCreateResult:
    """Operator chose Cancel — dismiss the dialog and back out of the form."""
    # First dismiss the dialog if it has a Cancel/No button.
    page.evaluate(
        """() => {
            const labels = ['cancel', 'no', 'close'];
            const dialogSels = ['[role="dialog"]', '[class*="Dialog"]', '[class*="Modal"]'];
            for (const ds of dialogSels) {
                const dialogs = Array.from(document.querySelectorAll(ds))
                    .filter(d => d.offsetParent !== null);
                for (const d of dialogs.slice().reverse()) {
                    const btns = Array.from(d.querySelectorAll('button'))
                        .filter(b => b.offsetParent !== null);
                    for (const want of labels) {
                        const hit = btns.find(b => (b.innerText || '').trim().toLowerCase() === want);
                        if (hit) { hit.click(); return; }
                    }
                }
            }
        }"""
    )
    page.wait_for_timeout(500)

    # Then back out of the form: Cancel → confirm Discard.
    try:
        _cancel_form(page)
    except Exception as exc:  # noqa: BLE001
        _log.error("Cancel/Discard after dup popup failed: %s", exc)

    return AccountCreateResult(
        outcome=AccountCreateOutcome.DUP_CANCELLED,
        account_name=account_name,
    )


def _cancel_form(page: Page) -> None:
    """Click Cancel on the form and confirm the Discard-changes dialog."""
    cancel = page.locator('button[type="button"]').filter(has_text="Cancel").first
    cancel.wait_for(state="visible", timeout=_CLICK_TIMEOUT)
    cancel.click(timeout=_CLICK_TIMEOUT)

    # Confirm Discard.
    page.wait_for_timeout(500)
    yes = page.locator('button').filter(has_text="Yes").first
    try:
        yes.wait_for(state="visible", timeout=2_000)
        yes.click(timeout=_CLICK_TIMEOUT)
    except PlaywrightTimeout:
        # No discard dialog (clean form) — fine.
        pass


def _collect_validation_messages(page: Page) -> list[str]:
    """Return any visible validation/error message text on the form."""
    try:
        return page.evaluate(
            """() => {
                const sels = [
                    '[class*="Error"]',
                    '[class*="error-message"]',
                    '[role="alert"]',
                    '.has-error',
                ];
                const out = new Set();
                for (const sel of sels) {
                    const els = Array.from(document.querySelectorAll(sel))
                        .filter(el => el.offsetParent !== null);
                    for (const el of els) {
                        const txt = (el.innerText || '').trim();
                        if (txt && txt.length < 200) out.add(txt);
                    }
                }
                return Array.from(out);
            }"""
        ) or []
    except Exception:  # noqa: BLE001
        return []


def _extract_lookup_code_from_title(title: str) -> str | None:
    """Pull a likely lookup code out of the post-save page title.

    EPIC sets the per-account title to ``"<CODE> - <Name>"`` (matching what
    we saw with ``GORBILL-01 - Bill Goran Widgets LLC``). Returns None when
    the title doesn't follow that shape.
    """
    if not title or " - " not in title:
        return None
    head = title.split(" - ", 1)[0].strip()
    # Lookup codes are short, no spaces, alphanumeric + dashes. Reject the
    # transient "Add Client Account" / "Loading..." / "Account Locate" titles.
    if not head or len(head) > 30:
        return None
    if not all(c.isalnum() or c in "-_" for c in head):
        return None
    # Belt-and-braces: anything that's obviously a screen name, not a code.
    if head.lower() in {"add client account", "account locate", "home", "loading"}:
        return None
    return head


def _wait_for_lookup_code_in_title(page: Page) -> str | None:
    """After save, poll the page title for up to *_LOOKUP_CODE_SETTLE_MS*
    until it matches the ``"<CODE> - <Name>"`` shape, then return the code.

    EPIC may show transient titles ("Loading...", briefly the form name, or
    just the bare account name) before settling on the canonical
    ``<lookup_code> - <name>`` format. Grabbing the title the instant the
    form unmounts often catches one of those transients.

    Returns ``None`` when no valid code appears before the timeout.
    """
    deadline = time.time() + _LOOKUP_CODE_SETTLE_MS / 1000.0
    last_title = ""
    while time.time() < deadline:
        try:
            title = (page.title() or "").strip()
        except Exception:  # noqa: BLE001
            title = ""
        if title and title != last_title:
            _log.debug("Post-save title poll: %r", title)
            last_title = title
        code = _extract_lookup_code_from_title(title)
        if code:
            return code
        page.wait_for_timeout(200)
    return None


# ── FEIN on Main Business Contact ──────────────────────────────────────────────

# The Identification-Numbers "Type" combo on a business contact's Business tab.
# Row text confirmed via live CDP 2026-05-27.
_FEIN_TYPE_LABEL = "FEIN - Federal Employer Identification Number"


def _normalize_fein(fein: str) -> str:
    """Reduce a FEIN to the bare 9 digits EPIC's Identification Numbers field wants.

    The Client page stores it masked ("12-3456789"); EPIC's value input
    rejects the dash, so strip every non-digit before entry.
    """
    return "".join(ch for ch in (fein or "") if ch.isdigit())


def _click_sidebar_button(page: Page, label: str) -> bool:
    """Click a top-level account sidebar button by its exact visible text.

    The numeric ``sidebar-button-{n}`` ids are not stable across accounts,
    so we match on the trimmed label ("Contacts", "Account Detail", ...).
    """
    return bool(page.evaluate(
        """(label) => {
            const btns = Array.from(document.querySelectorAll('[data-automation-id^="sidebar-button-"]'));
            const b = btns.find(x => (x.textContent || '').trim() === label);
            if (!b) return false;
            b.click();
            return true;
        }""",
        label,
    ))


def _wait_present(page: Page, selector: str, timeout_ms: int) -> bool:
    """Poll until *selector* matches at least one element. Returns True/False."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)
    return False


def _wait_js_true(page: Page, js_fn: str, timeout_ms: int, poll_ms: int = 200) -> bool:
    """Poll a JS predicate ``() => boolean`` until it returns true or timeout."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if bool(page.evaluate(js_fn)):
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(poll_ms)
    return False


def _install_dom_observer(page: Page) -> None:
    """Install a one-shot MutationObserver that timestamps the last DOM change.

    Persisted on ``window.__igaLastMut`` so :func:`_wait_dom_quiet` can tell
    how long the page has been still. Installed once per page; cheap no-op
    on subsequent calls.
    """
    try:
        page.evaluate(
            """() => {
                if (window.__igaMo) return;
                window.__igaLastMut = Date.now();
                const mo = new MutationObserver(() => { window.__igaLastMut = Date.now(); });
                mo.observe(document.documentElement, {
                    childList: true, subtree: true, attributes: true, characterData: true,
                });
                window.__igaMo = mo;
            }"""
        )
    except Exception:  # noqa: BLE001
        pass


def _wait_dom_quiet(
    page: Page,
    *,
    quiet_ms: int = 600,
    timeout_ms: int = 6_000,
    poll_ms: int = 100,
) -> bool:
    """Wait until the DOM has stopped mutating for *quiet_ms*.

    This is the "polling finished" signal for EPIC's CRM React screens —
    spinner/aria-busy markers aren't reliable here, but the DOM going
    still *after the expected element is already present* is. Always pair
    this with a positive element/status check FIRST (e.g. wait for the
    grid rows or target field): on its own, DOM-quiet can fire during the
    network gap *before* content renders (observed: quiet at ~1.35 s, grid
    not ready until ~1.63 s).

    Returns True once quiet is reached, False on timeout (caller proceeds
    anyway — the element check is the real gate).
    """
    _install_dom_observer(page)
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            idle = int(page.evaluate("() => Date.now() - (window.__igaLastMut || 0)"))
        except Exception:  # noqa: BLE001
            return False
        if idle >= quiet_ms:
            return True
        page.wait_for_timeout(poll_ms)
    return False


def _settle_after_click(
    page: Page,
    *,
    quiet_ms: int = 600,
    timeout_ms: int = 6_000,
    extra_ms: int = 500,
) -> None:
    """Wait for the DOM to go quiet, then an additional fixed settle.

    Call AFTER a click and AFTER confirming the click's expected target
    element is present — see :func:`_wait_dom_quiet`.
    """
    _wait_dom_quiet(page, quiet_ms=quiet_ms, timeout_ms=timeout_ms)
    page.wait_for_timeout(extra_ms)


# Status bar shows the active screen code; the contact detail is "crm-contact-detail".
_CONTACT_DETAIL_STATUS_JS = (
    "() => (document.querySelector('[class*=\"statusBar\"],[class*=\"status-bar\"]')"
    "?.textContent || '').includes('crm-contact-detail')"
)
# The contact-detail tabs (Preferences / Business / ...) — presence of a visible
# "Business" tab is our signal the CRM React tree has actually hydrated (vs the
# blank-crash state, where the detail body never renders).
_BUSINESS_TAB_JS = (
    "() => Array.from(document.querySelectorAll('div,button,[role=\"tab\"]'))"
    ".some(e => (e.textContent||'').trim() === 'Business' && e.getBoundingClientRect().width > 0)"
)


def _click_text_button(page: Page, text: str) -> bool:
    """Click the first visible ``<button>``/role=button with exact *text*."""
    return bool(page.evaluate(
        """(text) => {
            const b = Array.from(document.querySelectorAll('button,[role="button"],.button'))
                .find(x => (x.textContent || '').trim() === text && x.getBoundingClientRect().width > 0);
            if (!b) return false;
            b.click();
            return true;
        }""",
        text,
    ))


def _discard_dialog_present(page: Page) -> bool:
    """True when EPIC's "Do you wish to discard changes?" dialog is showing.

    This appears when a contact is closed while the edit form still has
    uncommitted changes (i.e. the Save didn't take). Answering "Yes"
    DISCARDS the edits — so the automation must never click it blindly.
    """
    try:
        return bool(page.evaluate(
            """() => Array.from(document.querySelectorAll('*'))
                .some(e => (e.textContent || '').trim() === 'Do you wish to discard changes?'
                           && e.getBoundingClientRect().width > 0)"""
        ))
    except Exception:  # noqa: BLE001
        return False


def _save_and_close_contact(page: Page) -> bool:
    """Save the contact, then close it — guarding against the discard dialog.

    EPIC keeps the contact detail in edit mode; the X-close prompts
    "Do you wish to discard changes?" whenever anything is still
    uncommitted. So we Save first (generous settle for the commit), then
    close. If the discard dialog still appears, the Save didn't take —
    answer **No** (never Yes; that drops the FEIN), re-Save, and retry the
    close once. Returns True only on a clean close.
    """
    if not _click_text_button(page, "Save Contact"):
        _log.warning("FEIN: Save Contact button not found")
        return False
    # Let the save commit: wait for the DOM to go quiet, then a generous settle.
    _settle_after_click(page, quiet_ms=800, timeout_ms=8_000, extra_ms=1_000)

    for attempt in (1, 2):
        _close_open_contact(page)
        _settle_after_click(page, extra_ms=600)
        if not _discard_dialog_present(page):
            _log.debug("FEIN: contact closed cleanly (attempt %d)", attempt)
            return True
        # Unsaved changes remained — keep our data (No), re-save, retry close.
        _log.warning(
            "FEIN: 'discard changes?' dialog on close (attempt %d) — "
            "answering No and re-saving", attempt,
        )
        _click_text_button(page, "No")
        page.wait_for_timeout(1_000)
        _click_text_button(page, "Save Contact")
        page.wait_for_timeout(2_000)

    # Still dirty — dismiss the dialog with No so we don't discard, and bail.
    if _discard_dialog_present(page):
        _click_text_button(page, "No")
    _log.warning("FEIN: could not confirm a clean save+close of the contact")
    return False


def _close_open_contact(page: Page) -> bool:
    """Close the open contact tab in the sidebar.

    EPIC requires an open contact to be CLOSED after saving — navigating
    away while it's still open leaves it pinned in the left rail and can
    mis-render the next screen. The open contact shows as a level-2
    sidebar entry (``sidebar-button-Contact.Business.Detail|...``) with a
    ``.close-button`` (the X) beside it in the rail.
    """
    return bool(page.evaluate(
        """() => {
            const entry = document.querySelector('[data-automation-id^="sidebar-button-Contact.Business.Detail"]');
            let closeBtn = null;
            if (entry) {
                const container = entry.closest('li, [class*="row"], [class*="item"]') || entry.parentElement;
                closeBtn = container && container.querySelector('.close-button');
            }
            if (!closeBtn) {
                // Fallback: the nearest close-button in the left rail.
                closeBtn = Array.from(document.querySelectorAll('.close-button'))
                    .find(b => { const r = b.getBoundingClientRect(); return r.width > 0 && r.x < 260; });
            }
            if (!closeBtn) return false;
            closeBtn.click();
            return true;
        }"""
    ))


def _select_fein_type(page: Page) -> bool:
    """Open the row-0 Identification-Numbers Type combo and pick the FEIN row.

    React ComboboxBase: click the input to open, then click the
    ``[data-test="dropdown-row"]`` whose text starts with "FEIN".
    """
    combo_sel = '[name="bIdNumbers.[0].description"]'
    # Open via the element's own click (the input sits under a wrapper that
    # Playwright's click can miss — evaluate-click matches what opened the
    # dropdown during inspection).
    opened = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
            el.click();
            return true;
        }""",
        combo_sel,
    )
    if not opened:
        return False
    page.wait_for_timeout(_COMBO_SETTLE_MS)

    deadline = time.time() + _COMBO_OPEN_TIMEOUT / 1000.0
    while time.time() < deadline:
        clicked = page.evaluate(
            """() => {
                const rows = Array.from(document.querySelectorAll('[data-test="dropdown-row"]'))
                    .filter(r => r.offsetParent !== null);
                const want = rows.find(r => /^FEIN\\b/i.test((r.textContent || '').trim()));
                if (want) { want.click(); return true; }
                return false;
            }"""
        )
        if clicked:
            page.wait_for_timeout(_COMBO_SETTLE_MS)
            return True
        page.wait_for_timeout(120)
    return False


def _select_business_type(page: Page, value: str) -> bool:
    """Select *value* in the Business-tab "Business Type" combo (businessDetail.type).

    Long (43-row), virtualized React combo. We open it, type the value to
    filter, then poll for an exact (then prefix) matching ``dropdown-row``.
    If typing didn't filter (some EPIC combos are click-only), we fall back
    to scrolling the virtualized list to bring the matching row into view.
    *value* is expected to be one of EPIC's exact labels (the GUI dropdown
    enforces this), so an exact match is the normal path.

    Returns True on a successful pick, False otherwise. Blank value is a
    no-op success (nothing to set).
    """
    value = (value or "").strip()
    if not value:
        return True

    combo_sel = '[name="businessDetail.type"]'
    if not _wait_present(page, combo_sel, 8_000):
        _log.warning("FEIN/BizType: business-type combo not present")
        return False

    opened = page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return false;
            el.scrollIntoView({block: 'center'});
            el.click();
            return true;
        }""",
        combo_sel,
    )
    if not opened:
        return False
    page.wait_for_timeout(_COMBO_SETTLE_MS)

    # Type the value to filter the list (best-effort — harmless if the combo
    # is click-only and ignores typing).
    try:
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.keyboard.type(value, delay=20)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(_COMBO_SETTLE_MS)

    _click_row_js = """(want) => {
        const rows = Array.from(document.querySelectorAll(
            '[data-test="dropdown-row"], .DropdownRow_module_row__4b41325c'
        )).filter(r => r.offsetParent !== null);
        const w = want.trim().toLowerCase();
        let row = rows.find(r => (r.textContent || '').trim().toLowerCase() === w);
        if (!row) row = rows.find(r => (r.textContent || '').trim().toLowerCase().startsWith(w));
        if (!row) return false;
        row.click();
        return true;
    }"""

    # Poll for the matching row (handles filter-on-type).
    deadline = time.time() + _COMBO_OPEN_TIMEOUT / 1000.0
    while time.time() < deadline:
        if bool(page.evaluate(_click_row_js, value)):
            page.wait_for_timeout(_COMBO_SETTLE_MS)
            return True
        page.wait_for_timeout(120)

    # Fallback: typing didn't surface the row — scroll the virtualized list.
    scrolled_hit = page.evaluate(
        """(want) => {
            const rows = Array.from(document.querySelectorAll(
                '[data-test="dropdown-row"], .DropdownRow_module_row__4b41325c'
            )).filter(r => r.offsetParent !== null);
            if (!rows.length) return false;
            let sc = rows[0].parentElement;
            for (let i = 0; i < 8 && sc; i++) { if (sc.scrollHeight > sc.clientHeight + 4) break; sc = sc.parentElement; }
            if (!sc) return false;
            const w = want.trim().toLowerCase();
            const step = Math.max(30, Math.floor(sc.clientHeight / 3));
            for (let y = 0; y <= sc.scrollHeight; y += step) {
                sc.scrollTop = y;
                const cur = Array.from(document.querySelectorAll(
                    '[data-test="dropdown-row"], .DropdownRow_module_row__4b41325c'
                )).filter(r => r.offsetParent !== null);
                const hit = cur.find(r => (r.textContent || '').trim().toLowerCase() === w);
                if (hit) { hit.click(); return true; }
            }
            return false;
        }""",
        value,
    )
    if scrolled_hit:
        page.wait_for_timeout(_COMBO_SETTLE_MS)
        return True
    _log.warning("FEIN/BizType: no dropdown row matched %r", value)
    return False


def _add_fein_to_contact(
    page: Page, account_name: str, fein: str, business_type: str = "",
) -> bool:
    """Write the insured's FEIN (and Business Type) onto the Main Business Contact.

    Path (confirmed via live CDP 2026-05-27):
      Contacts (sidebar) → double-click the Main Business Contact row →
      Business tab → Identification Numbers: Type = "FEIN - Federal
      Employer Identification Number", value = the FEIN → (Business
      Details) Business Type = *business_type* → Save Contact → close.

    Both *fein* and *business_type* are optional; whichever is supplied is
    entered. Best-effort: the account is already saved before this runs.
    Returns True on success, False on any non-fatal failure (the caller
    keeps the CREATED outcome regardless).
    """
    fein_digits = _normalize_fein(fein)
    business_type = (business_type or "").strip()
    if not fein_digits and not business_type:
        _log.info("FEIN/BizType both empty — skipping contact enrichment")
        return False
    _log.info(
        "Enriching Main Business Contact — FEIN raw %r → digits %r, business_type %r",
        fein, fein_digits, business_type or "(none)",
    )

    # Install the DOM-mutation observer up front so every click below can be
    # followed by "wait for target element, then wait for the DOM to go
    # quiet, then settle" — EPIC's CRM screens have no reliable spinner, but
    # they DO stop mutating once a click finishes rendering.
    _install_dom_observer(page)

    # 1. Open Contacts. Wait for the grid rows, then settle — EPIC's CRM
    # web component crashes (blank render) if poked before it hydrates.
    if not _click_sidebar_button(page, "Contacts"):
        _log.warning("FEIN: could not find the Contacts sidebar button")
        return False
    if not _wait_present(page, '[data-automation-id^="vlvwContacts body-row item-"]', 10_000):
        _log.warning("FEIN: Contacts grid did not populate")
        return False
    _settle_after_click(page)  # grid quiet + settle

    # 2-4. Open the Main Business Contact and reach its Business tab.
    # Double-clicking the row (rather than single-select + the Edit/View
    # pencil) is the more stable way in/out of a contact — confirmed by the
    # operator 2026-05-27. EPIC's CRM contact-detail still intermittently
    # blank-renders (a React crash in its own bundle); when the body fails
    # to render we close the blank contact and retry the open.
    _ROW_SEL = '[data-automation-id^="vlvwContacts body-row item-"]'
    business_ready = False
    for attempt in range(1, 4):
        if not _wait_present(page, _ROW_SEL, 8_000):
            _log.warning("FEIN: contacts grid not present (attempt %d)", attempt)
            break
        target_aid = page.evaluate(
            """(name) => {
                const rows = Array.from(document.querySelectorAll('[data-automation-id^="vlvwContacts body-row item-"]'));
                if (!rows.length) return null;
                const want = (name || '').trim().toLowerCase();
                let row = rows.find(r => (r.textContent || '').trim().toLowerCase().startsWith(want));
                if (!row) row = rows.find(r => (r.textContent || '').toLowerCase().includes(want));
                if (!row) row = rows[0];
                return row.getAttribute('data-automation-id');
            }""",
            account_name,
        )
        if not target_aid:
            _log.warning("FEIN: no contact rows to open")
            return False
        try:
            page.locator(f'[data-automation-id="{target_aid}"]').first.dblclick(timeout=_CLICK_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            _log.warning("FEIN: double-click attempt %d failed: %s", attempt, exc)
            _settle_after_click(page, extra_ms=800)
            continue
        # Wait for the contact-detail screen to come up (status marker), then
        # DOM-quiet + generous settle so EPIC's CRM React tree fully hydrates.
        if not _wait_js_true(page, _CONTACT_DETAIL_STATUS_JS, 15_000):
            _log.warning("FEIN: contact detail did not open (attempt %d)", attempt)
            _close_open_contact(page)
            _settle_after_click(page, extra_ms=800)
            continue
        _settle_after_click(page, quiet_ms=800, timeout_ms=8_000, extra_ms=800)
        # Business tab present == body rendered (not the blank-crash state).
        if _wait_js_true(page, _BUSINESS_TAB_JS, 8_000):
            business_ready = True
            break
        _log.warning(
            "FEIN: contact detail blank-rendered (attempt %d) — closing and retrying",
            attempt,
        )
        _close_open_contact(page)
        _settle_after_click(page, extra_ms=800)
    if not business_ready:
        _log.warning("FEIN: Business tab never rendered after retries — contact detail blank")
        return False

    # Click the Business tab, wait for the Identification-Numbers field to
    # mount, then DOM-quiet + settle.
    page.evaluate(
        """() => {
            const t = Array.from(document.querySelectorAll('div,button,[role="tab"]'))
                .find(e => (e.textContent || '').trim() === 'Business' && e.getBoundingClientRect().width > 0);
            if (t) t.click();
        }"""
    )
    if not _wait_present(page, '[name="bIdNumbers.[0].description"]', 10_000):
        _log.warning("FEIN: Business tab Identification Numbers did not render")
        return False
    _settle_after_click(page)

    # 5-6. FEIN (Identification Numbers) — only when supplied.
    if fein_digits:
        # Type = FEIN, then let the combo selection settle.
        if not _select_fein_type(page):
            _log.warning("FEIN: could not select the FEIN type in the combo")
            return False
        _settle_after_click(page, extra_ms=300)
        # Value (digits only — EPIC rejects the masked dash form).
        _fill_text(page, "bIdNumbers.[0].value", fein_digits)
        _settle_after_click(page, extra_ms=300)

    # 6b. Business Type (Business Details) — only when supplied. Same tab,
    # below Identification Numbers; the helper handles its own combo
    # open/poll, and we settle afterward.
    if business_type:
        if not _select_business_type(page, business_type):
            _log.warning("BizType: could not select %r — leaving blank", business_type)
        else:
            _settle_after_click(page, extra_ms=300)

    # 7. Save the contact, then close it. EPIC requires the open contact to
    # be closed after saving (you can't just navigate away). The guarded
    # helper saves first, then closes, handling the "discard changes?"
    # dialog if the save didn't commit (answers No + re-saves, never
    # discards the data).
    if not _save_and_close_contact(page):
        # Save/close couldn't be confirmed clean. The account is already
        # created, so this is non-fatal — report False (best-effort).
        return False

    _log.info(
        "Main Business Contact enriched (FEIN=%s, business_type=%r)",
        fein_digits or "(none)", business_type or "(none)",
    )
    return True


# ── Public API ───────────────────────────────────────────────────────────────

def run(page: Page, *, setup: AccountSetup) -> AccountCreateResult:
    """Create a new EPIC client account from *setup*.

    Pre-condition: the browser is on the Account Locate screen. The caller is
    expected to have already run a name-based dup-check via
    :mod:`step_account_dup_check`; this step does not re-check.

    The returned :class:`AccountCreateResult` carries the outcome and, on
    ``CREATED``, the lookup code EPIC assigned (extracted from the post-save
    page title — best effort).
    """
    checkpoint(page, f"Account: create {setup.account_name!r}")

    # 0. Pre-condition: must be on Account Locate.
    try:
        title_ok = (page.title() or "").strip().lower() == "account locate"
        marker_ok = page.locator('[data-automation-id="AccountLocate"]').count() > 0
        if not (title_ok or marker_ok):
            return AccountCreateResult(
                outcome=AccountCreateOutcome.ERROR,
                account_name=setup.account_name,
                error=(
                    "Account Locate screen is not open. "
                    "Run step_account_lookup_nav first."
                ),
            )
    except EntryCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        return AccountCreateResult(
            outcome=AccountCreateOutcome.ERROR,
            account_name=setup.account_name,
            error=f"Could not verify Account Locate state: {exc}",
        )

    # 1. Open the Add Account form.
    try:
        _open_add_form(page)
    except EntryCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        return AccountCreateResult(
            outcome=AccountCreateOutcome.ERROR,
            account_name=setup.account_name,
            error=f"Could not open Add Account form: {exc}",
        )

    try:
        # 2. Required radios.
        _set_radio(page, "clientInfo.clientFormat", setup.client_format)
        _set_radio(page, "accountInfo.clientType",  setup.client_type)

        # 3. Account name + optional source.
        _fill_text(page, "accountInfo.accountName",    setup.account_name)
        _fill_text(page, "accountInfo.accountSource",  setup.account_source)

        # 4. Structure: Agency then Branch (Branch options depend on Agency).
        _set_react_combo(
            page, "structure.agency", setup.agency,
            display_name=AGENCY_NAMES.get(setup.agency),
        )
        _set_react_combo(
            page, "structure.branch", setup.branch,
            display_name=BRANCH_NAMES.get(setup.branch),
        )

        # 5. Lines of business checkboxes — opt-in only, never untick existing.
        for lob in setup.lines_of_business:
            _set_checkbox(page, lob, True)

        # 6. Address (autocomplete) + manual line 2 / siteId override.
        if setup.street_address:
            _fill_address(page, setup.street_address)
        if setup.address_2:
            _fill_text(page, "clientAddress.address-address2", setup.address_2)

        # 7. Business phone / email / website / identification numbers.
        _set_simple_combo_by_text(page, "businessPhone.[0].types", setup.business_phone_type)
        _fill_text(page, "businessPhone.businessPhone.[0].number__phone", setup.business_phone)
        _fill_text(page, "businessEmailWebsite.emailAddress",  setup.business_email)
        _fill_text(page, "businessEmailWebsite.website",       setup.business_website)
        if setup.naics:
            _fill_code_combo(page, "identificationNumbers.naics", setup.naics)
        if setup.sic:
            _fill_code_combo(page, "identificationNumbers.sic",   setup.sic)

        # 8. Primary contact.
        _fill_text(page, "primaryContact.firstName", setup.primary_first_name)
        _fill_text(page, "primaryContact.lastName",  setup.primary_last_name)
        _set_simple_combo_by_text(page, "primaryPhone.[0].types", setup.primary_phone_type)
        _fill_text(page, "primaryPhone.primaryPhone.[0].number__phone", setup.primary_phone)
        _fill_text(page, "primaryContactEmail.emailAddress", setup.primary_email)

        # 9. Contact preferences.
        _set_simple_combo_by_text(page, "contactPrefs.contactVia",  setup.contact_via)
        _set_simple_combo_by_text(page, "contactPrefs.billing",     setup.billing_pref)
        _set_simple_combo_by_text(page, "contactPrefs.servicing",   setup.servicing_pref)

        # 10. Comments (free text).
        _fill_text(page, "agencyInfo.comments", setup.comments)

        # 11. Inline duplicate panel — EPIC fires this *before* Save once the
        # form has enough data (typically address + phone match an existing
        # account). The panel lives inline between the fields and the Save
        # button, NOT as a modal overlay — easy to miss if the form has
        # scrolled. Surface to the operator before submitting; they handle
        # the EPIC panel themselves (Dismiss, or Use this Client on a
        # specific row) and then press OK in our prompt to continue.
        page.wait_for_timeout(500)  # give the panel a beat to render
        panel_text = _inline_dup_panel_text(page)
        if panel_text:
            _log.info("Inline duplicate panel detected before Save")
            choice = _handle_inline_dup_panel(
                page, setup.account_name, panel_text,
            )
            if choice != "proceed":
                # Operator cancelled — back out via Cancel + Discard.
                try:
                    _cancel_form(page)
                except Exception:  # noqa: BLE001
                    pass
                return AccountCreateResult(
                    outcome=AccountCreateOutcome.DUP_CANCELLED,
                    account_name=setup.account_name,
                )
            # Operator returned proceed — they (should have) dismissed the
            # panel themselves. Belt-and-suspenders: if the panel is still
            # visible, click Dismiss for them so Save isn't intercepted.
            if _inline_dup_panel_text(page):
                _log.info("Panel still visible after operator OK — auto-dismissing")
                _dismiss_inline_dup_panel(page)
                page.wait_for_timeout(300)

        # 12. Save.
        _click_save(page)

    except EntryCancelled:
        raise
    except RuntimeError as exc:
        # Combo/selector failure mid-fill — back out and report.
        _log.error("Account create fill failed: %s", exc)
        try:
            _cancel_form(page)
        except Exception:  # noqa: BLE001
            pass
        return AccountCreateResult(
            outcome=AccountCreateOutcome.ERROR,
            account_name=setup.account_name,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001
        _log.error("Unexpected error filling Add Account form: %s", exc)
        try:
            _cancel_form(page)
        except Exception:  # noqa: BLE001
            pass
        return AccountCreateResult(
            outcome=AccountCreateOutcome.ERROR,
            account_name=setup.account_name,
            error=str(exc),
        )

    # 12. Watch for outcome.
    result = _wait_for_save_outcome(page, setup.account_name)

    # 13. Settle, then write the insured's FEIN + Business Type onto the Main
    # Business Contact. The account is already saved (lookup_code captured in
    # `result`), so this is best-effort: a failure is logged but does NOT
    # downgrade the CREATED outcome. Only runs when the account was actually
    # created and at least one of FEIN / Business Type was supplied.
    fein = (setup.fein or "").strip()
    business_type = (setup.business_type or "").strip()
    if result.outcome is AccountCreateOutcome.CREATED and (fein or business_type):
        page.wait_for_timeout(_FINAL_SETTLE_MS)  # 1s settle before navigating to Contacts
        try:
            _add_fein_to_contact(page, setup.account_name, fein, business_type)
        except EntryCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            _log.warning(
                "Contact enrichment (FEIN/BizType) failed (account still created OK): %s",
                exc,
            )

    return result

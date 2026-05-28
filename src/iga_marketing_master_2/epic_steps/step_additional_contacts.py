"""step_additional_contacts.py — add extra Contacts (Individual + Business) to an EPIC account.

Runs as an account-level entry step **between** Account Setup and Marketing
Submission setup. Account Setup already seeds the primary individual
(``state.contacts[0]`` → EPIC Primary Contact) and the main business entity
(``state.insured`` → Main Business Contact). This step adds the *rest*:

* **Individuals** — points of contact (``state.contacts``). Source columns:
  first/last/title/email/phone.
* **Business entities** — additional named insureds (the
  ``account.named_insured`` repeatable). Source columns: entity name, business
  type, FEIN, address, email, website.

Dedup: we read the live EPIC Contacts grid first and add only rows whose
(normalized) name isn't already present — so the seeded primary + main
business contact are skipped automatically, and re-runs are safe.

EPIC flow (old Angular proxy "Add a Contact" form, status CONTADD; confirmed
via live CDP 2026-05-27)
------------------------------------------------------------------------------
* Open: Contacts sidebar → grid Add icon
  ``[data-automation-id="fraContacts"] .icon-button[title="Add"]``.
* Type radio: ``rbtnIndividual`` / ``rbtnBusiness`` (proxy DIVs).
* Individual name: ``streFirstName`` / ``streMiddleName`` / ``streLastName``
  (+ ``cboPrefix`` / ``cboSuffix``). Business name: ``streName``.
* Category radios default to "Both Contact & Policy" (``rbtnIndBoth`` /
  ``rbtnBusBoth``) — left at the default per operator policy.
* Shared: phone ``pheNumberPrimary`` (+ type ``cboNumberTypePrimary``),
  email ``streEmail``.
* Actions (proxy buttons — click the inner ``<button>``):
    ``btnFinish``  — commit straight from the Add form (used for individuals
                     and businesses with no detail-only fields).
    ``btnDetail``  — go FORWARD to the contact detail page carrying the typed
                     data (used for businesses that need FEIN / Business Type).
                     This avoids the close+re-open blank-render flakiness.
    ``btnCancel``  — discard (raises the "Do you wish to discard changes?"
                     dialog when the form is dirty).

Field interactions (all validated live click-by-click 2026-05-27):
* **Name** — type into ``streFirstName``/``streLastName`` (individual) or
  ``streName`` (business).
* **Address** — individuals always "Use account address" (``chkUseAcctAddress``);
  businesses fill their own street+state (falls back to account address when
  no street). Manual path: click ``adePrimary`` to expand → ``streStreet``+Tab
  → ``strePostalCode``+Tab → if the ``vlvwZipPostCodes`` picker appears, click
  the row whose City matches (then ``btnOK``) or Cancel and fill ``streCity`` +
  ``cboState`` manually.
* **Phone** — type combo ``cboNumberTypePrimary`` = "Business"; digits go into
  the composite's ``pheNumberPrimaryChildPhoneNumber`` ("Local Number") child —
  EPIC adds the "1" dialing code and formats. (Individuals only.)
* **Email** — click ``streEmail`` then type. (Individuals only.)

FEIN / Business Type / Website are NOT on the Add form — they live on the
contact detail. For businesses we click ``btnDetail`` (forward transition,
carries the typed data — no close+re-open) and complete FEIN + Business Type
there, reusing the validated detail-screen helpers from
:mod:`step_account_create`. Business phone/email and Website are skipped
(operator policy 2026-05-27).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.sync_api import Page

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled, check_cancel
# Reuse the hardened contact-screen helpers validated during the FEIN work.
from iga_marketing_master_2.epic_steps.step_account_create import (
    _install_dom_observer,
    _settle_after_click,
    _wait_present,
    _wait_js_true,
    _click_sidebar_button,
    _save_and_close_contact,
    _select_fein_type,
    _select_business_type,
    _normalize_fein,
    _fill_text,
    _CONTACT_DETAIL_STATUS_JS,
    _BUSINESS_TAB_JS,
)

_log = logging.getLogger("iga.epic_steps.additional_contacts")

_CLICK_TIMEOUT = 5_000
_CONTACTS_GRID_SEL = '[data-automation-id^="vlvwContacts body-row item-"]'
_ADD_FORM_MARKER = '[data-automation-id="rbtnIndividual"]'  # present only on CONTADD


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class IndividualContact:
    """A point-of-contact person (mirrors state.Contact)."""
    first_name: str = ""
    last_name:  str = ""
    title:      str = ""
    email:      str = ""
    phone:      str = ""

    @property
    def display_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class BusinessContact:
    """An additional named business entity (one account.named_insured row)."""
    entity_name:    str = ""
    business_type:  str = ""
    fein:           str = ""
    street_address: str = ""
    state:          str = ""
    email:          str = ""
    website:        str = ""

    @property
    def display_name(self) -> str:
        return self.entity_name.strip()


@dataclass
class AdditionalContactsSetup:
    individuals: list[IndividualContact] = field(default_factory=list)
    businesses:  list[BusinessContact]  = field(default_factory=list)


@dataclass
class AdditionalContactsResult:
    added:   int = 0
    skipped: int = 0   # already present (dedup)
    failed:  int = 0


# ── Name normalization / dedup ──────────────────────────────────────────────

def _norm_name(s: str) -> str:
    """Normalize a contact name for dedup: lowercase, collapse whitespace,
    drop punctuation. ``"Deere & Company, Inc."`` → ``"deere company inc"``."""
    import re as _re
    s = (s or "").lower()
    s = _re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _read_existing_contact_names(page: Page) -> set[str]:
    """Return the normalized set of contact names currently in the grid.

    Reads the Name (first) cell of each ``vlvwContacts`` row. Falls back to
    the leading text of the row when a discrete cell can't be found.
    """
    try:
        names = page.evaluate(
            """() => {
                const rows = Array.from(document.querySelectorAll('[data-automation-id^="vlvwContacts body-row item-"]'));
                return rows.map(r => {
                    // Name is the first grid cell (class "body-cell first").
                    const cell = r.querySelector('.body-cell.first')
                        || r.querySelector('[data-test*="cell"], [role="gridcell"], td')
                        || r.firstElementChild || r;
                    return (cell.textContent || '').trim();
                });
            }"""
        ) or []
    except Exception as exc:  # noqa: BLE001
        _log.warning("Could not read existing contact names: %s", exc)
        return set()
    norm = {_norm_name(n) for n in names if n}
    _log.debug("Existing contacts (normalized): %r", sorted(norm))
    return norm


# ── Add-form primitives ──────────────────────────────────────────────────────

def _phone_digits(s: str) -> str:
    """Reduce a phone string to the bare digits EPIC's phone widget expects.

    EPIC rejects pre-formatted phones ("(615) 555-0199" → "Invalid phone
    number ... require 10 digits") — the masked widget wants raw digits and
    formats them itself. Drops a leading US country-code '1' on 11-digit input.
    """
    d = "".join(ch for ch in (s or "") if ch.isdigit())
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d


def _dismiss_message_box(page: Page) -> bool:
    """Dismiss EPIC's ``<message-box>`` validation/info overlay if present.

    Returns True if one was dismissed. The overlay intercepts pointer events,
    so a stray validation box (e.g. a bad field value) silently blocks every
    later click — call this before committing a row.
    """
    try:
        info = page.evaluate(
            """() => {
                // A REAL dialog has non-empty text AND an OK/Yes/Close button.
                // NOTE: the <message-box> HOST element is zero-size even for a
                // real dialog (the visible overlay is an absolutely-positioned
                // child), so DON'T filter on host size — discriminate on text +
                // an actionable button. The idle host has empty text → skipped.
                for (const mb of document.querySelectorAll('message-box, [class*="message-box"]')) {
                    const txt = (mb.textContent || '').trim();
                    if (!txt) continue;
                    const ok = Array.from(mb.querySelectorAll('button,[role="button"],.button'))
                        .find(b => /^(OK|Yes|Close)$/i.test((b.textContent||'').trim()));
                    if (!ok) continue;
                    ok.click();
                    return txt.slice(0, 160);
                }
                return null;
            }"""
        )
    except Exception:  # noqa: BLE001
        return False
    if info:
        _log.warning("Dismissed EPIC message-box: %r", info)
        page.wait_for_timeout(300)
        return True
    return False


def _fill_proxy(page: Page, aid: str, value: str) -> None:
    """Fill the old-proxy input/textarea inside ``[data-automation-id=aid]``."""
    if not value:
        return
    try:
        loc = page.locator(
            f'[data-automation-id="{aid}"] input, [data-automation-id="{aid}"] textarea'
        ).first
        loc.click(timeout=2_000)
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.keyboard.type(value, delay=20)
        page.keyboard.press("Tab")
        page.wait_for_timeout(150)
        _log.debug("proxy %s = %r OK", aid, value)
    except Exception as exc:  # noqa: BLE001
        _log.warning("proxy %s = %r FAILED: %s", aid, value, exc)


def _click_proxy(page: Page, aid: str) -> bool:
    """Click the inner control of a proxy radio/checkbox by automation-id."""
    return bool(page.evaluate(
        """(aid) => {
            const el = document.querySelector('[data-automation-id="' + aid + '"]');
            if (!el) return false;
            (el.querySelector('input,button') || el).click();
            return true;
        }""",
        aid,
    ))


def _click_form_button(page: Page, aid: str) -> bool:
    """Click an Add-form action button (btnDetail / btnFinish / btnCancel).

    These are proxy DIVs wrapping a real ``<button>``; the inner button needs
    a real Playwright click (evaluate-click doesn't register).
    """
    try:
        page.locator(f'[data-automation-id="{aid}"] button').first.click(timeout=_CLICK_TIMEOUT)
        return True
    except Exception as exc:  # noqa: BLE001
        _log.warning("form button %s click failed: %s", aid, exc)
        return False


def _type_field(page: Page, selector: str, value: str, *, clear: bool = True, tab: bool = True) -> None:
    """Click an input/textarea, optionally clear it, type *value*, optionally Tab.

    The reliable interaction for every text/combo/phone-child field on the Add
    form (validated live 2026-05-27). Uses real keystrokes — these old-proxy /
    Angular widgets ignore value-set and need typing to mark themselves dirty.
    """
    if not value:
        return
    loc = page.locator(selector).first
    loc.click(timeout=2_500)
    if clear:
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
    # press_sequentially (focuses the locator + types char-by-char) is the
    # exact method validated live 2026-05-27 — page.keyboard.type drops/mangles
    # chars on the masked widgets.
    loc.press_sequentially(value, delay=25)
    if tab:
        page.keyboard.press("Tab")
    page.wait_for_timeout(250)


# ── Address (Use-account vs manual with ZIP picker) ──────────────────────────

def _set_use_account_address(page: Page) -> None:
    """Check "Use account address" (auto-fills the account address, read-only)."""
    page.evaluate(
        """() => {
            const el = document.querySelector('[data-automation-id="chkUseAcctAddress"]');
            if (!el) return;
            const inp = el.querySelector('input');
            if (inp && !inp.checked) (inp).click();
            else if (!inp) el.click();
        }"""
    )
    page.wait_for_timeout(400)


def _expand_address_box(page: Page) -> bool:
    """Click the collapsed address box to expand its editable fields."""
    page.evaluate(
        """() => {
            const prev = document.querySelector('[data-automation-id="adePrimary"] .preview')
                || document.querySelector('[data-automation-id="adePrimary"]');
            if (prev) prev.click();
        }"""
    )
    return _wait_present(page, '[data-automation-id="streStreet"] textarea', 5_000)


def _handle_zip_picker(page: Page, desired_city: str) -> str:
    """Resolve the ZIP Codes picker if it appeared after a ZIP+Tab.

    Returns:
      "none"     — no picker (unambiguous ZIP; City/State auto-filled)
      "matched"  — a row matching *desired_city* was clicked + OK
      "cancelled"— no row matched (or no city given); modal Cancelled so the
                   caller fills City/State manually.
    """
    picker_js = (
        "() => !!document.querySelector('[data-automation-id^=\"vlvwZipPostCodes body-row item-\"]')"
    )
    if not _wait_js_true(page, picker_js, 3_000):
        return "none"
    want = _norm_name(desired_city)
    matched = bool(want) and bool(page.evaluate(
        """(want) => {
            const rows = Array.from(document.querySelectorAll('[data-automation-id^="vlvwZipPostCodes body-row item-"]'));
            for (const r of rows) {
                const cells = Array.from(r.querySelectorAll('.body-cell'));
                const city = cells[1] ? (cells[1].textContent || '').trim() : '';
                const norm = city.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
                if (norm === want) { r.click(); return true; }
            }
            return false;
        }""",
        want,
    ))
    if matched:
        # Click the modal's OK (scope to modal-screen — two btnOK/btnCancel on page).
        page.locator('modal-screen button.button.accept').first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(400)
        return "matched"
    # No match → cancel the modal so we can fill City/State manually.
    page.locator('modal-screen button.button.cancel').first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(400)
    return "cancelled"


def _fill_contact_address(
    page: Page,
    *,
    use_account: bool,
    street: str = "",
    city: str = "",
    state: str = "",
    zip_code: str = "",
) -> None:
    """Fill the contact address.

    * ``use_account=True`` (or no *street*) → check "Use account address".
    * else → expand the box, type Street+Tab, then:
        - if *zip_code*: type ZIP+Tab and resolve the ZIP picker (match *city*
          row+OK, else Cancel→manual City/State).
        - else: type City/State manually (no ZIP lookup).
    """
    street = (street or "").strip()
    if use_account or not street:
        _set_use_account_address(page)
        _log.debug("Address: used account address")
        return

    if not _expand_address_box(page):
        _log.warning("Address: could not expand the address box — skipping address")
        return
    _type_field(page, '[data-automation-id="streStreet"] textarea', street)

    need_manual_city_state = True
    if (zip_code or "").strip():
        _type_field(page, '[data-automation-id="strePostalCode"] input', zip_code.strip())
        outcome = _handle_zip_picker(page, city)
        if outcome in ("none", "matched"):
            need_manual_city_state = False  # City/State auto-filled

    if need_manual_city_state:
        if city:
            _type_field(page, '[data-automation-id="streCity"] input', city)
        if state:
            _type_field(page, '[data-automation-id="cboState"] input', state)
    _log.debug("Address: manual street=%r city=%r state=%r zip=%r", street, city, state, zip_code)


# ── Phone + email ─────────────────────────────────────────────────────────────

def _fill_phone(page: Page, raw_phone: str, *, number_type: str = "Business") -> None:
    """Set the Primary Number: type combo + the composite phone widget.

    Phone widget is a composite: ``cboNumberTypePrimary`` (type, asi-combo),
    ``pheNumberPrimaryChildPhoneNumber`` ("Local Number" — the digits go here;
    EPIC adds the "1" dialing code and formats). Validated live 2026-05-27:
    the digits MUST go into the Local-Number child, not ``pheNumberPrimary``.
    """
    digits = _phone_digits(raw_phone)
    # Type combo → Business (default). asi-combo resolves typed text (clear OK).
    _type_field(page, '[data-automation-id="cboNumberTypePrimary"] input', number_type)
    if not digits:
        return

    # Local Number is a MASKED child. The reliable method (validated live
    # 2026-05-27) is press_sequentially (focuses the element, caret at start)
    # — no click, no clear. EPIC adds the "1" dialing code and formats.
    # The script runs much faster than the live walk-through, and the masked
    # widget drops digits when typed too soon after the type-combo Tab — so we
    # settle, focus, type, then VERIFY the composite value contains all the
    # digits, retrying once with a longer settle on a miss.
    sel = '[data-automation-id="pheNumberPrimaryChildPhoneNumber"]'

    def _phone_ok() -> bool:
        # Read BOTH the composite AND the visible Local-Number input — the
        # composite lags (it only populates after EPIC formats on commit), so
        # checking it alone gives false negatives. The Local-Number input holds
        # the formatted value ("(615) 555-0123") right away.
        try:
            vals = page.evaluate(
                """() => {
                    const g = sel => { const e = document.querySelector(sel); return e ? (e.value || '') : ''; };
                    return [
                        g('[data-automation-id="pheNumberPrimary"]'),
                        g('[data-automation-id="pheNumberPrimaryChildPhoneNumber"] input'),
                    ];
                }"""
            ) or []
        except Exception:  # noqa: BLE001
            vals = []
        for v in vals:
            kept = "".join(ch for ch in str(v) if ch.isdigit())
            if kept == digits:
                _log.debug("phone verified via %r", v)
                return True
        _log.debug("phone not yet verified (read %r, want %r)", vals, digits)
        return False

    for attempt in (1, 2):
        page.wait_for_timeout(800)  # let the masked widget settle before typing
        try:
            loc = page.locator(sel).first
            loc.click(timeout=2_500)        # focus the Local-Number child
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")   # clear any partial from a prior attempt
            loc.press_sequentially(digits, delay=60)
            page.keyboard.press("Tab")
            page.wait_for_timeout(600)
        except Exception as exc:  # noqa: BLE001
            _log.warning("phone fill attempt %d raised: %s", attempt, exc)
        # A bad phone pops a validation message-box — clear it so it can't block.
        _dismiss_message_box(page)
        if _phone_ok():
            _log.debug("phone local number = %r OK (attempt %d)", digits, attempt)
            return
        _log.warning("phone digits didn't fully land (attempt %d) — retrying", attempt)
    _log.warning("phone number %r could not be entered cleanly — leaving as-is", digits)


def _fill_email(page: Page, email: str) -> None:
    """Fill the Primary Email (click-to-focus then type — validated 2026-05-27)."""
    if not (email or "").strip():
        return
    _type_field(page, '[data-automation-id="streEmail"] input', email.strip())


_ADD_BTN_SEL = '[data-automation-id="fraContacts"] .icon-button[title="Add"]'


def _ensure_on_contacts_grid(page: Page) -> bool:
    """Make sure the Contacts grid (with its Add toolbar) is the active view.

    A prior add can leave EPIC on a fresh Add form or elsewhere, so each add
    re-asserts the grid via the sidebar. Dismisses a stray validation
    message-box and any discard prompt for a leftover blank form first.
    """
    _dismiss_message_box(page)
    try:
        if page.locator(_ADD_BTN_SEL).count() > 0:
            return True
    except Exception:  # noqa: BLE001
        pass
    if not _click_sidebar_button(page, "Contacts"):
        _log.warning("Add-contact: Contacts sidebar button not found")
        return False
    # A leftover blank Add form may raise "discard changes?" — discard it.
    page.wait_for_timeout(400)
    page.evaluate(
        """() => {
            const yes = Array.from(document.querySelectorAll('button,[role="button"],.button'))
                .find(b => (b.textContent||'').trim()==='Yes' && b.getBoundingClientRect().width>0);
            if (yes) yes.click();
        }"""
    )
    if not _wait_present(page, '[data-automation-id="fraContacts"]', 10_000):
        _log.warning("Add-contact: Contacts grid did not render")
        return False
    _settle_after_click(page)
    return True


def _open_add_form(page: Page) -> bool:
    """Ensure we're on the Contacts grid, click Add, and wait for CONTADD."""
    if not _ensure_on_contacts_grid(page):
        return False
    opened = page.evaluate(
        """() => {
            const fra = document.querySelector('[data-automation-id="fraContacts"]');
            if (!fra) return false;
            const add = Array.from(fra.querySelectorAll('.icon-button'))
                .find(b => b.getAttribute('title') === 'Add');
            if (!add) return false;
            add.click();
            return true;
        }"""
    )
    if not opened:
        _log.warning("Add-contact: grid Add button not found")
        return False
    if not _wait_present(page, _ADD_FORM_MARKER, 10_000):
        _log.warning("Add-contact: Add form (CONTADD) did not render")
        return False
    _settle_after_click(page)
    return True


# ── Per-contact entry ────────────────────────────────────────────────────────

def _add_individual(page: Page, c: IndividualContact) -> bool:
    """Add one Individual point-of-contact via the Add form → Finish.

    Fills: name → address (always "Use account address" — the Contact record
    carries no per-person address) → phone (type Business + number) → email →
    Finish. `title` is skipped (no Primary-tab field).
    """
    if not _open_add_form(page):
        return False
    _click_proxy(page, "rbtnIndividual")  # default, but be explicit
    page.wait_for_timeout(200)
    _fill_proxy(page, "streFirstName", c.first_name)
    _fill_proxy(page, "streLastName",  c.last_name)
    # Individuals have no address data → use the account address.
    _fill_contact_address(page, use_account=True)
    if c.phone:
        _fill_phone(page, c.phone, number_type="Business")
    _fill_email(page, c.email)
    _settle_after_click(page, extra_ms=300)
    _dismiss_message_box(page)
    if not _click_form_button(page, "btnFinish"):
        return False
    _settle_after_click(page, extra_ms=600)
    _log.info("Added individual contact %r", c.display_name)
    return True


def _add_business(page: Page, b: BusinessContact) -> bool:
    """Add one Business contact. If it carries detail-only data (FEIN /
    Business Type), go through the Detail page to complete + save; otherwise
    commit straight from the Add form with Finish.
    """
    if not _open_add_form(page):
        return False
    _click_proxy(page, "rbtnBusiness")
    page.wait_for_timeout(400)  # let the Business layout swap in
    _fill_proxy(page, "streName", b.entity_name)
    # Address: the named insured's own street + state (no city/zip in the data
    # model). Falls back to "Use account address" when no street is given.
    # Phone/email are intentionally skipped for business contacts (operator
    # policy 2026-05-27). FEIN + Business Type are completed on the Detail page.
    _fill_contact_address(
        page, use_account=False, street=b.street_address, state=b.state,
    )
    _settle_after_click(page, extra_ms=300)
    _dismiss_message_box(page)

    fein_digits = _normalize_fein(b.fein)
    needs_detail = bool(fein_digits or (b.business_type or "").strip())

    if not needs_detail:
        if not _click_form_button(page, "btnFinish"):
            return False
        _settle_after_click(page, extra_ms=600)
        _log.info("Added business contact %r (no detail fields)", b.display_name)
        return True

    # Detail path — forward transition (carries typed data; no re-open).
    _dismiss_message_box(page)
    if not _click_form_button(page, "btnDetail"):
        return False
    if not _wait_js_true(page, _CONTACT_DETAIL_STATUS_JS, 15_000):
        _log.warning("Business %r: detail page did not open after Detail", b.display_name)
        return False
    _settle_after_click(page, quiet_ms=800, timeout_ms=8_000, extra_ms=800)

    # Business tab → FEIN + Business Type (reuses the validated helpers).
    if not _wait_js_true(page, _BUSINESS_TAB_JS, 10_000):
        _log.warning("Business %r: Business tab never rendered (blank?)", b.display_name)
        return False
    page.evaluate(
        """() => {
            const t = Array.from(document.querySelectorAll('div,button,[role="tab"]'))
                .find(e => (e.textContent || '').trim() === 'Business' && e.getBoundingClientRect().width > 0);
            if (t) t.click();
        }"""
    )
    if not _wait_present(page, '[name="bIdNumbers.[0].description"]', 10_000):
        _log.warning("Business %r: Identification Numbers did not render", b.display_name)
        return False
    _settle_after_click(page)

    if fein_digits:
        if _select_fein_type(page):
            _settle_after_click(page, extra_ms=300)
            _fill_text(page, "bIdNumbers.[0].value", fein_digits)
            _settle_after_click(page, extra_ms=300)
        else:
            _log.warning("Business %r: could not set FEIN type", b.display_name)
    if (b.business_type or "").strip():
        if _select_business_type(page, b.business_type):
            _settle_after_click(page, extra_ms=300)
        else:
            _log.warning("Business %r: could not set business type %r",
                         b.display_name, b.business_type)

    # TODO(v2): Website + mailing address on the detail summary (Edit Contact
    # Summary modal: summary.website.website, summary.address.address-*).

    if not _save_and_close_contact(page):
        _log.warning("Business %r: save+close not confirmed clean", b.display_name)
        return False
    _log.info("Added business contact %r (with detail fields)", b.display_name)
    return True


# ── Public API ───────────────────────────────────────────────────────────────

def run(page: Page, setup: AdditionalContactsSetup) -> AdditionalContactsResult:
    """Add the additional individual + business contacts to the open account.

    Pre-condition: the correct EPIC account is loaded (the account sidebar
    with "Contacts" is available). Best-effort & idempotent: dedups against
    the live Contacts grid by normalized name.
    """
    result = AdditionalContactsResult()
    if not setup.individuals and not setup.businesses:
        _log.info("Additional Contacts: nothing to add — skipping")
        return result

    _install_dom_observer(page)

    # Navigate to Contacts and wait for the grid.
    if not _click_sidebar_button(page, "Contacts"):
        _log.warning("Additional Contacts: Contacts sidebar button not found")
        return result
    if not _wait_present(page, _CONTACTS_GRID_SEL, 10_000):
        _log.warning("Additional Contacts: Contacts grid did not populate")
        return result
    _settle_after_click(page)

    existing = _read_existing_contact_names(page)
    _log.info(
        "Additional Contacts: %d individual(s), %d business(es) requested; "
        "%d contact(s) already in EPIC",
        len(setup.individuals), len(setup.businesses), len(existing),
    )

    def _process(items, adder) -> None:
        nonlocal existing
        for item in items:
            check_cancel()
            name = item.display_name
            if not name:
                _log.warning("Skipping a contact row with no name")
                result.failed += 1
                continue
            if _norm_name(name) in existing:
                _log.info("Skipping %r — already present in EPIC", name)
                result.skipped += 1
                continue
            try:
                ok = adder(page, item)
            except EntryCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("Adding %r failed: %s", name, exc)
                ok = False
            if ok:
                result.added += 1
                existing.add(_norm_name(name))
                # Re-sync the existing set from the live grid so item-* indices
                # and any EPIC name-normalization are reflected before the next row.
                existing |= _read_existing_contact_names(page)
            else:
                result.failed += 1

    _process(setup.individuals, _add_individual)
    _process(setup.businesses, _add_business)

    _log.info(
        "=== Additional Contacts complete: %d added, %d skipped (dup), %d failed ===",
        result.added, result.skipped, result.failed,
    )
    return result

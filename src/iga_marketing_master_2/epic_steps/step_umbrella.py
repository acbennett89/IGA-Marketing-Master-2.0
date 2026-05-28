"""step_umbrella.py — fill the Commercial Umbrella (CUMB) screens.

Pre-condition
-------------
The browser is on the Submission Detail sidebar tree with a Commercial Umbrella
line already created by the user.

Sections filled (in order)
---------------------------
1. **Policy Information** (CHM-UMPOLINF) — expiring policy #, occurrence limit,
   retained limit.
2. **Underlying Insurance** (CHM-UMUNDERL) — per-coverage rows (carrier, desc, limit).
3. **Additional Interests** — React vlvwInterest pattern (same as BAUT).
4. **Additional Coverages** — same pattern as BAUT.
5. **Forms & Endorsements** — IGA standard set.

Skipped sections
----------------
Primary Locations, General Information, Additional Exposures, Vehicles (covered
elsewhere), Remarks, Supplemental Screens, Attachments, Document View.

Selector notes (preliminary; verified during iteration 2026-05-21)
------------------------------------------------------------------
Umbrella uses ``Policy.CommercialUmbrella.*`` sidebar paths.  Most fields use
the React ``__textField`` suffix pattern.  The Underlying Insurance grid layout
is confirmed on first live run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

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
    _split_us_address,
    _fill_validated_address,
    _resolve_ai_interest,
)
from iga_marketing_master_2.epic_steps.step_general_liability import FormSpec as _GLFormSpec
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled, check_cancel
from iga_marketing_master_2.epic_steps.validation_check import checkpoint
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

_log = logging.getLogger("iga.epic_steps.umbrella")

_CLICK_TIMEOUT = 5_000
_NAV_WAIT_MS   = 1_200
_FILL_WAIT_MS  = 150


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class UmbrellaUnderlyingSpec:
    """One row on the Umbrella Underlying Insurance grid (the "other" category).

    Mirrors policy.umbrella.underlying.other.*:
      carrier  — streCarrier
      desc     — streDescription
      limit    — cureLimit__textField (currency)
    """
    carrier: str = ""
    desc:    str = ""
    limit:   str = ""


@dataclass
class UmbrellaAdditionalInterestSpec:
    name:           str
    interest_type:  str = ""
    address_line_1: str = ""


@dataclass
class UmbrellaAdditionalCoverageSpec:
    description: str
    code:        str = ""
    limit1:      str = ""
    deductible:  str = ""


@dataclass
class UmbrellaSetup:
    """Top-level container for the Umbrella entry."""
    expiring_pol_num: str = ""
    occurrence_limit: str = ""
    retained_limit:   str = ""
    underlying:       list[UmbrellaUnderlyingSpec]          = field(default_factory=list)
    additional_interests: list[UmbrellaAdditionalInterestSpec] = field(default_factory=list)
    additional_coverages: list[UmbrellaAdditionalCoverageSpec] = field(default_factory=list)


# ── Navigation ────────────────────────────────────────────────────────────────

def _aggressive_cleanup(page: Page) -> None:
    """Dismiss any cascade modals (validation errors, stale message-boxes).

    EPIC's validation modals can cascade from earlier-section incomplete rows
    and intercept all subsequent clicks.  Safe to call frequently.
    """
    try:
        for _ in range(5):
            dismissed = page.evaluate(
                """() => {
                    let n = 0;
                    for (const m of document.querySelectorAll('message-box')) {
                        if ((m.textContent || '').trim() === '') { m.remove(); n++; }
                    }
                    const portal = document.querySelector('[data-test="common-message-modal"]');
                    if (portal) {
                        const ok = Array.from(portal.querySelectorAll('button')).find(b => (b.textContent || '').trim() === 'OK');
                        if (ok) { ok.click(); n++; }
                    }
                    for (const m of document.querySelectorAll('message-box')) {
                        if (m.offsetParent === null) continue;
                        const ok = Array.from(m.querySelectorAll('button')).find(b => (b.textContent || '').trim() === 'OK');
                        if (ok) { ok.click(); n++; }
                    }
                    return n;
                }"""
            )
            if not dismissed:
                break
            page.wait_for_timeout(300)
    except Exception as exc:
        _log.debug("aggressive_cleanup failed: %s", exc)


def list_available_umbrella_lines(page: Page) -> "list[dict]":
    """Return info about every Umbrella line on the open submission."""
    try:
        _expand_policy_tree(page)
    except Exception:
        pass
    try:
        return list(page.evaluate(
            """() => {
                const out = [];
                for (const el of document.querySelectorAll('[data-automation-id*="level-3"]')) {
                    const txt = (el.textContent || '').trim();
                    if (!/umbrella/i.test(txt)) continue;
                    const aid = el.getAttribute('data-automation-id') || '';
                    const m = aid.match(/;([A-Z]{2}) level-3$/);
                    out.push({ state: m ? m[1] : '', aid, text: txt });
                }
                return out;
            }"""
        ))
    except Exception:
        return []


def _nav_to_umbrella(page: Page, max_attempts: int = 4) -> bool:
    for attempt in range(1, max_attempts + 1):
        try:
            _expand_policy_tree(page)
        except Exception as exc:
            _log.debug("Umbrella: tree-expand pre-step failed: %s", exc)

        clicked = page.evaluate(
            """() => {
                for (const el of document.querySelectorAll('[data-automation-id*="level-3"]')) {
                    if (/umbrella/i.test((el.textContent || '').trim())) {
                        el.scrollIntoView({block: 'center'});
                        el.click();
                        return true;
                    }
                }
                return false;
            }"""
        )
        if clicked:
            page.wait_for_timeout(_NAV_WAIT_MS)
            _log.debug("Clicked Umbrella sidebar parent (attempt %d/%d)", attempt, max_attempts)
            return True
        _log.warning("Umbrella sidebar entry not found — attempt %d/%d; waiting 2 s", attempt, max_attempts)
        page.wait_for_timeout(2_000)

    available = list_available_umbrella_lines(page)
    if available:
        _log.error("Umbrella sidebar entry not found. Available: %s", available)
    else:
        _log.error("No Umbrella lines on this submission — create one in EPIC first.")
    return False


def _nav_umbrella_section(page: Page, section_key: str, screen_code: str) -> bool:
    """Navigate to a Commercial Umbrella sub-section with aggressive
    modal cleanup before AND after the click.
    """
    _aggressive_cleanup(page)
    sidebar_sel = f'[data-automation-id^="sidebar-button-Policy.CommercialUmbrella.{section_key}"]'
    btn = page.locator(sidebar_sel)
    if not _wait_visible(page, btn, 5_000):
        _log.error("Umbrella sidebar link for %s not found", section_key)
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    _aggressive_cleanup(page)
    try:
        page.wait_for_function(
            f"""() => {{
                const text = document.body.innerText;
                if (text.includes("{screen_code}")) return true;
                for (const b of document.querySelectorAll('[class*="statusBar"], .status-bar')) {{
                    if (b.textContent.includes("{screen_code}")) return true;
                }}
                return document.querySelectorAll('[data-automation-id="{screen_code}"]').length > 0;
            }}""",
            timeout=6_000,
        )
        _log.debug("Navigated to CommercialUmbrella.%s (%s) OK", section_key, screen_code)
        return True
    except PlaywrightTimeout:
        _log.warning("CommercialUmbrella.%s navigated but %s not confirmed", section_key, screen_code)
        return True


# ── Policy Information ───────────────────────────────────────────────────────

def _fill_policy_information(page: Page, setup: UmbrellaSetup) -> None:
    """Fill the Umbrella Policy Information screen (CHM-UMPOLINF).

    Field IDs (verify live on first run):
      streExpiringPolicyNumber (or streExpiringPolNum)
      cureOccurrenceLimit__textField
      cureRetainedLimit__textField (or streRetainedLimit)
    """
    if not (setup.expiring_pol_num or setup.occurrence_limit or setup.retained_limit):
        _log.info("Policy Information: no data — skipping")
        return
    if not _nav_umbrella_section(page, "PolicyInformation", "CHM-UMPOLINF"):
        return

    _log.info("=== Policy Information ===")
    # Try several known id patterns — live verification will narrow this down.
    for fid in ("streExpiringPolicyNumber", "streExpiringPolNum", "streExpiringPolicyNum"):
        if _fill_either(page, fid, setup.expiring_pol_num):
            break
    _fill_either(page, "cureOccurrenceLimit", _strip_currency(setup.occurrence_limit))
    _fill_either(page, "cureRetainedLimit",   _strip_currency(setup.retained_limit))
    page.keyboard.press("Tab")
    page.wait_for_timeout(500)
    _log.info("Policy Information: filled")


# ── Underlying Insurance ─────────────────────────────────────────────────────

def _fill_underlying_insurance(page: Page, rows: list) -> None:
    """Fill Underlying Insurance — a fixed multi-section form (not a grid).

    Sections: Auto Liability, General Liability, Employers Liability, Other, Other2.
    Each row is routed to a section by keyword matching against its desc.
    Personal lines (Homeowners, Personal Auto, etc.) are skipped — they don't
    belong on ACORD 131's commercial underlying schedule.
    Only the primary limit field is filled (carrier/policy#/dates are skipped,
    mirroring v1's approach).
    """
    if not rows:
        _log.info("No underlying insurance rows; skipping")
        return
    if not _nav_umbrella_section(page, "UnderlyingInsurance", "CHM-UMUNDERL"):
        return

    _log.info("=== Underlying Insurance (%d input row(s)) ===", len(rows))

    _SECTION_PRIMARY_LIMIT = [
        # (keyword in desc — checked in order, first match wins, primary limit field name)
        ("commercial auto",       "streAutoCSLAccLimit"),
        ("business auto",         "streAutoCSLAccLimit"),
        ("automobile",            "streAutoCSLAccLimit"),
        ("general liability",     "streGLOccLimit"),
        ("employers liability",   "streELAccLimit"),
        ("workers comp",          "streELAccLimit"),
        ("workers'",              "streELAccLimit"),
    ]
    _PERSONAL_KEYWORDS = ("personal auto", "homeowner", "personal liability", "dwelling")
    _OTHER_SLOTS = ["streOtherLimit", "streOther2Limit"]

    other_idx = 0
    for i, r in enumerate(rows, 1):
        check_cancel()
        desc_lc = (r.desc or "").lower()
        if any(kw in desc_lc for kw in _PERSONAL_KEYWORDS):
            _log.info("Row %d: skipped (personal line) desc=%r", i, r.desc)
            continue
        target = next(
            (field_name for kw, field_name in _SECTION_PRIMARY_LIMIT if kw in desc_lc),
            None,
        )
        if target is None:
            if other_idx < len(_OTHER_SLOTS):
                target = _OTHER_SLOTS[other_idx]
                other_idx += 1
                _log.info("Row %d: routed to %s slot (no keyword match) desc=%r", i, target, r.desc)
            else:
                _log.warning("Row %d: no section match and Other slots exhausted — skipping desc=%r",
                             i, r.desc)
                continue
        limit_val = _strip_currency(r.limit)
        _log.info("Row %d: filling %s = %r (desc=%r)", i, target, limit_val, r.desc)
        try:
            sel = f'input[name="{target}__textField"]'
            loc = page.locator(sel)
            if loc.count() == 0:
                _log.warning("Field %s not found on screen — skipping row %d", target, i)
                continue
            loc.first.click(timeout=_CLICK_TIMEOUT)
            page.keyboard.press("Control+a")
            page.keyboard.press("Delete")
            page.keyboard.type(limit_val, delay=30)
            page.keyboard.press("Tab")
        except Exception as exc:
            _log.warning("Row %d fill failed: %s", i, exc)
    _log.info("Underlying Insurance: complete")


# ── Additional Interests / Coverages — same patterns as BAUT ──────────────────

def _fill_umbrella_additional_interests(page: Page, interests: list) -> None:
    """Add each AI row on Umbrella > AdditionalInterest (React vlvwInterest pattern).

    Rewritten 2026-05-26 to mirror the IM AI pattern.
    """
    if not interests:
        _log.info("No additional interests; skipping section")
        return
    if not _nav_umbrella_section(page, "AdditionalInterest", "CHM-UMADDINT"):
        _log.error("Additional Interests: nav failed — aborting section")
        return

    _log.info("=== Additional Interests: %d item(s) ===", len(interests))
    assert_screen_code(page, "CHM-UMADDINT", timeout_ms=8_000)

    add_btn_sel = '[data-test="vlvwInterest_add"]'

    for i, ai in enumerate(interests, 1):
        check_cancel()
        name = (ai.name or "").strip()
        if not name:
            _log.warning("AI #%d: blank name — skipping (EPIC requires name)", i)
            continue

        _log.info(
            "--- Row %d/%d: name=%r type=%r addr=%r",
            i, len(interests), name, ai.interest_type or "-",
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


def _fill_umbrella_additional_coverages(page: Page, coverages: list) -> None:
    if not coverages:
        return
    if not _nav_umbrella_section(page, "AdditionalCoverages", "CHM-UMADDCOV"):
        return
    _log.info("=== Additional Coverages (%d) ===", len(coverages))

    add_btn = page.locator('[data-test="vlvwCoverage_add"]')
    for i, ac in enumerate(coverages, 1):
        check_cancel()
        _log.info("Adding AC %d/%d: desc=%r", i, len(coverages), ac.description)
        _dismiss_modal(page)
        if not _wait_visible(page, add_btn, 5_000):
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)
        short_desc = abbreviate_if_too_long(
            ac.description, max_chars=AC_DESC_MAX_CHARS,
            row_idx=i, label="description", section="AC", logger=_log,
        )
        _fill_id_field(page, "streDescription",           short_desc)
        _fill_id_field(page, "streCode",                  ac.code or "N/A")
        _fill_id_field(page, "streLimit1__textField",     _strip_currency(ac.limit1))
        _fill_id_field(page, "streDeductible__textField", _strip_currency(ac.deductible))
        page.wait_for_timeout(300)
    _log.info("Additional Coverages: complete (%d rows)", len(coverages))


# ── Forms & Endorsements ─────────────────────────────────────────────────────

UMBRELLA_STANDARD_FORMS: list[_GLFormSpec] = [
    _GLFormSpec(name="Blanket by Written Contract - Additional Insured"),
    _GLFormSpec(name="Blanket by Written Contract - Waiver of Subrogation"),
    _GLFormSpec(name="Blanket by Written Contract - Primary and Non-Contributory"),
]


def _fill_umbrella_forms_endorsements(page: Page, forms: list) -> None:
    if not forms:
        return
    if not _nav_umbrella_section(page, "FormEndorsement", "CHM-UMFRMEND"):
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
            break
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(600)
        if frm.form_number:
            fill_fn(page, "streNumber", frm.form_number)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
        fill_fn(page, "streName", frm.name)
        page.wait_for_timeout(300)
    _log.info("Forms & Endorsements: complete (%d rows)", len(forms))


# ── Generic helpers (reused from IM pattern) ─────────────────────────────────

def _strip_currency(value: str) -> str:
    return (value or "").replace("$", "").replace(",", "").strip()


def _resolve_add_btn(page: Page, vlvw_name: str):
    react = page.locator(f'[data-test="{vlvw_name}_add"]')
    if react.count() > 0:
        return react
    proxy = page.locator(f'[name="{vlvw_name}"] .icon-button[title="Add"]')
    if proxy.count() > 0:
        return proxy
    return None


def _fill_either(page: Page, field_id: str, value: str) -> bool:
    if not value:
        return False
    react_inp = page.locator(f'#{field_id}, #{field_id}__textField')
    if react_inp.count() and react_inp.first.is_visible(timeout=400):
        try:
            react_inp.first.click(timeout=2_000)
            react_inp.first.fill(value, timeout=2_000)
            page.wait_for_timeout(_FILL_WAIT_MS)
            return True
        except Exception:
            pass
    proxy_inp = page.locator(f'[data-automation-id="{field_id}"] input, [data-automation-id="{field_id}"] textarea')
    if proxy_inp.count():
        try:
            proxy_inp.first.click(timeout=2_000)
            proxy_inp.first.fill(value, timeout=2_000)
            page.wait_for_timeout(_FILL_WAIT_MS)
            return True
        except Exception as exc:
            _log.warning("fill_either %s = %r FAILED: %s", field_id, value, exc)
    return False


# ── Entry point ───────────────────────────────────────────────────────────────

def run(page: Page, setup: UmbrellaSetup) -> bool:
    """Walk all Umbrella screens, filling from `setup`."""
    _log.info("=== Commercial Umbrella entry step starting ===")
    _log.info(
        "Setup: occ_limit=%r retained=%r %d underlying %d AI %d AC",
        setup.occurrence_limit, setup.retained_limit,
        len(setup.underlying), len(setup.additional_interests), len(setup.additional_coverages),
    )

    try:
        if not _nav_to_umbrella(page):
            return False

        _log.info("=== Step 1: Policy Information ===")
        checkpoint(page, "Umbrella: Policy Information")
        _fill_policy_information(page, setup)

        _log.info("=== Step 2: Underlying Insurance ===")
        checkpoint(page, "Umbrella: Underlying Insurance")
        _fill_underlying_insurance(page, setup.underlying)

        # Additional Interests — rewritten 2026-05-26 to mirror the IM AI
        # pattern (safe_action + verify, _wait_grid_grew, lenient validated
        # address with manual fallback + tail extraction).
        _log.info("=== Step 3: Additional Interests ===")
        checkpoint(page, "Umbrella: Additional Interests")
        _fill_umbrella_additional_interests(page, setup.additional_interests)

        _log.info("=== Step 4: Additional Coverages ===")
        checkpoint(page, "Umbrella: Additional Coverages")
        _fill_umbrella_additional_coverages(page, setup.additional_coverages)

        _log.info("=== Step 5: Forms & Endorsements ===")
        checkpoint(page, "Umbrella: Forms & Endorsements")
        _fill_umbrella_forms_endorsements(page, UMBRELLA_STANDARD_FORMS)

        _log.info("=== Commercial Umbrella entry step complete ===")
        return True
    except EntryCancelled:
        raise
    except Exception as exc:
        _log.error("Umbrella run aborted: %s", exc, exc_info=True)
        return False

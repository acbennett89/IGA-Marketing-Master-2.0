"""step_inland_marine.py — fill the Commercial Inland Marine (Equipment Floater) screens.

Pre-condition
-------------
The browser is on the Submission Detail sidebar tree for the target MMS, with an
Inland Marine line already created by the user.  IM lines may or may not be
state-suffixed (older accounts: no suffix; newer: ``;{STATE} level-3`` like BAUT).

Sections filled (in order)
---------------------------
1. **Coverage / Deductible** (CHM-EFCOVDED) — total scheduled amount + ACV/Replacement
   Cost deductible singletons.
2. **Scheduled Equipment** (CHM-EFSCHEQU) — per-item rows: item#, type, manufacturer,
   model, year, description, insured amount, deductible.
3. **Unscheduled Equipment** (CHM-EFUNEQIP) — description + insured amount per row.
4. **Additional Interests** (CHM-EFADDLIN) — same React vlvwInterest pattern as BAUT.
5. **Additional Coverages** (CHM-EFADDCOV) — same React vlvwCoverage pattern.
6. **Forms & Endorsements** — IGA standard endorsement set (Inland Marine Extension).

Skipped sections
----------------
Territory/Type, Equipment Storage, Remarks, Supplemental Screens, Attachments,
Document View, and scraped forms from state.json (only IGA standard forms are added).

Selector notes (preliminary — verified live during iteration 2026-05-21)
-----------------------------------------------------------------------
Most IM screens follow the React ``__textField`` + ``data-test="vlvw*_add"``
pattern.  Equipment grids (vlvwSchedEquip, vlvwUnschEquip) likely match the
vlvwCoverage pattern from BAUT — confirmed selectors are filled in during the
first live run.
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
    EPICHardError,
    assert_clean,
    assert_screen_code,
    safe_action,
    verify_input_value,
    verify_radio_checked,
    wait_for,
    wait_for_grid_add_ready,
    wait_for_loading_clear,
)

_log = logging.getLogger("iga.epic_steps.inland_marine")

_CLICK_TIMEOUT  = 5_000
_NAV_WAIT_MS    = 1_200
_FILL_WAIT_MS   = 150


# ── Data types ────────────────────────────────────────────────────────────────

@dataclass
class IMScheduledItemSpec:
    """One row on the IM Scheduled Equipment grid.

    Mirrors policy.inland_marine.scheduled_item.* in state.json:
      item_number    — inteItemNumber__textField (or similar)
      type           — cboType combo (e.g. "Contractors Equipment")
      manufacturer   — streManufacturer
      model          — streModel
      model_year     — inteYear / inteModelYear
      description    — streDescription
      serial_number  — streSerialNumber (labelled "ID/Serial number" in EPIC;
                       desired-but-not-required, added 2026-05-26)
      amt_insurance  — cureAmountOfInsurance__textField (currency)
      deductible     — cureDeductible__textField (per-item override)
    """
    item_number:   str = ""
    type:          str = ""
    manufacturer:  str = ""
    model:         str = ""
    model_year:    str = ""
    description:   str = ""
    serial_number: str = ""
    amt_insurance: str = ""
    deductible:    str = ""


@dataclass
class IMUnscheduledItemSpec:
    """One row on the IM Unscheduled Equipment grid.

    Mirrors policy.inland_marine.unscheduled_item.*:
      description       — streDescription (full original; preserved for audit)
      description_short — ≤30-char form actually typed into EPIC.
                          Populated by the GUI (lazy Claude call + persist)
                          or by Claude during PDF extraction (derived field
                          in the Field Map). When blank, the EPIC entry
                          step falls back to abbreviate_for_epic on the fly.
      amt_insurance     — cureAmountOfInsurance__textField
    """
    description:       str = ""
    description_short: str = ""
    amt_insurance:     str = ""


@dataclass
class IMAdditionalInterestSpec:
    """One Additional Interest row on IM > AdditionalInterest.

    ``item_number`` is the Scheduled-Equipment item this AI applies to,
    typed into ``inteItemNumber__textField`` on the AI row form. Blank
    means policy-level (no specific scheduled item).
    """
    name:            str
    interest_type:   str = ""
    address_line_1:  str = ""
    reason_for_int:  str = ""
    item_number:     str = ""


@dataclass
class IMAdditionalCoverageSpec:
    """One Additional Coverage row on IM > AdditionalCoverage.

    ``item_number`` mirrors the AI subject-ref field — links this coverage
    to a specific Scheduled-Equipment item when relevant. Blank means
    policy-level. State.json doesn't currently populate this; extraction
    may gain it later.
    """
    description:    str
    code:           str = ""
    each_claim:     str = ""
    deductible:     str = ""
    item_number:    str = ""


@dataclass
class InlandMarineSetup:
    """Top-level container for the IM entry.

    state                       — 2-letter state code (only used if line is state-suffixed)
    total_scheduled_amount      — singleton (free text "See ... Schedule" allowed)
    acv_replacement_cost_deductible — singleton (currency)
    scheduled_items             — list of equipment rows
    unscheduled_items           — list of unscheduled rows
    additional_interests        — list of AI rows
    additional_coverages        — list of AC rows
    """
    state:                      str = ""
    total_scheduled_amount:     str = ""
    acv_replacement_cost_deductible: str = ""
    scheduled_items:            list[IMScheduledItemSpec]      = field(default_factory=list)
    unscheduled_items:          list[IMUnscheduledItemSpec]    = field(default_factory=list)
    additional_interests:       list[IMAdditionalInterestSpec] = field(default_factory=list)
    additional_coverages:       list[IMAdditionalCoverageSpec] = field(default_factory=list)


# ── Navigation ────────────────────────────────────────────────────────────────

def _aggressive_cleanup(page: Page) -> None:
    """Dismiss every common-message-modal and validation message-box currently
    visible.  These cascade from prior-section incomplete rows and block all
    subsequent clicks until OK'd.

    Safe to call frequently — does nothing when no blocker is present.
    """
    try:
        for _ in range(5):
            dismissed = page.evaluate(
                """() => {
                    let n = 0;
                    // Remove empty stale message-boxes
                    for (const m of document.querySelectorAll('message-box')) {
                        if ((m.textContent || '').trim() === '') { m.remove(); n++; }
                    }
                    // Click OK on any validation modal
                    const portal = document.querySelector('[data-test="common-message-modal"]');
                    if (portal) {
                        const ok = Array.from(portal.querySelectorAll('button')).find(b => (b.textContent || '').trim() === 'OK');
                        if (ok) { ok.click(); n++; }
                    }
                    // Click OK on any visible <message-box> dialog with content
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


def list_available_im_lines(page: Page) -> "list[dict]":
    """Return info about every IM (Equipment Floater / Inland Marine) line on the
    open submission.  Returns a list of dicts with ``state`` (or "" if not
    state-suffixed) and ``aid`` for each entry.
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
                    const txt = (el.textContent || '').trim();
                    if (!/inland marine|equipment/i.test(txt)) continue;
                    const m = aid.match(/;([A-Z]{2}) level-3$/);
                    out.push({ state: m ? m[1] : '', aid, text: txt });
                }
                return out;
            }"""
        ))
    except Exception:
        return []


def _nav_to_im(page: Page, state: str = "", max_attempts: int = 4) -> bool:
    """Click the IM (Equipment Floater) sidebar entry.  If `state` is set and
    multiple IM lines exist, picks the matching state suffix; otherwise picks
    the first IM entry.
    """
    state_upper = state.upper() if state else ""
    for attempt in range(1, max_attempts + 1):
        try:
            _expand_policy_tree(page)
        except Exception as exc:
            _log.debug("IM: tree-expand pre-step failed: %s", exc)

        clicked = page.evaluate(
            """(want_state) => {
                let target = null;
                for (const el of document.querySelectorAll('[data-automation-id*="level-3"]')) {
                    const txt = (el.textContent || '').trim();
                    if (!/inland marine|equipment/i.test(txt)) continue;
                    const aid = el.getAttribute('data-automation-id') || '';
                    if (want_state) {
                        if (aid.endsWith(`;${want_state} level-3`)) { target = el; break; }
                    } else {
                        target = el; break;
                    }
                }
                if (!target) return false;
                target.scrollIntoView({block: 'center'});
                target.click();
                return true;
            }""",
            state_upper,
        )
        if clicked:
            page.wait_for_timeout(_NAV_WAIT_MS)
            _log.debug("Clicked IM sidebar parent (attempt %d/%d)", attempt, max_attempts)
            return True
        _log.warning("IM sidebar entry not found — attempt %d/%d; waiting 2 s", attempt, max_attempts)
        page.wait_for_timeout(2_000)

    available = list_available_im_lines(page)
    if available:
        _log.error("IM sidebar entry not found. Available IM lines: %s", available)
    else:
        _log.error("No IM lines on this submission — create one in EPIC first.")
    return False


def _nav_im_section(
    page: Page,
    section_key: str,
    screen_code: str,
    *,
    vlvw_name: str | None = None,
) -> bool:
    """Navigate to an IM (EquipmentFloater) sub-section.

    Readiness model (revised 2026-05-26 from DOM-dump diagnostics):

      EPIC's SPA performs the section swap silently — no spinner, no
      aria-busy ever fires. The previous screen's DOM stays mounted for
      several seconds, so polling on loading indicators returns "clean"
      while the page is actually mid-transition. The two reliable signals
      are:

        1. The new screen code appears in body text. The previous screen's
           code (e.g. ``CHM-EFCOVDED``) is still present at 500 ms after
           the click, so ``text.includes(new_code)`` going True is the
           transition moment.
        2. The section's grid Add button mounts in the DOM, visible and
           enabled. For sections that don't have an Add button (e.g.
           Coverage/Deductible) pass ``vlvw_name=None`` to skip this
           check.

    Timeout extended to 15 s — real-world transitions were observed
    completing somewhere between 0.5 s and 10 s.

    Includes aggressive modal cleanup before nav — EPIC's validation modals
    (``[data-test="common-message-modal"]`` + ``<message-box>``) cascade from
    incomplete rows in prior sections, and intercept all sidebar clicks until
    explicitly dismissed.
    """
    _aggressive_cleanup(page)
    sidebar_sel = f'[data-automation-id^="sidebar-button-Policy.EquipmentFloater.{section_key}"]'
    btn = page.locator(sidebar_sel)
    if not _wait_visible(page, btn, 5_000):
        _log.error("IM sidebar link for %s not found", section_key)
        return False
    btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(_NAV_WAIT_MS)
    # One more cleanup pass — a validation modal may have appeared during nav
    _aggressive_cleanup(page)

    # Signal 1: new screen code in body text. Timeout 15s — observed
    # transitions ran 0.5–10s in the 2026-05-26 dump.
    try:
        page.wait_for_function(
            f"""() => {{
                const text = document.body.innerText;
                if (text.includes("{screen_code}")) return true;
                for (const b of document.querySelectorAll('[class*="statusBar"], [class*="status-bar"], .status-bar')) {{
                    if (b.textContent.includes("{screen_code}")) return true;
                }}
                return document.querySelectorAll('[data-automation-id="{screen_code}"]').length > 0;
            }}""",
            timeout=15_000,
        )
        _log.debug("Navigated to EquipmentFloater.%s (%s screen code present)",
                   section_key, screen_code)
    except PlaywrightTimeout:
        _log.error(
            "EquipmentFloater.%s: %s did not appear within 15 s — section "
            "did not mount, refusing to proceed",
            section_key, screen_code,
        )
        return False

    # Signal 2: grid Add button mounted + interactable. Skip for sections
    # that have no Add button (Coverage/Deductible).
    if vlvw_name:
        if not wait_for_grid_add_ready(page, vlvw_name, timeout_ms=15_000):
            _log.error(
                "EquipmentFloater.%s: %s_add never became ready within 15 s "
                "(screen code present but grid not interactable)",
                section_key, vlvw_name,
            )
            return False
        _log.debug(
            "EquipmentFloater.%s: %s_add ready", section_key, vlvw_name,
        )
    return True


# ── Coverage / Deductible ────────────────────────────────────────────────────

def _sum_scheduled_amounts(items: list) -> int:
    """Sum ``amt_insurance`` across all scheduled items.

    Each ``amt_insurance`` may be formatted as ``"$364,695"``, ``"364,695"``,
    ``"364695"``, or ``"1234.50"``. Strips currency formatting and tolerates
    parse failures (skips that row). Returns 0 when no numeric values found.
    """
    total = 0
    for it in items or []:
        raw = (getattr(it, "amt_insurance", "") or "").strip()
        if not raw:
            continue
        numeric = _strip_currency(raw)
        if not numeric:
            continue
        try:
            total += int(float(numeric))
        except ValueError:
            continue
    return total


def _fill_coverage_deductible(page: Page, setup: InlandMarineSetup) -> None:
    """Fill the IM Coverage/Deductible screen (CHM-EFCOVDED).

    Rewritten 2026-05-26 to use ``_page_health`` primitives. Every action
    is wrapped in :func:`safe_action`, every post-condition is verified
    before we advance. The prior version silently progressed through soft
    validation errors and corrupted EPIC's state — that is what this
    function exists to prevent.

    Three independent radio groups must ALL have a non-None selection
    whenever any deductible / type field is filled. Otherwise downstream
    grids' Add buttons are silently disabled:

      Group 1 (amount):     Scheduled amount  ← when ``scheduled_items``
                                                exist; fills
                                                streTotalScheduledAmount
                                                with the SUM of all item
                                                amounts.
      Group 2 (perils):     All risks         ← always (IGA policy)
      Group 3 (cost basis): Replacement cost  ← default

    Group 3's Type+Deductible tail only fires when
    ``setup.acv_replacement_cost_deductible`` is non-empty.
    """
    has_sched      = bool(setup.scheduled_items)
    has_deductible = bool(setup.acv_replacement_cost_deductible)
    if not has_sched and not has_deductible and not setup.total_scheduled_amount:
        _log.info("Coverage/Deductible: no data to fill — skipping")
        return

    if not _nav_im_section(page, "CoverageDeductible", "CHM-EFCOVDED"):
        _log.error("Coverage/Deductible: nav failed — aborting section")
        return

    _log.info("=== Coverage / Deductible (safe_action mode) ===")

    # Confirm we're on the right EPIC screen before touching anything.
    # Raises EPICHardError on miss, which terminates the run cleanly.
    assert_screen_code(page, "CHM-EFCOVDED", timeout_ms=8_000)
    wait_for_loading_clear(page, timeout_ms=10_000)
    # Extra settle window. EPIC's SPA renders the screen code in the
    # footer before the form panel finishes binding React state to the
    # radio inputs — clicking too soon races the React reconcile and
    # the radio can flip back to unchecked after our click. 1.5 s is
    # empirically enough on a warm browser without adding noticeable
    # latency to a cold one.
    page.wait_for_timeout(1_500)

    # Any error already on the screen at entry should stop us. assert_clean
    # is called first so we can log it explicitly (safe_action would also
    # catch it, but the entry-state diagnostic is worth a separate line).
    entry_state = assert_clean(page)
    if entry_state.is_hard_error:
        raise EPICHardError(entry_state)
    if entry_state.is_soft_error:
        _log.warning(
            "Coverage/Deductible: page already in soft-error state on entry "
            "— %s :: %s",
            entry_state.summary, entry_state.detail[:200],
        )
        # Let safe_action's pre-check handle the operator halt on first action.

    # ── Group 1: schedule amount ─────────────────────────────────────────────
    sched_total = _sum_scheduled_amounts(setup.scheduled_items)
    if sched_total > 0:
        with safe_action(page, context="IM Coverage: click rbtnScheduled"):
            _click_radio(page, "rbtnScheduled")
        if not verify_radio_checked(page, "rbtnScheduled", timeout_ms=2_000):
            _log.error(
                "Group 1: rbtnScheduled never registered as checked — "
                "halting before fill"
            )
            raise EntryCancelled(
                "IM Coverage: Group 1 (Scheduled amount) radio failed to stick"
            )

        sched_str = str(sched_total)
        with safe_action(
            page,
            context=f"IM Coverage: fill streTotalScheduledAmount={sched_str}",
        ):
            _keyboard_fill(
                page, "streTotalScheduledAmount__textField", sched_str,
            )
        # EPIC reformats the value (5000 -> 5,000); strip commas for compare.
        norm = lambda s: (s or "").replace(",", "").replace("$", "").strip()
        if not verify_input_value(
            page, "#streTotalScheduledAmount__textField", sched_str,
            normalize=norm, timeout_ms=2_500,
        ):
            _log.error(
                "Group 1: streTotalScheduledAmount value did not stick "
                "(wanted %s)", sched_str,
            )
            raise EntryCancelled(
                f"IM Coverage: total scheduled amount {sched_str} did not "
                "save in the input"
            )
        _log.info(
            "Group 1 (amount) = Scheduled amount; total = %d (sum of %d item(s))",
            sched_total, len(setup.scheduled_items or []),
        )
    else:
        _log.debug(
            "Group 1 (amount): no numeric scheduled-item amounts; leaving at None"
        )

    # ── Group 2: perils — always All Risks per IGA policy ───────────────────
    with safe_action(page, context="IM Coverage: click rbtnAllRisks"):
        _click_radio(page, "rbtnAllRisks")
    if not verify_radio_checked(page, "rbtnAllRisks", timeout_ms=2_000):
        _log.error("Group 2: rbtnAllRisks never registered as checked")
        raise EntryCancelled(
            "IM Coverage: Group 2 (All risks) radio failed to stick"
        )
    _log.info("Group 2 (perils) = All risks")

    # ── Group 3: cost basis + type + deductible ─────────────────────────────
    with safe_action(page, context="IM Coverage: click rbtnReplacementCost"):
        _click_radio(page, "rbtnReplacementCost")
    if not verify_radio_checked(page, "rbtnReplacementCost", timeout_ms=2_000):
        _log.error(
            "Group 3: rbtnReplacementCost never registered as checked — "
            "downstream Adds would be silently disabled, so halting now"
        )
        raise EntryCancelled(
            "IM Coverage: Group 3 (Replacement cost) radio failed to stick"
        )
    _log.info("Group 3 (cost basis) = Replacement cost")

    if has_deductible:
        ded_raw    = (setup.acv_replacement_cost_deductible or "").strip()
        is_percent = ded_raw.endswith("%")
        ded_type   = "P" if is_percent else "FL"
        ded_val    = (
            ded_raw.rstrip("%").strip() if is_percent
            else _strip_currency(ded_raw)
        )

        with safe_action(
            page,
            context=f"IM Coverage: combo cboACVReplacementCostDedType={ded_type}",
        ):
            _fill_react_combo(page, "cboACVReplacementCostDedType", ded_type)

        with safe_action(
            page,
            context=f"IM Coverage: fill streACVReplacementCostDeductible={ded_val}",
        ):
            _keyboard_fill(
                page, "streACVReplacementCostDeductible__textField", ded_val,
            )
        norm = lambda s: (s or "").replace(",", "").replace("$", "").replace("%", "").strip()
        if not verify_input_value(
            page, "#streACVReplacementCostDeductible__textField", ded_val,
            normalize=norm, timeout_ms=2_500,
        ):
            _log.error(
                "Group 3 deductible value did not stick (wanted %s)", ded_val,
            )
            raise EntryCancelled(
                f"IM Coverage: deductible {ded_val} did not save in the input"
            )

    # Final commit + clean-page assertion. Tab to fire any final blur
    # handlers, then confirm EPIC is happy before we leave.
    page.keyboard.press("Tab")
    page.wait_for_timeout(400)
    wait_for_loading_clear(page, timeout_ms=10_000)
    final = assert_clean(page)
    if final.is_hard_error:
        raise EPICHardError(final)
    if final.is_soft_error:
        _log.error(
            "Coverage/Deductible: section ended with a soft error still on "
            "screen — %s :: %s",
            final.summary, final.detail[:200],
        )
        raise EntryCancelled(
            f"IM Coverage: section finished with unresolved error: {final.summary}"
        )
    _log.info("Coverage/Deductible: filled cleanly")


def _click_radio(page: Page, radio_value: str) -> bool:
    """Click an EPIC radio button by its `value` attribute and verify it stuck.

    EPIC's React radios on this screen render as `<input type="radio" value="rbtnX">`
    inside a wrapper. The radio handler can race with adjacent component renders;
    a single click may not register. We click via Playwright (which dispatches
    a real DOM event React will hear), then poll `.checked` and retry up to 3
    times if it didn't take.

    Idempotent: if the radio is already checked when we arrive (e.g.,
    Coverage/Deductible was filled by a prior test run), skip the click
    entirely. Re-clicking an already-checked React radio can momentarily
    flip its state during the React reconcile, which trips
    ``verify_radio_checked`` immediately after.
    """
    sel = f'input[type="radio"][value="{radio_value}"]'
    try:
        if page.evaluate(
            f"() => document.querySelector('{sel}')?.checked === true"
        ):
            _log.debug("radio %s already checked — skipping click", radio_value)
            return True
    except Exception:
        pass
    for attempt in range(1, 4):
        try:
            loc = page.locator(sel).first
            if not loc.count():
                _log.warning("radio %s: element not found", radio_value)
                return False
            loc.click(timeout=2_000, force=True)
        except Exception as exc:
            _log.debug("radio %s click(%d) exception: %s", radio_value, attempt, exc)
        # Poll for up to 1s for the radio to register
        for _ in range(10):
            page.wait_for_timeout(100)
            try:
                if page.evaluate(
                    f"() => document.querySelector('{sel}')?.checked === true"
                ):
                    _log.debug("radio %s checked after attempt %d", radio_value, attempt)
                    return True
            except Exception:
                pass
        _log.debug("radio %s not checked after attempt %d — retrying", radio_value, attempt)
    _log.warning("radio %s did not stick after 3 attempts", radio_value)
    return False


def _keyboard_fill(page: Page, field_id: str, value: str) -> None:
    """Fill a React `__textField` input using real keystrokes so EPIC marks it dirty."""
    if not value:
        _log.debug("kb_fill #%s: skipped (blank)", field_id)
        return
    try:
        inp = page.locator(f'#{field_id}')
        if not inp.count():
            _log.warning("kb_fill #%s: element not found", field_id)
            return
        inp.first.click(timeout=2_000)
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.keyboard.type(value, delay=25)
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)
        _log.debug("kb_fill #%s = %r OK", field_id, value)
    except Exception as exc:
        _log.warning("kb_fill #%s = %r FAILED: %s", field_id, value, exc)


def _strip_currency(value: str) -> str:
    """Strip $ and commas from a currency string."""
    return (value or "").replace("$", "").replace(",", "").strip()


# ── Scheduled Equipment ──────────────────────────────────────────────────────

def _ensure_unique_item_numbers(items: list) -> None:
    """Renumber scheduled-equipment ``item_number`` values so they're unique.

    EPIC enforces uniqueness on ``inteItemNumber`` within the schedule. A
    duplicate (or blank) makes the row invalid and silently blocks the next
    Add click — so the grid stops growing and the loop ends up skipping
    half the items (saw 8 of 51 dropped in the 2026-05-22 17:54 run).

    Extraction frequently produces duplicates (the PDF reuses unit IDs
    across pages, or numbers are illegible). We walk the list in order
    and:
      - Keep any non-empty, not-already-used value verbatim.
      - Replace duplicates and blanks with the next unused positive integer
        (1, 2, 3, ...).  Logs each substitution so the operator can audit
        in the run log.

    Mutates *items* in place.
    """
    used: set[str] = set()
    next_free = 1
    for it in items:
        raw = (it.item_number or "").strip()
        if raw and raw not in used:
            used.add(raw)
            continue
        while str(next_free) in used:
            next_free += 1
        new_num = str(next_free)
        used.add(new_num)
        if raw:
            _log.info(
                "Scheduled item: number %r already used — renumbering to %r",
                raw, new_num,
            )
        else:
            _log.info("Scheduled item: blank item number — assigning %r", new_num)
        it.item_number = new_num
        next_free += 1


_MAX_CONSECUTIVE_ADD_FAILURES = 3


def _fill_im_field(
    page: Page,
    field_id: str,
    value: str,
    row_idx: int,
    label: str,
    *,
    normalize=None,
    section: str = "IM",
    lenient: bool = False,
) -> None:
    """Per-field write + verify for any IM grid row.

    ``field_id`` is the ``id`` attribute of the EPIC input (including any
    ``__textField`` suffix). ``section`` is a short tag used in log lines
    (e.g. ``"Sched"``, ``"Unsched"``, ``"AI"``) so the operator can trace
    which subsection a fill belonged to. Wrapped in :func:`safe_action`
    so a soft or hard error surfaced by the fill halts the run instead
    of cascading into a corrupt row.

    Verification mode:
      * ``lenient=False`` (default): strict — verify the field's value
        matches *value* (after optional *normalize*). Used for fields
        where EPIC echoes input verbatim. Mismatch raises
        :class:`EntryCancelled`.
      * ``lenient=True``: only verify the field is non-empty after the
        fill. Used for fields where EPIC actively normalizes input
        (e.g. ``adePrimary-streetLine`` running USPS street-name
        canonicalization on blur, abbreviating "Place" to "Pl" and
        dropping non-USPS suffixes). The actual stored value is logged
        so the operator can audit.

    Blank values are skipped (logged at DEBUG).
    """
    if not value:
        _log.debug("%s #%d: %s blank — skipping", section, row_idx, label)
        return
    _log.debug("%s #%d: filling %s (#%s) = %r", section, row_idx, label, field_id, value)
    with safe_action(page, context=f"{section} #{row_idx}: fill {label}={value!r}"):
        _keyboard_fill(page, field_id, value)

    if lenient:
        # Only require the field is non-empty. Log the actual stored
        # value so EPIC's normalization is visible to the operator.
        try:
            actual = page.evaluate(
                "(sel) => { const e = document.querySelector(sel); "
                "return e ? (e.value ?? '') : ''; }",
                f"#{field_id}",
            )
        except Exception:  # noqa: BLE001
            actual = ""
        if not str(actual or "").strip():
            _log.error(
                "%s #%d: %s left field blank (wanted %r)",
                section, row_idx, label, value,
            )
            raise EntryCancelled(
                f"{section} #{row_idx}: {label}={value!r} did not save"
            )
        if str(actual).strip() != value.strip():
            _log.info(
                "%s #%d: %s — EPIC normalized %r → %r (lenient, accepted)",
                section, row_idx, label, value, actual,
            )
        else:
            _log.debug("%s #%d: %s verified OK", section, row_idx, label)
        return

    if not verify_input_value(
        page, f"#{field_id}", value,
        normalize=normalize, timeout_ms=2_000,
    ):
        _log.error(
            "%s #%d: %s value did not stick (wanted %r)",
            section, row_idx, label, value,
        )
        raise EntryCancelled(
            f"{section} #{row_idx}: {label}={value!r} did not save"
        )
    _log.debug("%s #%d: %s verified OK", section, row_idx, label)


def _fill_scheduled_equipment(page: Page, items: list) -> None:
    """Add scheduled equipment rows on CHM-EFSCHEQU.

    Rewritten 2026-05-26 to use ``_page_health`` primitives and EPIC's
    natural auto-numbering:

      EPIC auto-increments the item number on each Add click (1, 2, 3,
      ...). Items have already been deduplicated upstream — both the GUI
      (``section_forms.InlandMarineForm._refresh_tables``) and the
      EPIC-side ``_ensure_unique_item_numbers`` safety net run the same
      algorithm — so the normal case is a contiguous integer sequence
      and we do not type item numbers at all.

      When a gap exists (e.g. ``[1, 2, 4, 5]`` — missing 3) or the
      sequence starts at something other than 1, the desired number
      won't match EPIC's auto-fill on that row and we type over it.
      The next row's expectation is then ``desired + 1``.

    Confirmed live 2026-05-21:
      Grid:    vlvwScheduledEquipment (React)
      Add:     [data-test="vlvwScheduledEquipment_add"]
      Fields:
        inteItemNumber__textField   (auto-filled by EPIC; only typed on gap)
        streDescription             (no __textField suffix)
        streSerialNumber            (labelled "ID/Serial number")
        streManufacturer
        streModel
        inteModelYear__textField
        streAmtInsurance__textField (currency; verified with $/comma strip)
        streType — INTENTIONALLY SKIPPED. state.json's value (e.g.
          "Contractors Equipment") is a category, not the per-item Type
          EPIC expects. Description already identifies the item.
      No per-item deductible field — policy-level only.
    """
    if not items:
        _log.info("Scheduled Equipment: no items — skipping")
        return
    if not _nav_im_section(
        page, "ScheduledEquipment", "CHM-EFSCHEQU",
        vlvw_name="vlvwScheduledEquipment",
    ):
        _log.error("Scheduled Equipment: nav failed — aborting section")
        return

    # Safety net — the GUI dedupes on load (see
    # gui/section_forms.InlandMarineForm._refresh_tables), but if anything
    # else mutated state.json between save and run we want a final pass.
    _ensure_unique_item_numbers(items)

    _log.info("=== Scheduled Equipment: %d item(s) ===", len(items))
    assert_screen_code(page, "CHM-EFSCHEQU", timeout_ms=8_000)

    add_btn_sel = '[data-test="vlvwScheduledEquipment_add"]'
    norm_currency = lambda s: (s or "").replace(",", "").replace("$", "").strip()

    expected_next = 1   # EPIC's predicted auto-fill for the next Add
    consecutive_failures = 0

    for i, it in enumerate(items, 1):
        check_cancel()

        # Parse desired item number; fall back to ``expected_next`` if
        # invalid (post-dedupe this should never trigger, but defensive).
        raw_num = (it.item_number or "").strip()
        try:
            desired_num = int(raw_num)
        except ValueError:
            _log.warning(
                "Sched #%d: item_number %r is not an integer — falling back to %d",
                i, raw_num, expected_next,
            )
            desired_num = expected_next

        _log.info(
            "--- Row %d/%d: desired item#=%d, EPIC auto-fill will be %d, "
            "%s | %s | %s | %s | $%s",
            i, len(items), desired_num, expected_next,
            it.manufacturer or "-", it.model or "-",
            it.model_year or "-", (it.description or "-")[:40],
            norm_currency(it.amt_insurance) or "0",
        )

        _dismiss_modal(page)
        rows_before = _grid_row_count(page, "vlvwScheduledEquipment")
        _log.debug("Sched #%d: rows_before=%d", i, rows_before)

        # Settle BEFORE Add. EPIC's React DataGrid finishes reconciling
        # from the previous row's last field-fill (or from the initial
        # nav) for ~500-800 ms; clicking Add during that window
        # registers visually but the handler is a no-op — the row
        # never grows. Observed reproducibly at iteration 1 (post-nav)
        # and at iterations 19+ (post-prior-row-fill) in the 2026-05-26
        # 14:26 e2e run.
        page.wait_for_timeout(750)

        with safe_action(page, context=f"Sched #{i}: click Add"):
            page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)

        grew = _wait_grid_grew(
            page, "vlvwScheduledEquipment", rows_before, timeout_ms=3_000,
        )
        if not grew:
            # Retry the SAME row once with a longer settle. If even that
            # fails, halt the whole run via EntryCancelled — silently
            # skipping rows here misaligns every downstream row in EPIC
            # (EPIC's auto-fill counter doesn't increment when an Add
            # is ignored, so subsequent items get the wrong item#).
            _log.warning(
                "Sched #%d: row did not grow after Add — retrying with longer settle",
                i,
            )
            page.wait_for_timeout(2_500)
            try:
                page.locator(add_btn_sel).first.scroll_into_view_if_needed(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            with safe_action(page, context=f"Sched #{i}: RETRY Add"):
                page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)
            grew = _wait_grid_grew(
                page, "vlvwScheduledEquipment", rows_before, timeout_ms=5_000,
            )
            if not grew:
                _log.error(
                    "Sched #%d: row still did not grow after retry — "
                    "halting run to avoid downstream misalignment. "
                    "EPIC has %d rows but state says we should have %d.",
                    i, rows_before, i,
                )
                raise EntryCancelled(
                    f"Sched #{i}: EPIC rejected Add (twice); halting before "
                    "subsequent items get misaligned item numbers"
                )
            _log.info("Sched #%d: retry succeeded", i)
        consecutive_failures = 0
        _log.debug("Sched #%d: row appeared (count %d → %d)",
                   i, rows_before, rows_before + 1)

        # Settle window after Add — EPIC's React reconcile needs a beat
        # to bind the new row's form inputs. Without this, the first
        # field-fill of the new row sometimes lands on the PREVIOUS
        # row's inputs that are still focused, which produces blank
        # rows downstream (observed 2026-05-26 in items 20-21 after
        # item 19's add).
        page.wait_for_timeout(1_000)

        # Wait for the new row's item-number input to mount before typing.
        if not wait_for(
            page,
            "document.querySelector('#inteItemNumber__textField') !== null",
            timeout_ms=3_000, poll_ms=100,
            label=f"sched row {i} inteItemNumber present",
        ):
            _log.error("Sched #%d: inteItemNumber__textField never mounted", i)
            raise EntryCancelled(
                f"Sched #{i}: new row's item-number input never appeared"
            )

        # Item number — only type when EPIC's auto-fill won't already match.
        if desired_num != expected_next:
            _log.info(
                "Sched #%d: GAP — EPIC would auto-fill %d, we want %d. Overriding.",
                i, expected_next, desired_num,
            )
            with safe_action(
                page, context=f"Sched #{i}: override item#={desired_num}",
            ):
                _keyboard_fill(page, "inteItemNumber__textField", str(desired_num))
            if not verify_input_value(
                page, "#inteItemNumber__textField", str(desired_num),
                timeout_ms=2_000,
            ):
                _log.error("Sched #%d: item# override %d did not stick", i, desired_num)
                raise EntryCancelled(
                    f"Sched #{i}: item number {desired_num} did not save"
                )
            _log.debug("Sched #%d: item# override verified", i)
        else:
            _log.debug(
                "Sched #%d: EPIC auto-fill (%d) matches desired — no typing",
                i, expected_next,
            )

        # Remaining fields. Free-text fields (description, serial,
        # manufacturer, model) all use lenient mode: EPIC enforces
        # silent maxlength truncation on each (e.g., streSerialNumber
        # caps at 17 — source "083-T-45MCX4Y4-04083" → "083-T-45MCX4Y4-04").
        # Lenient mode accepts whatever EPIC stores; the diff is logged
        # so the operator can audit. Strict verify stays on the
        # numeric fields below (year, amount) where exact match matters.
        _fill_im_field(page, "streDescription",          it.description,   i, "description",  section="Sched", lenient=True)
        _fill_im_field(page, "streSerialNumber",         it.serial_number, i, "serial",       section="Sched", lenient=True)
        _fill_im_field(page, "streManufacturer",         it.manufacturer,  i, "manufacturer", section="Sched", lenient=True)
        _fill_im_field(page, "streModel",                it.model,         i, "model",        section="Sched", lenient=True)
        _fill_im_field(page, "inteModelYear__textField", it.model_year,    i, "year",         section="Sched")
        _fill_im_field(
            page, "streAmtInsurance__textField",
            norm_currency(it.amt_insurance), i, "amount",
            normalize=norm_currency, section="Sched",
        )

        # Advance the EPIC counter prediction.
        expected_next = desired_num + 1
        page.wait_for_timeout(300)
        _log.debug("Sched #%d: row complete; expected_next=%d", i, expected_next)

    _log.info("=== Scheduled Equipment: complete (%d row(s) processed) ===", len(items))


# ── Unscheduled Equipment ────────────────────────────────────────────────────

UNSCHED_DESC_MAX_CHARS = 30
AI_REASON_MAX_CHARS    = 30  # streReasonForInt maxlength on CHM-EFADDLIN


def _fill_unscheduled_equipment(page: Page, items: list) -> None:
    """Add unscheduled equipment rows on CHM-EFUNEQIP.

    Rewritten 2026-05-26 to mirror the Scheduled-Equipment rewrite:
      - Every action wrapped in ``safe_action`` so soft / hard errors halt.
      - Description and amount are write-then-``verify_input_value`` to
        catch silent rejections.
      - EPIC caps the Unscheduled description column at 30 characters.
        Anything longer is sent through
        :func:`claude_client.abbreviate_for_epic` so it fits using
        standard insurance shorthand (BPP, RC, ACV, ...). The original
        full text is still shown in the GUI on hover for context.

    Confirmed live 2026-05-21:
      Grid:    vlvwEquipment (React)
      Add:     [data-test="vlvwEquipment_add"]
      Fields:  streDescription, streAmtInsurance__textField
    """
    if not items:
        _log.info("Unscheduled Equipment: no items — skipping")
        return
    if not _nav_im_section(
        page, "UnscheduledEquipment", "CHM-EFUNEQIP",
        vlvw_name="vlvwEquipment",
    ):
        _log.error("Unscheduled Equipment: nav failed — aborting section")
        return

    _log.info("=== Unscheduled Equipment: %d item(s) ===", len(items))
    assert_screen_code(page, "CHM-EFUNEQIP", timeout_ms=8_000)

    # Import here so the step file can still be imported without a
    # working anthropic SDK (test scripts that only exercise navigation,
    # etc.). At entry time the runtime always has the SDK available.
    from iga_marketing_master_2.claude_client import abbreviate_for_epic

    add_btn_sel = '[data-test="vlvwEquipment_add"]'
    norm_currency = lambda s: (s or "").replace(",", "").replace("$", "").strip()
    consecutive_failures = 0

    for i, it in enumerate(items, 1):
        check_cancel()

        raw_desc = (it.description or "").strip()
        # Prefer the cached short form from state.json — the GUI
        # populates it via abbreviate_for_epic on refresh and persists
        # it on the description_short field (see Field Map +
        # InlandMarineForm._refresh_tables). Falls back to an at-entry
        # API call only when state has nothing cached (legacy
        # extractions or unedited brand-new clients).
        cached_short = (it.description_short or "").strip()
        if cached_short:
            short_desc = cached_short[:UNSCHED_DESC_MAX_CHARS]
            if cached_short != raw_desc:
                _log.debug(
                    "Unsched #%d: using cached description_short %r (full was %d chars)",
                    i, short_desc, len(raw_desc),
                )
        elif len(raw_desc) > UNSCHED_DESC_MAX_CHARS:
            short_desc = abbreviate_for_epic(
                raw_desc, max_chars=UNSCHED_DESC_MAX_CHARS,
            )
            _log.info(
                "Unsched #%d: no cached short form — abbreviating on the fly "
                "(%d → %d chars): %r → %r",
                i, len(raw_desc), len(short_desc), raw_desc, short_desc,
            )
        else:
            short_desc = raw_desc

        amt = norm_currency(it.amt_insurance)
        _log.info(
            "--- Row %d/%d: desc=%r ($%s)",
            i, len(items), short_desc, amt or "0",
        )

        _dismiss_modal(page)
        rows_before = _grid_row_count(page, "vlvwEquipment")
        _log.debug("Unsched #%d: rows_before=%d", i, rows_before)

        # Pre-Add settle — see comment in _fill_scheduled_equipment for
        # why this is necessary (EPIC's React reconcile race).
        page.wait_for_timeout(750)

        with safe_action(page, context=f"Unsched #{i}: click Add"):
            page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)

        grew = _wait_grid_grew(
            page, "vlvwEquipment", rows_before, timeout_ms=3_000,
        )
        if not grew:
            # Retry the same row with a longer settle; halt the entire
            # run if it still fails (rather than silently skipping).
            _log.warning(
                "Unsched #%d: row did not grow after Add — retrying with longer settle",
                i,
            )
            page.wait_for_timeout(2_500)
            with safe_action(page, context=f"Unsched #{i}: RETRY Add"):
                page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)
            grew = _wait_grid_grew(
                page, "vlvwEquipment", rows_before, timeout_ms=5_000,
            )
            if not grew:
                _log.error(
                    "Unsched #%d: row still did not grow after retry — halting run.",
                    i,
                )
                raise EntryCancelled(
                    f"Unsched #{i}: EPIC rejected Add (twice); halting"
                )
            _log.info("Unsched #%d: retry succeeded", i)
        consecutive_failures = 0
        _log.debug(
            "Unsched #%d: row appeared (count %d → %d)",
            i, rows_before, rows_before + 1,
        )

        if not wait_for(
            page,
            "document.querySelector('#streDescription') !== null",
            timeout_ms=3_000, poll_ms=100,
            label=f"unsched row {i} streDescription present",
        ):
            _log.error("Unsched #%d: streDescription never mounted", i)
            raise EntryCancelled(
                f"Unsched #{i}: new row's description input never appeared"
            )

        _fill_im_field(page, "streDescription", short_desc, i, "description", section="Unsched")
        _fill_im_field(
            page, "streAmtInsurance__textField", amt, i, "amount",
            normalize=norm_currency, section="Unsched",
        )

        page.wait_for_timeout(300)
        _log.debug("Unsched #%d: row complete", i)

    _log.info("=== Unscheduled Equipment: complete (%d row(s) processed) ===", len(items))


# ── Additional Interests ─────────────────────────────────────────────────────

def _address_tail_after_normalization(original: str, epic_kept: str) -> str:
    """Compute the unit/suite suffix that EPIC stripped from the street line.

    EPIC runs USPS-style canonicalization on ``adePrimary-streetLine``
    when the field blurs: abbreviates "Place" → "Pl", "Street" → "St",
    etc., and drops any trailing tokens it doesn't recognize as USPS
    suffixes. For an input like "505 Summer Place UTT 1048C", EPIC
    keeps "505 Summer Pl" and silently drops "UTT 1048C" — that suffix
    is what belongs on address line 2.

    Strategy: tokenize both strings on whitespace. The first
    ``len(epic_kept_tokens)`` tokens of *original* are assumed to be
    what EPIC's kept value represents (possibly abbreviated); everything
    after is the tail.

    Returns the tail as a single space-joined string, or ``""`` when
    the original has no extra tokens (e.g. when EPIC kept the full
    address or only abbreviated in place).
    """
    o_tokens = (original or "").split()
    e_tokens = (epic_kept or "").split()
    if len(o_tokens) <= len(e_tokens):
        return ""
    return " ".join(o_tokens[len(e_tokens):]).strip()


def _read_input_value(page: Page, selector: str) -> str:
    """One-shot read of an input's current value (returns ``""`` on failure)."""
    try:
        v = page.evaluate(
            "(sel) => { const e = document.querySelector(sel); "
            "return e ? (e.value ?? '') : ''; }",
            selector,
        )
        return str(v or "")
    except Exception:  # noqa: BLE001
        return ""


def _fill_im_additional_interests(page: Page, interests: list) -> None:
    """Add Additional Interest rows on CHM-EFADDLIN.

    Rewritten 2026-05-26 to use ``_page_health`` primitives. Mirrors the
    Scheduled / Unscheduled pattern: every action wrapped in
    :func:`safe_action`, every field write verified via
    :func:`verify_input_value`, consecutive-add failures bail out instead
    of cascading.

    Per the 2026-05-26 AI Add-click DOM dump, the per-row inputs all
    mount within ~1 s of clicking Add:

      streName             — required (interest holder's name)
      cboInterest          — interest-type combo (Loss Payee, Lienholder,
                             Additional Insured, Mortgagee, ...).
                             Resolved via the shared
                             :func:`_resolve_ai_interest` table.
      adePrimary-*         — address widget. We try the SmartyStreets-style
                             validated lookup first; on a miss (e.g.
                             addresses with "Ste 100" / "PO Box" suffixes
                             USPS rejects) we fall back to per-field fills
                             on adePrimary-streetLine / -city / -zipCode
                             + the state combo.
      streReasonForInt     — free-text textarea (optional per spec).

    Inputs NOT filled (no source data in IMAdditionalInterestSpec):
      streLookup, streInterestOther, streReferenceNumber, streEmail,
      inteItemNumber__textField, inteRank__textField,
      inteLienAmount__textField, phePrimary__phone, pheFax__phone,
      chkCertificateRequired, chkPolicy, chkSendBill.

    A row whose name fill fails is deleted via
    :func:`_delete_pending_row` so a half-built row doesn't block nav
    to the next section.
    """
    if not interests:
        _log.info("Additional Interests: no items — skipping")
        return
    if not _nav_im_section(
        page, "AdditionalInterest", "CHM-EFADDLIN",
        vlvw_name="vlvwInterest",
    ):
        _log.error("Additional Interests: nav failed — aborting section")
        return

    _log.info("=== Additional Interests: %d item(s) ===", len(interests))
    assert_screen_code(page, "CHM-EFADDLIN", timeout_ms=8_000)

    add_btn_sel = '[data-test="vlvwInterest_add"]'
    consecutive_failures = 0

    for i, ai in enumerate(interests, 1):
        check_cancel()
        name = (ai.name or "").strip()
        if not name:
            _log.warning("AI #%d: blank name — skipping (EPIC requires name)", i)
            continue

        _log.info(
            "--- Row %d/%d: name=%r interest=%r addr=%r",
            i, len(interests), name, ai.interest_type or "-",
            (ai.address_line_1 or "-")[:60],
        )

        _dismiss_modal(page)
        rows_before = _grid_row_count(page, "vlvwInterest")
        _log.debug("AI #%d: rows_before=%d", i, rows_before)

        # Pre-Add settle (see _fill_scheduled_equipment for context).
        page.wait_for_timeout(750)

        with safe_action(page, context=f"AI #{i}: click Add"):
            page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)

        grew = _wait_grid_grew(
            page, "vlvwInterest", rows_before, timeout_ms=3_000,
        )
        if not grew:
            _log.warning(
                "AI #%d: row did not grow after Add — retrying with longer settle",
                i,
            )
            page.wait_for_timeout(2_500)
            with safe_action(page, context=f"AI #{i}: RETRY Add"):
                page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)
            grew = _wait_grid_grew(
                page, "vlvwInterest", rows_before, timeout_ms=5_000,
            )
            if not grew:
                _log.error(
                    "AI #%d: row still did not grow after retry — halting run.",
                    i,
                )
                raise EntryCancelled(
                    f"AI #{i}: EPIC rejected Add (twice); halting"
                )
            _log.info("AI #%d: retry succeeded", i)
        consecutive_failures = 0
        _log.debug(
            "AI #%d: row appeared (count %d → %d)",
            i, rows_before, rows_before + 1,
        )

        # Go straight to filling Name. Playwright's locator.click() in
        # _keyboard_fill auto-waits up to 2 s for the input to be visible
        # and enabled before clicking, so an explicit pre-poll for
        # #streName readiness is redundant. The 2026-05-26 Add-click DOM
        # dump showed the input mounted enabled at +1 s. If EPIC ever
        # delays past 2 s, verify_input_value below catches the empty
        # value and raises EntryCancelled — no silent skip.
        # Name (required).
        _fill_im_field(page, "streName", name, i, "name", section="AI")

        # Subject reference — which Scheduled item this AI applies to.
        # Blank means policy-level. EPIC's input is integer-only; we send
        # the value verbatim and trust the GUI dedupe kept the original
        # numbering intact (see section_forms.InlandMarineForm._refresh_tables).
        if ai.item_number:
            _fill_im_field(
                page, "inteItemNumber__textField", ai.item_number,
                i, "item#", section="AI",
            )

        # Interest type — resolve via shared directory.
        if ai.interest_type:
            code = _resolve_ai_interest(ai.interest_type)
            if code:
                with safe_action(
                    page, context=f"AI #{i}: combo cboInterest={code}",
                ):
                    _fill_react_combo(page, "cboInterest", code)
                _log.debug("AI #%d: cboInterest = %r (resolved from %r)", i, code, ai.interest_type)
            else:
                _log.warning(
                    "AI #%d: interest_type %r did not resolve — leaving combo blank",
                    i, ai.interest_type,
                )

        # Address. Try SmartyStreets-style validated lookup first; fall
        # back to per-field manual fills if the validator returns no
        # suggestions (common for "Ste 100" / "PO Box" addresses).
        if ai.address_line_1:
            street, city, state_code, zip_code = _split_us_address(ai.address_line_1)
            _log.debug(
                "AI #%d address split: street=%r city=%r state=%r zip=%r",
                i, street, city, state_code, zip_code,
            )
            validated_ok = False
            try:
                validated_ok = _fill_validated_address(
                    page, street or ai.address_line_1, ai.address_line_1,
                )
            except Exception as exc:  # noqa: BLE001
                _log.debug("AI #%d: validated address path raised: %s", i, exc)

            if validated_ok:
                _log.debug("AI #%d: address filled via validated lookup", i)
            else:
                _log.info(
                    "AI #%d: validated lookup returned no rows — manual address fill",
                    i,
                )
                # streetLine + city run EPIC's USPS-style normalizer on
                # blur ("Place" → "Pl", non-USPS unit suffixes dropped).
                # Accept whatever EPIC keeps; log the normalization.
                _fill_im_field(page, "adePrimary-streetLine", street,   i, "addr.street", section="AI", lenient=True)

                # If EPIC dropped a unit/suite suffix from the street
                # (e.g. "UTT 1048C"), push it onto address line 2 so the
                # information isn't lost. Token-count diff: anything in
                # the original beyond what EPIC kept is the tail.
                epic_street = _read_input_value(page, "#adePrimary-streetLine")
                tail = _address_tail_after_normalization(street, epic_street)
                if tail:
                    _log.info(
                        "AI #%d: EPIC dropped %r from streetLine — pushing to address2",
                        i, tail,
                    )
                    _fill_im_field(
                        page, "adePrimary-address2", tail, i, "addr.line2",
                        section="AI", lenient=True,
                    )

                _fill_im_field(page, "adePrimary-city",       city,     i, "addr.city",   section="AI", lenient=True)
                _fill_im_field(page, "adePrimary-zipCode",    zip_code, i, "addr.zip",    section="AI")
                if state_code:
                    try:
                        with safe_action(
                            page, context=f"AI #{i}: combo state={state_code}",
                        ):
                            _fill_react_combo(page, "state", state_code)
                    except Exception as exc:  # noqa: BLE001
                        _log.warning(
                            "AI #%d: state combo %r failed: %s",
                            i, state_code, exc,
                        )

        # Reason for interest. EPIC caps this input at maxlength=30
        # (confirmed via the 2026-05-26 Add-click DOM dump). Anything
        # longer is shortened via Claude using the same insurance-
        # shorthand path as Unscheduled descriptions. Full original
        # stays in state.json for audit.
        if ai.reason_for_int:
            raw_reason = ai.reason_for_int.strip()
            if len(raw_reason) > AI_REASON_MAX_CHARS:
                from iga_marketing_master_2.claude_client import abbreviate_for_epic
                short_reason = abbreviate_for_epic(
                    raw_reason, max_chars=AI_REASON_MAX_CHARS,
                )
                _log.info(
                    "AI #%d: reason abbreviated (%d → %d chars): %r → %r",
                    i, len(raw_reason), len(short_reason), raw_reason, short_reason,
                )
            else:
                short_reason = raw_reason
            _fill_im_field(
                page, "streReasonForInt", short_reason, i, "reason",
                section="AI",
            )

        page.wait_for_timeout(300)
        _log.debug("AI #%d: row complete", i)

    _log.info("=== Additional Interests: complete (%d row(s) processed) ===", len(interests))


# ── Additional Coverages ─────────────────────────────────────────────────────

def delete_all_rows_in_grid(
    page: Page,
    vlvw_name: str,
    *,
    max_rows: int = 100,
    required_field_stubs: dict[str, str] | None = None,
) -> int:
    """Iteratively delete every row in an IM React grid. Returns rows deleted.

    Pattern per iteration:
      1. Select the first remaining row (click via the visible row body,
         not the invisible focusable-row marker).
      2. If *required_field_stubs* is provided, stub any blank required
         inputs so EPIC's row-validation doesn't fire on delete (e.g.
         ``{"streCode": "N/A"}`` for AC, where streCode is required and
         a blank value triggers "Required information is missing" —
         which intercepts the delete click as a modal).
      3. Click the grid's delete button.
      4. Wait for the count to drop. If a ``common-message-modal``
         appears (validation alert), click its OK button and retry.
      5. If the count never decreases, abort.

    *required_field_stubs* maps ``input_id → stub_value``. Pass it for
    grids where blank-required-field validation blocks deletion; omit
    for grids where delete works on empty rows.
    """
    initial = _grid_row_count(page, vlvw_name)
    if initial == 0:
        _log.debug("delete_all_rows: %s already empty", vlvw_name)
        return 0
    _log.info("delete_all_rows: %s has %d row(s) — clearing", vlvw_name, initial)

    row_sel = f'[data-test^="{vlvw_name}-focusable-row-"]'
    del_sel = f'[data-test="{vlvw_name}_delete"]'

    deleted = 0
    for attempt in range(max_rows):
        remaining = _grid_row_count(page, vlvw_name)
        if remaining == 0:
            break

        # Select the first remaining row. The focusable-row data-test is
        # a 0×0 invisible focus marker; click its visible parent (the
        # [data-test="row"] div with class DataGrid_module_row).
        try:
            clicked = page.evaluate(
                f"() => {{ const fr = document.querySelector('{row_sel}'); "
                f"if (!fr) return false; "
                f"const parent = fr.closest('[data-test=\"row\"]') || fr.parentElement; "
                f"if (parent) {{ parent.click(); return true; }} "
                f"return false; }}"
            )
            if not clicked:
                _log.debug("delete_all_rows %s: no row to select", vlvw_name)
                break
            page.wait_for_timeout(300)
        except Exception as exc:  # noqa: BLE001
            _log.warning("delete_all_rows %s: row-select failed: %s", vlvw_name, exc)
            break

        # Stub any required fields so EPIC's row-validation lets us delete.
        if required_field_stubs:
            for input_id, stub in required_field_stubs.items():
                try:
                    current = page.evaluate(
                        "(sel) => { const e = document.querySelector(sel); "
                        "return e ? (e.value ?? '') : ''; }",
                        f"#{input_id}",
                    )
                    if not str(current or "").strip():
                        _keyboard_fill(page, input_id, stub)
                        _log.debug(
                            "delete_all_rows %s: stubbed #%s = %r",
                            vlvw_name, input_id, stub,
                        )
                except Exception as exc:  # noqa: BLE001
                    _log.debug(
                        "delete_all_rows %s: stub fill #%s failed: %s",
                        vlvw_name, input_id, exc,
                    )

        # Click delete. If a validation modal appears, OK it and retry.
        try:
            del_btn = page.locator(del_sel).first
            try:
                del_btn.click(timeout=2_500)
            except Exception:
                # Modal blocking? Dismiss any open OK-modal and retry.
                _click_ok_modal(page)
                del_btn.click(timeout=2_500)
            page.wait_for_timeout(500)
            # After delete: an "are you sure" confirmation modal may
            # appear (also OK-only) — click OK to confirm.
            _click_ok_modal(page)
            page.wait_for_timeout(700)
        except Exception as exc:  # noqa: BLE001
            _log.warning("delete_all_rows %s attempt %d failed: %s",
                         vlvw_name, attempt + 1, exc)
            break

        new_count = _grid_row_count(page, vlvw_name)
        if new_count >= remaining:
            _log.warning(
                "delete_all_rows %s: row count did not decrease (%d → %d) — aborting",
                vlvw_name, remaining, new_count,
            )
            break
        deleted += (remaining - new_count)
    _log.info("delete_all_rows: %s cleared %d row(s) (final count=%d)",
              vlvw_name, deleted, _grid_row_count(page, vlvw_name))
    return deleted


def _click_ok_modal(page: Page) -> bool:
    """If an EPIC ``[data-test=\"dialog-ok-btn\"]`` is visible, click it.

    Returns True when a click was issued. Used to acknowledge validation
    errors AND confirm delete prompts (EPIC uses the same single-button
    modal pattern for both). Silent if no modal is up.
    """
    try:
        btn = page.locator('[data-test="dialog-ok-btn"]')
        if btn.count() and btn.first.is_visible(timeout=400):
            btn.first.click(timeout=2_000, force=True)
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _fill_im_additional_coverages(page: Page, coverages: list) -> None:
    """Add Additional Coverage rows on CHM-EFADDCOV.

    Rewritten 2026-05-26 to mirror the Sched / Unsched / AI pattern:
    safe_action around every action, post-write verify, consecutive-Add
    failures abort.

    Per the 2026-05-26 AC inspection dump, the per-row form panel mounts
    these inputs on Add:

      streCode                   — required (EPIC: "Required information
                                   is missing" without it). maxlength=5.
                                   State.json doesn't currently capture
                                   ``code`` (Claude historically confused
                                   it with form_number, so it's excluded
                                   from the GUI — see the comment in
                                   section_forms._add_cov_table). Falls
                                   back to "N/A" when blank.
      streDescription            — the coverage name. maxlength=175.
      streLimit__textField       — each-claim/aggregate limit (currency).
      streDeductible__textField  — currency.
      inteItemNumber__textField  — optional Scheduled-item ref.

    Inputs NOT filled (no source data in IMAdditionalCoverageSpec):
      curePremium__textField, streRemarks.
    """
    if not coverages:
        _log.info("Additional Coverages: no items — skipping")
        return
    if not _nav_im_section(
        page, "AdditionalCoverage", "CHM-EFADDCOV",
        vlvw_name="vlvwAdditionalCoverages",
    ):
        _log.error("Additional Coverages: nav failed — aborting section")
        return

    _log.info("=== Additional Coverages: %d item(s) ===", len(coverages))
    assert_screen_code(page, "CHM-EFADDCOV", timeout_ms=8_000)

    add_btn_sel = '[data-test="vlvwAdditionalCoverages_add"]'
    norm_currency = lambda s: (s or "").replace(",", "").replace("$", "").strip()
    consecutive_failures = 0

    for i, ac in enumerate(coverages, 1):
        check_cancel()
        desc = (ac.description or "").strip()
        if not desc:
            _log.warning("AC #%d: blank description — skipping", i)
            continue

        code = (ac.code or "").strip() or "N/A"
        limit = norm_currency(ac.each_claim)
        ded = norm_currency(ac.deductible)
        _log.info(
            "--- Row %d/%d: code=%r desc=%r limit=$%s ded=$%s",
            i, len(coverages), code, desc, limit or "0", ded or "0",
        )

        _aggressive_cleanup(page)
        rows_before = _grid_row_count(page, "vlvwAdditionalCoverages")
        _log.debug("AC #%d: rows_before=%d", i, rows_before)

        # Scroll Add into view — virtualized grid may move it off-screen.
        try:
            page.locator(add_btn_sel).first.scroll_into_view_if_needed(timeout=2_000)
        except Exception:  # noqa: BLE001
            pass

        # Pre-Add settle (see _fill_scheduled_equipment for context).
        page.wait_for_timeout(750)

        with safe_action(page, context=f"AC #{i}: click Add"):
            page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)

        grew = _wait_grid_grew(
            page, "vlvwAdditionalCoverages", rows_before, timeout_ms=4_000,
        )
        if not grew:
            _log.warning(
                "AC #%d: row did not grow after Add — retrying with longer settle",
                i,
            )
            page.wait_for_timeout(2_500)
            try:
                page.locator(add_btn_sel).first.scroll_into_view_if_needed(timeout=2_000)
            except Exception:  # noqa: BLE001
                pass
            with safe_action(page, context=f"AC #{i}: RETRY Add"):
                page.locator(add_btn_sel).first.click(timeout=_CLICK_TIMEOUT)
            grew = _wait_grid_grew(
                page, "vlvwAdditionalCoverages", rows_before, timeout_ms=5_000,
            )
            if not grew:
                _log.error(
                    "AC #%d: row still did not grow after retry — halting run.",
                    i,
                )
                raise EntryCancelled(
                    f"AC #{i}: EPIC rejected Add (twice); halting"
                )
            _log.info("AC #%d: retry succeeded", i)
        consecutive_failures = 0
        _log.debug(
            "AC #%d: row appeared (count %d → %d)",
            i, rows_before, rows_before + 1,
        )

        # Use plain _keyboard_fill (no safe_action/verify wrapper) for
        # AC field fills — matches the original code path that worked
        # reliably on this section. The verify polling we use on
        # Sched/Unsched/AI seemed to interact with AC's form-panel
        # commit timing and silently broke subsequent Add clicks.
        # Code first (required — EPIC errors "Required information is
        # missing" if blank when other fields are filled).
        from iga_marketing_master_2.epic_steps._grid_helpers import (
            abbreviate_if_too_long, AC_DESC_MAX_CHARS,
        )
        short_desc = abbreviate_if_too_long(
            desc, max_chars=AC_DESC_MAX_CHARS,
            row_idx=i, label="description", section="AC", logger=_log,
        )
        _keyboard_fill(page, "streCode",                  code)
        _keyboard_fill(page, "streDescription",           short_desc)
        _keyboard_fill(page, "streLimit__textField",      limit)
        _keyboard_fill(page, "streDeductible__textField", ded)
        if ac.item_number:
            _keyboard_fill(page, "inteItemNumber__textField", ac.item_number)

        page.wait_for_timeout(300)
        _log.debug("AC #%d: row complete", i)

    _log.info("=== Additional Coverages: complete (%d row(s) processed) ===", len(coverages))


# ── Forms & Endorsements ─────────────────────────────────────────────────────

IM_STANDARD_FORMS: list[_GLFormSpec] = [
    _GLFormSpec(name="Inland Marine Extension Endorsement"),
]


def _fill_im_forms_endorsements(page: Page, forms: list) -> None:
    """Add Forms & Endorsements on EFFRMEND.

    The IM Forms screen uses the OLD proxy pattern (Angular-style
    `data-automation-id` wrappers), not React. The Add button is
    `[name="vlvwFormsEnd"] .icon-button[title="Add"]`. Fields:
      streNumber (form #), streName (form name), dteEdition (edition date).
    Both Number and Name are lookup fields — typing + Tab triggers EPIC's
    server-side autocomplete which resolves to the canonical form.

    Note: screen code is ``EFFRMEND`` (no ``CHM-`` prefix), unlike the
    other IM sub-screens (CHM-EFCOVDED, CHM-EFSCHEQU, etc.). Verified
    live 2026-05-26 — the 15 s screen-code wait timed out because we
    were looking for CHM-EFFRMEND.
    """
    if not forms:
        return
    if not _nav_im_section(page, "FormEndorsement", "EFFRMEND",
                           vlvw_name="vlvwFormsEnd"):
        return
    _log.info("=== Forms & Endorsements (%d) ===", len(forms))

    # Wait for either Add button to render — page may still be loading.
    proxy_add = page.locator('[name="vlvwFormsEnd"] .icon-button[title="Add"]')
    react_add = page.locator('[data-test="vlvwFormsEnd_add"]')
    use_proxy = False
    for _ in range(20):
        if proxy_add.count() > 0:
            use_proxy = True
            break
        if react_add.count() > 0:
            break
        page.wait_for_timeout(150)

    add_btn = proxy_add if use_proxy else react_add
    fill_fn = _fill_proxy_field if use_proxy else _fill_id_field
    _log.debug("Forms screen pattern: %s", "old-proxy" if use_proxy else "react")

    for i, frm in enumerate(forms):
        check_cancel()
        _log.info("Adding form %d: %r", i + 1, frm.name)
        _aggressive_cleanup(page)
        if not _wait_visible(page, add_btn, 5_000):
            _log.error("Forms Add button not visible at row %d", i + 1)
            break
        rows_before = _grid_row_count(page, "vlvwFormsEnd")
        add_btn.first.click(timeout=_CLICK_TIMEOUT)
        page.wait_for_timeout(800)

        if frm.form_number:
            fill_fn(page, "streNumber", frm.form_number)
            page.keyboard.press("Tab")
            page.wait_for_timeout(400)
        fill_fn(page, "streName", frm.name)
        page.keyboard.press("Tab")
        page.wait_for_timeout(600)
    _log.info("Forms & Endorsements: complete (%d rows attempted)", len(forms))


# ── Generic helpers ──────────────────────────────────────────────────────────

def _resolve_add_btn(page: Page, vlvw_name: str):
    """Return whichever Add-button locator exists for this grid (React or old proxy)."""
    react = page.locator(f'[data-test="{vlvw_name}_add"]')
    if react.count() > 0:
        return react
    proxy = page.locator(f'[name="{vlvw_name}"] .icon-button[title="Add"]')
    if proxy.count() > 0:
        return proxy
    return None


def _grid_row_count(page: Page, vlvw_name: str) -> int:
    """Return the grid's data-row count.

    Prefers the React grid's stable per-row data-test entries; falls
    back to the "X Items" footer label when virtualization unrenders
    trailing rows.

    The "X Items" search is SCOPED to the specific grid's footer
    (``data-test="<vlvw_name>-footer"``), not the whole body — if
    multiple grids' footers happen to be in the DOM during a section
    transition (e.g., Unscheduled → AI), reading the unscoped body
    text picks up the WRONG grid's count and ``rows_before`` ends up
    huge. Observed 2026-05-26 in the e2e: AI #1 captured rows_before=6
    from Unscheduled's leftover footer text, then the Add-grew check
    failed because AI's true count (1) never exceeded 6.
    """
    try:
        dom_count = int(page.evaluate(
            f"""() => document.querySelectorAll('[data-test^="{vlvw_name}-focusable-row-"]').length"""
        ))
    except Exception:
        dom_count = 0
    try:
        label_count = int(page.evaluate(
            f"""() => {{
                const footer = document.querySelector('[data-test="{vlvw_name}-footer"]');
                if (!footer) return -1;
                const txt = footer.textContent || '';
                const m = txt.match(/(\\d+)\\s+Items?\\b/);
                return m ? parseInt(m[1], 10) : -1;
            }}"""
        ))
    except Exception:
        label_count = -1
    # Use the larger of the two — label may lag during render but is
    # authoritative for total count.
    return max(dom_count, label_count if label_count >= 0 else 0)


def _wait_grid_grew(
    page: Page,
    vlvw_name: str,
    rows_before: int,
    *,
    timeout_ms: int = 5_000,
    poll_ms: int = 150,
) -> bool:
    """Poll until the grid's row count exceeds *rows_before*. Returns True/False.

    Uses :func:`_grid_row_count` which prefers the footer label (e.g.
    "18 Items") when the DOM's focusable-row count lags behind because
    of virtualization — the React DataGrid only renders rows that fit
    the visible viewport plus a small buffer, so a newly-added row
    that falls below the fold has no ``data-test=...-focusable-row-N``
    element in the DOM. The footer label is authoritative.
    """
    import time as _t
    deadline = _t.time() + timeout_ms / 1000.0
    while _t.time() < deadline:
        if _grid_row_count(page, vlvw_name) > rows_before:
            return True
        page.wait_for_timeout(poll_ms)
    return False


def _delete_pending_row(page: Page, vlvw_name: str) -> None:
    """Click the React grid's delete button to discard a pending blank row.

    Called when an Add succeeded but the row's required-field fill failed —
    leaving the blank row pending would trigger EPIC's "Required information
    is missing" modal cascade on the next navigation.
    """
    try:
        sel = f'[data-test="{vlvw_name}_delete"]'
        btn = page.locator(sel).first
        if btn.count():
            btn.click(timeout=2_000)
            page.wait_for_timeout(300)
            _aggressive_cleanup(page)
            _log.debug("deleted pending row in %s", vlvw_name)
    except Exception as exc:
        _log.debug("delete pending row in %s failed: %s", vlvw_name, exc)


def _fill_either(page: Page, field_id: str, value: str) -> bool:
    """Try React id (#field_id) first, fall back to old-proxy [data-automation-id].

    Returns True if a fill was attempted (value non-blank), False if skipped.
    """
    if not value:
        return False
    # React id pattern
    react_inp = page.locator(f'#{field_id}, #{field_id}__textField')
    if react_inp.count() and react_inp.first.is_visible(timeout=400):
        try:
            react_inp.first.click(timeout=2_000)
            react_inp.first.fill(value, timeout=2_000)
            page.wait_for_timeout(_FILL_WAIT_MS)
            _log.debug("fill_either(react) #%s = %r OK", field_id, value)
            return True
        except Exception as exc:
            _log.debug("fill_either(react) #%s failed: %s — trying proxy", field_id, exc)
    # Old proxy pattern
    proxy_inp = page.locator(f'[data-automation-id="{field_id}"] input, [data-automation-id="{field_id}"] textarea')
    if proxy_inp.count():
        try:
            proxy_inp.first.click(timeout=2_000)
            proxy_inp.first.fill(value, timeout=2_000)
            page.wait_for_timeout(_FILL_WAIT_MS)
            _log.debug("fill_either(proxy) %s = %r OK", field_id, value)
            return True
        except Exception as exc:
            _log.warning("fill_either %s = %r FAILED: %s", field_id, value, exc)
    return False


def _fill_combo_either(page: Page, combo_id: str, value: str) -> None:
    """Fill a combo — try React (data-test={id}-combobox-input) then old proxy."""
    if not value:
        return
    try:
        _fill_react_combo(page, combo_id, value)
    except Exception:
        try:
            proxy = page.locator(f'[data-automation-id="{combo_id}"] input')
            if proxy.count():
                proxy.first.click(timeout=2_000)
                proxy.first.fill(value, timeout=2_000)
                page.keyboard.press("Tab")
                page.wait_for_timeout(_FILL_WAIT_MS)
                _log.debug("fill_combo_either(proxy) %s = %r OK", combo_id, value)
        except Exception as exc:
            _log.warning("fill_combo_either %s = %r FAILED: %s", combo_id, value, exc)


# ── Entry point ───────────────────────────────────────────────────────────────

def run(page: Page, setup: InlandMarineSetup) -> bool:
    """Walk all IM screens, filling from `setup`."""
    _log.info("=== Inland Marine entry step starting (state=%r) ===", setup.state or "(unsuffixed)")
    _log.info(
        "Setup: %d sched, %d unsched, %d AI, %d AC",
        len(setup.scheduled_items), len(setup.unscheduled_items),
        len(setup.additional_interests), len(setup.additional_coverages),
    )

    try:
        if not _nav_to_im(page, setup.state):
            _log.error("Aborting — could not open IM line")
            return False

        _log.info("=== Step 1: Coverage / Deductible ===")
        checkpoint(page, "Inland Marine: Coverage / Deductible")
        _fill_coverage_deductible(page, setup)

        _log.info("=== Step 2: Scheduled Equipment ===")
        checkpoint(page, "Inland Marine: Scheduled Equipment")
        _fill_scheduled_equipment(page, setup.scheduled_items)

        _log.info("=== Step 3: Unscheduled Equipment ===")
        checkpoint(page, "Inland Marine: Unscheduled Equipment")
        _fill_unscheduled_equipment(page, setup.unscheduled_items)

        _log.info("=== Step 4: Additional Interests ===")
        checkpoint(page, "Inland Marine: Additional Interests")
        _fill_im_additional_interests(page, setup.additional_interests)

        _log.info("=== Step 5: Additional Coverages ===")
        checkpoint(page, "Inland Marine: Additional Coverages")
        _fill_im_additional_coverages(page, setup.additional_coverages)

        _log.info("=== Step 6: Forms & Endorsements ===")
        checkpoint(page, "Inland Marine: Forms & Endorsements")
        _fill_im_forms_endorsements(page, IM_STANDARD_FORMS)

        _log.info("=== Inland Marine entry step complete ===")
        return True
    except EntryCancelled:
        # User clicked Cancel — let it propagate so the worker terminates.
        raise
    except Exception as exc:
        _log.error("IM run aborted: %s", exc, exc_info=True)
        return False

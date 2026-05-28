"""Standalone candidate rewrite of step_property._fill_subjects.

Goal: prove the **pre-Add gate** (footer "X Items" + Add enabled + DOM-quiet)
is enough to land each Add cleanly with NO Premises round-trip, AND that every
click / data entry is guarded by a popup check so a single validation alert
can't cascade into a 20-second failure chain.

Adds two rows back-to-back on a settled Subjects screen:

  Row 1: Building, $500,000, Replacement Cost, 4% inflation
         CoL: Special (SPC), 80% coins, $2,500 flat deductible (FL)

  Row 2: BPP (Personal Property of Insured), $250,000, ACV, no inflation
         CoL: Special (SPC), 80% coins, 5% percent deductible (P)

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the Property Subjects screen (CHM-PRSUBJCT) of an MMS.
  - Premises already added (Loc 1 / Bldg 1 at minimum).
  - **Subjects screen is in a clean 0-Items state** (operator clears it).

Run from repo root:
    .venv/Scripts/python test_subjects_rewrite.py
"""
from __future__ import annotations

import sys
import time
import logging

sys.path.insert(0, r"src")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("playwright").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.INFO)

from playwright.sync_api import sync_playwright, Page

# Reuse the validated helpers from production. We replace ONLY the
# activate/commit logic and add popup-checking around every interaction.
from iga_marketing_master_2.epic_steps.step_property import (
    SubjectSpec,
    _add_cause_of_loss,
    _SUBJECT_TYPE_SEARCH,
    _wait_for_subject_form_active,
)

_log = logging.getLogger("subjects_rewrite")


# ── Popup / error guard ────────────────────────────────────────────────────
#
# EPIC pops a `common-message-modal` shroud for validation errors ("Required
# information is missing"), confirmation prompts ("Are you sure?"), and other
# alerts. If we ignore one, its shroud intercepts every subsequent click and
# every step in the row cascades into a Playwright pointer-events failure.
#
# `check_popups` MUST be called after every click and every data entry.
# It returns a structured record so the caller can decide what to do —
# benign validation alerts get auto-dismissed and counted; fatal errors
# (e.g., a server error dialog) abort the row.

_BENIGN_TITLES = (
    "required information is missing",
    "are you sure",
    "do you wish to",
    "do you want to",
)


def check_popups(page: Page, *, where: str) -> dict:
    """Detect and handle EPIC popups. Always call after every click / fill.

    Returns dict with: count, dismissed (list of strings), fatal (bool).
    """
    info = {"count": 0, "dismissed": [], "fatal": False}
    for _round in range(5):
        snap = page.evaluate(
            """() => {
                const out = [];
                const alerts = document.querySelectorAll('[data-test="common-message-modal"]');
                for (const a of alerts) {
                    // The shroud wraps the dialog; look for the dialog content.
                    const text = (a.innerText || '').trim();
                    if (!text) continue;
                    out.push({ kind: 'message-modal', text: text.slice(0, 300) });
                }
                // Generic modal dialogs (e.g., Location/Building Lookup is benign
                // and handled separately; we only flag *unexpected* ones).
                return out;
            }"""
        )
        if not snap:
            break
        info["count"] += len(snap)
        for s in snap:
            txt = (s.get("text") or "").lower()
            is_benign = any(t in txt for t in _BENIGN_TITLES)
            if not is_benign and ("error" in txt or "failed" in txt or "exception" in txt):
                # Server / unexpected error — log but still dismiss so we can recover.
                _log.error("[%s] unexpected popup: %s", where, s["text"][:200])
                info["fatal"] = True
            else:
                _log.warning("[%s] popup dismissed: %s", where, s["text"][:140])
            info["dismissed"].append(s["text"][:140])
        # Dismiss every visible alert via OK / close button.
        page.evaluate(
            """() => {
                const alerts = document.querySelectorAll('[data-test="common-message-modal"]');
                for (const a of alerts) {
                    let btn = a.querySelector('[data-test="dialog-ok-btn"]')
                        || a.querySelector('button[data-test$="-close-button"]')
                        || a.querySelector('button');
                    if (btn) btn.click();
                }
            }"""
        )
        page.wait_for_timeout(400)
    return info


def safe_click(page, locator, *, where: str, timeout: int = 5_000) -> bool:
    """Pre-check popups → click → post-check popups. Returns True if click landed."""
    check_popups(page, where=f"{where}/pre")
    try:
        locator.first.click(timeout=timeout)
    except Exception as exc:
        _log.warning("[%s] click failed: %s", where, exc)
        check_popups(page, where=f"{where}/post-fail")
        return False
    check_popups(page, where=f"{where}/post")
    return True


def safe_fill_id(page: Page, field_id: str, value: str, *, where: str) -> bool:
    """Fill a React `__textField` input; popup-checked BEFORE and AFTER click+fill."""
    if not value:
        return True
    try:
        check_popups(page, where=f"{where}/pre")
        inp = page.locator(f'#{field_id}').first
        inp.click(timeout=2_000)
        check_popups(page, where=f"{where}/post-click")
        inp.fill(value, timeout=2_000)
        check_popups(page, where=f"{where}/post-fill")
        page.wait_for_timeout(150)
        _log.debug("[%s] fill #%s = %r OK", where, field_id, value)
        return True
    except Exception as exc:
        _log.warning("[%s] fill #%s = %r FAILED: %s", where, field_id, value, exc)
        check_popups(page, where=f"{where}/post-fail")
        return False


def safe_combo(page: Page, combo_id: str, value: str, *, where: str) -> bool:
    """Fill a React combobox; popups checked before AND after every click."""
    if not value:
        return True
    try:
        check_popups(page, where=f"{where}/pre")
        inp = page.locator(f'[data-test="{combo_id}-combobox-input"]').first
        inp.click(timeout=2_000)
        if check_popups(page, where=f"{where}/post-open").get("fatal"):
            return False
        inp.fill(value, timeout=2_000)
        check_popups(page, where=f"{where}/post-type")
        page.wait_for_timeout(700)
        rows = page.locator('[data-test="dropdown-row"]')
        count = min(rows.count(), 20)
        if count == 0:
            _log.warning("[%s] combo %s = %r — no dropdown rows", where, combo_id, value)
            return False
        import re as _re
        _label_re = _re.compile(r'^[A-Z0-9]+?([A-Z][a-z].*)')
        val_lower = value.lower()
        best_idx, best_pri = None, 99
        for n in range(count):
            rt = (rows.nth(n).text_content() or "").strip()
            rtl = rt.lower()
            if rtl == val_lower:
                best_idx, best_pri = n, 1; break
            m = _label_re.match(rt)
            label = m.group(1).strip() if m else rt
            if label.lower() == val_lower and best_pri > 2:
                best_idx, best_pri = n, 2
            elif val_lower in rtl and best_pri > 3:
                best_idx, best_pri = n, 3
        if best_idx is None:
            best_idx = 0
            _log.warning("[%s] combo %s = %r — no good match, picked first", where, combo_id, value)
        check_popups(page, where=f"{where}/pre-select")
        rows.nth(best_idx).click(timeout=2_000)
        check_popups(page, where=f"{where}/post-select")
        page.wait_for_timeout(150)
        _log.debug("[%s] combo %s = %r OK", where, combo_id, value)
        return True
    except Exception as exc:
        _log.warning("[%s] combo %s = %r FAILED: %s", where, combo_id, value, exc)
        check_popups(page, where=f"{where}/post-fail")
        return False


# ── DOM observer + pre-Add gate ────────────────────────────────────────────

def _install_dom_observer(page: Page) -> None:
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


def _dom_idle_ms(page: Page) -> int:
    try:
        return int(page.evaluate("() => Date.now() - (window.__igaLastMut || 0)"))
    except Exception:
        return 0


def _read_subject_gate_state(page: Page) -> dict:
    return page.evaluate(
        """() => {
            const footer = document.querySelector('[data-test="vlvwSubject-footer"]');
            const addBtn = document.querySelector('[data-test="vlvwSubject_add"]');
            return {
                footerText: footer ? footer.innerText.trim() : null,
                addPresent: !!addBtn,
                addDisabled: addBtn ? !!addBtn.disabled : null,
                addAriaDisabled: addBtn ? addBtn.getAttribute('aria-disabled') : null,
                addVisible: addBtn ? (addBtn.offsetParent !== null) : null,
            };
        }"""
    )


def _wait_pre_add_gate(page: Page, *, quiet_ms: int = 600, timeout_ms: int = 8_000) -> bool:
    _install_dom_observer(page)
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        # Drain any pending popups while waiting.
        check_popups(page, where="pre-add-gate")
        st = _read_subject_gate_state(page)
        footer_ok = bool(st.get("footerText")) and st["footerText"].endswith("Items")
        add_ok = (
            st.get("addPresent")
            and st.get("addVisible")
            and not st.get("addDisabled")
            and (st.get("addAriaDisabled") in (None, "false"))
        )
        if footer_ok and add_ok and _dom_idle_ms(page) >= quiet_ms:
            _log.debug("pre-Add gate satisfied: footer=%r", st["footerText"])
            return True
        time.sleep(0.1)
    _log.warning("pre-Add gate TIMED OUT after %dms; last=%s", timeout_ms, _read_subject_gate_state(page))
    return False


def _activate_subject_row_v2(page: Page) -> bool:
    if not _wait_pre_add_gate(page):
        return False
    add_btn = page.locator('[data-test="vlvwSubject_add"]').first
    if not safe_click(page, add_btn, where="row-add"):
        return False
    ok = _wait_for_subject_form_active(page, timeout_ms=5_000)
    check_popups(page, where="row-add/post-active")
    return ok


# ── Loc/Bldg modal handler ────────────────────────────────────────────────

def _maybe_resolve_loc_bldg_modal(page: Page, loc: str, bldg: str) -> None:
    try:
        heading = page.get_by_role("heading", name="Location/Building Lookup")
        if not heading.first.is_visible(timeout=600):
            return
    except Exception:
        return
    _log.debug("Loc/Bldg lookup modal detected — picking loc=%s bldg=%s", loc, bldg)
    page.evaluate("""({loc, bldg}) => {
        const portal = document.querySelector('[data-test="ads-portal"]') || document;
        const rows = portal.querySelectorAll('[data-test="row"]');
        for (const r of rows) {
            const t = (r.innerText || '').replace(/\\s+/g, ' ');
            if (t.includes(loc) && t.includes(bldg)) { r.click(); return true; }
        }
        if (rows.length) { rows[0].click(); return 'first'; }
        return false;
    }""", {"loc": loc, "bldg": bldg})
    page.wait_for_timeout(300)
    check_popups(page, where="loc-bldg-modal/select")
    save = page.get_by_role("button", name="Save", exact=True)
    try:
        if save.first.is_visible(timeout=1_500):
            safe_click(page, save, where="loc-bldg-modal/save")
            page.wait_for_timeout(400)
    except Exception:
        pass


# ── Row fill ───────────────────────────────────────────────────────────────

def _fill_one_subject(page: Page, subj: SubjectSpec, *, row_num: int) -> bool:
    _log.info(
        "Row %d: type=%r loc=%s bldg=%s amount=%r val=%r infl=%r cause=%r coins=%r ded=%r",
        row_num, subj.subject_type, subj.location_number, subj.building_number,
        subj.amount, subj.valuation, subj.inflation_guard,
        subj.cause_of_loss, subj.coinsurance, subj.deductible,
    )

    before = _read_subject_gate_state(page)
    try:
        before_n = int((before.get("footerText") or "0 Items").split(" ")[0])
    except Exception:
        before_n = 0

    if not _activate_subject_row_v2(page):
        _log.error("Row %d: gate or form-active poll FAILED", row_num)
        return False

    # Post-Add settle: the form-active poll (loc field enabled) returns within
    # a few ms, but EPIC is still wiring up the inline form's field handlers.
    # Without this hold, the very next loc-field fill landed in a stale form
    # and Row 2's data was silently dropped (validation popup + row reset).
    page.wait_for_timeout(750)
    check_popups(page, where=f"row{row_num}/post-activate-settle")

    if not safe_fill_id(page, "inteLocationNumber__textField", subj.location_number, where=f"row{row_num}/loc"):
        return False
    page.keyboard.press("Tab")
    check_popups(page, where=f"row{row_num}/loc-tab")
    if not safe_fill_id(page, "inteBuildingNumber__textField", subj.building_number, where=f"row{row_num}/bldg"):
        return False
    page.keyboard.press("Tab")
    check_popups(page, where=f"row{row_num}/bldg-tab")
    page.wait_for_timeout(400)
    _maybe_resolve_loc_bldg_modal(page, subj.location_number, subj.building_number)

    subj_search = _SUBJECT_TYPE_SEARCH.get(subj.subject_type.lower(), subj.subject_type)
    if not safe_combo(page, "cboSubject", subj_search, where=f"row{row_num}/type"):
        return False

    if subj.description:
        safe_fill_id(page, "streDescription", subj.description, where=f"row{row_num}/desc")
    if not safe_fill_id(page, "deceAmount__textField", subj.amount, where=f"row{row_num}/amt"):
        return False
    if subj.valuation:
        safe_combo(page, "cboValuation1", subj.valuation, where=f"row{row_num}/val")
    if subj.inflation_guard:
        safe_fill_id(page, "pereInflationGuard__textField", subj.inflation_guard, where=f"row{row_num}/infl")
    page.wait_for_timeout(300)

    if subj.cause_of_loss:
        check_popups(page, where=f"row{row_num}/pre-col")
        _add_cause_of_loss(
            page, subj.cause_of_loss,
            coinsurance=subj.coinsurance,
            deductible=subj.deductible,
        )
        check_popups(page, where=f"row{row_num}/post-col")

    # Commit verification: don't trust just the footer (a "-" placeholder row
    # also ticks the counter). Require BOTH footer-bump AND the new row's
    # cells to contain the expected amount.
    expected_amt_digits = "".join(ch for ch in (subj.amount or "") if ch.isdigit())
    deadline = time.time() + 5.0
    while time.time() < deadline:
        check_popups(page, where=f"row{row_num}/commit-wait")
        st = _read_subject_gate_state(page)
        try:
            now_n = int((st.get("footerText") or "0 Items").split(" ")[0])
        except Exception:
            now_n = before_n
        if now_n > before_n:
            rows = _read_subject_grid_rows(page)
            new_row = rows[before_n] if before_n < len(rows) else None
            if new_row and expected_amt_digits and expected_amt_digits in (new_row.get("text") or "").replace(",", ""):
                _log.info("Row %d: committed — footer %s; row=%r", row_num, st.get("footerText"), new_row["text"][:80])
                return True
            elif new_row:
                _log.error(
                    "Row %d: footer bumped but new row missing data — text=%r (expected to contain %s)",
                    row_num, (new_row.get("text") or "")[:80], expected_amt_digits,
                )
                return False
        time.sleep(0.15)
    _log.error("Row %d: footer never bumped from %d after CoL save", row_num, before_n)
    return False


def _read_subject_grid_rows(page: Page) -> list[dict]:
    return page.evaluate(
        """() => {
            const grid = document.querySelector('[data-test="vlvwSubject"]');
            if (!grid) return [];
            const rows = grid.querySelectorAll('[data-test="row"]');
            return Array.from(rows).map(r => ({
                text: (r.innerText || '').replace(/\\s+/g, ' ').trim(),
            }));
        }"""
    )


# ── Test data ─────────────────────────────────────────────────────────────

ROWS = [
    SubjectSpec(
        subject_type="Building", location_number="1", building_number="1",
        description="Main Building", amount="500000", valuation="Replacement Cost",
        inflation_guard="4", cause_of_loss="Special", coinsurance="80",
        deductible="2500",
    ),
    SubjectSpec(
        subject_type="Personal Property of Insured", location_number="1", building_number="1",
        description="Contents", amount="250000", valuation="Actual Cash Value",
        inflation_guard="", cause_of_loss="Special", coinsurance="80",
        deductible="5%",
    ),
]


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "appliedepic.com" in p.url), ctx.pages[0])

        print("\n=== Subjects rewrite test (popup-guarded, no Premises round-trip) ===")
        print(f"  Page: {page.title()}")
        print(f"  Rows: {len(ROWS)}")

        # Reset is operator-driven (per feedback-no-reset-scripts.md). Refuse
        # to start unless the screen is in a clean 0-Items state — bail with
        # a clear message so the operator can fix it before retry.
        existing = _read_subject_gate_state(page)
        print(f"\n[0] Starting footer: {existing.get('footerText')}")
        try:
            start_n = int((existing.get("footerText") or "0 Items").split(" ")[0])
        except Exception:
            start_n = -1
        if start_n != 0:
            print(
                "    ABORT: expected '0 Items' (operator should clear Subjects first). "
                f"Got: {existing.get('footerText')!r}"
            )
            return 2

        ok_count = 0
        t_start = time.time()
        for i, subj in enumerate(ROWS, start=1):
            if _fill_one_subject(page, subj, row_num=i):
                ok_count += 1
            else:
                print(f"\n[!] Row {i} failed — stopping.")
                break

        elapsed = int((time.time() - t_start) * 1000)
        final = _read_subject_gate_state(page)
        print(f"\n=== Result: {ok_count}/{len(ROWS)} rows added in {elapsed}ms ===")
        print(f"    Final footer: {final.get('footerText')}")
        return 0 if ok_count == len(ROWS) else 1


if __name__ == "__main__":
    sys.exit(main())

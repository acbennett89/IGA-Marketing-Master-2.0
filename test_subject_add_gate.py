"""Focused test: pre-Add settle + Add Subject + form-active poll.

Validates the gate logic for the second-and-later Subject row Add on the EPIC
Property Subjects screen WITHOUT the Premises round-trip recovery currently in
``step_property._activate_subject_row``.

The bug we're trying to defeat:
    Clicking ``[data-test="vlvwSubject_add"]`` immediately after the screen
    finishes rendering opens a row whose fields stay disabled — first-Add fails
    "0 Items" stays at the footer. The legacy recovery navigates away to
    Premises and back; we want to never do that.

The hypothesis:
    A correct **pre-Add settle** (footer rendered as "X Items" + Add button
    enabled + DOM-quiet) is enough to make first-Add land cleanly. No retry,
    no round-trip.

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the Property Subjects screen (CHM-PRSUBJCT) of an MMS.
  - At least Row 1 already added and saved (footer reads "1 Items").
  - No subject form currently open (this script Adds Row 2).

Run from repo root:
    .venv/Scripts/python test_subject_add_gate.py
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

_log = logging.getLogger("subject_add_gate")


# ---- pre-Add gate ---------------------------------------------------------

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
    return int(page.evaluate("() => Date.now() - (window.__igaLastMut || 0)"))


def _read_gate_state(page: Page) -> dict:
    """One-shot read of every signal the pre-Add gate cares about."""
    return page.evaluate(
        """() => {
            const footer = document.querySelector('[data-test="vlvwSubject-footer"]');
            const addBtn = document.querySelector('[data-test="vlvwSubject_add"]');
            const loc = document.querySelector('#inteLocationNumber__textField');
            return {
                footerText: footer ? footer.innerText.trim() : null,
                addPresent: !!addBtn,
                addDisabled: addBtn ? !!addBtn.disabled : null,
                addAriaDisabled: addBtn ? addBtn.getAttribute('aria-disabled') : null,
                addVisible: addBtn ? (addBtn.offsetParent !== null) : null,
                locFieldPresent: !!loc,
                locFieldDisabled: loc ? loc.disabled : null,
                locFieldVisible: loc ? (loc.offsetParent !== null) : null,
            };
        }"""
    )


def _wait_pre_add_gate(page: Page, *, timeout_ms: int = 8_000) -> tuple[bool, dict]:
    """Poll until: footer reads 'N Items' + Add button enabled + DOM quiet.

    Returns (passed, last_state). Quiet threshold: 600ms with no mutations.
    """
    _install_dom_observer(page)
    deadline = time.time() + timeout_ms / 1000.0
    last_state: dict = {}
    while time.time() < deadline:
        st = _read_gate_state(page)
        last_state = st
        footer_ok = bool(st.get("footerText")) and st["footerText"].endswith("Items")
        add_ok = (
            st.get("addPresent")
            and st.get("addVisible")
            and not st.get("addDisabled")
            and (st.get("addAriaDisabled") in (None, "false"))
        )
        if footer_ok and add_ok:
            idle = _dom_idle_ms(page)
            if idle >= 600:
                _log.info("Pre-Add gate satisfied: footer=%r addOk idle=%dms", st["footerText"], idle)
                return True, st
        time.sleep(0.1)
    _log.warning("Pre-Add gate TIMED OUT after %dms; last state: %s", timeout_ms, last_state)
    return False, last_state


def _wait_subject_form_active(page: Page, *, timeout_ms: int = 5_000) -> tuple[bool, int]:
    """Poll #inteLocationNumber__textField until enabled. Return (ok, ms_elapsed)."""
    start = time.time()
    deadline = start + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            loc = page.locator('#inteLocationNumber__textField').first
            if loc.is_enabled(timeout=200):
                return True, int((time.time() - start) * 1000)
        except Exception:
            pass
        time.sleep(0.1)
    return False, int((time.time() - start) * 1000)


# ---- main -----------------------------------------------------------------

def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "appliedepic.com" in p.url), ctx.pages[0])

        print("\n=== Subject Add Gate (pre-Add settle) test ===")
        print(f"  Page: {page.title()}")

        # 1. Read pre-state.
        pre = _read_gate_state(page)
        print(f"\n[1] Pre-state: {pre}")
        if pre.get("footerText") in (None, "0 Items"):
            print("    ERROR: footer says '0 Items' or missing — need at least Row 1 added.")
            return 1

        # 2. Pre-Add gate.
        t0 = time.time()
        ok, last = _wait_pre_add_gate(page, timeout_ms=8_000)
        gate_ms = int((time.time() - t0) * 1000)
        print(f"\n[2] Pre-Add gate: {'PASS' if ok else 'FAIL'} in {gate_ms}ms; final state={last}")
        if not ok:
            return 1

        # 3. Click Add ONCE. No dismiss-modal, no round-trip.
        print("\n[3] Clicking [data-test='vlvwSubject_add'] once.")
        page.locator('[data-test="vlvwSubject_add"]').first.click(timeout=5_000)

        # 4. Poll for form-active (no recovery).
        form_ok, form_ms = _wait_subject_form_active(page, timeout_ms=5_000)
        print(f"\n[4] Form-active poll: {'PASS' if form_ok else 'FAIL'} in {form_ms}ms")

        # 5. Confirm footer ticked to '2 Items' (row exists once form opens; some
        #    EPIC builds only bump the count after first field commit — record both).
        post = _read_gate_state(page)
        print(f"\n[5] Post-Add state: {post}")

        verdict = "PASS" if form_ok else "FAIL"
        print(f"\n=== Verdict: {verdict} ===")
        print(f"  Pre-Add gate took: {gate_ms}ms")
        print(f"  Form-active took : {form_ms}ms")
        return 0 if form_ok else 1


if __name__ == "__main__":
    sys.exit(main())

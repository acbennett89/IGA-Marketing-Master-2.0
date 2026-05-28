"""One-shot: exercise the updated _fill_react_combo against the open CoL dialog.

Pre-condition: Add Cause Of Loss dialog is OPEN with cboDedType1 currently
showing some wrong value (e.g. "OC"). This script attaches via CDP, reads the
current value, calls the production helper to set it to "P" (Percent), and
reports what got selected.
"""
from __future__ import annotations
import sys, logging
sys.path.insert(0, r"src")

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_property import _fill_react_combo

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    ctx = browser.contexts[0]
    page = next((p for p in ctx.pages if "appliedepic.com" in p.url), ctx.pages[0])

    def read(field):
        return page.evaluate(
            "id => { const e = document.getElementById(id); return e ? e.value : null; }",
            field,
        )

    print(f"BEFORE: cboDedType1 = {read('cboDedType1')!r}")
    _fill_react_combo(page, "cboDedType1", "P")
    page.wait_for_timeout(300)
    print(f"AFTER : cboDedType1 = {read('cboDedType1')!r}")

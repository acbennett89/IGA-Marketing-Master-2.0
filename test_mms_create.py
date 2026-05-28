"""Quick live test of step_mms_create against the open EPIC browser (CDP port 9222).

Run from the repo root:
    .venv\Scripts\python test_mms_create.py
"""
import sys
sys.path.insert(0, r"src")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_mms_create import (
    LineSpec, MmsSetup, run as mms_run,
)

SETUP = MmsSetup(
    name="2026 New Business MMS - Bill Goran Widgets LLC",
    effective="1/1/2026",
    expiration="1/1/2027",
    agency="IGA",
    branch="002",
    department="CL",
    lines=[
        LineSpec(line_code="GLIA"),   # General Liability
        LineSpec(line_code="BAUT"),   # Business Auto
    ],
)

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
    print(f"Connected — page title: {page.title()}")
    ok = mms_run(page, SETUP)
    print(f"mms_run returned: {ok}")

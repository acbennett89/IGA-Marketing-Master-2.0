"""Quick live test of step_commercial_ap against the open EPIC browser (CDP port 9222).

Expects the browser to already be on the Submission Detail page for an open MMS
with the Commercial AP subtree visible in the left sidebar.

Run from the repo root:
    .venv\Scripts\python test_commercial_ap.py
"""
import sys
sys.path.insert(0, r"src")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_commercial_ap import (
    CommercialApSetup,
    NamedInsuredSpec,
    PremiseSpec,
    run as commercial_ap_run,
)

SETUP = CommercialApSetup(
    named_insureds=[
        NamedInsuredSpec(name="Bill Goran Properties LLC"),
    ],
    premises=[
        PremiseSpec(
            street="456 Industrial Pkwy",
            city="Nashville",
            state="TN",
            zip_code="37201",
        ),
    ],
    # General Information defaults (all No, Q2=Yes, OSHA) are always applied.
)

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
    page.set_viewport_size(None)
    print(f"Connected — page title: {page.title()}")
    ok = commercial_ap_run(page, SETUP)
    print(f"commercial_ap_run returned: {ok}")

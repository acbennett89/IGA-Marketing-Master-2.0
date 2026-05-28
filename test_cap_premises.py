"""Quick live test of step_commercial_ap._fill_premises against the open browser.

Expects the browser to be on the Commercial AP Premises screen (CHM-APPREMSE)
for a submission that already has Loc 1 pre-created from the account.

Run from repo root:
    .venv/Scripts/python test_cap_premises.py
"""
import sys
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_commercial_ap import (
    PremiseSpec,
    _fill_premises,
    _nav_section,
)

# Mirrors the _DIAGNOSTIC state.json location repeatable:
#   Loc 1 Bldg 1: 120 Mockingbird Ave, Henry, TN 38231-3830
#   Loc 2 Bldg 1-3: 120 Mockingbird Ave, Henry, TN 38231-3830 (3 buildings)
#   Loc 3 Bldg 1: Henry, TN 38231 (garaging, no street)
#   Loc 4 Bldg 1: 234 Ross Sawmill Rd, Paris, TN 38242-6161
TEST_PREMISES = [
    PremiseSpec(location_number=1, building_number=1,
                street="120 Mockingbird Ave", city="Henry", state="TN", zip_code="38231-3830"),
    PremiseSpec(location_number=2, building_number=1,
                street="120 Mockingbird Ave", city="Henry", state="TN", zip_code="38231-3830"),
    PremiseSpec(location_number=2, building_number=2,
                street="120 Mockingbird Ave", city="Henry", state="TN", zip_code="38231-3830"),
    PremiseSpec(location_number=2, building_number=3,
                street="120 Mockingbird Ave", city="Henry", state="TN", zip_code="38231-3830"),
    PremiseSpec(location_number=3, building_number=1,
                street="Henry", city="", state="TN", zip_code="38231"),
    PremiseSpec(location_number=4, building_number=1,
                street="234 Ross Sawmill Rd", city="Paris", state="TN", zip_code="38242-6161"),
]

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
    print(f"Connected — {page.title()}")

    print("\n--- Testing _fill_premises ---")
    print(f"Sending {len(TEST_PREMISES)} rows:")
    for p in TEST_PREMISES:
        print(f"  Loc {p.location_number} Bldg {p.building_number}: {p.street}, {p.city} {p.state} {p.zip_code}")

    _fill_premises(page, TEST_PREMISES)
    print("\n_fill_premises completed")

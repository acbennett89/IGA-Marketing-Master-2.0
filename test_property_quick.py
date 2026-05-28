"""Quick live test of step_property against the open browser (CDP port 9222).

Expects the browser to be on the Property Subjects screen (or anywhere in the
Property submission sidebar) for a submission that already has premises added.

Run from repo root:
    .venv/Scripts/python test_property_quick.py
"""
import sys
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_property import (
    PropertySetup,
    SubjectSpec,
    _fill_subjects,
    _fill_premises,
    _nav_section,
)

# Only 2 subjects for a quick smoke test
TEST_SUBJECTS = [
    SubjectSpec(
        subject_type="Building",
        location_number="1",
        building_number="1",
        description="Test Building",
        amount="100000",
        valuation="Replacement Cost",
        cause_of_loss="Special",
    ),
    SubjectSpec(
        subject_type="Personal Property of Insured",
        location_number="1",
        building_number="1",
        description="Test BPP",
        amount="50000",
        valuation="Replacement Cost",
        cause_of_loss="Special",
    ),
]

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
    # page.set_viewport_size(None)  # not supported on CDP-attached pages
    print(f"Connected — {page.title()}")

    # Navigate to Subjects
    print("\n--- Testing _fill_subjects ---")
    ok = _nav_section(page, "Subject", "CHM-PRSUBJCT")
    print(f"Nav to Subjects: {ok}")

    _fill_subjects(page, TEST_SUBJECTS)
    print("_fill_subjects completed")

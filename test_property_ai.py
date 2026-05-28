"""Standalone test for the Property → Additional Interests section.

Pre-condition: browser is on the Additional Interests sub-section of a Property
submission (or anywhere on the Property tree — `_nav_section` will route us).

Run from repo root:
    .venv/Scripts/python test_property_ai.py
"""
import sys
import json
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_property import (
    AdditionalInterestSpec,
    _fill_additional_interests,
)

STATE_JSON = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def main():
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    interests = [
        AdditionalInterestSpec(
            name            =_v(row, "policy.property.additional_interest.name"),
            interest_type   =_v(row, "policy.property.additional_interest.interest_type"),
            street          =_v(row, "policy.property.additional_interest.street"),
            city            =_v(row, "policy.property.additional_interest.city"),
            state           =_v(row, "policy.property.additional_interest.state"),
            zip_code        =_v(row, "policy.property.additional_interest.zip_code"),
            location_number =_v(row, "policy.property.additional_interest.location_number"),
            building_number =_v(row, "policy.property.additional_interest.building_number"),
            loan_number     =_v(row, "policy.property.additional_interest.loan_number"),
        )
        for row in (rep.get("policy.property.additional_interest") or [])
        if _v(row, "policy.property.additional_interest.name")
    ]

    print(f"\n=== Additional Interests ({len(interests)}) ===")
    for i, ai in enumerate(interests, 1):
        print(f"  {i}. {ai.name!r}  type={ai.interest_type!r}  loc={ai.location_number} bldg={ai.building_number}")
    print()

    if not interests:
        print("Nothing to add — exiting.")
        return

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
        print(f"Connected — {page.title()}\n")
        _fill_additional_interests(page, interests)
        print("\nAdditional Interests section complete.")


if __name__ == "__main__":
    main()

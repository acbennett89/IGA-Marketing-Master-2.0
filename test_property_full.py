"""Full property step test against the open EPIC browser (CDP port 9222).

Loads real state.json for the _DIAGNOSTIC client and runs the entire property
step (Premises → Subjects → Additional Interests → Forms → Additional Coverages).

Expects the browser to be on a Property submission's Submission Detail page
(any property sub-section selected).

Run from repo root:
    .venv/Scripts/python test_property_full.py
"""
import sys
import json
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_property import (
    PropertySetup, SubjectSpec, AdditionalInterestSpec,
    PropertyFormSpec, PropertyCoverageSpec,
    run as property_run,
)

STATE_JSON = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    """Read a value out of a repeatable row by domain tag."""
    f = row.get(key) if isinstance(row, dict) else None
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def _sv(state, key):
    """Read a singleton field value by domain tag."""
    f = state.get("fields", {}).get(key)
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def main():
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)

    rep = state.get("repeatables", {})

    # Build subjects from policy.property.subject
    subjects = [
        SubjectSpec(
            subject_type    =_v(row, "policy.property.subject.subject"),
            location_number =_v(row, "policy.property.subject.location_number") or "1",
            building_number =_v(row, "policy.property.subject.building_number") or "1",
            description     =_v(row, "policy.property.subject.description"),
            amount          =_v(row, "policy.property.subject.amount"),
            valuation       =_v(row, "policy.property.subject.valuation1"),
            form_number     =_v(row, "policy.property.subject.form_number"),
            cause_of_loss   =_sv(state, "policy.property.applicable_causes_of_loss"),
            coinsurance     =_v(row, "policy.property.subject.coinsurance"),
            deductible      =_v(row, "policy.property.subject.deductible"),
        )
        for row in (rep.get("policy.property.subject") or [])
        if _v(row, "policy.property.subject.subject")
    ]

    # Skip scraped forms — only PROPERTY_STANDARD_FORMS (Property Extension
    # Endorsement) is added on every submission.
    forms: list = []

    # Build additional interests from policy.property.additional_interest
    additional_interests = [
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

    # Build additional coverages
    coverages = [
        PropertyCoverageSpec(
            description=_v(row, "policy.property.additional_coverage.name"),
            code       =_v(row, "policy.property.additional_coverage.code"),
            limit1     =_v(row, "policy.property.additional_coverage.each_claim_limit"),
            limit2     =_v(row, "policy.property.additional_coverage.aggregate_limit"),
            deductible =_v(row, "policy.property.additional_coverage.deductible"),
            form_number=_v(row, "policy.property.additional_coverage.form_number"),
        )
        for row in (rep.get("policy.property.additional_coverage") or [])
        if _v(row, "policy.property.additional_coverage.name")
    ]

    setup = PropertySetup(
        subjects=subjects,
        additional_interests=additional_interests,
        forms=forms,
        coverages=coverages,
        add_all_premises=True,
    )

    print(f"\n=== Property Setup ===")
    print(f"  {len(subjects)} subject(s)")
    print(f"  {len(additional_interests)} additional interest(s)")
    print(f"  {len(forms)} form(s)")
    print(f"  {len(coverages)} additional coverage(s)")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
        print(f"Connected — {page.title()}\n")
        ok = property_run(page, setup)
        print(f"\nproperty_run returned: {ok}")


if __name__ == "__main__":
    main()

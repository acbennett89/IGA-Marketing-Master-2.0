"""Focused test: BAUT Forms & Endorsements + Additional Coverages only.

Skips Coverages / Vehicles / AIs (which already passed in earlier runs) so we
can iterate fast on the F&E and AC selectors without re-entering 5+ vehicles.

Pre-condition: a BAUT line for STATE exists; the user has the EPIC browser
open at the BAUT tree (any sub-section is fine — we navigate from there).

Run from repo root:
    .venv/Scripts/python test_ba_forms_and_ac.py
"""
import sys
import json
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_business_auto import (
    AutoAdditionalCoverageSpec,
    BA_STANDARD_FORMS,
    _fill_ba_forms_endorsements,
    _fill_auto_additional_coverages,
    _nav_to_business_auto_state,
)

STATE       = "TN"
STATE_JSON  = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def main():
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    acs = [
        AutoAdditionalCoverageSpec(
            description=_v(row, "policy.auto.additional_coverage.name"),
            code       =_v(row, "policy.auto.additional_coverage.code"),
            limit1     =_v(row, "policy.auto.additional_coverage.each_claim_limit"),
            deductible =_v(row, "policy.auto.additional_coverage.deductible"),
        )
        for row in (rep.get("policy.auto.additional_coverage") or [])
        if _v(row, "policy.auto.additional_coverage.name")
    ]

    print(f"\n=== Focused F&E + AC test (state={STATE}) ===")
    print(f"  Forms       : {len(BA_STANDARD_FORMS)} (IGA standard)")
    for f in BA_STANDARD_FORMS:
        print(f"     - {f.name}")
    print(f"  Add'l Cov   : {len(acs)}")
    for ac in acs:
        print(f"     - desc={ac.description!r}  code={ac.code!r}  ded={ac.deductible!r}")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
        print(f"Connected — {page.title()}\n")

        if not _nav_to_business_auto_state(page, STATE):
            print(f"FAIL: could not nav to BAUT {STATE}")
            return

        print("\n--- Step 1: Forms & Endorsements ---")
        _fill_ba_forms_endorsements(page, BA_STANDARD_FORMS)

        print("\n--- Step 2: Additional Coverages ---")
        _fill_auto_additional_coverages(page, acs)

        print("\nDone.")


if __name__ == "__main__":
    main()

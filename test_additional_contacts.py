"""Focused test: add additional Individual + Business contacts to the open account.

Drives the real ``step_additional_contacts.run`` against the open EPIC browser
(CDP :9222). Exercises dedup (names already in the grid are skipped) and the
add paths (a new individual via Finish; a new business via the Detail page →
FEIN + Business Type → Save + close).

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the created account (IGAE2ET-01 / "IGA E2E Test 20260526-153708"),
    ideally Account Detail or the Contacts grid, no contact open.

Run from repo root (ONCE per fresh session — the CRM screen degrades on heavy
cycling):
    .venv/Scripts/python test_additional_contacts.py
"""
from __future__ import annotations

import sys
import logging

sys.path.insert(0, r"src")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("playwright").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.INFO)

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps import step_additional_contacts as sac
from iga_marketing_master_2.epic_steps.step_additional_contacts import (
    AdditionalContactsSetup,
    IndividualContact,
    BusinessContact,
)

SETUP = AdditionalContactsSetup(
    individuals=[
        # Already the primary contact → should be SKIPPED (dedup).
        IndividualContact(first_name="Dana", last_name="Reynolds",
                          email="dana.reynolds@acmewidgets.example.com",
                          phone="(615) 555-0101"),
        # New point of contact → ADDED via Finish. Exercises: name → Use-account
        # address → phone (type Business + number) → email.
        IndividualContact(first_name="Pat", last_name="Sample",
                          email="pat.sample@example.com", phone="(615) 555-0123"),
    ],
    businesses=[
        # Same as the account's main business entity → should be SKIPPED.
        BusinessContact(entity_name="IGA E2E Test 20260526-153708"),
        # New named insured → ADDED via the Detail page (FEIN + Business Type).
        # Has its own street + state (manual address path); phone/email skipped.
        BusinessContact(entity_name="Acme Subsidiary LLC",
                        business_type="Limited Liability Company",
                        fein="98-7654321",
                        street_address="200 Commerce Way",
                        state="TN"),
    ],
)


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "appliedepic.com" in p.url), ctx.pages[0])

        print("\n=== Additional Contacts focused test ===")
        print(f"  Page        : {page.title()}")
        print(f"  Individuals : {[i.display_name for i in SETUP.individuals]}")
        print(f"  Businesses  : {[b.display_name for b in SETUP.businesses]}")
        print("  Expect      : Dana Reynolds + IGA E2E Test skipped (dup); "
              "Sam Tester + Acme Subsidiary LLC added")
        print()

        res = sac.run(page, SETUP)

        print(f"\nResult: added={res.added}, skipped(dup)={res.skipped}, failed={res.failed}")
        # Expected: 2 added (Sam Tester, Acme Subsidiary LLC), 2 skipped, 0 failed.
        return 0 if res.failed == 0 and res.added >= 1 else 1


if __name__ == "__main__":
    sys.exit(main())

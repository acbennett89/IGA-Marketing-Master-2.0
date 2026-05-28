"""Focused test: write the insured's FEIN onto the Main Business Contact.

Drives the real ``step_account_create._add_fein_to_contact`` against the
open EPIC browser (CDP :9222). Verifies the masked FEIN is stripped to
9 digits before entry.

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On a created account (e.g. IGAE2ET-01 / "IGA E2E Test 20260526-153708"),
    ideally on Account Detail with no contact tab open.

Run from repo root:
    .venv/Scripts/python test_fein_contact.py
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
from iga_marketing_master_2.epic_steps import step_account_create as sac

# The account currently open in the browser.
ACCOUNT_NAME = "IGA E2E Test 20260526-153708"
# Masked form as the Client page stores it — should be stripped to 123456789.
TEST_FEIN = "12-3456789"
# Exact EPIC Business Type label (as the Client-page dropdown supplies).
TEST_BUSINESS_TYPE = "Corporation"


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "appliedepic.com" in p.url), ctx.pages[0])

        print("\n=== FEIN + Business Type on-contact focused test ===")
        print(f"  Page         : {page.title()}")
        print(f"  Account name : {ACCOUNT_NAME}")
        print(f"  Raw FEIN     : {TEST_FEIN!r}")
        print(f"  Expect entry : {sac._normalize_fein(TEST_FEIN)!r}")
        print(f"  Business Type: {TEST_BUSINESS_TYPE!r}")
        print()

        ok = sac._add_fein_to_contact(
            page, ACCOUNT_NAME, TEST_FEIN, TEST_BUSINESS_TYPE,
        )

        print(f"\n_add_fein_to_contact returned: {ok}")
        print("(contact is closed on success — re-open in EPIC to eyeball values)")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

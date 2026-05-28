"""Focused test: ONLY the Inland Marine Additional Interests section.

Runs the rewritten ``_fill_im_additional_interests`` against the open
EPIC browser (CDP :9222). All 5 AI items in the diagnostic state.json
are entered.

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the submission with an Inland Marine line.
  - **Stray AI rows cleaned up.** The Add-click diagnostic dump left an
    empty row in EPIC's AI grid; delete it manually before running, or
    this test will start adding rows underneath the stray.

Run from repo root:
    .venv/Scripts/python test_im_ai_only.py
"""
from __future__ import annotations

import sys
import json
import logging

sys.path.insert(0, r"src")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("playwright").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_inland_marine import (
    IMAdditionalInterestSpec,
    _fill_im_additional_interests,
    _nav_to_im,
    list_available_im_lines,
)
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps._page_health import EPICHardError

AI_LIMIT   = 5
STATE      = ""
STATE_JSON = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    return str(f.get("value", "") or "") if isinstance(f, dict) else ""


def main() -> int:
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    all_ai = [
        IMAdditionalInterestSpec(
            name           =_v(r, "policy.inland_marine.additional_interest.name"),
            interest_type  =_v(r, "policy.inland_marine.additional_interest.interest"),
            address_line_1 =_v(r, "policy.inland_marine.additional_interest.primary_address.line_1"),
            reason_for_int =_v(r, "policy.inland_marine.additional_interest.reason_for_int"),
            item_number    =_v(r, "policy.inland_marine.additional_interest.item_number"),
        )
        for r in (rep.get("policy.inland_marine.additional_interest") or [])
        if _v(r, "policy.inland_marine.additional_interest.name")
    ]
    ai_list = all_ai[:AI_LIMIT]

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        avail = list_available_im_lines(page)
        if not avail:
            print(f"\nNo IM lines visible on {page.title()!r}.")
            return 1
        target_state = STATE.upper() if STATE else (avail[0].get("state") or "")

        print("\n=== Additional Interests focused test ===")
        print(f"  Page              : {page.title()}")
        print(f"  IM lines available: {avail}")
        print(f"  Target state      : {target_state or '(unsuffixed)'}")
        print(f"  AI items (capped) : {len(ai_list)} of {len(all_ai)} (cap={AI_LIMIT})")
        for i, ai in enumerate(ai_list, 1):
            print(f"    [{i}] {ai.name!r} ({ai.interest_type or '-'}) item#={ai.item_number or '-'}")
            print(f"        addr: {ai.address_line_1 or '-'}")
            if ai.reason_for_int:
                print(f"        reason: {ai.reason_for_int}")
        print()

        if not _nav_to_im(page, target_state):
            print("FAILED: could not select the IM line in the sidebar.")
            return 1

        try:
            _fill_im_additional_interests(page, ai_list)
            print(f"\nAdditional Interests: {len(ai_list)} row(s) attempted.")
            return 0
        except EPICHardError as exc:
            print(f"\nHARD ERROR — run would terminate: {exc.health.summary}")
            print(f"  detail: {exc.health.detail[:500]}")
            return 2
        except EntryCancelled as exc:
            print(f"\nCANCELLED: {exc}")
            return 3


if __name__ == "__main__":
    sys.exit(main())

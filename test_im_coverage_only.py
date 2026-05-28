"""Focused test: ONLY the Inland Marine Coverage/Deductible section.

Runs the new safe_action / assert_clean code path against the open EPIC
browser (CDP port 9222) without touching any other IM section.

Pre-conditions (do these in EPIC before running):
  1. Browser open via Launch Browser (CDP on :9222).
  2. Navigated to the submission that has an Inland Marine line.
  3. Submission Detail sidebar visible (the IM script clicks the IM tree
     node itself — you do not need to click into it first, but you DO need
     the submission tree loaded).

Run from repo root:
    .venv/Scripts/python test_im_coverage_only.py
"""
import sys
import json
import logging

sys.path.insert(0, r"src")

logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_inland_marine import (
    InlandMarineSetup,
    IMScheduledItemSpec,
    _fill_coverage_deductible,
    _nav_to_im,
    list_available_im_lines,
)
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps._page_health import EPICHardError

STATE      = ""   # "" picks the first IM line on the submission
STATE_JSON = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    return str(f.get("value", "") or "") if isinstance(f, dict) else ""


def _sv(state, key):
    f = state.get("fields", {}).get(key)
    return str(f.get("value", "") or "") if isinstance(f, dict) else ""


def main() -> int:
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    sched = [
        IMScheduledItemSpec(
            item_number  =_v(r, "policy.inland_marine.scheduled_item.item_number"),
            amt_insurance=_v(r, "policy.inland_marine.scheduled_item.amt_insurance"),
        )
        for r in (rep.get("policy.inland_marine.scheduled_item") or [])
        if _v(r, "policy.inland_marine.scheduled_item.amt_insurance")
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        available = list_available_im_lines(page)
        if not available:
            print(f"\nNo IM lines visible on {page.title()!r}.")
            print("Open the submission with an Inland Marine line first.")
            return 1
        target_state = STATE.upper() if STATE else (available[0].get("state") or "")

        setup = InlandMarineSetup(
            state                          =target_state,
            total_scheduled_amount         =_sv(state, "policy.inland_marine.total_scheduled_amount"),
            acv_replacement_cost_deductible=_sv(state, "policy.inland_marine.acv_replacement_cost_deductible"),
            scheduled_items                =sched,
        )

        print("\n=== Coverage/Deductible-only test ===")
        print(f"  IM lines available     : {available}")
        print(f"  Target state           : {target_state or '(unsuffixed)'}")
        print(f"  Scheduled items (sum)  : {len(sched)} row(s)")
        print(f"  ACV/RC deductible      : {setup.acv_replacement_cost_deductible!r}")
        print(f"  Page                   : {page.title()}")
        print()

        if not _nav_to_im(page, setup.state):
            print("FAILED: could not select the IM line in the sidebar.")
            return 1

        try:
            _fill_coverage_deductible(page, setup)
            print("\nCoverage/Deductible: filled cleanly.")
            return 0
        except EPICHardError as exc:
            print(f"\nHARD ERROR (run would terminate): {exc.health.summary}")
            print(f"  detail: {exc.health.detail[:400]}")
            return 2
        except EntryCancelled as exc:
            print(f"\nCANCELLED: {exc}")
            return 3


if __name__ == "__main__":
    sys.exit(main())

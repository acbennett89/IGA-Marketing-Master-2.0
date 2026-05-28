"""Focused test: ONLY the Inland Marine Unscheduled Equipment section.

Runs the rewritten ``_fill_unscheduled_equipment`` against the open
EPIC browser (CDP :9222). All 5 sample items in the diagnostic
state.json are entered. Descriptions over 30 characters are sent
through Claude (insurance-shorthand abbreviation) before EPIC entry —
the original full text is preserved in state.json and the GUI shows it
on hover.

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the submission with an Inland Marine line.

Run from repo root:
    .venv/Scripts/python test_im_unsched_only.py
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
    IMUnscheduledItemSpec,
    _fill_unscheduled_equipment,
    _nav_to_im,
    list_available_im_lines,
)
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps._page_health import EPICHardError

UNSCHED_LIMIT = 5
STATE         = ""
STATE_JSON    = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    return str(f.get("value", "") or "") if isinstance(f, dict) else ""


def main() -> int:
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    all_unsched = [
        IMUnscheduledItemSpec(
            description      =_v(r, "policy.inland_marine.unscheduled_item.description"),
            description_short=_v(r, "policy.inland_marine.unscheduled_item.description_short"),
            amt_insurance    =_v(r, "policy.inland_marine.unscheduled_item.amt_insurance"),
        )
        for r in (rep.get("policy.inland_marine.unscheduled_item") or [])
        if _v(r, "policy.inland_marine.unscheduled_item.description")
    ]
    unsched = all_unsched[:UNSCHED_LIMIT]

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        avail = list_available_im_lines(page)
        if not avail:
            print(f"\nNo IM lines visible on {page.title()!r}.")
            return 1
        target_state = STATE.upper() if STATE else (avail[0].get("state") or "")

        print("\n=== Unscheduled Equipment focused test ===")
        print(f"  Page              : {page.title()}")
        print(f"  IM lines available: {avail}")
        print(f"  Target state      : {target_state or '(unsuffixed)'}")
        print(f"  Items (capped)    : {len(unsched)} of {len(all_unsched)} (cap={UNSCHED_LIMIT})")
        for i, it in enumerate(unsched, 1):
            d = it.description or ""
            flag = "ABBREV" if len(d) > 30 else "fits"
            print(f"    [{i}] ({len(d):3} chars / {flag}) {d!r}  --  ${it.amt_insurance or '0'}")
        print()

        if not _nav_to_im(page, target_state):
            print("FAILED: could not select the IM line in the sidebar.")
            return 1

        try:
            _fill_unscheduled_equipment(page, unsched)
            print(f"\nUnscheduled Equipment: {len(unsched)} row(s) attempted.")
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

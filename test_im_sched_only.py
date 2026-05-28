"""Focused test: ONLY the Inland Marine Scheduled Equipment section.

Caps the list at 5 items for fast iteration. Runs the rewritten
``_fill_scheduled_equipment`` against the open EPIC browser (CDP :9222).
No other IM sections touched.

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the submission with an Inland Marine line.
  - Submission Detail sidebar visible. The script selects the IM line
    itself; you do not need to pre-click it.

Run from repo root:
    .venv/Scripts/python test_im_sched_only.py
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
# Quieten Playwright's own debug spam.
logging.getLogger("playwright").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.INFO)

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_inland_marine import (
    IMScheduledItemSpec,
    _fill_scheduled_equipment,
    _nav_to_im,
    list_available_im_lines,
)
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps._page_health import EPICHardError

SCHEDULED_LIMIT = 5
STATE           = ""
STATE_JSON      = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    return str(f.get("value", "") or "") if isinstance(f, dict) else ""


def main() -> int:
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    all_sched = [
        IMScheduledItemSpec(
            item_number  =_v(r, "policy.inland_marine.scheduled_item.item_number"),
            type         =_v(r, "policy.inland_marine.scheduled_item.type"),
            manufacturer =_v(r, "policy.inland_marine.scheduled_item.manufacturer"),
            model        =_v(r, "policy.inland_marine.scheduled_item.model"),
            model_year   =_v(r, "policy.inland_marine.scheduled_item.model_year"),
            description  =_v(r, "policy.inland_marine.scheduled_item.description"),
            serial_number=_v(r, "policy.inland_marine.scheduled_item.serial_number"),
            amt_insurance=_v(r, "policy.inland_marine.scheduled_item.amt_insurance"),
            deductible   =_v(r, "policy.inland_marine.scheduled_item.deductible"),
        )
        for r in (rep.get("policy.inland_marine.scheduled_item") or [])
        if _v(r, "policy.inland_marine.scheduled_item.description")
        or _v(r, "policy.inland_marine.scheduled_item.amt_insurance")
    ]
    sched = all_sched[:SCHEDULED_LIMIT]

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        avail = list_available_im_lines(page)
        if not avail:
            print(f"\nNo IM lines visible on {page.title()!r}.")
            return 1
        target_state = STATE.upper() if STATE else (avail[0].get("state") or "")

        print("\n=== Scheduled Equipment focused test ===")
        print(f"  Page              : {page.title()}")
        print(f"  IM lines available: {avail}")
        print(f"  Target state      : {target_state or '(unsuffixed)'}")
        print(f"  Items (capped)    : {len(sched)} of {len(all_sched)} (cap={SCHEDULED_LIMIT})")
        for i, it in enumerate(sched, 1):
            print(
                f"    [{i}] #{it.item_number or '-':<3} "
                f"{it.model_year or '----':<4} {it.manufacturer or '-':<14} "
                f"{it.model or '-':<14} {(it.description or '-')[:38]:<38} "
                f"${it.amt_insurance or '0'}"
            )
        print()

        if not _nav_to_im(page, target_state):
            print("FAILED: could not select the IM line in the sidebar.")
            return 1

        try:
            _fill_scheduled_equipment(page, sched)
            print(f"\nScheduled Equipment: {len(sched)} row(s) attempted.")
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

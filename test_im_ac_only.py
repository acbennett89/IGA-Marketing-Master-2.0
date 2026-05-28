"""Focused test: ONLY the Inland Marine Additional Coverages section.

Runs the rewritten ``_fill_im_additional_coverages`` against the open
EPIC browser (CDP :9222). 5 items from state.json.

**Auto-cleanup**: before adding, deletes any existing rows in the AC
grid so the test starts from a clean slate. This means you don't need
to manually clean up between iterations.

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the submission with an Inland Marine line.

Run from repo root:
    .venv/Scripts/python test_im_ac_only.py
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
    IMAdditionalCoverageSpec,
    _fill_im_additional_coverages,
    _nav_to_im,
    list_available_im_lines,
)
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps._page_health import EPICHardError

AC_LIMIT   = 5
STATE      = ""
STATE_JSON = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    return str(f.get("value", "") or "") if isinstance(f, dict) else ""


def main() -> int:
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    all_ac = [
        IMAdditionalCoverageSpec(
            description=_v(r, "policy.inland_marine.additional_coverage.name"),
            code       =_v(r, "policy.inland_marine.additional_coverage.code"),
            each_claim =_v(r, "policy.inland_marine.additional_coverage.each_claim_limit"),
            deductible =_v(r, "policy.inland_marine.additional_coverage.deductible"),
            item_number=_v(r, "policy.inland_marine.additional_coverage.item_number"),
        )
        for r in (rep.get("policy.inland_marine.additional_coverage") or [])
        if _v(r, "policy.inland_marine.additional_coverage.name")
    ]
    ac_list = all_ac[:AC_LIMIT]

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "appliedepic.com" in p.url), ctx.pages[0])

        avail = list_available_im_lines(page)
        if not avail:
            print(f"\nNo IM lines visible on {page.title()!r}.")
            return 1
        target_state = STATE.upper() if STATE else (avail[0].get("state") or "")

        print("\n=== Additional Coverages focused test ===")
        print(f"  Page              : {page.title()}")
        print(f"  IM lines available: {avail}")
        print(f"  Target state      : {target_state or '(unsuffixed)'}")
        print(f"  AC items (capped) : {len(ac_list)} of {len(all_ac)} (cap={AC_LIMIT})")
        for i, ac in enumerate(ac_list, 1):
            print(
                f"    [{i}] code={ac.code or '-':<5} desc={ac.description!r}  "
                f"limit=${ac.each_claim or '0'}  ded=${ac.deductible or '0'}"
            )
        print()

        if not _nav_to_im(page, target_state):
            print("FAILED: could not select the IM line in the sidebar.")
            return 1

        # No pre-clean: _fill_im_additional_coverages reuses any
        # pre-existing rows as scratch space for the test data (EPIC
        # blocks new Adds when an in-progress row is invalid, so
        # filling-in-place is more robust than delete-then-add).

        try:
            _fill_im_additional_coverages(page, ac_list)
            print(f"\nAdditional Coverages: {len(ac_list)} row(s) attempted.")
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

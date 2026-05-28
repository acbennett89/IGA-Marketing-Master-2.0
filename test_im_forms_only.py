"""Focused test: ONLY the Inland Marine Forms & Endorsements section.

Runs ``_fill_im_forms_endorsements`` against the open EPIC browser
(CDP :9222) using the project's IM_STANDARD_FORMS list (the IGA
"Inland Marine Extension Endorsement").

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the submission with an Inland Marine line already created.

Run from repo root:
    .venv/Scripts/python test_im_forms_only.py
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
logging.getLogger("httpx").setLevel(logging.WARNING)

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_inland_marine import (
    IM_STANDARD_FORMS,
    _fill_im_forms_endorsements,
    _nav_to_im,
    list_available_im_lines,
)
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps._page_health import EPICHardError

STATE = ""  # "" picks the first IM line


def main() -> int:
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        ctx = browser.contexts[0]
        page = next((p for p in ctx.pages if "appliedepic.com" in p.url), ctx.pages[0])

        avail = list_available_im_lines(page)
        if not avail:
            print(f"\nNo IM lines visible on {page.title()!r}.")
            return 1
        target_state = STATE.upper() if STATE else (avail[0].get("state") or "")

        print("\n=== Forms & Endorsements focused test ===")
        print(f"  Page              : {page.title()}")
        print(f"  IM lines available: {avail}")
        print(f"  Target state      : {target_state or '(unsuffixed)'}")
        print(f"  Forms to add      : {len(IM_STANDARD_FORMS)}")
        for i, f in enumerate(IM_STANDARD_FORMS, 1):
            print(f"    [{i}] {f.name!r}")
        print()

        if not _nav_to_im(page, target_state):
            print("FAILED: could not select the IM line in the sidebar.")
            return 1

        try:
            _fill_im_forms_endorsements(page, IM_STANDARD_FORMS)
            print(f"\nForms & Endorsements: {len(IM_STANDARD_FORMS)} form(s) attempted.")
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

"""Standalone test for the Commercial Umbrella entry step.

Loads real state.json data and runs the Umbrella step against the open EPIC
browser (CDP port 9222).

Pre-condition: a Commercial Umbrella line must exist on the open submission.

Run from repo root:
    .venv/Scripts/python test_umbrella.py
"""
import sys
import json
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_umbrella import (
    UmbrellaSetup,
    UmbrellaUnderlyingSpec,
    UmbrellaAdditionalInterestSpec,
    UmbrellaAdditionalCoverageSpec,
    list_available_umbrella_lines,
    run as umbrella_run,
)

STATE_JSON = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    f = row.get(key) if isinstance(row, dict) else None
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def _sv(state, key):
    f = state.get("fields", {}).get(key)
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def main():
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    underlying = [
        UmbrellaUnderlyingSpec(
            carrier=_v(row, "policy.umbrella.underlying.other.carrier"),
            desc   =_v(row, "policy.umbrella.underlying.other.desc"),
            limit  =_v(row, "policy.umbrella.underlying.other.limit"),
        )
        for row in (rep.get("policy.umbrella.underlying.other") or [])
        if _v(row, "policy.umbrella.underlying.other.carrier")
        or _v(row, "policy.umbrella.underlying.other.desc")
    ]

    # State.json has no umbrella-specific AI or AC repeatables in the diagnostic
    # data — pull from the policy-wide AI repeatable if it exists, else empty.
    ais: list = []
    acs: list = []

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        available = list_available_umbrella_lines(page)
        if not available:
            print(f"\nNo Umbrella lines on the open submission ({page.title()}).")
            print("Create a Commercial Umbrella line in EPIC first.")
            return

        setup = UmbrellaSetup(
            expiring_pol_num=_sv(state, "policy.umbrella.expiring_pol_num"),
            occurrence_limit=_sv(state, "policy.umbrella.occurrence_limit"),
            retained_limit  =_sv(state, "policy.umbrella.retained_limit"),
            underlying           =underlying,
            additional_interests =ais,
            additional_coverages =acs,
        )

        print(f"\n=== Commercial Umbrella Setup ===")
        print(f"  Umbrella lines on submission: {available}")
        print(f"  Expiring policy #     : {setup.expiring_pol_num!r}")
        print(f"  Occurrence limit      : {setup.occurrence_limit!r}")
        print(f"  Retained limit        : {setup.retained_limit!r}")
        print(f"  Underlying rows       : {len(underlying)}")
        for u in underlying:
            print(f"     - {u.carrier!r}  {u.desc!r}  limit={u.limit!r}")
        print()
        print(f"Connected — {page.title()}\n")
        ok = umbrella_run(page, setup)
        print(f"\numbrella_run returned: {ok}")


if __name__ == "__main__":
    main()

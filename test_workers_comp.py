"""Standalone test for the Workers' Compensation entry step.

Loads real state.json for the _DIAGNOSTIC client and runs the WCOM step
(Policy Info → Total Premium Calcs → Forms) against the open EPIC browser
(CDP port 9222).

STATE auto-falls-back to the first available WCOM line on the open submission
if the configured value isn't present.

Pre-condition: a WCOM line for some state already exists on the open submission.

Run from repo root:
    .venv/Scripts/python test_workers_comp.py
"""
import sys
import json
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_workers_comp import (
    WorkersCompSetup,
    WcRatingInfoSpec,
    WcClassCodeSpec,
    WcLocationSpec,
    list_available_wcom_states,
    run as workers_comp_run,
)
from iga_marketing_master_2.epic_steps.step_business_auto import _split_us_address

# ── Test config ──────────────────────────────────────────────────────────────
STATE      = "TN"   # preferred WCOM line; falls back to whichever is available
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

    rating_info = [
        WcRatingInfoSpec(
            state         =_v(row, "policy.workers_comp.rating_info.state"),
            experience_mod=_v(row, "policy.workers_comp.rating_info.experience_mod"),
            deductible    =_v(row, "policy.workers_comp.rating_info.deductible"),
        )
        for row in (rep.get("policy.workers_comp.rating_info") or [])
        if _v(row, "policy.workers_comp.rating_info.state")
    ]

    class_codes = [
        WcClassCodeSpec(
            state       =_v(row, "policy.workers_comp.class_code.state"),
            class_code  =_v(row, "policy.workers_comp.class_code.class_code"),
            description =_v(row, "policy.workers_comp.class_code.description_code"),
            payroll     =_v(row, "policy.workers_comp.class_code.payroll"),
        )
        for row in (rep.get("policy.workers_comp.class_code") or [])
        if _v(row, "policy.workers_comp.class_code.class_code")
    ]

    # Build one WC Location per unique state present in the class codes.
    # State.json's only address source for this client is the account-level
    # mailing address (a single comma-separated string at
    # `account.named_insured.mailing_address.line_1`).  We parse it and use the
    # same address for every WC location — EPIC requires a non-blank address
    # for each Location row.  When `policy.location` rows exist with their
    # own per-state addresses, those would be preferred (not present here).
    fallback_addr_raw = _sv(state, "account.named_insured.mailing_address.line_1")
    fb_street, fb_city, fb_state, fb_zip = _split_us_address(fallback_addr_raw or "")

    wc_states_seen: list = []
    for cc in class_codes:
        s = cc.state.upper()
        if s and s not in wc_states_seen:
            wc_states_seen.append(s)
    wc_locations = []
    for idx, s in enumerate(wc_states_seen, 1):
        # Use the per-state address from policy.location if present, else the
        # account mailing address.  State code on the location overrides the
        # one parsed from the fallback string (which may be from a different state).
        wc_locations.append(WcLocationSpec(
            loc_num=str(idx), state=s,
            street=fb_street, city=fb_city, zip_code=fb_zip,
        ))

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        available = list_available_wcom_states(page)
        if not available:
            print(f"\nNo WCOM lines found on the open submission ({page.title()}).")
            print("Create at least one WCOM line in EPIC before running this test.")
            return
        target_state = STATE.upper() if STATE and STATE.upper() in available else available[0]
        if target_state != (STATE or "").upper():
            print(f"\nNote: STATE={STATE!r} not found (available: {available}). Using {target_state!r}.")

        setup = WorkersCompSetup(
            state                =target_state,
            each_accident        =_sv(state, "policy.workers_comp.each_accident"),
            disease_each_employee=_sv(state, "policy.workers_comp.disease_each_employee"),
            disease_policy_limit =_sv(state, "policy.workers_comp.disease_policy_limit"),
            part1_states         =_sv(state, "policy.workers_comp.part1_states"),
            part3_states         =_sv(state, "policy.workers_comp.part3_states"),
            locations            =wc_locations,
            rating_info          =rating_info,
            class_codes          =class_codes,
        )

        print(f"\n=== Workers' Compensation Setup (state={target_state}) ===")
        print(f"  Part 1 states  : {setup.part1_states!r}")
        print(f"  Part 3 states  : {setup.part3_states!r}")
        print(f"  Each accident  : {setup.each_accident!r}")
        print(f"  Disease ea emp : {setup.disease_each_employee!r}")
        print(f"  Disease pol    : {setup.disease_policy_limit!r}")
        print(f"  Locations      : {len(wc_locations)}")
        for L in wc_locations:
            print(f"     - Loc {L.loc_num} {L.state}: street={L.street!r} city={L.city!r} zip={L.zip_code!r}")
        print(f"  Rating rows    : {len(rating_info)}")
        for r in rating_info:
            print(f"     - {r.state}: exp_mod={r.experience_mod!r} ded={r.deductible!r}")
        print(f"  Class codes    : {len(class_codes)}")
        for cc in class_codes:
            print(f"     - {cc.state} {cc.class_code}: payroll={cc.payroll!r}")
        print()

        print(f"Connected — {page.title()}\n")
        ok = workers_comp_run(page, setup)
        print(f"\nworkers_comp_run returned: {ok}")


if __name__ == "__main__":
    main()

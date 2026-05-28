"""Standalone test for the Business Auto entry step.

Loads real state.json for the _DIAGNOSTIC client and runs the entire BAUT step
(Coverages → Vehicles → Additional Interests → Additional Coverages) against the
open EPIC browser (CDP port 9222).

VEHICLE CAP: limits vehicle entry to 5 rows for fast iteration during testing.
Edit VEHICLE_LIMIT below (or set to None) to run the full set.

Set STATE below to choose which BAUT line to drive: "TN", "KY", etc.

Pre-condition: a BAUT line for STATE already exists on the open submission.

Run from repo root:
    .venv/Scripts/python test_business_auto.py
"""
import sys
import json
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_business_auto import (
    BusinessAutoSetup,
    AutoCoverageSpec,
    VehicleSpec,
    AutoAdditionalInterestSpec,
    AutoAdditionalCoverageSpec,
    list_available_baut_states,
    run as business_auto_run,
)

# ── Test config ──────────────────────────────────────────────────────────────
# Preferred state — script falls back to whichever BAUT line(s) actually exist
# on the open submission.  Set to None to skip the preference and drive the
# first available state.
STATE         = "TN"
VEHICLE_LIMIT = 5        # cap vehicles for fast iteration; set to None for all
STATE_JSON    = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


def _v(row, key):
    """Read a value out of a repeatable row by domain tag."""
    f = row.get(key) if isinstance(row, dict) else None
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def _sv(state, key):
    """Read a singleton field value by domain tag."""
    f = state.get("fields", {}).get(key)
    if isinstance(f, dict):
        return str(f.get("value", "") or "")
    return ""


def main():
    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    # --- Coverages (singletons in state.json) ---
    cov = AutoCoverageSpec(
        liability_symbols        =_sv(state, "policy.auto.liability.symbols"),
        liability_csl_limit1     =_sv(state, "policy.auto.liability_csl_limit1"),
        liability_bi_limit1      =_sv(state, "policy.auto.liability_bi_limit1"),
        liability_bi_limit2      =_sv(state, "policy.auto.liability_bi_limit2"),
        liability_pd_limit1      =_sv(state, "policy.auto.liability_pd_limit1"),
        medical_symbols          =_sv(state, "policy.auto.medical.symbols"),
        medical_limit1           =_sv(state, "policy.auto.medical_limit1"),
        uninsured_symbols        =_sv(state, "policy.auto.uninsured.symbols"),
        uninsured_csl_limit1     =_sv(state, "policy.auto.uninsured_csl_limit1"),
        uninsured_bi_limit1      =_sv(state, "policy.auto.uninsured_bi_limit1"),
        uninsured_bi_limit2      =_sv(state, "policy.auto.uninsured_bi_limit2"),
        uninsured_pd_each_accident=_sv(state, "policy.auto.uninsured_pd_each_accident"),
        uninsured_pd_deductible  =_sv(state, "policy.auto.uninsured_pd_deductible"),
        towing_symbols           =_sv(state, "policy.auto.towing.symbols"),
        towing_limit1            =_sv(state, "policy.auto.towing_limit1"),
        comprehensive_symbols    =_sv(state, "policy.auto.comprehensive.symbols"),
        comprehensive_deductible1=_sv(state, "policy.auto.comprehensive_deductible1"),
        cause_of_loss_symbols    =_sv(state, "policy.auto.specified_causes_loss.symbols"),
        cause_of_loss_deductible1=_sv(state, "policy.auto.cause_of_loss_deductible1"),
        collision_symbols        =_sv(state, "policy.auto.collision.symbols"),
        collision_deductible1    =_sv(state, "policy.auto.collision_deductible1"),
    )

    # --- Vehicles (repeatable, capped for testing) ---
    # vehicle_num is renumbered sequentially 1..N to match the IGA GUI display
    # (the dec-page unit numbers in policy.auto.vehicle.vehicle_num are noisy:
    # they have gaps and duplicates from extraction).
    _veh_rows = [
        row for row in (rep.get("policy.auto.vehicle") or [])
        if _v(row, "policy.auto.vehicle.vin") or _v(row, "policy.auto.vehicle.year")
    ]
    all_vehicles = [
        VehicleSpec(
            vehicle_num   =str(idx),
            year          =_v(row, "policy.auto.vehicle.year"),
            make          =_v(row, "policy.auto.vehicle.make"),
            model         =_v(row, "policy.auto.vehicle.model"),
            vin           =_v(row, "policy.auto.vehicle.vin"),
            body_type     =_v(row, "policy.auto.vehicle.body_type"),
            garage_address=_v(row, "policy.auto.vehicle.garage_address.line_1"),
            # Rating tab — only Cost New + Class Code per IGA spec
            cost_new      =_v(row, "policy.auto.vehicle.cost_new"),
            class_code    =_v(row, "policy.auto.vehicle.class_code"),
            # Coverages tab
            valuation_type=_v(row, "policy.auto.vehicle.valuation_type"),
            comprehensive_deductible=_v(row, "policy.auto.vehicle.comprehensive_deductible"),
            collision_deductible    =_v(row, "policy.auto.vehicle.collision_deductible"),
        )
        for idx, row in enumerate(_veh_rows, start=1)
    ]
    vehicles = all_vehicles[:VEHICLE_LIMIT] if VEHICLE_LIMIT else all_vehicles

    # --- Additional Interests (repeatable) ---
    ais = [
        AutoAdditionalInterestSpec(
            name          =_v(row, "policy.auto.additional_interest.name"),
            interest_type =_v(row, "policy.auto.additional_interest.interest"),
            vehicle_number=_v(row, "policy.auto.additional_interest.vehicle_number"),
            address_line_1=_v(row, "policy.auto.additional_interest.primary_address.line_1"),
        )
        for row in (rep.get("policy.auto.additional_interest") or [])
        if _v(row, "policy.auto.additional_interest.name")
    ]

    # --- Additional Coverages (repeatable) ---
    acs = [
        AutoAdditionalCoverageSpec(
            description=_v(row, "policy.auto.additional_coverage.name"),
            code       =_v(row, "policy.auto.additional_coverage.code"),
            limit1     =_v(row, "policy.auto.additional_coverage.each_claim_limit"),
            deductible =_v(row, "policy.auto.additional_coverage.deductible"),
        )
        for row in (rep.get("policy.auto.additional_coverage") or [])
        if _v(row, "policy.auto.additional_coverage.name")
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        # Discover which BAUT states actually exist on the open submission
        # and resolve STATE to a real one (or fail clearly if none match).
        available = list_available_baut_states(page)
        if not available:
            print(f"\nNo BAUT lines found on the open submission ({page.title()}).")
            print("Create at least one BAUT line in EPIC before running this test.")
            return
        if STATE and STATE.upper() in available:
            target_state = STATE.upper()
        else:
            target_state = available[0]
            if STATE:
                print(
                    f"\nNote: STATE={STATE!r} not found on this submission "
                    f"(available: {available}). Falling back to {target_state!r}."
                )
            else:
                print(f"\nUsing first available BAUT line: {target_state} (available: {available}).")

        setup = BusinessAutoSetup(
            state               =target_state,
            coverages           =cov,
            vehicles            =vehicles,
            additional_interests=ais,
            additional_coverages=acs,
        )

        print(f"\n=== Business Auto Setup (state={target_state}) ===")
        print(f"  Coverages: liability_csl={cov.liability_csl_limit1!r}  liab_symbols={cov.liability_symbols!r}")
        print(f"  Vehicles : {len(vehicles)} (of {len(all_vehicles)} total{' — CAPPED' if VEHICLE_LIMIT and len(all_vehicles) > VEHICLE_LIMIT else ''})")
        print(f"  AIs      : {len(ais)}")
        print(f"  ACs      : {len(acs)}")
        print()
        print(f"Connected — {page.title()}\n")
        ok = business_auto_run(page, setup)
        print(f"\nbusiness_auto_run returned: {ok}")


if __name__ == "__main__":
    main()

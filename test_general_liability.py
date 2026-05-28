"""Quick live test of step_general_liability against the open EPIC browser (CDP port 9222).

Expects the browser to already be on the Submission Detail page for an open MMS
with the General Liability subtree visible in the left sidebar.

Run from the repo root:
    .venv\Scripts\python test_general_liability.py
"""
import sys
sys.path.insert(0, r"src")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_general_liability import (
    ContractorsSpec,
    GlCoveragesSpec,
    GeneralLiabilitySetup,
    HazardSpec,
    run as gl_run,
)

SETUP = GeneralLiabilitySetup(
    coverages=GlCoveragesSpec(
        each_occ_limit="1000000",
        gen_aggr_limit="2000000",
        pers_adv_inj_limit="1000000",
        prod_oper_limit="2000000",
        med_limit="5000",
        dam_prem_limit="100000",
    ),
    hazards=[
        HazardSpec(
            class_code="91342",   # Contractors — subcontracted work
            exposure="250000",
            premium_basis="P",    # P = Payroll - Per $1,000/Pay
            loc_num=1,
            bldg_num=1,
        ),
    ],
    contractors=ContractorsSpec(
        num_full_time="5",
        num_part_time="2",
        percent_subcontract="10",
        dollars_subcontract="50000",
    ),
)

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
    page.set_viewport_size(None)
    print(f"Connected — page title: {page.title()}")
    ok = gl_run(page, SETUP)
    print(f"gl_run returned: {ok}")

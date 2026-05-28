"""Quick live test of step_account_create against the open EPIC browser (CDP port 9222).

Use this to iterate on the Add Account form without going through the full
Begin Entry flow each time:

  1. Launch EPIC in debug mode (Launch EPIC Debug.vbs or the app's --debug
     mode) — this exposes CDP on port 9222.
  2. Get past the login + database picker so you're sitting on the EPIC home
     or any account screen. The script will navigate to Account Locate itself.
  3. Edit SETUP below to whatever you want to test, then run:

         .venv\\Scripts\\python test_account_create.py

  4. Watch EPIC. The form opens, gets filled, and stops before Save (unless
     you flip SUBMIT below). You can Cancel the form manually and re-run
     immediately — no app restart needed.

Each run uses a unique account name (timestamp suffix) so EPIC's built-in
duplicate check doesn't gum up the works.
"""
from __future__ import annotations

import sys
from datetime import datetime

sys.path.insert(0, r"src")

from playwright.sync_api import sync_playwright

from iga_marketing_master_2.epic_steps import step_account_lookup_nav
from iga_marketing_master_2.epic_steps.step_account_create import (
    AccountSetup,
    AccountCreateOutcome,
    run as create_run,
)


# ── Knobs ────────────────────────────────────────────────────────────────────

# Set True to actually click Save Account. Default False so you can inspect
# the filled form, fix bugs, and re-run without creating real records.
SUBMIT = False

# Unique-per-run name so EPIC's dup check doesn't block iteration.
_STAMP = datetime.now().strftime("%Y%m%d-%H%M%S")
ACCOUNT_NAME = f"TEST CREATE {_STAMP}"

SETUP = AccountSetup(
    account_name=ACCOUNT_NAME,
    agency="IGA",
    branch="002",
    # Required-with-defaults — uncomment to override:
    # client_format="BUSINESS",
    # client_type="PROSPECT",
    # business_phone_type="Business",
    # primary_phone_type="Mobile",
    # contact_via="Email",
    # billing_pref="Email",
    # servicing_pref="Email",
    # Realistic prospect set — fill or comment out per the case you want to test.
    street_address="6640 Carothers Pkwy",
    business_phone="6155550100",
    business_email="ops@example.com",
    business_website="https://example.com",
    naics="332710",
    sic="3599",
    primary_first_name="Dana",
    primary_last_name="Reynolds",
    primary_phone="6155550101",
    primary_email="dana@example.com",
    lines_of_business=["COMMERCIAL"],
    comments=f"Created by test_account_create.py at {_STAMP}",
)


# ── Driver ───────────────────────────────────────────────────────────────────

def _monkeypatch_no_submit() -> None:
    """When SUBMIT is False, replace step_account_create._click_save with a no-op
    so the form stays open for inspection instead of being submitted.

    This keeps the rest of the step's flow intact (form open, fill, watch) —
    it just stops short of clicking the Save Account button. The post-save
    watcher then times out and reports VALIDATION_FAILED / ERROR, which is
    expected — read the populated form in EPIC, then Cancel and re-run.
    """
    from iga_marketing_master_2.epic_steps import step_account_create

    def _stub_click_save(page):
        print("[DRY RUN] would click Save Account — skipping (set SUBMIT=True to submit)")

    step_account_create._click_save = _stub_click_save


def _close_any_open_form(page) -> None:
    """If an Add Account form is still open from a previous run, Cancel +
    confirm Discard so we get back to Account Locate cleanly.

    No-op when the form isn't open.
    """
    if (page.title() or "").strip().lower() != "add client account":
        return
    print("Add Account form is open from a previous run — cancelling...")
    try:
        page.evaluate(
            """() => {
                const btn = Array.from(document.querySelectorAll('button'))
                    .find(b => b.innerText.trim() === 'Cancel' && b.offsetParent !== null);
                if (btn) btn.click();
            }"""
        )
        page.wait_for_timeout(500)
        page.evaluate(
            """() => {
                const yes = Array.from(document.querySelectorAll('button'))
                    .find(b => b.innerText.trim() === 'Yes' && b.offsetParent !== null);
                if (yes) yes.click();
            }"""
        )
        page.wait_for_timeout(500)
    except Exception as exc:
        print(f"WARNING: could not auto-cancel form: {exc}")


def main() -> int:
    if not SUBMIT:
        _monkeypatch_no_submit()

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        if not browser.contexts:
            print("ERROR: no browser context on CDP port 9222 — is EPIC actually open?")
            return 2
        context = browser.contexts[0]
        if not context.pages:
            print("ERROR: no pages in the browser context.")
            return 2
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
        print(f"Connected — page title: {page.title()!r}")

        # Clean up a left-open form from a prior run.
        _close_any_open_form(page)

        # Get to Account Locate first (no-op if we're already there).
        if not step_account_lookup_nav.run(page):
            print("ERROR: could not open Account Locate screen.")
            return 3
        print("On Account Locate.")

        print(f"Running create with account_name={ACCOUNT_NAME!r}")
        result = create_run(page, setup=SETUP)
        print(f"\nOutcome: {result.outcome.name}")
        if result.lookup_code:
            print(f"  lookup_code: {result.lookup_code!r}")
        if result.account_name:
            print(f"  account_name: {result.account_name!r}")
        if result.error:
            print(f"  error: {result.error}")

        if not SUBMIT:
            print(
                "\nDRY RUN — form was filled but Save was skipped. "
                "Click Cancel in EPIC (and confirm Discard) before re-running."
            )
        return 0 if result.outcome is AccountCreateOutcome.CREATED or not SUBMIT else 1


if __name__ == "__main__":
    sys.exit(main())

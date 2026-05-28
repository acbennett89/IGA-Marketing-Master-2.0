"""Diagnostic: dump page HTML across two phases — section-click and Add-click.

**Phase 1 — section nav.** Clicks the configured IM-section sidebar link
and captures ``document.documentElement.outerHTML`` at:

  t = 0.0 s  (immediately after the click)
  t = 0.5 s
  t = 10.0 s

Phase 1 shows the SPA's transition window: previous-screen DOM still
present at 0.5s, new screen mounted by ~10s.

**Phase 2 — Add click.** Once the section is mounted, clicks the grid's
Add button and captures at:

  t = 0.0 s  (immediately after Add)
  t = 1.0 s
  t = 3.0 s
  t = 10.0 s

Phase 2 reveals the per-row inputs that ship with a new grid row —
critical for designing the section's row-fill logic without guessing
selectors.

Currently targeted: **Additional Interests** (CHM-EFADDLIN, vlvwInterest).
Swap the SECTION_* constants below to retarget at another IM screen.
Set ``DO_ADD_PHASE = False`` to skip Phase 2 for sections where Add
would be destructive in a non-throwaway submission.

Pre-conditions:
  - Browser open via Launch Browser (CDP on :9222).
  - On the submission that has an Inland Marine line.
  - Submission Detail sidebar visible. The script will select the IM
    line itself; you do not need to pre-click it.

Run from repo root:
    .venv/Scripts/python test_im_sched_load_dump.py

Output files land in:
    diagnostics/im_<section>_load/<YYYYmmdd_HHMMSS>/
"""
from __future__ import annotations

import sys
import time
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"src")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
_log = logging.getLogger("test_im_sched_load_dump")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_inland_marine import (
    _nav_to_im,
    list_available_im_lines,
)

STATE         = ""     # "" picks the first IM line
DO_ADD_PHASE  = True   # Phase 2: click Add and capture the new-row inputs

# ── Section under test ──────────────────────────────────────────────────────
# Swap these four values to retarget the dump at another IM section.
SECTION_LABEL    = "AdditionalInterest"   # used in sidebar selector + output dir
SECTION_SLUG     = "ai"                   # short tag for the diagnostics folder
SIDEBAR_SEL      = f'[data-automation-id^="sidebar-button-Policy.EquipmentFloater.{SECTION_LABEL}"]'
SCREEN_CODE      = "CHM-EFADDLIN"
ADD_BTN_SEL      = '[data-test="vlvwInterest_add"]'
GRID_ROW_PREFIX  = "vlvwInterest-focusable-row-"


def _probe(page) -> dict:
    """Cheap one-shot probe for diagnostic comment headers.

    All booleans/strings here are observable from the DOM at the moment
    of capture — keeps the header informative without re-running the dump.
    """
    return page.evaluate(
        """({screenCode, addBtnSel, rowPrefix}) => {
            const body = (document.body && document.body.innerText) || '';
            const addBtn = document.querySelector(addBtnSel);
            const spinner = document.querySelector(
                '[aria-busy="true"], [class*="Spinner"], [class*="LoadingIndicator"], .loading-overlay'
            );
            return {
                title: document.title,
                screen_code_in_body: body.includes(screenCode),
                add_button: addBtn ? {
                    visible: addBtn.offsetParent !== null,
                    disabled: addBtn.disabled === true,
                    aria_disabled: addBtn.getAttribute('aria-disabled'),
                    class: addBtn.className,
                } : null,
                spinner_visible: !!(spinner && spinner.offsetParent !== null),
                grid_rows: document.querySelectorAll(
                    `[data-test^="${rowPrefix}"]`
                ).length,
                program_alert: /Program Alert\\b/i.test(body),
                modal_open: !!document.querySelector(
                    '[role="dialog"]:not([style*="display: none"]), [role="alertdialog"]'
                ),
            };
        }""",
        {
            "screenCode": SCREEN_CODE,
            "addBtnSel": ADD_BTN_SEL,
            "rowPrefix": GRID_ROW_PREFIX,
        },
    )


def _dump(page, out_dir: Path, label: str, t_elapsed_s: float) -> None:
    """Capture outerHTML + a probe summary header. Writes to ``<label>.html``."""
    probe = _probe(page)
    html = page.evaluate("() => document.documentElement.outerHTML")
    path = out_dir / f"{label}.html"
    header = (
        f"<!--\n"
        f"  IM {SECTION_LABEL} ({SCREEN_CODE}) load dump\n"
        f"  label              : {label}\n"
        f"  t_elapsed_s        : {t_elapsed_s:.3f}\n"
        f"  wall_clock         : {datetime.now().isoformat(timespec='milliseconds')}\n"
        f"  title              : {probe['title']!r}\n"
        f"  screen_code_in_body: {probe['screen_code_in_body']}\n"
        f"  add_button         : {probe['add_button']}\n"
        f"  spinner_visible    : {probe['spinner_visible']}\n"
        f"  grid_rows          : {probe['grid_rows']}\n"
        f"  modal_open         : {probe['modal_open']}\n"
        f"  program_alert      : {probe['program_alert']}\n"
        f"-->\n"
    )
    path.write_text(header + html, encoding="utf-8")
    _log.info("Wrote %s (%d bytes)", path.name, path.stat().st_size)
    _log.info("  probe: %s", probe)


def main() -> int:
    run_dir = Path("diagnostics") / f"im_{SECTION_SLUG}_load" / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    _log.info("Output dir: %s", run_dir.resolve())
    _log.info("Section: %s (%s)", SECTION_LABEL, SCREEN_CODE)

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        avail = list_available_im_lines(page)
        if not avail:
            print(f"\nNo IM lines visible on {page.title()!r}.")
            return 1
        target_state = STATE.upper() if STATE else (avail[0].get("state") or "")
        _log.info("IM lines: %s; selecting state=%r", avail, target_state or "(unsuffixed)")

        if not _nav_to_im(page, target_state):
            print("FAILED: could not select the IM line in the sidebar.")
            return 1

        btn = page.locator(SIDEBAR_SEL)
        if not btn.count():
            print(f"FAILED: sidebar link {SIDEBAR_SEL!r} not found.")
            return 1

        _log.info("Phase 1: clicking %s sidebar link...", SECTION_LABEL)
        t0 = time.monotonic()
        btn.first.click(timeout=5_000)

        _dump(page, run_dir, "p1_t0_immediate", time.monotonic() - t0)

        time.sleep(0.5)
        _dump(page, run_dir, "p1_t1_500ms", time.monotonic() - t0)

        time.sleep(9.5)
        _dump(page, run_dir, "p1_t2_10s", time.monotonic() - t0)

        if DO_ADD_PHASE:
            add_btn = page.locator(ADD_BTN_SEL)
            if not add_btn.count():
                print(f"Phase 2 skipped: Add button {ADD_BTN_SEL!r} not in DOM.")
            else:
                _log.info("Phase 2: clicking Add button %s...", ADD_BTN_SEL)
                t_add = time.monotonic()
                add_btn.first.click(timeout=5_000)

                _dump(page, run_dir, "p2_t0_immediate", time.monotonic() - t_add)

                time.sleep(1.0)
                _dump(page, run_dir, "p2_t1_1s", time.monotonic() - t_add)

                time.sleep(2.0)
                _dump(page, run_dir, "p2_t2_3s", time.monotonic() - t_add)

                time.sleep(7.0)
                _dump(page, run_dir, "p2_t3_10s", time.monotonic() - t_add)

        print(f"\nDumps written to: {run_dir.resolve()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())

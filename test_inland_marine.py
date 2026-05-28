"""Standalone test for the Inland Marine entry step.

Loads real state.json data and runs the IM step against the open EPIC browser
(CDP port 9222).  SCHEDULED_LIMIT caps scheduled equipment for fast iteration.

Pre-condition: an Inland Marine (Equipment Floater) line must exist on the open
submission.

Run from repo root:
    .venv/Scripts/python test_inland_marine.py

LOGGING + STOP CONTROL
----------------------
Every run writes a timestamped log file under:
    diagnostics/im_e2e/<YYYYmmdd_HHMMSS>.log
The path is printed at the start of the run so you can `tail -f` it.

To stop a run cleanly mid-flight (without losing logs):
    1. Press Ctrl+C in the terminal — SIGINT is caught and the script
       sets the runtime's cancel event so the next ``check_cancel()``
       call in the step file raises EntryCancelled.
    2. OR create the file ``diagnostics/im_e2e/STOP_NOW`` from another
       terminal / Explorer — a background thread polls every 500 ms and
       triggers the same cancel path.
Both options flush the log file before exiting.
"""
import sys
import json
import os
import signal
import threading
import time
import logging
import threading as _threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, r"src")

# ── Logging: console + file ──────────────────────────────────────────────────
_LOG_DIR  = Path(r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0") / "diagnostics" / "im_e2e"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_LOG_FILE = _LOG_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
_STOP_FILE = _LOG_DIR / "STOP_NOW"

# Pre-emptively wipe a stale STOP_NOW from a prior run so we don't
# abort immediately on launch.
if _STOP_FILE.exists():
    _STOP_FILE.unlink()

_fmt = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s"
_datefmt = "%H:%M:%S"
_handlers: list = [
    logging.StreamHandler(sys.stdout),
    logging.FileHandler(_LOG_FILE, encoding="utf-8"),
]
logging.basicConfig(
    level=logging.DEBUG, format=_fmt, datefmt=_datefmt, handlers=_handlers,
)
# Quiet noisy libraries — keep the run log focused on our own messages.
logging.getLogger("playwright").setLevel(logging.INFO)
logging.getLogger("asyncio").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("anthropic._base_client").setLevel(logging.INFO)
logging.getLogger("keyring").setLevel(logging.WARNING)
logging.getLogger("win32ctypes").setLevel(logging.WARNING)

_log = logging.getLogger("iga.test_inland_marine")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_inland_marine import (
    InlandMarineSetup,
    IMScheduledItemSpec,
    IMUnscheduledItemSpec,
    IMAdditionalInterestSpec,
    IMAdditionalCoverageSpec,
    list_available_im_lines,
    run as inland_marine_run,
)
from iga_marketing_master_2.epic_steps import runtime as _rt

# ── Cancel plumbing: SIGINT + stop-file → runtime.cancel_event ───────────────
_cancel_event = _threading.Event()


def _install_runtime() -> None:
    """Register a minimal EntryRuntime so step-file ``check_cancel()`` calls
    will trip on our cancel_event."""
    rt = _rt.EntryRuntime(
        run_id=f"e2e_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        artifacts_dir=_LOG_DIR,
        cancel_event=_cancel_event,
        on_validation_halt=lambda finding: "cancel",  # any soft error halts
    )
    _rt.set_runtime(rt)


def _request_cancel(reason: str) -> None:
    if _cancel_event.is_set():
        return
    _log.warning("CANCEL requested: %s — flushing logs and stopping at next checkpoint", reason)
    _cancel_event.set()


def _sigint_handler(signum, frame) -> None:  # noqa: ARG001
    _request_cancel("SIGINT (Ctrl+C)")


def _stop_file_watcher() -> None:
    """Poll for the stop-file every 500 ms; trigger cancel if seen."""
    while not _cancel_event.is_set():
        try:
            if _STOP_FILE.exists():
                _request_cancel(f"stop file detected: {_STOP_FILE}")
                # Consume the file so we don't re-trigger on next run.
                try:
                    _STOP_FILE.unlink()
                except Exception:  # noqa: BLE001
                    pass
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)


signal.signal(signal.SIGINT, _sigint_handler)
_threading.Thread(target=_stop_file_watcher, daemon=True).start()
_install_runtime()

# ── Test config ──────────────────────────────────────────────────────────────
SCHEDULED_LIMIT = None # cap for fast iteration; None = run all items
STATE           = ""   # optional state suffix; "" picks the first IM line
STATE_JSON      = r"C:\Users\Andrew\Documents\GitHub\IGA-Marketing-Master-2.0\Testing and Example Library\diagnostic_workspace\_DIAGNOSTIC\state.json"


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
    banner = (
        "\n" + "=" * 72 + "\n"
        f"  IM e2e run\n"
        f"  Log file : {_LOG_FILE}\n"
        f"  Stop file: touch {_STOP_FILE}\n"
        f"            (or press Ctrl+C in this terminal)\n"
        + "=" * 72 + "\n"
    )
    print(banner, flush=True)
    _log.info("e2e run starting — log_file=%s stop_file=%s", _LOG_FILE, _STOP_FILE)

    with open(STATE_JSON, encoding="utf-8") as fh:
        state = json.load(fh)
    rep = state.get("repeatables", {})

    all_sched = [
        IMScheduledItemSpec(
            item_number  =_v(row, "policy.inland_marine.scheduled_item.item_number"),
            type         =_v(row, "policy.inland_marine.scheduled_item.type"),
            manufacturer =_v(row, "policy.inland_marine.scheduled_item.manufacturer"),
            model        =_v(row, "policy.inland_marine.scheduled_item.model"),
            model_year   =_v(row, "policy.inland_marine.scheduled_item.model_year"),
            description  =_v(row, "policy.inland_marine.scheduled_item.description"),
            serial_number=_v(row, "policy.inland_marine.scheduled_item.serial_number"),
            amt_insurance=_v(row, "policy.inland_marine.scheduled_item.amt_insurance"),
            deductible   =_v(row, "policy.inland_marine.scheduled_item.deductible"),
        )
        for row in (rep.get("policy.inland_marine.scheduled_item") or [])
        if _v(row, "policy.inland_marine.scheduled_item.description")
        or _v(row, "policy.inland_marine.scheduled_item.amt_insurance")
    ]
    sched = all_sched[:SCHEDULED_LIMIT] if SCHEDULED_LIMIT else all_sched

    unsched = [
        IMUnscheduledItemSpec(
            description      =_v(row, "policy.inland_marine.unscheduled_item.description"),
            description_short=_v(row, "policy.inland_marine.unscheduled_item.description_short"),
            amt_insurance    =_v(row, "policy.inland_marine.unscheduled_item.amt_insurance"),
        )
        for row in (rep.get("policy.inland_marine.unscheduled_item") or [])
        if _v(row, "policy.inland_marine.unscheduled_item.description")
    ]

    ais = [
        IMAdditionalInterestSpec(
            name           =_v(row, "policy.inland_marine.additional_interest.name"),
            interest_type  =_v(row, "policy.inland_marine.additional_interest.interest"),
            address_line_1 =_v(row, "policy.inland_marine.additional_interest.primary_address.line_1"),
            reason_for_int =_v(row, "policy.inland_marine.additional_interest.reason_for_int"),
            item_number    =_v(row, "policy.inland_marine.additional_interest.item_number"),
        )
        for row in (rep.get("policy.inland_marine.additional_interest") or [])
        if _v(row, "policy.inland_marine.additional_interest.name")
    ]

    acs = [
        IMAdditionalCoverageSpec(
            description=_v(row, "policy.inland_marine.additional_coverage.name"),
            code       =_v(row, "policy.inland_marine.additional_coverage.code"),
            each_claim =_v(row, "policy.inland_marine.additional_coverage.each_claim_limit"),
            deductible =_v(row, "policy.inland_marine.additional_coverage.deductible"),
            item_number=_v(row, "policy.inland_marine.additional_coverage.item_number"),
        )
        for row in (rep.get("policy.inland_marine.additional_coverage") or [])
        if _v(row, "policy.inland_marine.additional_coverage.name")
    ]

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:9222")
        context = browser.contexts[0]
        page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])

        available = list_available_im_lines(page)
        if not available:
            print(f"\nNo IM lines on the open submission ({page.title()}).")
            print("Create an Inland Marine / Equipment Floater line in EPIC first.")
            return
        target_state = STATE.upper() if STATE else (available[0].get("state") or "")

        setup = InlandMarineSetup(
            state                          =target_state,
            total_scheduled_amount         =_sv(state, "policy.inland_marine.total_scheduled_amount"),
            acv_replacement_cost_deductible=_sv(state, "policy.inland_marine.acv_replacement_cost_deductible"),
            scheduled_items                =sched,
            unscheduled_items              =unsched,
            additional_interests           =ais,
            additional_coverages           =acs,
        )

        print(f"\n=== Inland Marine Setup ===")
        print(f"  IM lines on submission: {available}")
        print(f"  Total scheduled amount : {setup.total_scheduled_amount!r}")
        print(f"  ACV/RC deductible      : {setup.acv_replacement_cost_deductible!r}")
        print(f"  Scheduled items        : {len(sched)} (of {len(all_sched)}{' — CAPPED' if SCHEDULED_LIMIT and len(all_sched) > SCHEDULED_LIMIT else ''})")
        print(f"  Unscheduled items      : {len(unsched)}")
        print(f"  Additional interests   : {len(ais)}")
        print(f"  Additional coverages   : {len(acs)}")
        print()
        print(f"Connected — {page.title()}\n")
        ok = inland_marine_run(page, setup)
        print(f"\ninland_marine_run returned: {ok}")


if __name__ == "__main__":
    main()

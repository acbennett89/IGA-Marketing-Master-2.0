"""One-shot: open MMKADNEW on the current account, scrape cboLineStatus, cancel out."""
import json, sys, time
sys.path.insert(0, "src")
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT = Path("Library/dropdown_codes/cboLineStatus.json")
OUT.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.contexts[0].pages[0]
    print(f"Connected: {page.title()}")

    # Open Add Submission form
    add_sub = page.locator('[data-automation-id="vlvwMasterSubmissions"] .icon-button[title="Add"]')
    add_sub.first.click(timeout=5_000)
    page.locator('[data-automation-id="fraMasterMarketingSubmission"]').wait_for(state="visible", timeout=15_000)

    # Open Add Line picker → Continue → MMKADNEW
    add_line = page.locator('[data-automation-id="vlvwLines"] .icon-button[title="Add"]')
    add_line.first.click(timeout=5_000)
    radio = page.locator('input[value="rbtnNew"]')
    radio.wait_for(state="visible", timeout=10_000)
    radio.first.check()
    page.locator('[data-test="btnContinue"]').first.click(timeout=5_000)
    page.locator('[data-automation-id="cboLine"]').wait_for(state="visible", timeout=10_000)

    # Open cboLineStatus dropdown
    modal = page.locator("modal-screen").last
    modal.locator('[data-automation-id="cboLineStatus"] .drop-btn').click(timeout=3_000)
    page.wait_for_timeout(600)

    # Scrape all body-row items
    results = {}
    for _ in range(100):
        rows = page.locator('div[data-automation-id*=" body-row item-"]')
        for i in range(rows.count()):
            try:
                raw = rows.nth(i).inner_text(timeout=300).strip()
                for sep in ("\n", "\t"):
                    if sep in raw:
                        code, desc = raw.split(sep, 1)
                        code, desc = code.strip(), desc.strip()
                        if code and code not in results:
                            results[code] = desc
                            print(f"  {code:<10} {desc}")
                        break
            except Exception:
                pass
        prev = len(results)
        page.keyboard.press("PageDown")
        page.wait_for_timeout(200)
        if len(results) == prev:
            break

    print(f"\nCollected {len(results)} entries")

    # Cancel out of MMKADNEW then discard MKADMSTR
    try:
        modal.locator('[data-automation-id="btnCancel"]').first.click(timeout=3_000)
        page.wait_for_timeout(400)
    except Exception:
        pass
    # Dismiss any "discard changes" dialog
    for btn_text in ("Yes", "No"):
        try:
            b = page.locator(f'button:has-text("{btn_text}")')
            if b.count() > 0 and b.first.is_visible():
                b.first.click(); break
        except Exception:
            pass
    page.wait_for_timeout(300)
    # Close MKADMSTR with its Cancel button
    try:
        page.locator('[data-automation-id="btnCancel"]').first.click(timeout=3_000)
        page.wait_for_timeout(300)
        for btn_text in ("Yes", "No"):
            b = page.locator(f'button:has-text("{btn_text}")')
            if b.count() > 0 and b.first.is_visible():
                b.first.click(); break
    except Exception:
        pass

    entries = [{"code": k, "description": v} for k, v in sorted(results.items())]
    OUT.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    print(f"Saved → {OUT}")

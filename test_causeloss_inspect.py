"""Inspect the Cause of Loss dialog field IDs by opening it via the full subject flow."""
import sys, time
sys.path.insert(0, r"src")

import logging
logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s %(message)s")

from playwright.sync_api import sync_playwright
from iga_marketing_master_2.epic_steps.step_property import (
    _nav_section, _fill_id_field, _fill_react_combo, _dismiss_modal, _SUBJECT_TYPE_SEARCH,
    _CLICK_TIMEOUT
)

with sync_playwright() as pw:
    browser = pw.chromium.connect_over_cdp("http://localhost:9222")
    context = browser.contexts[0]
    page = next((p for p in context.pages if "appliedepic.com" in p.url), context.pages[0])
    print(f"Connected — {page.title()}")

    _dismiss_modal(page)
    _nav_section(page, "Subject", "CHM-PRSUBJCT")

    # Click Add
    page.locator('[data-test="vlvwSubject_add"]').click()
    page.wait_for_timeout(600)
    _dismiss_modal(page)

    # Fill loc/bldg/type/amount (minimum needed to enable Cause of Loss)
    _fill_id_field(page, "inteLocationNumber__textField", "1")
    page.keyboard.press("Tab")
    _fill_id_field(page, "inteBuildingNumber__textField", "1")
    page.keyboard.press("Tab")
    page.wait_for_timeout(400)
    _fill_react_combo(page, "cboSubject", "Building")
    _fill_id_field(page, "streDescription", "Test")
    _fill_id_field(page, "deceAmount__textField", "100000")
    _fill_react_combo(page, "cboValuation1", "Replacement Cost")
    page.wait_for_timeout(300)

    # Click Cause of Loss Add
    add_btn = page.locator('[data-test="vlvwCauseOfLoss_add"]')
    add_btn.first.click(timeout=_CLICK_TIMEOUT)
    page.wait_for_timeout(1000)

    # Inspect the modal
    modal = page.locator('[data-modal-id]')
    if not modal.first.is_visible(timeout=3000):
        print("ERROR: Cause of Loss dialog did not open")
    else:
        result = page.evaluate("""() => {
            // Search globally - the dialog may render outside any specific container
            const allInputs = document.querySelectorAll('input[id]');
            const allCombos = document.querySelectorAll('[data-test$="-combobox-input"]');
            // Look for any dialog-like containers
            const dialogContainers = document.querySelectorAll('[data-modal-id], [class*="DialogBox"], [class*="ModalBase"]');
            return {
                inputCount: allInputs.length,
                inputs: Array.from(allInputs).map(el => ({id: el.id, name: el.name, value: el.value, disabled: el.disabled})),
                comboCount: allCombos.length,
                combos: Array.from(allCombos).map(el => ({dt: el.getAttribute('data-test'), id: el.id, value: el.value})),
                dialogContainerInfo: Array.from(dialogContainers).map(el => ({
                    tag: el.tagName, cls: String(el.className).substring(0,60),
                    children: el.children.length,
                    inputsInside: el.querySelectorAll('input[id]').length
                }))
            };
        }""")
        print(f"\n=== Cause of Loss Dialog Fields ({result['inputCount']} inputs, {result['comboCount']} combos) ===")
        print("INPUTS:")
        for inp in result['inputs']:
            print(f"  id={inp['id']!r:40} name={inp['name']!r} disabled={inp['disabled']} value={inp['value']!r}")
        print("COMBOS:")
        for c in result['combos']:
            print(f"  dt={c['dt']!r:50} id={c['id']!r} value={c['value']!r}")
        print("DIALOG CONTAINERS:")
        for d in result['dialogContainerInfo']:
            print(f"  {d['tag']} cls={d['cls']!r} children={d['children']} inputs={d['inputsInside']}")

    # Close the dialog
    page.evaluate("() => document.querySelectorAll('[class*=CloseButton_module_closeIcon]').forEach(b=>b.click())")
    page.wait_for_timeout(500)
    _dismiss_modal(page)
    # Navigate away to discard the unsaved row
    _nav_section(page, "Premise", "CHM-PRPREMSE")
    print("\nDone")

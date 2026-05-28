# Known Issues — IGA Marketing Master 2.0

Issues discovered during development/testing that need to be fixed.
Newest entries at the top.

---

## 2026-05-27 — Apply the contact address ZIP-picker workflow to Workers' Comp locations

**Severity:** Medium — WC location addresses use the same old-proxy address widget
(`streStreet` / `streCity` / `cboState` / `strePostalCode`), so they have the same
ambiguous-ZIP behavior the Additional Contacts work just mapped, but
`_fill_wc_address` doesn't handle it. A ZIP that maps to multiple cities will pop the
ZIP Codes picker and stall, or the city/state won't resolve correctly.

**Context:** While building `step_additional_contacts._add_*`, the contact Address
widget (`[data-automation-id="adePrimary"]`) was mapped live (2026-05-27). The
validated workflow is:
1. Click the address box to expand it (`adePrimary` `.preview`).
2. Type street into `streStreet`, Tab.
3. Type ZIP into `strePostalCode`, Tab.
4. If the **ZIP Codes picker** modal appears (`vlvwZipPostCodes`, status `ZIPPOST` —
   fires when the ZIP maps to >1 city):
   - desired city IS in the rows → click that `vlvwZipPostCodes body-row item-N`
     row → click `btnOK` (City/State auto-fill from the row).
   - desired city NOT in the rows → modal `btnCancel` → type `streCity` → Tab →
     type `cboState` (2-letter code, e.g. "NE") → Tab.
5. If the ZIP is unambiguous, no picker — City/State auto-fill directly.
6. (Separately, contacts also support "Use account address" via `chkUseAcctAddress`,
   which auto-fills the account address and makes the street read-only.)

Notes: `cboState` is an Angular `asi-combo-box` that accepts the 2-letter code and
holds it; tab order in the widget is Street → ZIP, and City → State; the lone
`<message-box>` host is empty/zero-size when idle (filter on non-empty text +
non-zero size before treating it as a real validation dialog).

**Fixes to apply:**
1. Refactor the address-entry logic in
   `step_additional_contacts.py` into a shared helper once it's finalized, then
   reuse it from `step_workers_comp._fill_wc_address`
   ([step_workers_comp.py:522](src/iga_marketing_master_2/epic_steps/step_workers_comp.py#L522)).
2. WC's current `_fill_wc_address` types into `streStreet`/`streCity`/`cboState`/
   `strePostalCode` but does NOT handle the ZIP Codes picker — add the picker
   match-or-cancel branch.

**Files involved:** `src/iga_marketing_master_2/epic_steps/step_workers_comp.py`,
`src/iga_marketing_master_2/epic_steps/step_additional_contacts.py`

---

## 2026-05-26 — IM Additional Interest item_number not remapped when Scheduled items are renumbered

**Severity:** Medium — silently breaks the AI-to-Scheduled link when extraction
produced duplicate or out-of-order Scheduled item numbers. AI rows end up pointing
at the wrong (or non-existent) scheduled item in EPIC.

**Symptom:** When the GUI's IM Scheduled-Items dedupe renumbers a duplicate or
blank `item_number` to fill a gap (see
`section_forms.InlandMarineForm._refresh_tables` — the per-row dedupe loop that
fires on every refresh and persists via `field_changed`), any Additional Interest
row that references the original item number is left pointing at the old label.
After dedupe, the AI's `inteItemNumber__textField` value either references a
different item than intended, or references a number that no longer exists.

**Root cause:** The Scheduled-item dedupe rewrites
`policy.inland_marine.scheduled_item.item_number` values in place but does not
build the `original_label → new_label` map, and does not walk
`policy.inland_marine.additional_interest.item_number` to remap matching values.

**Fixes to apply:**
1. In `section_forms.InlandMarineForm._refresh_tables`, build the
   `old → new` label map alongside the existing dedupe loop. Only record entries
   where the label actually changed (skip identity mappings).
2. After the dedupe loop, walk
   `state.repeatables["policy.inland_marine.additional_interest"]` and for each
   row whose `item_number.value` matches a key in the map, emit
   `field_changed("__rep:<ai_group>:<idx>:item_number_tag", new_value)` so the
   change persists.
3. Edge case — duplicate originals (two scheduled items both labelled "7"):
   the first occurrence keeps the original label, subsequent occurrences get
   renumbered. AI references to "7" route to the first occurrence by definition.
   AI references that *originally* pointed to the now-renumbered duplicate are
   ambiguous and stay stale; log a warning so the operator can audit.
4. Same change applies to any future AI subject-ref fields that point at
   Scheduled items (e.g., if Additional Coverages gain item references).

**Files involved:** `src/iga_marketing_master_2/gui/section_forms.py` (the
InlandMarineForm `_refresh_tables` override around the dedupe block).

---

## 2026-05-20 — Property Additional Interest: GUI missing per-field columns

**Severity:** Medium — operator can't review or edit AI rows field-by-field; have to
trust extraction or edit raw state.json.

**Symptom:** When an Additional Interest is added to a property in the GUI, the
Additional Interests table doesn't expose dedicated columns for the fields that
EPIC needs (name, interest_type, street, city, state, zip_code, location_number,
building_number, loan_number). Operator has no place to enter or correct these
post-extraction.

**Root cause:** The GUI's Additional Interest section table model doesn't include
the AI sub-field columns. Compare to how Subject rows expose Loc/Bldg/Type/Amount/etc.

**Fixes to apply:**
1. Update the AI section table model in [main_window.py](src/iga_marketing_master_2/gui/main_window.py)
   to expose one column per AI sub-field domain tag (mirroring the subject row layout).
2. Hook the cells to the same `policy.property.additional_interest.*` repeatable fields
   that the test/runner reads via `_v(row, "policy.property.additional_interest.<key>")`.

**Files involved:** `src/iga_marketing_master_2/gui/main_window.py`

---

## 2026-05-20 — Property Additional Interest: address is a validated-lookup field

**Severity:** Medium — entry will fail or save junk if we just `fill()` the street/city/state/zip
plainly; EPIC requires the address to resolve through its address-validation widget
(same pattern as Premises Location/Building Lookup).

**Symptom:** Anticipated, not yet hit in a live run. The AI address fields in EPIC use
the same validated-address widget as the Premises section — typing free-text and tabbing
out either snaps to a wrong autocomplete row (like Commercial AP's "Ste 160" bug) or
fails validation on save.

**Root cause:** EPIC reuses its address-resolver component on every address-bearing
screen. Plain `fill()` triggers the autocomplete dropdown which then picks the wrong
row when focus moves away.

**Fixes to apply:**
1. In `_fill_additional_interests` ([step_property.py:857](src/iga_marketing_master_2/epic_steps/step_property.py#L857)),
   adopt the same Tab-to-commit pattern used in
   [step_commercial_ap.py](src/iga_marketing_master_2/epic_steps/step_commercial_ap.py)'s
   `_fill_location_modal` (fill street → Tab to commit before autocomplete row click,
   then fill city/state/zip).
2. Confirm via CDP inspection whether the AI address fields are React combos (use
   `_fill_react_combo`) or text inputs with autocomplete overlays (use the Tab pattern).
3. Add a screenshot under `--debug` before/after address fill so the validation result
   is captured for diagnosis.

**Files involved:** `src/iga_marketing_master_2/epic_steps/step_property.py`

---

## 2026-05-19 — Logout-on-close was removed

**Severity:** Low — EPIC handles the session conflict on next login, so no functional break. Cosmetic.

**Symptom:** When the IGA app closes (X button or Close App button), the EPIC browser is torn down without logging out. On the next launch, EPIC sees a stale session.

**Root cause:** The logout-on-close flow (`_LogoutWorker`, `_run_logout_then_close`, etc.) was removed from `main_window.py` on 2026-05-19 after multiple failed fix attempts (threading/timing issues prevented the logout from running before the window closed).

**Current mitigation:** `step_session_conflict.py` handles "already logged in" prompts automatically on the next login.

**Fixes to apply:**
1. Re-add the logout flow from git history (commit before e733453).
2. Verify `step_logout.py` selector still works via CDP before wiring it back into the close handler.
3. May be coupled with the Chromium dirty-shutdown issue below — both stem from the browser not shutting down gracefully.

**Files involved:** `src/iga_marketing_master_2/gui/main_window.py`, `src/iga_marketing_master_2/epic_steps/step_logout.py`

---

## 2026-05-19 — EPIC viewport doesn't scale to full Chrome window

**Severity:** Medium — UX annoyance; doesn't affect automation but makes manual review awkward.

**Symptom:** When the IGA app launches Chrome via `launch_persistent_context`, EPIC renders at a smaller viewport than the Chrome window — leaving grey dead space to the right and bottom. On a 5120px ultrawide at 1.25 DPR, about 569px of dead space was observed.

**Root cause:** Playwright imposes a fixed viewport even with `viewport=None` on persistent contexts. Chrome DevTools `emulation.deviceMetricsOverride` remains set after launch.

**Partial fix written (untested):** `epic_session.py` now calls `Emulation.clearDeviceMetricsOverride` via CDP immediately after `launch_persistent_context`, and hooks `context.on("page", ...)` to apply the same clear to new tabs. The app had not been restarted with this code before the bug was deferred on 2026-05-19.

**Manual workaround:** Open Chrome DevTools → Toggle Device Toolbar on → Toggle off. This clears the override and EPIC scales correctly. (The window resizes as a side effect — that's expected.)

**Fixes to apply:**
1. Restart the IGA app with the current `epic_session.py` code and verify the override-clear actually fires.
2. If EPIC still doesn't fill the window, check whether Playwright re-applies the override on navigation.

**Files involved:** `src/iga_marketing_master_2/epic_session.py`

---

## 2026-05-19 — Chromium dirty shutdown / "Restore pages?" dialog

**Severity:** Medium — interrupts every relaunch; user must dismiss the dialog before EPIC loads.

**Symptom:** After the IGA app closes, the next Chrome launch shows the "Restore pages? Chromium didn't shut down correctly" crash-recovery dialog. EPIC doesn't load until that dialog is dismissed.

**Root cause:** `_tear_down_browser()` in `main_window.py` is not cleanly terminating the Playwright browser process before the app exits. The persistent context's underlying Chrome process is left in an unclean state.

**Mitigation already in code:** `epic_session.launch_with_persistent_context` deletes `Default/Current Session`, `Default/Current Tabs`, `Default/Last Session`, `Default/Last Tabs` on every launch — this suppresses the dialog most of the time but isn't 100% reliable.

**Fixes to apply:**
1. In `_tear_down_browser`, ensure `context.close()` actually completes before the Python process exits (currently may be racing).
2. Consider `page.evaluate("window.close()")` or a `context.close()` timeout to force clean shutdown.
3. Verify the Chrome process actually terminates (Task Manager check) before the Python process exits.
4. Likely related to the logout-on-close issue above — both are symptoms of the same teardown timing problem.

**Files involved:** `src/iga_marketing_master_2/gui/main_window.py` (`_tear_down_browser`), `src/iga_marketing_master_2/epic_session.py`

---

## 2026-05-19 — Property Subject Description: extracted values are wrong

**Severity:** Medium — saves to EPIC with misleading data; not a blocker but degrades data quality.

**Symptom:** After running the property entry, the EPIC Subjects of Insurance grid shows descriptions that don't fit the subject:
- `B - Excavation` at Loc 1 Bldg 1 (227,000)
- `BPP - Earthquake` at Loc 1 Bldg 1 (143,000)
- `B - Storage` at Loc 1 Bldg 2 (1,462,000)
- `BPP - Earthquake` at Loc 1 Bldg 2 (894,000)

**Root cause:** Claude's extraction populates `policy.property.subject.description` with contextual labels from the dec page rather than actual coverage notes:
- `"Excavation"`, `"Storage"` — these are the **building site names** (the `location.site_id` values). The dec page labels each row with the building's site name, and Claude treats that as the description.
- `"Earthquake"` — this is a **peril name**, not a description. Belongs in the Cause of Loss combo, not the Description field.

**Verified in state.json** (`_DIAGNOSTIC` workspace):
```
policy.property.subject[0].description = "Excavation"   ← site name for Loc 2 Bldg 1
policy.property.subject[1].description = "Earthquake"   ← peril name
policy.property.subject[2].description = "Storage"      ← site name
```

The Field Map entry for `policy.property.subject.description` has `"notes_for_claude": null` — so Claude has no guidance on what should go in this field.

**EPIC's intent:** The Description field is for per-subject notes (e.g., "Frame construction, metal roof", "Inventory at retail value", "Stock"). Most subjects should leave it blank.

**Fixes to apply:**
1. **Update the field map** — add `notes_for_claude` to `policy.property.subject.description` instructing Claude:
   - Leave blank unless the dec page has actual descriptive notes for this subject.
   - Do NOT put the building site name here (that goes in `location.site_id`).
   - Do NOT put the peril/cause of loss here (that goes in the Cause of Loss combo).
2. **Re-extract** the affected client(s) once the field map is updated, OR clear the existing descriptions in the GUI review pane (state auto-saves) before the next entry run.

**Files involved:**
- `Library/Epic Field Map.json` — field definition needs `notes_for_claude`
- `src/iga_marketing_master_2/epic_steps/step_property.py:570` — `_fill_id_field(page, "streDescription", subj.description)` (no code change needed; correctly fills whatever's in state)

# STATUS — field-map-agent

**Status:** GREEN
**Date:** 2026-04-30
**Agent:** field-map-agent
**Scope:** `src/iga_marketing_master_2/field_map.py` + `tests/test_field_map.py`

---

## Files written

| Path | Purpose |
|---|---|
| `src/iga_marketing_master_2/field_map.py` | Public API: `load`, `save_atomic`, `lookup_by_domain_tag`, `lookup_by_name`, `fields_for_screen`, `generate_domain_tag_enum`, `update_field`, `screens_touched_by_domain_tags`. Plus `FieldMap`, `FieldEntry`, `FieldMapValidationError`. |
| `tests/test_field_map.py` | 31 tests against a tmp copy of the real Field Map. All passing. |
| `DECISION-MAP-field-map-agent.md` | Mermaid flowchart + open-item ruling for ARCHITECTURE §14 #6. |
| `STATUS_field-map-agent.md` | This file. |

The real `Library/Epic Field Map.json` was **read only** and is byte-identical to its pre-session state. Tests use `shutil.copy2` into pytest's `tmp_path`.

---

## Test results

```
tests/test_field_map.py ......................... 31 passed in 0.35s
```

Coverage of the protocol checklist:

| Required test | Test name | Result |
|---|---|---|
| Round-trip: load -> modify -> save -> reload -> same shape | `test_round_trip_preserves_shape`, `test_save_atomic_writes_then_reload_includes_patch`, `test_save_atomic_cleans_up_or_leaves_no_corrupt_canonical` | PASS |
| `update_field()` adds metadata without breaking existing keys | `test_update_field_preserves_existing_keys`, `test_update_field_does_not_introduce_version_key` | PASS |
| `generate_domain_tag_enum()` is stable, sorted, distinct, skips empties | `test_generate_domain_tag_enum_empty_when_no_tags`, `test_generate_domain_tag_enum_after_updates_is_sorted_unique`, `test_generate_domain_tag_enum_skips_aliases` | PASS |
| Atomic write survives simulated crash | `test_save_atomic_simulated_crash_leaves_original_intact` | PASS |
| Lookup by name across nested `tabs[].sub_tabs[].fields[]` | `test_lookup_by_name_finds_a_nested_subtab_field`, `test_fields_for_screen_includes_tab_and_subtab_fields` | PASS |

Also covered: validation rejection paths (invalid JSON, non-object root, missing required keys, unknown patch keys, wrong value types, list-of-string element types), index sync after rename, alias resolution, the absence of `version` from the schema (Path B / amendment #11).

---

## Open-item ruling

**Item #6 — JIT enrichment write batching (ARCHITECTURE.md §14):** Per-call atomic write is the *recommended* default but is not enforced inside `update_field()`. The I/O contract from ARCHITECTURE §4.3 already specifies that `update_field` mutates the in-memory map only; the caller (GUI) calls `save_atomic` afterward. Documented in `DECISION-MAP-field-map-agent.md` and in the docstrings on both functions.

This keeps batching policy in the GUI where it belongs (it knows whether the user is mid-batch or has clicked Confirm All) without coupling the I/O contract to a future GUI behavior.

---

## Conformance to ARCHITECTURE.md

- §2 (naming): module is `snake_case`, classes `PascalCase`, type hints use Python-3.13 syntax (`X | None`, `list[T]`), no `os.path.join`, no string-concat paths, no bare `except:`.
- §3 (domain_tag grammar): the agent does not validate grammar (Architecture treats grammar checks as the JIT-confirmation dialog's responsibility, not the storage layer's). All grammar-shaped strings round-trip cleanly.
- §4 (Field Map schema): every public function from §4.3 is implemented with the exact signature described. Existing keys are read but never required to be writable through `update_field` (only the new metadata keys in §4.2 are accepted as patch keys).
- §4.4 (atomic write protocol): implemented as documented — copy current to .bak, write to .tmp, `os.replace` to canonical. Verified by simulated-crash test.
- §13.3 (`schema_version` belongs to state.json, not the Field Map): respected; the Field Map has no version field, no `schema_version` field.
- §13.6 (`screens_touched_by_domain_tags` lives on field_map.py): implemented here.
- §14.6 (JIT batching): ruling documented above.
- Amendment #11 (no `version` field): enforced — `version` is not in `ALLOWED_PATCH_KEYS`; a test asserts this.

---

## Flags / notes for downstream agents

- **For state-agent / extract-agent / claude-client-agent:** `generate_domain_tag_enum` returns `[]` early in v1 (no tags populated yet). Per ARCHITECTURE §6.2, the Extractor should drop the `enum` constraint on `record_extracted_field` when this list is empty so Claude can free-text its tag proposal for JIT confirmation.
- **For gui-agent:** Patches go through `update_field` then a *single* `save_atomic` (per call OR after a Confirm-All batch — your call). The `update_field` docstring and DECISION-MAP both call this out.
- **For enter-agent:** `screens_touched_by_domain_tags` returns a `set[str]` of screen codes; alias-resolved. Use it for the touched-screens-only pre-flight smoke (ARCHITECTURE §7.3).
- **No new Python deps** introduced; field_map.py uses only the stdlib (`json`, `logging`, `os`, `shutil`, `dataclasses`, `pathlib`, `typing`).
- **Python 3.12 compatibility verified locally** (3.13 is the project target per pyproject.toml; the syntax used is also valid on 3.12, which is what the test runner happened to be).

---

## Out of scope (not done, on purpose)

- domain_tag grammar enforcement (lives at the JIT-confirmation dialog per the architecture).
- Logging configuration (logger_agent owns `iga.field_map` handler config).
- Any state.json or claude_client.py work.
- Modifying the real `Library/Epic Field Map.json`.

---

GREEN.

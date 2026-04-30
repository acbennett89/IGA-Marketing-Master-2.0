# STATUS — gui-agent

Owner: gui-agent
Phase: Phase 3 — Review GUI
Status: Complete

---

## What landed

### Source files (in scope)

- `src/iga_marketing_master_2/gui/__init__.py` — public re-exports.
- `src/iga_marketing_master_2/gui/main_window.py` — `IgaApp`, `MainWindow`,
  tab-derivation logic, worker-thread plumbing, edit/persist flow,
  client picker + drag-drop, recover-interrupted-run flow.
- `src/iga_marketing_master_2/gui/operator_modal.py` — mandatory
  `OperatorModal` base class plus the six subclasses required by
  ARCHITECTURE §8.3 (`DomainTagConfirmationDialog`,
  `ConflictResolutionDialog`, `EpicValidationPauseDialog`,
  `SelectorUnresolvedPauseDialog`, `ApiKeyPromptDialog`,
  `RecoverInterruptedRunDialog`). 4-part structure (headline /
  what_to_do / cancel_effect / collapsible technical_detail) is enforced
  by the constructor; technical fold-out collapses by default.
  `OperatorAction`, `OperatorActionRole`, and `PauseChoice` enums are
  exported for use by `enter.py` callers.
- `src/iga_marketing_master_2/gui/section_table.py` — `SectionTableModel`
  (`QAbstractTableModel`), `SectionDelegate` (`QStyledItemDelegate`),
  `SectionTableView` (`QTableView`). Confidence highlighting,
  Opus/lock/conflict glyphs, source-cell click signal.
- `src/iga_marketing_master_2/gui/repeatable_pane.py` — list-on-left +
  form-on-right pattern for repeatable groups (NOT nested grids).
- `src/iga_marketing_master_2/gui/pdf_preview.py` — `QPdfDocument` +
  `QPdfView` viewer with deep-link to a 1-indexed page; gracefully
  shows a fallback message when the PDF is missing.
- `src/iga_marketing_master_2/gui/audit_log.py` — read-only
  `QPlainTextEdit` with a `QtLogHandler` that bridges the `iga` logger
  tree into the GUI without coupling other modules to Qt.
- `src/iga_marketing_master_2/gui/run_controls.py` — Run-control toolbar.
  Begin Entry button gated on `approved_count > 0`. Force Opus toggle.
  Cancel / Resume buttons.

The historical top-level `gui.py` module was removed; the `gui` package's
`__init__.py` is the single, unambiguous entry point and re-exports the
full public surface (`IgaApp`, `OperatorModal`, all six dialog classes,
all model/view/widget classes). `from iga_marketing_master_2 import gui`
and `from iga_marketing_master_2.gui import IgaApp` both resolve cleanly.

### Tests (`tests/test_gui_models.py`)

29 tests, all passing in 0.3s. Coverage:

- Confidence-color thresholds + status overrides.
- Value-coercion path (text / number / checkbox).
- `FieldRow.from_field_record` reads source / conflicts shape correctly.
- Tab-derivation: known namespaces, ordering, alpha-sorted leftovers,
  empty-state anchor.
- `OperatorModal` copy validation: empty-text rejection, technical-token
  warning, window-title truncation, what-to-do bullet rendering.
- Qt-bound: `SectionTableModel` round-trip, commit-callback invocation,
  no-op-edit guard, locked-row flag, default-actions OK/Cancel,
  `PauseChoice` vocabulary, `RunControlsBar` Begin Entry gating,
  `AuditLogPane.append_event`, `PdfPreview` missing-file fallback.

### Decision document

`DECISION-MAP-gui-agent.md` published with:
- Open-item ruling for ARCHITECTURE §14 #3 (section-tab derivation).
- Mermaid flows for app launch, main-window state machine, tab
  derivation, cell edit, conflict resolve, domain-tag confirmation,
  Begin Entry / pause / resume.
- `TAB_LABELS` + `TAB_ORDER` vocabulary.

---

## Open-item ruling

**§14 #3 — section-tab derivation.** Resolved.

Tabs derive from the union of `state.fields` namespaces and
`state.repeatables` keys; `policy.<lob>.*` rolls up to `policy.<lob>`;
known keys appear in the `TAB_ORDER` order; unknown keys append in
alphabetical order at the tail; an empty state still shows the
"Submission" anchor tab. See `DECISION-MAP-gui-agent.md` §1.

---

## Cross-agent contracts the GUI relies on

These are the public APIs the GUI calls. If any drift, the GUI breaks.

| Module | Symbol | Role |
|---|---|---|
| `state` | `load(client_path) -> State` | Read state on client load. |
| `state` | `save_atomic(state, client_path)` | Persist after each edit. |
| `state` | `State`, `FieldRecord`, `SourceRef`, `ConflictCandidate`, `HistoryEntry`, `PendingPause`, `PendingExtraction`, `RunHistoryEntry` dataclasses | Reconstructed from dicts at the GUI/state boundary. |
| `extract` | `run_extraction(client_path, pdf_paths, *, force_opus, progress_callback)` | Worker-thread call from drag-drop / Add PDFs. |
| `enter` | `run_entry_session(client_path, *, on_pause_callback, progress_callback)` | Worker-thread call from Begin Entry. |
| `secret_store` | `get_anthropic_api_key`, `set_anthropic_api_key`, `SecretStoreError` | First-run + reprompt flow. |
| `config` | `load_settings`, `save_user_config`, `is_first_run`, `Settings`, `APP_NAME` | First-run picker, Working Library path, app naming. |
| `logger` | `configure_logging`, `get_logger` | Logger setup. The audit log pane attaches a `QtLogHandler` to `"iga"`. |

The GUI **boundary-converts** between `state.State` (dataclass) and an
internal `dict` view via `dataclasses.asdict` at load and a public-API
reconstruction at save (using only the dataclass classes that
`state.py` exports — no private helpers). This isolates GUI internals
from any future state-schema reorganization.

The pause-callback contract from the GUI to `enter.py`:

```python
def on_pause_callback(payload: dict) -> str:
    # payload keys (subset of PendingPause + UI hints):
    #   reason_code: "validation_rejected" | "selector_unresolved" | ...
    #   field_label: human-readable field name (NOT domain_tag)
    #   screen_label: human-readable screen name
    #   attempted_value: the value EPIC rejected (for validation pauses)
    #   technical_detail: optional fold-out string
    # returns one of PauseChoice.RESUME / SKIP / CANCEL (string values).
```

`enter.py` should construct that payload from its `PendingPause` plus
the field's friendly label (looked up via `field_map`).

---

## Deviations from the plan

None. The 4-part `OperatorModal` structure, six subclasses, list+form
repeatable pattern, `QPdfView` (not `QWebEngineView`), worker-thread
extraction/entry, confidence highlighting, Opus badge, conflict
resolution, Begin Entry gating, recover-interrupted-run flow, and
client picker / drag-drop intake are all in. Modules outside the GUI
were not touched.

---

## Known limitations / nice-to-haves

- The Force Opus toggle is wired through to `run_extraction`'s
  `force_opus=` kwarg (per the contract above). If
  `extract.run_extraction` doesn't accept that kwarg yet, the GUI will
  surface a clean failure modal.
- Cancel mid-run is best-effort: it sets a `cancel_requested` attribute
  on the worker. The actual cooperative-cancellation semantics belong
  to `extract.py` / `enter.py`.
- `DomainTagConfirmationDialog` is implemented and exported; its caller
  (the JIT enrichment loop) is owned by the extractor, which will hand
  proposals back to the GUI through a queue mechanism the
  extraction-agent defines.
- Per-repeatable add/delete is naive (appends an empty record /
  removes the selected row). The state-agent's natural-key dedup logic
  will replace this once the open item §14 #1 is settled.
- UI integration tests are intentionally minimal (29 tests, mostly
  pure-Python). The model layer carries the contract risk.

---

## Verification

```
$ pytest tests/test_gui_models.py
============================= 29 passed in 0.30s ==============================
```

Imports verified for: `gui.__init__`, `main_window`, `operator_modal`,
`section_table`, `repeatable_pane`, `pdf_preview`, `audit_log`,
`run_controls`. All symbols listed in `gui/__init__.py:__all__` resolve.

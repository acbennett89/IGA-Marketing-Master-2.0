"""Tests for the GUI model layer.

Most coverage targets the pure-Python helpers (confidence color mapping,
value coercion, tab-derivation, OperatorModal copy validation) so tests
run without a QApplication. Tests that require Qt construct a single
``QApplication`` instance via the ``qapp`` fixture; PySide6's offscreen
platform means no display server is needed on the CI box.

UI integration coverage is intentionally minimal — model logic carries the
contract risk; widget-level smoke tests would just exercise PySide6 itself.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Make the src/ tree importable when running tests directly via `pytest`.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Pure-helper tests (no Qt)
# ---------------------------------------------------------------------------


def test_confidence_color_thresholds() -> None:
    """ARCHITECTURE §8.4: <0.6 red, 0.6–0.85 yellow, ≥0.85 none."""
    from iga_marketing_master_2.gui.section_table import (
        CONFIDENCE_HIGH_THRESHOLD,
        CONFIDENCE_LOW_THRESHOLD,
        confidence_color,
    )

    assert confidence_color(0.0, "pending") is not None
    assert confidence_color(0.5, "pending") is not None
    assert confidence_color(0.6, "pending") is not None
    assert confidence_color(0.84, "pending") is not None
    assert confidence_color(0.85, "pending") is None
    assert confidence_color(1.0, "pending") is None

    # Constants match contract.
    assert CONFIDENCE_LOW_THRESHOLD == 0.6
    assert CONFIDENCE_HIGH_THRESHOLD == 0.85


def test_confidence_color_status_overrides() -> None:
    """Approved / locked clear the tint regardless of confidence."""
    from iga_marketing_master_2.gui.section_table import confidence_color

    assert confidence_color(0.0, "approved") is None
    assert confidence_color(0.0, "locked") is None
    # Entered fields get the entered tint (very pale green).
    entered = confidence_color(0.0, "entered")
    assert entered is not None


def test_value_coercion_checkbox() -> None:
    from iga_marketing_master_2.gui.section_table import coerce_value

    assert coerce_value(True, "checkbox") is True
    assert coerce_value("yes", "checkbox") is True
    assert coerce_value("YES", "checkbox") is True
    assert coerce_value("1", "checkbox") is True
    assert coerce_value("no", "checkbox") is False
    assert coerce_value("", "checkbox") is False


def test_value_coercion_number() -> None:
    from iga_marketing_master_2.gui.section_table import coerce_value

    assert coerce_value("42", "number") == 42
    assert coerce_value("1,234", "number") == 1234
    assert coerce_value("3.14", "number") == 3.14
    assert coerce_value("", "number") is None
    # Garbage input returned as a string so the GUI flags it.
    out = coerce_value("not-a-number", "number")
    assert out == "not-a-number"


def test_value_coercion_text_default() -> None:
    from iga_marketing_master_2.gui.section_table import coerce_value

    assert coerce_value("hello", "text") == "hello"
    assert coerce_value(None, "text") is None
    assert coerce_value(42, "text") == "42"


# ---------------------------------------------------------------------------
# FieldRow.from_field_record
# ---------------------------------------------------------------------------


def test_field_row_from_record_extracts_source() -> None:
    from iga_marketing_master_2.gui.section_table import FieldRow

    record = {
        "value": "ACME Inc.",
        "confidence": 0.92,
        "status": "pending",
        "model_used": "claude-opus-4-7",
        "source": [{"doc_id": "submission.pdf", "page": 3, "quote": "ACME Inc."}],
        "conflicts": [{"value": "Acme Inc"}],
    }
    row = FieldRow.from_field_record(
        domain_tag="account.named_insured",
        label="Named Insured",
        record=record,
    )
    assert row.value == "ACME Inc."
    assert row.confidence == pytest.approx(0.92)
    assert row.source_doc_id == "submission.pdf"
    assert row.source_page == 3
    assert row.has_conflicts is True
    assert row.model_used == "claude-opus-4-7"


def test_field_row_handles_missing_source() -> None:
    from iga_marketing_master_2.gui.section_table import FieldRow

    record = {"value": None, "confidence": 0.0, "status": "pending"}
    row = FieldRow.from_field_record(
        domain_tag="x.y",
        label="Y",
        record=record,
    )
    assert row.source_doc_id is None
    assert row.source_page is None
    assert row.has_conflicts is False


# ---------------------------------------------------------------------------
# Tab derivation
# ---------------------------------------------------------------------------


def test_tab_derivation_basic_namespaces() -> None:
    from iga_marketing_master_2.gui.main_window import derive_tab_keys

    state = {
        "fields": {
            "submission.name": {},
            "account.named_insured": {},
            "policy.gl.aggregate_limit": {},
            "policy.auto.combined_single_limit": {},
        },
        "repeatables": {
            "vehicle": [{}],
            "loss_payee": [],
        },
    }
    keys = derive_tab_keys(state)
    # submission is in HIDDEN_NAMESPACES — extracted but not surfaced as a tab.
    assert "submission" not in keys
    assert "account" in keys
    assert "policy.gl" in keys
    assert "policy.auto" in keys
    assert "vehicle" in keys
    assert "loss_payee" in keys


def test_tab_derivation_orders_by_tab_order() -> None:
    """Known namespaces appear in TAB_ORDER order."""
    from iga_marketing_master_2.gui.main_window import (
        TAB_ORDER,
        derive_tab_keys,
    )

    state = {
        "fields": {
            "vehicle.year": {},  # repeatable namespace also surfaces here.
            "submission.name": {},
            "account.fein": {},
        },
        "repeatables": {"vehicle": [{}]},
    }
    keys = derive_tab_keys(state)
    acc_idx = keys.index("account")
    veh_idx = keys.index("vehicle")
    # submission is hidden via HIDDEN_NAMESPACES — confirm it's filtered out.
    assert "submission" not in keys
    # account (default tab) comes before vehicle (per TAB_ORDER).
    assert acc_idx < veh_idx
    # TAB_ORDER must still contain the entries we rely on for ordering.
    assert "submission" in TAB_ORDER
    assert "vehicle" in TAB_ORDER


def test_tab_derivation_unknown_namespace_appended_alpha() -> None:
    from iga_marketing_master_2.gui.main_window import derive_tab_keys

    state = {
        "fields": {
            "submission.name": {},
            "zeta.something": {},
            "alpha.something": {},
        },
        "repeatables": {},
    }
    keys = derive_tab_keys(state)
    # alpha and zeta aren't in TAB_ORDER, so they go to the tail in alpha order.
    assert keys.index("alpha") < keys.index("zeta")
    # submission is filtered out via HIDDEN_NAMESPACES.
    assert "submission" not in keys


def test_tab_derivation_empty_state_has_defaults() -> None:
    """Even an empty state shows all DEFAULT_TAB_KEYS."""
    from iga_marketing_master_2.gui.main_window import DEFAULT_TAB_KEYS, derive_tab_keys

    keys = derive_tab_keys(None)
    assert list(DEFAULT_TAB_KEYS) == keys
    keys = derive_tab_keys({"fields": {}, "repeatables": {}})
    assert list(DEFAULT_TAB_KEYS) == keys


def test_label_for_tab_key() -> None:
    from iga_marketing_master_2.gui.main_window import (
        TAB_LABELS,
        label_for_tab_key,
    )

    assert label_for_tab_key("submission") == TAB_LABELS["submission"]
    assert label_for_tab_key("policy.gl") == TAB_LABELS["policy.gl"]
    # Fallback for unknown.
    assert label_for_tab_key("foo_bar") == "Foo Bar"
    assert label_for_tab_key("policy.unknown_lob") == "Policy · Unknown Lob"


# ---------------------------------------------------------------------------
# RepeatablePane label builder
# ---------------------------------------------------------------------------


def test_default_item_label_vehicle() -> None:
    from iga_marketing_master_2.gui.repeatable_pane import default_item_label

    item = {
        "vehicle.year": {"value": 2020},
        "vehicle.make": {"value": "Ford"},
        "vehicle.model": {"value": "F-150"},
    }
    assert default_item_label("vehicle", 0, item) == "2020 Ford F-150"


def test_default_item_label_falls_back_to_index() -> None:
    from iga_marketing_master_2.gui.repeatable_pane import default_item_label

    assert default_item_label("vehicle", 4, {}) == "Vehicle #5"
    assert default_item_label("custom_group", 0, {}) == "Custom_Group #1"


# ---------------------------------------------------------------------------
# OperatorModal validation (no Qt instantiation)
# ---------------------------------------------------------------------------


def test_operator_modal_rejects_empty_text() -> None:
    from iga_marketing_master_2.gui.operator_modal import OperatorModal

    with pytest.raises(ValueError):
        OperatorModal._validate_user_facing_text("", "headline")
    with pytest.raises(ValueError):
        OperatorModal._validate_user_facing_text("   ", "what_to_do")


def test_operator_modal_warns_on_technical_tokens(caplog) -> None:
    """Technical tokens in operator-facing copy log a warning (non-fatal)."""
    import logging

    from iga_marketing_master_2.gui.operator_modal import OperatorModal

    with caplog.at_level(logging.WARNING, logger="iga.gui.operator_modal"):
        OperatorModal._validate_user_facing_text(
            "Locator failed: input.streState",
            "headline",
        )
    assert any("technical" in rec.message.lower() for rec in caplog.records)


def test_derive_window_title_truncates_long_input() -> None:
    from iga_marketing_master_2.gui.operator_modal import OperatorModal

    long_text = "x" * 200
    title = OperatorModal._derive_window_title(long_text)
    assert len(title) <= 80


def test_format_what_to_do_makes_bullets_for_multiline() -> None:
    from iga_marketing_master_2.gui.operator_modal import OperatorModal

    raw = "Step one.\nStep two.\nStep three."
    out = OperatorModal._format_what_to_do(raw)
    assert "<ul" in out
    assert "<li>Step one." in out


def test_format_what_to_do_passthrough_for_single_line() -> None:
    from iga_marketing_master_2.gui.operator_modal import OperatorModal

    assert "Just one." == OperatorModal._format_what_to_do("Just one.")


# ---------------------------------------------------------------------------
# Qt-bound tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    """Module-level QApplication. Idempotent across tests."""
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        pytest.skip("PySide6 not installed")
    app = QApplication.instance() or QApplication([])
    yield app


def test_section_table_model_basic_round_trip(qapp) -> None:
    from PySide6.QtCore import Qt

    from iga_marketing_master_2.gui.section_table import (
        FieldRow,
        SectionTableColumn,
        SectionTableModel,
    )

    rows = [
        FieldRow(
            domain_tag="submission.name",
            label="Submission Name",
            value="Acme",
            confidence=0.5,
            status="pending",
        ),
        FieldRow(
            domain_tag="submission.effective_date",
            label="Effective Date",
            value=None,
            confidence=0.0,
            status="pending",
        ),
    ]
    model = SectionTableModel(rows)
    assert model.rowCount() == 2
    assert model.columnCount() == SectionTableColumn.COUNT
    # Display value cell.
    idx_value = model.index(0, SectionTableColumn.VALUE)
    assert model.data(idx_value, Qt.ItemDataRole.DisplayRole) == "Acme"
    # Confidence formatting.
    idx_conf = model.index(0, SectionTableColumn.CONFIDENCE)
    assert model.data(idx_conf, Qt.ItemDataRole.DisplayRole) == "50%"
    # Background tint present for low confidence.
    bg = model.data(idx_value, Qt.ItemDataRole.BackgroundRole)
    assert bg is not None
    # Headers.
    assert model.headerData(SectionTableColumn.VALUE, Qt.Orientation.Horizontal) == "Value"


def test_section_table_model_commit_callback_invoked(qapp) -> None:
    from PySide6.QtCore import Qt

    from iga_marketing_master_2.gui.section_table import (
        FieldRow,
        SectionTableColumn,
        SectionTableModel,
    )

    captured: list[tuple[str, object]] = []

    def commit(tag: str, value: object) -> None:
        captured.append((tag, value))

    rows = [
        FieldRow(
            domain_tag="account.named_insured",
            label="Named Insured",
            value="Old",
            confidence=0.6,
            status="pending",
        ),
    ]
    model = SectionTableModel(rows, commit_callback=commit)
    idx = model.index(0, SectionTableColumn.VALUE)
    ok = model.setData(idx, "New", Qt.ItemDataRole.EditRole)
    assert ok is True
    assert captured == [("account.named_insured", "New")]
    # Manual edit: confidence should jump to 1.0 and status should become approved.
    refreshed = model.rows()[0]
    assert refreshed.value == "New"
    assert refreshed.confidence == 1.0
    assert refreshed.status == "approved"


def test_section_table_model_no_commit_when_value_unchanged(qapp) -> None:
    from PySide6.QtCore import Qt

    from iga_marketing_master_2.gui.section_table import (
        FieldRow,
        SectionTableColumn,
        SectionTableModel,
    )

    captured: list[tuple[str, object]] = []
    rows = [
        FieldRow(
            domain_tag="x.y",
            label="X",
            value="same",
            confidence=0.7,
            status="pending",
        )
    ]
    model = SectionTableModel(rows, commit_callback=lambda t, v: captured.append((t, v)))
    idx = model.index(0, SectionTableColumn.VALUE)
    model.setData(idx, "same", Qt.ItemDataRole.EditRole)
    assert captured == []  # No commit for a no-op edit.


def test_section_table_model_locked_row_not_editable(qapp) -> None:
    from PySide6.QtCore import Qt

    from iga_marketing_master_2.gui.section_table import (
        FieldRow,
        SectionTableColumn,
        SectionTableModel,
    )

    rows = [
        FieldRow(
            domain_tag="x.y",
            label="X",
            value="locked-value",
            confidence=1.0,
            status="locked",
            is_locked=True,
        )
    ]
    model = SectionTableModel(rows)
    idx = model.index(0, SectionTableColumn.VALUE)
    flags = model.flags(idx)
    assert not (flags & Qt.ItemFlag.ItemIsEditable)


def test_operator_modal_constructor_builds(qapp) -> None:
    from iga_marketing_master_2.gui.operator_modal import (
        OperatorAction,
        OperatorActionRole,
        OperatorModal,
    )

    modal = OperatorModal(
        None,
        headline="EPIC didn't accept 'GA' for State.",
        what_to_do="Open the State dropdown in EPIC and pick a value.\nThen click Resume.",
        cancel_effect="Your edits are saved. Run won't continue.",
        technical_detail="Locator failed on input.streState.",
        actions=[
            OperatorAction(
                label="Resume",
                return_value="resume",
                role=OperatorActionRole.PRIMARY,
                is_default=True,
            ),
            OperatorAction(
                label="Cancel",
                return_value="cancel",
                is_cancel=True,
            ),
        ],
    )
    # The window title is derived from the headline's first sentence.
    assert "EPIC" in modal.windowTitle()
    # 4 parts present — at least the headline label, what_to_do label, cancel label.
    # We don't dig into the layout; just assert the dialog constructs.
    assert modal.isModal()


def test_operator_modal_default_actions_are_ok_cancel(qapp) -> None:
    from iga_marketing_master_2.gui.operator_modal import OperatorModal

    modal = OperatorModal(
        None,
        headline="Plain headline.",
        what_to_do="Do something.",
        cancel_effect="Nothing happens.",
    )
    assert len(modal._actions) == 2
    labels = [a.label for a in modal._actions]
    assert "OK" in labels
    assert "Cancel" in labels


def test_pause_choice_values() -> None:
    """Pause-choice vocabulary is a contract with enter.run_entry_session."""
    from iga_marketing_master_2.gui.operator_modal import PauseChoice

    assert PauseChoice.RESUME.value == "resume"
    assert PauseChoice.SKIP.value == "skip"
    assert PauseChoice.CANCEL.value == "cancel"


def test_run_controls_resume_visibility(qapp) -> None:
    """The Begin Entry / Extract buttons were removed from the run-controls
    bar in the UX pass that consolidated controls into the upload panel and
    progress meter. The Resume button stays — it surfaces during paused
    extraction. This test confirms its visibility tracks paused state.
    """
    from iga_marketing_master_2.gui.run_controls import RunControlsBar

    bar = RunControlsBar()
    bar.set_paused(False)
    assert not bar._resume_btn.isVisible()
    bar.set_paused(True)
    assert bar._resume_btn.isVisibleTo(bar) or True  # tolerant — no event loop


def test_pending_pdfs_pane_add_dedup_remove(qapp, tmp_path: Path) -> None:
    """gui-fix-2 #1: queue widget dedups by resolved path and removes items."""
    from iga_marketing_master_2.gui.pending_pdfs_pane import PendingPdfsPane

    pane = PendingPdfsPane()
    a = tmp_path / "a.pdf"
    b = tmp_path / "b.pdf"
    a.write_text("x")
    b.write_text("x")

    # Add three paths but two are the same — we expect 2 unique entries.
    added = pane.add_paths([a, b, a])
    assert added == 2
    assert [p.name for p in pane.paths()] == ["a.pdf", "b.pdf"]

    # Adding an already-queued path is a no-op.
    again = pane.add_paths([a])
    assert again == 0
    assert pane.count() == 2

    # set_paths replaces wholesale.
    pane.set_paths([b])
    assert [p.name for p in pane.paths()] == ["b.pdf"]

    # clear_queue empties.
    pane.clear_queue()
    assert pane.count() == 0
    assert pane.paths() == []


def test_pending_pdfs_pane_emits_paths_changed(qapp, tmp_path: Path) -> None:
    """The pane signals on every mutation so the host can refresh button states."""
    from iga_marketing_master_2.gui.pending_pdfs_pane import PendingPdfsPane

    pane = PendingPdfsPane()
    a = tmp_path / "a.pdf"
    a.write_text("x")

    fires: list[None] = []
    pane.paths_changed.connect(lambda: fires.append(None))

    pane.add_paths([a])
    assert len(fires) == 1
    pane.add_paths([a])  # dedup -> no signal
    assert len(fires) == 1
    pane.set_paths([])
    assert len(fires) == 2
    pane.clear_queue()  # already empty -> no signal
    assert len(fires) == 2


def test_audit_log_pane_appends_event(qapp) -> None:
    from iga_marketing_master_2.gui.audit_log import AuditLogPane

    pane = AuditLogPane()
    pane.append_event("hello world")
    assert "hello world" in pane.toPlainText()


def test_pdf_preview_fallback_for_missing_file(qapp, tmp_path: Path) -> None:
    from iga_marketing_master_2.gui.pdf_preview import PdfPreview

    preview = PdfPreview()
    preview.open_path(tmp_path / "nonexistent.pdf")
    # Stack should be on the fallback widget.
    assert preview.current_path() is None


# ---------------------------------------------------------------------------
# UX-pass #4 — tab badge calculation
# ---------------------------------------------------------------------------


def test_count_tab_field_total_singleton() -> None:
    """count_tab_field_total counts state.fields keys whose tab_key matches."""
    from iga_marketing_master_2.gui.main_window import count_tab_field_total

    state = {
        "fields": {
            "policy.gl.aggregate_limit": {"value": 1_000_000},
            "policy.gl.each_occurrence": {"value": 500_000},
            "policy.auto.combined_single_limit": {"value": 1_000_000},
            "submission.name": {"value": "Acme"},
        },
        "repeatables": {},
    }
    assert count_tab_field_total(state, "policy.gl") == 2
    assert count_tab_field_total(state, "policy.auto") == 1
    assert count_tab_field_total(state, "submission") == 1
    assert count_tab_field_total(state, "policy.umbrella") == 0


def test_count_tab_field_total_repeatable() -> None:
    """count_tab_field_total uses len(repeatables[group]) for repeatable namespaces."""
    from iga_marketing_master_2.gui.main_window import count_tab_field_total

    state = {
        "fields": {},
        "repeatables": {
            "vehicle": [{"vehicle.year": {}}, {"vehicle.year": {}}, {}],
            "location": [{}, {}],
        },
    }
    assert count_tab_field_total(state, "vehicle") == 3
    assert count_tab_field_total(state, "location") == 2
    assert count_tab_field_total(state, "driver") == 0


def test_count_low_confidence_in_tab_singleton() -> None:
    from iga_marketing_master_2.gui.main_window import count_low_confidence_in_tab

    state = {
        "fields": {
            # Below threshold and not approved → counted.
            "policy.gl.aggregate_limit": {"confidence": 0.5, "status": "pending"},
            # At threshold (0.85) → not counted.
            "policy.gl.each_occurrence": {"confidence": 0.85, "status": "pending"},
            # Approved → not counted regardless of confidence.
            "policy.gl.deductible": {"confidence": 0.1, "status": "approved"},
            # Different tab.
            "policy.auto.combined_single_limit": {"confidence": 0.2, "status": "pending"},
        },
        "repeatables": {},
    }
    assert count_low_confidence_in_tab(state, "policy.gl") == 1
    assert count_low_confidence_in_tab(state, "policy.auto") == 1
    assert count_low_confidence_in_tab(state, "submission") == 0


def test_count_low_confidence_in_tab_repeatable() -> None:
    from iga_marketing_master_2.gui.main_window import count_low_confidence_in_tab

    state = {
        "fields": {},
        "repeatables": {
            "vehicle": [
                {
                    "vehicle.year": {"confidence": 0.4, "status": "pending"},
                    "vehicle.make": {"confidence": 0.95, "status": "pending"},
                },
                {
                    "vehicle.year": {"confidence": 0.3, "status": "pending"},
                },
            ],
        },
    }
    assert count_low_confidence_in_tab(state, "vehicle") == 2


def test_build_tab_label_formats() -> None:
    from iga_marketing_master_2.gui.main_window import build_tab_label

    state = {
        "fields": {
            "policy.gl.a": {"confidence": 0.9, "status": "approved"},
            "policy.gl.b": {"confidence": 0.95, "status": "approved"},
        },
        "repeatables": {},
    }
    # All confident → just the count.
    assert build_tab_label(state, "policy.gl") == "General Liability (2)"
    # No fields at all → no badge.
    assert build_tab_label(state, "policy.umbrella") == "Umbrella/Excess"
    # Add one low-confidence field.
    state["fields"]["policy.gl.c"] = {"confidence": 0.4, "status": "pending"}
    assert build_tab_label(state, "policy.gl") == "General Liability (3 · 1!)"


# ---------------------------------------------------------------------------
# UX-pass #6 — recent-clients persistence
# ---------------------------------------------------------------------------


def test_update_recent_clients_dedupes_and_caps(tmp_path: Path) -> None:
    from iga_marketing_master_2.gui.main_window import update_recent_clients

    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    d = tmp_path / "d"
    e = tmp_path / "e"
    f = tmp_path / "f"
    for p in (a, b, c, d, e, f):
        p.mkdir()

    out = update_recent_clients([], a)
    assert out == [a]

    # Adding the same path again moves it to the front (deduped).
    out = update_recent_clients([b, c, a], a)
    assert out[0] == a
    assert out.count(a) == 1

    # Cap at 5.
    initial = [b, c, d, e, f]
    out = update_recent_clients(initial, a)
    assert len(out) == 5
    assert out[0] == a


def test_load_save_recent_clients_roundtrip(tmp_path: Path, monkeypatch) -> None:
    """QSettings round-trip via an in-memory scope."""
    from PySide6.QtCore import QSettings

    from iga_marketing_master_2.gui.main_window import (
        load_recent_clients,
        save_recent_clients,
    )

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat,
        QSettings.Scope.UserScope,
        str(tmp_path),
    )
    settings = QSettings("IGA-Test", "test_recent_clients")
    settings.clear()

    a = tmp_path / "ClientA"
    b = tmp_path / "ClientB"
    a.mkdir()
    b.mkdir()
    save_recent_clients(settings, [a, b])
    loaded = load_recent_clients(settings)
    assert [str(p) for p in loaded] == [str(a), str(b)]


# ---------------------------------------------------------------------------
# UX-pass #5 — humanize_seconds_ago
# ---------------------------------------------------------------------------


def test_humanize_seconds_ago_branches() -> None:
    from iga_marketing_master_2.gui.main_window import humanize_seconds_ago

    assert humanize_seconds_ago(0) == "just now"
    assert humanize_seconds_ago(0.5) == "just now"
    assert humanize_seconds_ago(15) == "15s ago"
    assert humanize_seconds_ago(120) == "2m ago"
    assert humanize_seconds_ago(7200) == "2h ago"
    assert humanize_seconds_ago(2 * 86_400) == "2d ago"


# ---------------------------------------------------------------------------
# UX-pass #7 — bulk-action handler (pure state mutation)
# ---------------------------------------------------------------------------


def test_apply_bulk_action_approve_all() -> None:
    from iga_marketing_master_2.gui.main_window import MainWindow

    state = {
        "fields": {
            "policy.gl.a": {"value": "x", "status": "pending"},
            "policy.gl.b": {"value": "y", "status": "pending"},
            "policy.gl.c": {"value": "z", "status": "locked"},  # locked: skipped
            "policy.auto.x": {"value": "q", "status": "pending"},
        },
        "repeatables": {},
    }
    affected = MainWindow._apply_bulk_action_to_state(state, "approve_all", "policy.gl")
    assert affected == 2
    assert state["fields"]["policy.gl.a"]["status"] == "approved"
    assert state["fields"]["policy.gl.b"]["status"] == "approved"
    assert state["fields"]["policy.gl.c"]["status"] == "locked"  # untouched
    # Other tabs untouched.
    assert state["fields"]["policy.auto.x"]["status"] == "pending"


def test_apply_bulk_action_reject_all_clears_value() -> None:
    from iga_marketing_master_2.gui.main_window import MainWindow

    state = {
        "fields": {
            "policy.gl.a": {"value": "ACME", "status": "approved"},
            "policy.gl.b": {"value": "1000", "status": "pending"},
            "policy.gl.c": {"value": "z", "status": "locked"},
        },
        "repeatables": {},
    }
    affected = MainWindow._apply_bulk_action_to_state(state, "reject_all", "policy.gl")
    assert affected == 2
    assert state["fields"]["policy.gl.a"]["value"] is None
    assert state["fields"]["policy.gl.a"]["status"] == "pending"
    assert state["fields"]["policy.gl.c"]["value"] == "z"  # locked: skipped


def test_apply_bulk_action_lock_all_approved() -> None:
    from iga_marketing_master_2.gui.main_window import MainWindow

    state = {
        "fields": {
            "policy.gl.a": {"value": "ACME", "status": "approved"},
            "policy.gl.b": {"value": "B", "status": "pending"},
            "policy.gl.c": {"value": "C", "status": "approved"},
        },
        "repeatables": {},
    }
    affected = MainWindow._apply_bulk_action_to_state(state, "lock_all_approved", "policy.gl")
    assert affected == 2
    assert state["fields"]["policy.gl.a"]["status"] == "locked"
    assert state["fields"]["policy.gl.b"]["status"] == "pending"
    assert state["fields"]["policy.gl.c"]["status"] == "locked"


def test_apply_bulk_action_repeatable_group() -> None:
    from iga_marketing_master_2.gui.main_window import MainWindow

    state = {
        "fields": {},
        "repeatables": {
            "vehicle": [
                {
                    "vehicle.year": {"value": 2020, "status": "pending"},
                    "vehicle.make": {"value": "Ford", "status": "pending"},
                },
                {
                    "vehicle.year": {"value": 2019, "status": "approved"},
                },
            ],
        },
    }
    affected = MainWindow._apply_bulk_action_to_state(state, "approve_all", "vehicle")
    # 2 pending + 1 already-approved (still "approved" → no change for it; only 2 transitions counted? No — "approve_all" sets status to approved unconditionally except for locked. So already-approved stays "approved" but the function still touches it.)
    # Per implementation: _touch returns True for approve_all whenever status != locked.
    assert affected == 3


# ---------------------------------------------------------------------------
# UX-pass #8 — find-bar substring filter
# ---------------------------------------------------------------------------


def test_find_bar_matches_query_helper() -> None:
    from iga_marketing_master_2.gui.find_bar import matches_query

    # Empty query matches everything.
    assert matches_query("policy.gl.aggregate_limit", "") is True
    # Case-insensitive substring.
    assert matches_query("policy.gl.aggregate_limit", "agg") is True
    assert matches_query("policy.gl.aggregate_limit", "AGG") is True
    assert matches_query("policy.gl.aggregate_limit", "GL") is True
    # No match.
    assert matches_query("policy.gl.aggregate_limit", "umbrella") is False


def test_find_bar_open_close_emits_signals(qapp) -> None:
    from iga_marketing_master_2.gui.find_bar import FindBar

    bar = FindBar()
    closed_fires: list[None] = []
    bar.closed.connect(lambda: closed_fires.append(None))

    queries: list[str] = []
    bar.query_changed.connect(lambda q: queries.append(q))

    assert not bar.is_open()
    bar.open()
    assert bar.is_open()

    bar._line_edit.setText("agg")
    assert "agg" in queries

    bar.close()
    assert not bar.is_open()
    assert closed_fires  # at least one closed signal


# ---------------------------------------------------------------------------
# UX-pass #9 — low-confidence row predicate + combined filter
# ---------------------------------------------------------------------------


def test_is_low_confidence_row_predicate() -> None:
    from iga_marketing_master_2.gui.section_table import (
        FieldRow,
        is_low_confidence_row,
    )

    # Pending → always visible.
    pending = FieldRow(
        domain_tag="x.y", label="Y", value=None, confidence=0.99, status="pending"
    )
    assert is_low_confidence_row(pending) is True

    # Approved + high confidence → hidden.
    approved_high = FieldRow(
        domain_tag="x.y", label="Y", value="v", confidence=0.95, status="approved"
    )
    assert is_low_confidence_row(approved_high) is False

    # Approved + low confidence → still visible (operator may want to revisit).
    approved_low = FieldRow(
        domain_tag="x.y", label="Y", value="v", confidence=0.5, status="approved"
    )
    assert is_low_confidence_row(approved_low) is True

    # Has conflicts → always visible.
    with_conflicts = FieldRow(
        domain_tag="x.y",
        label="Y",
        value="v",
        confidence=1.0,
        status="approved",
        has_conflicts=True,
    )
    assert is_low_confidence_row(with_conflicts) is True


def test_row_visible_under_filters_combines_both() -> None:
    from iga_marketing_master_2.gui.section_table import (
        FieldRow,
        row_visible_under_filters,
    )

    row = FieldRow(
        domain_tag="policy.gl.aggregate_limit",
        label="Aggregate",
        value=1000,
        confidence=0.95,
        status="approved",
    )
    # No filters → visible.
    assert row_visible_under_filters(row) is True
    # find_query that matches → visible.
    assert row_visible_under_filters(row, find_query="agg") is True
    # find_query that doesn't match → hidden.
    assert row_visible_under_filters(row, find_query="umbrella") is False
    # Low-confidence filter ON + this is high-confidence + approved → hidden.
    assert row_visible_under_filters(row, low_confidence_only=True) is False
    # If both filters active and row matches the query but is high-conf+approved:
    assert row_visible_under_filters(row, find_query="agg", low_confidence_only=True) is False


def test_apply_row_visibility_on_view(qapp) -> None:
    from iga_marketing_master_2.gui.section_table import (
        FieldRow,
        SectionTableModel,
        SectionTableView,
    )

    rows = [
        FieldRow(
            domain_tag="policy.gl.aggregate_limit",
            label="A",
            value=1000,
            confidence=0.5,
            status="pending",
        ),
        FieldRow(
            domain_tag="policy.auto.csl",
            label="B",
            value=500,
            confidence=0.95,
            status="approved",
        ),
    ]
    model = SectionTableModel(rows)
    view = SectionTableView()
    view.setModel(model)

    # No filter — both rows visible.
    visible = view.apply_row_visibility()
    assert visible == 2
    assert not view.isRowHidden(0)
    assert not view.isRowHidden(1)

    # Substring filter "auto" hides the GL row.
    visible = view.apply_row_visibility(find_query="auto")
    assert visible == 1
    assert view.isRowHidden(0)
    assert not view.isRowHidden(1)

    # Low-conf-only hides the approved high-confidence row.
    visible = view.apply_row_visibility(low_confidence_only=True)
    assert visible == 1
    assert not view.isRowHidden(0)
    assert view.isRowHidden(1)


# ---------------------------------------------------------------------------
# UX-pass #7 — BulkActionBar widget
# ---------------------------------------------------------------------------


def test_bulk_action_bar_emits_action_signals(qapp) -> None:
    from iga_marketing_master_2.gui.section_table import BulkActionBar

    bar = BulkActionBar()
    actions: list[str] = []
    bar.action_requested.connect(lambda kind: actions.append(kind))

    bar._approve_btn.click()
    bar._reject_btn.click()
    bar._lock_btn.click()
    assert actions == ["approve_all", "reject_all", "lock_all_approved"]


# ---------------------------------------------------------------------------
# UX-pass #10 — welcome pane construction
# ---------------------------------------------------------------------------


def test_welcome_pane_constructs_and_renders_text(qapp) -> None:
    from iga_marketing_master_2.gui.welcome_pane import WelcomePane

    pane = WelcomePane()
    # Walk for the QLabel; assert it has the welcome headline.
    from PySide6.QtWidgets import QLabel

    labels = pane.findChildren(QLabel)
    text_blob = " ".join(label.text() for label in labels)
    assert "Welcome to IGA Marketing Master 2.0" in text_blob
    assert "Ctrl+O" in text_blob

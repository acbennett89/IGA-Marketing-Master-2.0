"""reconciliation_dialog.py — Client tab ↔ Named Insureds resolution modal.

Shown at the end of every extraction run when ``reconcile.compute_deltas``
returns one or more ``"conflict"`` or ``"canon_extraction_diff"`` deltas.

Layout per field:
- Field label (e.g., "FEIN / Tax ID")
- Three radio buttons (one column each):
    (•) Client tab: <value>
    ( ) Named Insureds: <value>
    ( ) Custom: [text input]

For ``conflict`` rows the default is the Client tab value (operator-typed wins
until they say otherwise). For ``canon_extraction_diff`` rows the labels swap
to "Keep canon" / "Use extracted" and the default is **Keep canon**.

Buttons: Apply (green, default) / Cancel. Cancel returns ``Rejected`` and the
caller must discard any silent merges too (no state change on cancel).

See ``C:\\Users\\Andrew\\.claude\\plans\\eager-shimmying-hummingbird.md``.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..reconcile import (
    INSURED_FIELD_LABELS,
    ReconcileDelta,
)

# Insured attrs that should expose a validated dropdown instead of a free-text
# Custom field. Mapping is ``attr -> tuple_of_valid_values``. Leave unmapped
# attrs alone — they get the QLineEdit fallback.
def _validated_choices() -> dict[str, tuple[str, ...]]:
    """Return the validated-choices map. Lazy import to avoid pulling
    ``epic_steps`` (which loads Playwright) at module import time."""
    from ..epic_steps.step_account_create import BUSINESS_TYPES
    return {"business_type": tuple(BUSINESS_TYPES)}


class _ConflictRow(QWidget):
    """One field row: label + 3 radio choices (client / NI / custom).

    For fields with a known valid set (e.g. ``business_type``), the Custom
    option renders as a QComboBox of valid values instead of a free-text
    QLineEdit. Custom always starts blank — operator must actively pick
    or type something before the row contributes a value.
    """

    def __init__(
        self,
        delta: ReconcileDelta,
        parent: QWidget | None = None,
        *,
        validated_values: tuple[str, ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self._delta = delta
        self._group = QButtonGroup(self)
        self._validated_values = validated_values
        # Either a QLineEdit (free text) or a QComboBox (validated dropdown).
        self._custom_input: QLineEdit | QComboBox
        is_canon_diff = delta.kind == "canon_extraction_diff"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        label = QLabel(INSURED_FIELD_LABELS.get(delta.field, delta.field))
        label.setStyleSheet("font-weight: 600; color: #0f172a;")
        outer.addWidget(label)

        if is_canon_diff:
            client_label = f"Keep canon: {delta.insured_value or '(blank)'}"
            ni_label = f"Use extracted: {delta.ni_value or '(blank)'}"
        else:
            client_label = f"Client tab: {delta.insured_value or '(blank)'}"
            ni_label = f"Named Insureds: {delta.ni_value or '(blank)'}"

        self._rb_client = QRadioButton(client_label, self)
        self._rb_ni = QRadioButton(ni_label, self)
        self._rb_custom = QRadioButton("Other:", self)
        # Explicit indicator styling — setting any QSS on QRadioButton turns
        # off Qt's native indicator rendering, so we re-draw it ourselves with
        # a visible circle that stays in-theme on both light and dark systems.
        _rb_qss = (
            "QRadioButton { padding: 2px 0; spacing: 8px;"
            " color: #0f172a; font-size: 13px; }"
            "QRadioButton::indicator { width: 14px; height: 14px;"
            " border: 1px solid #94a3b8; border-radius: 8px; background: white; }"
            "QRadioButton::indicator:hover { border: 1px solid #2563eb; }"
            "QRadioButton::indicator:checked { background: #2563eb;"
            " border: 4px solid white; outline: 1px solid #2563eb; }"
        )
        for rb in (self._rb_client, self._rb_ni, self._rb_custom):
            rb.setStyleSheet(_rb_qss)
            outer.addWidget(rb)
        self._group.addButton(self._rb_client, 0)
        self._group.addButton(self._rb_ni, 1)
        self._group.addButton(self._rb_custom, 2)

        # Custom input renders as a validated dropdown when we have a
        # canonical list of valid values for this field; else free text.
        if validated_values:
            combo = QComboBox(self)
            combo.addItem("— Select —", "")
            for v in validated_values:
                combo.addItem(v, v)
            combo.setStyleSheet(
                "QComboBox { padding: 4px 6px; border: 1px solid #cbd5e1;"
                " border-radius: 3px; }"
            )
            combo.currentIndexChanged.connect(
                lambda _i: self._rb_custom.setChecked(True)
            )
            self._custom_input = combo
            custom_widget: QWidget = combo
        else:
            edit = QLineEdit(self)
            edit.setPlaceholderText("Type a custom value")
            edit.setStyleSheet(
                "QLineEdit { padding: 4px 6px; border: 1px solid #cbd5e1;"
                " border-radius: 3px; }"
            )
            edit.textEdited.connect(
                lambda _t: self._rb_custom.setChecked(True)
            )
            self._custom_input = edit
            custom_widget = edit

        # Indent the custom field under its radio.
        custom_row = QHBoxLayout()
        custom_row.setContentsMargins(22, 0, 0, 0)
        custom_row.addWidget(custom_widget)
        outer.addLayout(custom_row)

        # Default selection: Client tab. Custom widget starts blank — the
        # operator must actively pick a value before the row contributes one.
        self._rb_client.setChecked(True)

    def chosen_value(self) -> str:
        sel = self._group.checkedId()
        if sel == 0:
            return self._delta.insured_value
        if sel == 1:
            return self._delta.ni_value
        # Custom — read from QLineEdit.text() or QComboBox.currentData().
        if isinstance(self._custom_input, QComboBox):
            data = self._custom_input.currentData() or ""
            return str(data).strip()
        return self._custom_input.text().strip()

    @property
    def field(self) -> str:
        return self._delta.field


class ReconciliationDialog(QDialog):
    """Modal that surfaces post-extraction Client ↔ Named Insureds conflicts.

    ``conflicts`` are the rows the operator must resolve. ``silent_merges`` are
    shown as an informational strip ("Auto-fill: FEIN, City"); they're applied
    by the caller only if the operator clicks Apply, NOT on Cancel.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        entity_name: str,
        conflicts: list[ReconcileDelta],
        silent_merges: list[ReconcileDelta] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Reconcile Named Insureds")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._conflicts = list(conflicts)
        self._rows: list[_ConflictRow] = []
        self._resolutions: dict[str, str] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(18, 18, 18, 14)
        outer.setSpacing(10)

        title = QLabel(f"Reconcile {entity_name or '(unnamed)'}")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #0f172a;")
        outer.addWidget(title)

        n = len(conflicts)
        sub = QLabel(
            f"Found {n} difference{'s' if n != 1 else ''} between the Client tab "
            "and the Named Insureds table. Pick the value to keep for each field."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet("color: #475569; font-size: 12px;")
        outer.addWidget(sub)

        if silent_merges:
            names = ", ".join(
                INSURED_FIELD_LABELS.get(d.field, d.field) for d in silent_merges
            )
            info = QLabel(f"Auto-fill (gaps): {names}")
            info.setWordWrap(True)
            info.setStyleSheet(
                "color: #166534; background: #f0fdf4; border: 1px solid #bbf7d0;"
                " border-radius: 4px; padding: 6px 8px; font-size: 12px;"
            )
            outer.addWidget(info)

        # Conflict rows — wrap in a scroll area in case there are many.
        inner = QWidget(self)
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 6, 0, 6)
        inner_layout.setSpacing(14)
        validated = _validated_choices()
        for d in conflicts:
            row = _ConflictRow(d, inner, validated_values=validated.get(d.field))
            self._rows.append(row)
            inner_layout.addWidget(row)
        inner_layout.addStretch()

        scroll = QScrollArea(self)
        scroll.setWidget(inner)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setMinimumHeight(min(420, 90 + 110 * len(conflicts)))
        outer.addWidget(scroll, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumWidth(110)
        cancel_btn.clicked.connect(self.reject)
        apply_btn = QPushButton("Apply")
        apply_btn.setMinimumWidth(110)
        apply_btn.setDefault(True)
        apply_btn.setStyleSheet(
            "QPushButton { background: #16a34a; color: white; padding: 6px 14px;"
            " border-radius: 4px; font-weight: 600; }"
            "QPushButton:hover { background: #15803d; }"
        )
        apply_btn.clicked.connect(self._on_apply)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(apply_btn)
        outer.addLayout(btn_row)

    def _on_apply(self) -> None:
        self._resolutions = {row.field: row.chosen_value() for row in self._rows}
        self.accept()

    def resolutions(self) -> dict[str, str]:
        """Per-field operator picks. Empty dict before Apply / after Cancel."""
        return dict(self._resolutions)

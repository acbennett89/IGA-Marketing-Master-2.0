"""section_table.py — per-section table model + delegate + view.

A "section" is the slice of state shown in one tab. For singleton-namespace
sections (``submission``, ``account``, ``policy.gl``, etc.) the slice is a
filtered view of ``state.fields`` whose keys start with the section's tag
prefix. For repeatable groups the table is owned by
:class:`~iga_marketing_master_2.gui.repeatable_pane.RepeatablePane`, which
re-uses :class:`SectionTableModel` against a single ``RepeatableItem``
record.

Visual contract (ARCHITECTURE §8.4):

- Confidence-based cell tint (red < 0.6, yellow < 0.85, none otherwise).
  Manually edited / approved fields lose tint.
- Locked fields show a small lock icon next to the value.
- Opus-sourced fields show an "O" badge in the dedicated badge column.
- Source column is clickable and emits ``source_clicked`` so the host
  window can navigate the PDF preview.

The model is intentionally generic — it consumes a list of "row records"
(plain dicts, see :class:`FieldRow` for the typed schema) so the same code
serves both the singleton fields tabs and the per-item form pane in
``RepeatablePane``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    Qt,
    Signal,
)
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateEdit,
    QLineEdit,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QWidget,
)

from ..logger import get_logger

__all__ = [
    "CONFIDENCE_HIGH_THRESHOLD",
    "CONFIDENCE_LOW_THRESHOLD",
    "FieldRow",
    "OPUS_BADGE_GLYPH",
    "SectionDelegate",
    "SectionTableColumn",
    "SectionTableModel",
    "SectionTableView",
    "confidence_color",
]


_logger = get_logger("gui.section_table")


# Confidence thresholds from ARCHITECTURE.md §8.4.
CONFIDENCE_LOW_THRESHOLD: float = 0.6
CONFIDENCE_HIGH_THRESHOLD: float = 0.85

# Pale tints — readable on white with dark text.
_TINT_LOW: QColor = QColor("#FFD6D6")     # pale red
_TINT_MID: QColor = QColor("#FFF7CC")     # pale yellow
_TINT_ENTERED: QColor = QColor("#E6F4EA")  # very pale green
_TINT_NONE: QColor | None = None

OPUS_BADGE_GLYPH: str = "Ⓞ"  # CIRCLED LATIN CAPITAL LETTER O — used as the Opus badge.
LOCK_GLYPH: str = "\U0001F512"
CONFLICT_GLYPH: str = "▾"  # small downward chevron


# ---------------------------------------------------------------------------
# Public column enum + row dataclass
# ---------------------------------------------------------------------------


class SectionTableColumn:
    """Stable column indices the model and delegate share."""

    LABEL = 0
    VALUE = 1
    STATUS = 2
    CONFIDENCE = 3
    SOURCE = 4
    BADGES = 5
    COUNT = 6


_HEADERS: tuple[str, ...] = ("Field", "Value", "Status", "Conf.", "Source", "")


@dataclass(slots=True, kw_only=True)
class FieldRow:
    """One row in a section table.

    The model treats each row as opaque; it reads only the public attributes.
    Mutation flows through :meth:`SectionTableModel.update_value`, which calls
    the host-supplied ``commit_callback``.
    """

    domain_tag: str
    label: str
    value: object
    confidence: float
    status: str  # one of: pending, approved, locked, entered
    model_used: str | None = None
    source_doc_id: str | None = None
    source_page: int | None = None
    has_conflicts: bool = False
    type_hint: str = "text"  # text | date | select | checkbox | number
    enum_values: list[str] = field(default_factory=list)
    is_required: bool = False
    is_locked: bool = False  # Mirrors status == "locked"; surfaced separately for icon use.

    @classmethod
    def from_field_record(cls, *, domain_tag: str, label: str, record: dict, type_hint: str = "text", enum_values: list[str] | None = None, is_required: bool = False) -> "FieldRow":
        """Build a row from a state ``FieldRecord`` dict."""
        sources = record.get("source") or []
        first_source = sources[0] if sources else {}
        return cls(
            domain_tag=domain_tag,
            label=label,
            value=record.get("value"),
            confidence=float(record.get("confidence", 0.0) or 0.0),
            status=record.get("status", "pending"),
            model_used=record.get("model_used"),
            source_doc_id=first_source.get("doc_id"),
            source_page=first_source.get("page"),
            has_conflicts=bool(record.get("conflicts")),
            type_hint=type_hint,
            enum_values=list(enum_values or []),
            is_required=is_required,
            is_locked=record.get("status") == "locked",
        )


# ---------------------------------------------------------------------------
# Color helper (pure function — easy to unit-test)
# ---------------------------------------------------------------------------


def confidence_color(confidence: float, status: str) -> QColor | None:
    """Return the background tint per ARCHITECTURE §8.4.

    - ``status`` of ``approved`` or ``locked`` → no tint (the operator's
      stamp wins over Claude's confidence).
    - ``status`` of ``entered`` → very pale green (signals "this is in EPIC").
    - Otherwise: red if ``confidence < CONFIDENCE_LOW_THRESHOLD``, yellow if
      between low and high, no tint at/above ``CONFIDENCE_HIGH_THRESHOLD``.
    """
    # Once a human has signed off (approved/locked), Claude's confidence
    # score is no longer relevant — strip the tint so the row reads clean.
    if status in {"approved", "locked"}:
        return _TINT_NONE
    if status == "entered":
        return _TINT_ENTERED
    if confidence < CONFIDENCE_LOW_THRESHOLD:
        return _TINT_LOW
    if confidence < CONFIDENCE_HIGH_THRESHOLD:
        return _TINT_MID
    return _TINT_NONE


# ---------------------------------------------------------------------------
# Value coercion (delegate uses these to map editor output → field type)
# ---------------------------------------------------------------------------


def coerce_value(raw: object, type_hint: str) -> object:
    """Coerce an editor's raw value into the canonical field type.

    Pure function — decoupled from Qt so tests can exercise it without a
    QApplication.
    """
    if raw is None:
        return None
    if type_hint == "checkbox":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"true", "yes", "1", "y"}
        return bool(raw)
    if type_hint == "number":
        if isinstance(raw, (int, float)):
            return raw
        text = str(raw).strip().replace(",", "")
        if not text:
            return None
        try:
            if "." in text:
                return float(text)
            return int(text)
        except ValueError:
            # Not a clean number — store the user's literal, GUI will flag.
            return str(raw).strip()
    if type_hint == "date":
        # Keep ISO-style strings; downstream validation lives elsewhere.
        return str(raw).strip() or None
    if type_hint == "select":
        text = str(raw).strip()
        return text or None
    # Default text path.
    text = str(raw)
    return text


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------


class SectionTableModel(QAbstractTableModel):
    """``QAbstractTableModel`` over a list of :class:`FieldRow`.

    The model never touches state.json; on edit, it invokes
    ``commit_callback(domain_tag, new_value)`` and the host is expected to
    persist via :func:`state.save_atomic` and write a history entry.

    Signals:
        source_clicked(doc_id: str, page: int) — emitted when the user
            clicks the Source cell.
        conflict_clicked(domain_tag: str) — emitted when the user clicks
            the chevron in a cell that has conflicts.
    """

    source_clicked = Signal(str, int)
    conflict_clicked = Signal(str)

    def __init__(
        self,
        rows: list[FieldRow] | None = None,
        *,
        commit_callback: Callable[[str, object], None] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows: list[FieldRow] = list(rows or [])
        self._commit_callback = commit_callback

    # -- Public mutation surface --------------------------------------------

    def set_rows(self, rows: list[FieldRow]) -> None:
        """Replace the row set in one shot. Resets the model."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def rows(self) -> list[FieldRow]:
        return list(self._rows)

    def row_for_domain_tag(self, domain_tag: str) -> FieldRow | None:
        for row in self._rows:
            if row.domain_tag == domain_tag:
                return row
        return None

    def update_value(self, row_index: int, new_value: object) -> None:
        """Public entry point for edits coming from delegates.

        Calls the host's commit callback (which persists state); on success
        updates the in-memory row to ``confidence=1.0`` and ``status="approved"``
        per ARCHITECTURE §8.4 (manual edits clear the tint).
        """
        if not 0 <= row_index < len(self._rows):
            return
        row = self._rows[row_index]
        coerced = coerce_value(new_value, row.type_hint)
        # Avoid emitting dataChanged for a typing churn that ended up at the
        # same value the row already had — saves needless state.json writes.
        if coerced == row.value:
            return  # no-op
        if self._commit_callback is not None:
            try:
                self._commit_callback(row.domain_tag, coerced)
            except Exception:  # noqa: BLE001
                # Persistence failed; don't lie to the operator by updating
                # the in-memory row to a value that isn't on disk.
                _logger.exception("commit_callback failed for %s", row.domain_tag)
                return
        # A manual edit is implicitly an operator approval — bump confidence
        # to 100% and promote pending → approved so the row is eligible for
        # the next entry session.
        row.value = coerced
        row.confidence = 1.0
        if row.status == "pending":
            row.status = "approved"
        top = self.index(row_index, 0)
        bottom = self.index(row_index, SectionTableColumn.COUNT - 1)
        self.dataChanged.emit(top, bottom)

    # -- QAbstractTableModel boilerplate ------------------------------------

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return SectionTableColumn.COUNT

    def headerData(  # noqa: D401
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return _HEADERS[section] if 0 <= section < len(_HEADERS) else ""
        return section + 1

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
        col = index.column()
        row = self._rows[index.row()]
        if col == SectionTableColumn.VALUE and row.status != "locked":
            base |= Qt.ItemFlag.ItemIsEditable
        return base

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # noqa: C901
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole or role == Qt.ItemDataRole.EditRole:
            if col == SectionTableColumn.LABEL:
                return self._format_label(row)
            if col == SectionTableColumn.VALUE:
                return self._format_value(row)
            if col == SectionTableColumn.STATUS:
                return row.status
            if col == SectionTableColumn.CONFIDENCE:
                return f"{int(round(row.confidence * 100))}%"
            if col == SectionTableColumn.SOURCE:
                if row.source_doc_id is None:
                    return ""
                return f"{row.source_doc_id} p{row.source_page}"
            if col == SectionTableColumn.BADGES:
                return self._format_badges(row)

        if role == Qt.ItemDataRole.BackgroundRole:
            tint = confidence_color(row.confidence, row.status)
            if tint is not None:
                return QBrush(tint)
            return None

        if role == Qt.ItemDataRole.ToolTipRole:
            if col == SectionTableColumn.VALUE:
                return self._tooltip_for_value(row)
            if col == SectionTableColumn.SOURCE and row.source_doc_id:
                return f"Open {row.source_doc_id} at page {row.source_page}"
            if col == SectionTableColumn.BADGES and row.model_used == "claude-opus-4-7":
                return "Sourced from Claude Opus 4.7 (escalated)"
            if col == SectionTableColumn.LABEL and row.is_required:
                return "Required field"

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col == SectionTableColumn.CONFIDENCE:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        return None

    def setData(
        self,
        index: QModelIndex,
        value: Any,
        role: int = Qt.ItemDataRole.EditRole,
    ) -> bool:
        if role != Qt.ItemDataRole.EditRole:
            return False
        if not index.isValid() or index.column() != SectionTableColumn.VALUE:
            return False
        self.update_value(index.row(), value)
        return True

    # -- Click helpers (called from view) -----------------------------------

    def emit_click(self, index: QModelIndex) -> None:
        """Translate a click on row/col into the right signal.

        Called from :class:`SectionTableView` on left-click. Encapsulating it
        on the model keeps the column meanings in one place.
        """
        if not index.isValid():
            return
        row = self._rows[index.row()]
        col = index.column()
        if col == SectionTableColumn.SOURCE and row.source_doc_id and row.source_page:
            self.source_clicked.emit(row.source_doc_id, int(row.source_page))
        elif col == SectionTableColumn.BADGES and row.has_conflicts:
            self.conflict_clicked.emit(row.domain_tag)

    # -- Formatting helpers --------------------------------------------------

    @staticmethod
    def _format_label(row: FieldRow) -> str:
        suffix = " *" if row.is_required else ""
        return f"{row.label}{suffix}"

    @staticmethod
    def _format_value(row: FieldRow) -> str:
        if row.value is None or row.value == "":
            return ""
        if row.type_hint == "checkbox":
            return "Yes" if bool(row.value) else "No"
        return str(row.value)

    @staticmethod
    def _format_badges(row: FieldRow) -> str:
        glyphs: list[str] = []
        if row.model_used == "claude-opus-4-7":
            glyphs.append(OPUS_BADGE_GLYPH)
        if row.is_locked:
            glyphs.append(LOCK_GLYPH)
        if row.has_conflicts:
            glyphs.append(CONFLICT_GLYPH)
        return " ".join(glyphs)

    @staticmethod
    def _tooltip_for_value(row: FieldRow) -> str:
        bits = [
            f"domain_tag: {row.domain_tag}",
            f"confidence: {row.confidence:.2f}",
            f"status: {row.status}",
        ]
        if row.model_used:
            bits.append(f"model: {row.model_used}")
        if row.has_conflicts:
            bits.append("conflicts available — click the chevron")
        return "\n".join(bits)


# ---------------------------------------------------------------------------
# Delegate
# ---------------------------------------------------------------------------


class SectionDelegate(QStyledItemDelegate):
    """Per-row editor selection driven by ``FieldRow.type_hint``.

    Editor mapping:
        text     → ``QLineEdit``
        number   → ``QLineEdit`` (coercion happens in the model)
        select   → ``QComboBox`` populated from ``enum_values``
        date     → ``QDateEdit`` with calendar popup
        checkbox → ``QCheckBox``
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    def createEditor(
        self,
        parent: QWidget,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> QWidget:
        row = self._row_for_index(index)
        if row is None or row.is_locked:
            return super().createEditor(parent, option, index)

        if row.type_hint == "select":
            combo = QComboBox(parent)
            combo.setEditable(False)
            for value in row.enum_values:
                combo.addItem(str(value))
            return combo
        if row.type_hint == "date":
            editor = QDateEdit(parent)
            editor.setCalendarPopup(True)
            return editor
        if row.type_hint == "checkbox":
            return QCheckBox(parent)
        # text / number / fallback
        return QLineEdit(parent)

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:
        row = self._row_for_index(index)
        if row is None:
            return
        if isinstance(editor, QComboBox):
            text = "" if row.value is None else str(row.value)
            pos = editor.findText(text)
            if pos >= 0:
                editor.setCurrentIndex(pos)
            return
        if isinstance(editor, QDateEdit):
            from PySide6.QtCore import QDate

            text = "" if row.value is None else str(row.value)
            qd = QDate.fromString(text, "yyyy-MM-dd")
            if qd.isValid():
                editor.setDate(qd)
            return
        if isinstance(editor, QCheckBox):
            editor.setChecked(bool(row.value))
            return
        if isinstance(editor, QLineEdit):
            editor.setText("" if row.value is None else str(row.value))
            return
        super().setEditorData(editor, index)

    def setModelData(
        self,
        editor: QWidget,
        model: QAbstractTableModel,
        index: QModelIndex,
    ) -> None:
        if isinstance(editor, QComboBox):
            value: object = editor.currentText()
        elif isinstance(editor, QDateEdit):
            value = editor.date().toString("yyyy-MM-dd")
        elif isinstance(editor, QCheckBox):
            value = editor.isChecked()
        elif isinstance(editor, QLineEdit):
            value = editor.text()
        else:
            super().setModelData(editor, model, index)
            return
        model.setData(index, value, Qt.ItemDataRole.EditRole)

    @staticmethod
    def _row_for_index(index: QModelIndex) -> FieldRow | None:
        model = index.model()
        if isinstance(model, SectionTableModel):
            row_idx = index.row()
            rows = model.rows()
            if 0 <= row_idx < len(rows):
                return rows[row_idx]
        return None


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class SectionTableView(QTableView):
    """``QTableView`` pre-wired with :class:`SectionDelegate`.

    Connects clicked → ``model.emit_click`` so the model can fan out
    ``source_clicked`` / ``conflict_clicked`` signals from a single place.
    Also emits :attr:`focus_changed` when the current row changes — the host
    main window uses this to drive the PDF preview deep-link.
    """

    focus_changed = Signal(str)  # domain_tag of the newly focused row.

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(
            QTableView.EditTrigger.DoubleClicked | QTableView.EditTrigger.EditKeyPressed
        )
        self.setItemDelegate(SectionDelegate(self))
        self.clicked.connect(self._on_clicked)

    def setModel(self, model: QAbstractTableModel) -> None:  # type: ignore[override]
        super().setModel(model)
        sel_model = self.selectionModel()
        if sel_model is not None:
            sel_model.currentRowChanged.connect(self._on_current_row_changed)
        # Tighten column sizing.
        self.resizeColumnsToContents()

    def _on_clicked(self, index: QModelIndex) -> None:
        model = self.model()
        if isinstance(model, SectionTableModel):
            model.emit_click(index)

    def _on_current_row_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        model = self.model()
        if not isinstance(model, SectionTableModel):
            return
        rows = model.rows()
        if 0 <= current.row() < len(rows):
            self.focus_changed.emit(rows[current.row()].domain_tag)

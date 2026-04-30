"""repeatable_pane.py — list-on-left + form-on-right pattern for repeatable groups.

Per ARCHITECTURE §8.5 / PLAN-REVIEW: repeatable groups (vehicle, driver,
location, loss_payee, additional_insured, prior_carrier, loss) render as a
list of items on the left with a per-item form on the right. **Not** nested
grids.

The pane reuses :class:`SectionTableModel` for the right-hand form so the
confidence highlighting, Opus badge, conflict chevron, and source-click
behavior all behave identically to singleton-namespace tables.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..logger import get_logger
from .section_table import FieldRow, SectionTableModel, SectionTableView

__all__ = [
    "RepeatablePane",
    "default_item_label",
]


_logger = get_logger("gui.repeatable_pane")


def default_item_label(group: str, index: int, item: dict) -> str:
    """Produce a short, sensible label for an item in a repeatable group.

    Group-specific rules are documented in DECISION-MAP-state-agent.md when
    the state-agent settles natural keys (open item §14 #1). Until then we
    use a small set of common-sense rules and fall back to a generic
    ``"<Group> #N"``.
    """
    def get(tag: str) -> object:
        rec = item.get(tag) or {}
        return rec.get("value") if isinstance(rec, dict) else None

    if group == "vehicle":
        year = get("vehicle.year")
        make = get("vehicle.make")
        model = get("vehicle.model")
        bits = [str(b) for b in (year, make, model) if b]
        return " ".join(bits) or f"Vehicle #{index + 1}"
    if group == "driver":
        name = get("driver.name")
        return str(name) if name else f"Driver #{index + 1}"
    if group == "location":
        line1 = get("location.address.line1")
        return str(line1) if line1 else f"Location #{index + 1}"
    if group == "loss_payee":
        name = get("loss_payee.name")
        return str(name) if name else f"Additional Interest #{index + 1}"
    if group == "additional_insured":
        name = get("additional_insured.name")
        return str(name) if name else f"Additional Insured #{index + 1}"
    if group == "prior_carrier":
        name = get("prior_carrier.name")
        return str(name) if name else f"Prior Carrier #{index + 1}"
    if group == "loss":
        date = get("loss.date_of_loss")
        return f"Loss on {date}" if date else f"Loss #{index + 1}"
    return f"{group.title()} #{index + 1}"


class RepeatablePane(QWidget):
    """List + form pane for one ``state.repeatables[<group>]`` array.

    Signals:
        source_clicked(doc_id, page) — re-emitted from the inner table.
        conflict_clicked(domain_tag) — re-emitted from the inner table.
        item_added() / item_deleted(index) — operator pressed Add / Delete.
        commit_requested(group, index, domain_tag, value) — operator edited
            a field; host should persist via ``state.update_field``.
    """

    source_clicked = Signal(str, int)
    conflict_clicked = Signal(str)
    item_added = Signal()
    item_deleted = Signal(int)
    commit_requested = Signal(str, int, str, object)
    focus_changed = Signal(str, int, str)  # group, item_index, domain_tag

    def __init__(
        self,
        group: str,
        items: list[dict],
        *,
        row_builder: Callable[[dict], list[FieldRow]] | None = None,
        label_builder: Callable[[str, int, dict], str] = default_item_label,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._group = group
        self._items: list[dict] = list(items)
        self._row_builder = row_builder or self._fallback_row_builder
        self._label_builder = label_builder

        self._build_ui()
        self.refresh()

    # -- Public surface ------------------------------------------------------

    @property
    def group(self) -> str:
        return self._group

    def set_items(self, items: list[dict]) -> None:
        """Replace the items in one shot (used after extraction merges)."""
        self._items = list(items)
        self.refresh()

    def items(self) -> list[dict]:
        return list(self._items)

    def current_index(self) -> int:
        return self._list_widget.currentRow()

    # -- Layout --------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        splitter = QSplitter(self)
        splitter.setOrientation(Qt.Orientation.Horizontal)

        # Left side: list + add/delete buttons.
        left = QWidget(splitter)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)

        self._list_widget = QListWidget(left)
        self._list_widget.currentRowChanged.connect(self._on_row_changed)
        left_layout.addWidget(self._list_widget, 1)

        button_row = QHBoxLayout()
        add_btn = QPushButton("Add", left)
        del_btn = QPushButton("Delete", left)
        add_btn.clicked.connect(self._on_add)
        del_btn.clicked.connect(self._on_delete)
        button_row.addWidget(add_btn)
        button_row.addWidget(del_btn)
        button_row.addStretch(1)
        left_layout.addLayout(button_row)

        # Right side: form via SectionTableView.
        self._table_model = SectionTableModel(commit_callback=self._on_commit)
        self._table_view = SectionTableView()
        self._table_view.setModel(self._table_model)
        self._table_model.source_clicked.connect(self.source_clicked)
        self._table_model.conflict_clicked.connect(self.conflict_clicked)
        self._table_view.focus_changed.connect(self._on_field_focus_changed)

        splitter.addWidget(left)
        splitter.addWidget(self._table_view)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)

        outer.addWidget(splitter)

    # -- Refresh logic -------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the list widget + form from ``self._items``."""
        # Save the operator's current selection so a refresh after an edit
        # doesn't yank them back to row 0 mid-thought. blockSignals avoids
        # firing currentRowChanged for every item we re-add.
        prior_index = self._list_widget.currentRow()
        self._list_widget.blockSignals(True)
        self._list_widget.clear()
        for i, item in enumerate(self._items):
            self._list_widget.addItem(QListWidgetItem(self._label_builder(self._group, i, item)))
        # Clamp the restored index in case items were deleted.
        if self._items:
            new_index = max(0, min(prior_index, len(self._items) - 1))
            self._list_widget.setCurrentRow(new_index)
        self._list_widget.blockSignals(False)
        self._render_form()

    def _render_form(self) -> None:
        idx = self._list_widget.currentRow()
        if idx < 0 or idx >= len(self._items):
            self._table_model.set_rows([])
            return
        rows = self._row_builder(self._items[idx])
        self._table_model.set_rows(rows)

    @staticmethod
    def _fallback_row_builder(item: dict) -> list[FieldRow]:
        rows: list[FieldRow] = []
        for tag, record in sorted(item.items()):
            if not isinstance(record, dict):
                continue
            label = tag.split(".", 1)[1] if "." in tag else tag
            rows.append(
                FieldRow.from_field_record(
                    domain_tag=tag,
                    label=label.replace("_", " ").title(),
                    record=record,
                )
            )
        return rows

    # -- Event handlers ------------------------------------------------------

    def _on_row_changed(self, _row: int) -> None:
        self._render_form()

    def _on_add(self) -> None:
        self.item_added.emit()

    def _on_delete(self) -> None:
        idx = self._list_widget.currentRow()
        if idx < 0:
            return
        self.item_deleted.emit(idx)

    def _on_commit(self, domain_tag: str, value: object) -> None:
        idx = self._list_widget.currentRow()
        if idx < 0:
            return
        self.commit_requested.emit(self._group, idx, domain_tag, value)

    def _on_field_focus_changed(self, domain_tag: str) -> None:
        idx = self._list_widget.currentRow()
        if idx < 0:
            return
        self.focus_changed.emit(self._group, idx, domain_tag)

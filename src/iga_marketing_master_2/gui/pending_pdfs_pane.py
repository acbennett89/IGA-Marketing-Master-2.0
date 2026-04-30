"""pending_pdfs_pane.py — visible queue of PDFs awaiting extraction.

Operator UX (issue gui-fix-2 #1): drag-and-drop and the "Add PDFs..."
browse button populate this queue rather than auto-extracting. The
operator can review, remove items, and click "Extract" on the run
controls bar to actually launch :func:`extract.run_extraction`.

The pane is a thin :class:`QListWidget` subclass that owns its visible
state. ``MainWindow`` keeps a parallel ``list[Path]`` so it can pass the
queue to the extraction worker; we expose :meth:`paths` and the
``paths_changed`` signal for that integration.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QListWidget,
    QListWidgetItem,
    QMenu,
    QWidget,
)

__all__ = ["PendingPdfsPane"]


class PendingPdfsPane(QListWidget):
    """Visible queue of PDFs pending extraction.

    Signals:
        paths_changed(): emitted whenever the queue contents change
            (add / remove / clear). Not parameterized — the host is
            expected to read :meth:`paths` to refresh dependent UI.
    """

    paths_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context_menu)
        self.setUniformItemSizes(True)
        self.setAlternatingRowColors(True)
        self.setToolTip(
            "PDFs queued for extraction. Drop more files here or click 'Add PDFs...' "
            "to add. Right-click an item or press Delete to remove it. Click 'Extract' "
            "on the run-controls bar to start."
        )
        # Compact height so the pane doesn't dominate the window when small.
        self.setMaximumHeight(120)

        # Keyboard: Delete / Backspace removes selected items.
        self._delete_action = QAction("Remove from queue", self)
        self._delete_action.setShortcut(QKeySequence(Qt.Key.Key_Delete))
        self._delete_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self._delete_action.triggered.connect(self._remove_selected)
        self.addAction(self._delete_action)

        self._backspace_action = QAction("Remove from queue (backspace)", self)
        self._backspace_action.setShortcut(QKeySequence(Qt.Key.Key_Backspace))
        self._backspace_action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
        self._backspace_action.triggered.connect(self._remove_selected)
        self.addAction(self._backspace_action)

    # -- Public API --------------------------------------------------------

    def paths(self) -> list[Path]:
        """Return the current queue contents in display order."""
        out: list[Path] = []
        for i in range(self.count()):
            item = self.item(i)
            if item is None:
                continue
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, Path):
                out.append(data)
        return out

    def set_paths(self, paths: Iterable[Path]) -> None:
        """Replace queue contents with ``paths``. Emits ``paths_changed``."""
        self.blockSignals(True)
        try:
            self.clear()
            for p in paths:
                self._append_item(Path(p))
        finally:
            self.blockSignals(False)
        self.paths_changed.emit()

    def add_paths(self, paths: Iterable[Path]) -> int:
        """Append ``paths`` (deduping against existing entries by resolved path).

        Returns the count actually appended. Emits ``paths_changed`` when at
        least one new path is added.
        """
        existing: set[str] = {str(self._resolve(p)) for p in self.paths()}
        added = 0
        for p in paths:
            path = Path(p)
            key = str(self._resolve(path))
            if key in existing:
                continue
            existing.add(key)
            self._append_item(path)
            added += 1
        if added:
            self.paths_changed.emit()
        return added

    def clear_queue(self) -> None:
        """Empty the queue. Emits ``paths_changed`` if the queue was non-empty."""
        was_populated = self.count() > 0
        self.clear()
        if was_populated:
            self.paths_changed.emit()

    # -- Internal ----------------------------------------------------------

    def _append_item(self, path: Path) -> None:
        item = QListWidgetItem(path.name, self)
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(str(path))

    @staticmethod
    def _resolve(p: Path) -> Path:
        try:
            return p.resolve()
        except OSError:
            return p

    def _on_context_menu(self, pos) -> None:  # noqa: ANN001 (Qt-style point)
        menu = QMenu(self)
        remove_action = menu.addAction("Remove from queue")
        clear_action = menu.addAction("Clear queue")
        # Disable Remove if nothing is selected at the click point.
        clicked_item = self.itemAt(pos)
        if clicked_item is None and not self.selectedItems():
            remove_action.setEnabled(False)
        if self.count() == 0:
            clear_action.setEnabled(False)
        chosen = menu.exec(self.viewport().mapToGlobal(pos))
        if chosen is remove_action:
            # If the user right-clicked an unselected item, treat that one as the target.
            if clicked_item is not None and not clicked_item.isSelected():
                self._remove_item(clicked_item)
            else:
                self._remove_selected()
        elif chosen is clear_action:
            self.clear_queue()

    def _remove_selected(self) -> None:
        items = self.selectedItems()
        if not items:
            return
        for item in items:
            self._remove_item(item, emit=False)
        self.paths_changed.emit()

    def _remove_item(self, item: QListWidgetItem, *, emit: bool = True) -> None:
        row = self.row(item)
        if row >= 0:
            self.takeItem(row)
        if emit:
            self.paths_changed.emit()

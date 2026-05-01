"""find_bar.py — drop-down quick field search for MainWindow.

Hidden by default; toggled via Ctrl+F (Edit → Find Field). Emits
``query_changed(str)`` whenever the user edits the text and ``closed()``
when dismissed (X button or Esc on the line edit). The host applies the
substring filter against ``domain_tag`` across all section tables.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QWidget,
)

__all__ = ["FindBar"]


class _FindLineEdit(QLineEdit):
    """``QLineEdit`` that emits ``escape_pressed`` on the Esc key."""

    escape_pressed = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 (Qt-style)
        if event.key() == Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            return
        super().keyPressEvent(event)


class FindBar(QWidget):
    """Inline search bar.

    Signals:
        query_changed(str) — emitted whenever the search text changes.
        closed() — emitted when the bar is dismissed (X button or Esc).
    """

    query_changed = Signal(str)
    closed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self.setVisible(False)

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        label = QLabel("Find field:", self)
        layout.addWidget(label)

        self._line_edit = _FindLineEdit(self)
        self._line_edit.setPlaceholderText(
            "Type a domain_tag fragment (e.g. 'gl', 'effective_date')..."
        )
        self._line_edit.textChanged.connect(self._on_text_changed)
        self._line_edit.escape_pressed.connect(self._on_escape)
        layout.addWidget(self._line_edit, 1)

        self._close_btn = QPushButton("X", self)
        self._close_btn.setToolTip("Close search (Esc)")
        self._close_btn.setFixedWidth(28)
        self._close_btn.clicked.connect(self._on_escape)
        layout.addWidget(self._close_btn)

    # -- Public surface ----------------------------------------------------

    def query(self) -> str:
        return self._line_edit.text()

    def open(self) -> None:
        """Reveal and focus the search bar."""
        self.setVisible(True)
        self._line_edit.setFocus()
        self._line_edit.selectAll()

    def close(self) -> None:  # type: ignore[override]
        """Hide and clear the search bar, then notify."""
        self.setVisible(False)
        if self._line_edit.text():
            self._line_edit.clear()  # also fires textChanged → query_changed("")
        self.closed.emit()

    def is_open(self) -> bool:
        return self.isVisible()

    # -- Internal ----------------------------------------------------------

    def _on_text_changed(self, text: str) -> None:
        self.query_changed.emit(text)

    def _on_escape(self) -> None:
        self.close()


def matches_query(domain_tag: str, query: str) -> bool:
    """Pure helper: does ``domain_tag`` match ``query`` (case-insensitive substring)?

    Empty query matches everything (filter inactive). Exposed at module
    scope so unit tests can exercise the matching logic without a Qt
    instance.
    """
    if not query:
        return True
    return query.strip().lower() in domain_tag.lower()

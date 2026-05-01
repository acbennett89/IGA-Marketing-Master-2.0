"""welcome_pane.py — empty-state landing widget for MainWindow.

Shown as a single tab when no client is loaded. Hidden the moment a client
is loaded (the host clears the tab widget and rebuilds against state).
The pane contains a friendly headline plus actionable next-step copy.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

__all__ = ["WelcomePane"]


_WELCOME_HTML: str = (
    "<div style='text-align:center;'>"
    "<h1 style='margin-bottom:18px;'>Welcome to IGA Marketing Master 2.0</h1>"
    "<p style='font-size:14pt; color:#444; margin:8px;'>"
    "To begin, <b>pick a client</b> "
    "(<span style='font-family:monospace;'>Ctrl+O</span>) "
    "or <b>drop PDFs onto this window</b> to start a new one."
    "</p>"
    "<p style='font-size:12pt; color:#666; margin-top:24px;'>"
    "Once a client is open, drop PDFs into the queue at the bottom and "
    "click <b>Extract</b> to pull insurance data into a structured review."
    "</p>"
    "</div>"
)


class WelcomePane(QWidget):
    """A simple centered welcome label.

    The host treats this as an opaque widget; it is never modified after
    construction.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 48, 24, 48)
        layout.setSpacing(0)
        layout.addStretch(1)

        label = QLabel(_WELCOME_HTML, self)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setStyleSheet("QLabel { color: #222; }")
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignHCenter)

        layout.addStretch(2)

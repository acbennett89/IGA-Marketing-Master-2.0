"""entry_busy_dialog.py — modal "Entering..." popup for the entry walker.

Mirrors ``_ExtractionBusyDialog`` (working.gif + status label) but adds:

* A Cancel button that hard-aborts the current run by setting the runtime
  ``cancel_event``. The dialog stays up until the worker thread emits
  finished/failed, at which point the host closes it.
* A live status text label the worker updates via ``set_status_text``
  (the host wires this to ``worker.progress``).

The dialog is non-cancellable from Esc / window-X because Qt's default
``reject`` is overridden to a no-op. Cancellation goes through the explicit
button so the host can clearly distinguish "user requested cancel" from
"system tried to dismiss".
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QMovie
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


def _busy_gif_path() -> Path:
    """Resolve ``assets/working.gif`` relative to this module.

    Path: gui/entry_busy_dialog.py → gui → iga_marketing_master_2 → src → repo root.
    """
    here = Path(__file__).resolve()
    return here.parent.parent.parent.parent / "assets" / "working.gif"


class EntryBusyDialog(QDialog):
    """Modal "Entering..." popup with cancel + live status.

    Signals:
      ``cancel_requested`` — user clicked Cancel. The host should set its
      runtime ``cancel_event`` and disable further interactions; the dialog
      itself stays open until the host closes it after worker termination.
    """

    cancel_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Entering")
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self.setFixedSize(360, 340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(12)

        # GIF
        self._movie: QMovie | None = None
        gif_label = QLabel()
        gif_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gif_path = _busy_gif_path()
        if gif_path.exists():
            self._movie = QMovie(str(gif_path))
            self._movie.setScaledSize(QSize(240, 180))
            gif_label.setMovie(self._movie)
            self._movie.start()
        else:
            gif_label.setText("⏳")
            gif_label.setStyleSheet("font-size: 48px;")
        layout.addWidget(gif_label)

        # Status line (updated by the worker via set_status_text)
        self._status_label = QLabel("Entering...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setWordWrap(True)
        self._status_label.setStyleSheet(
            "font-size: 13px; font-weight: 600; color: #0f172a;"
        )
        layout.addWidget(self._status_label)

        # Cancel button
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setMinimumWidth(120)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def set_status_text(self, text: str) -> None:
        """Update the small status line under the GIF."""
        self._status_label.setText(text)

    def disable_cancel(self) -> None:
        """Disable the Cancel button (e.g. once cancellation is already requested)."""
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.setText("Cancelling...")

    def _on_cancel_clicked(self) -> None:
        # First click sets the cancel flag in the runtime via the host slot.
        # Disable the button to prevent double-clicks; host closes the dialog
        # when the worker emits finished/failed.
        self.disable_cancel()
        self.cancel_requested.emit()

    def closeEvent(self, event) -> None:
        if self._movie is not None:
            self._movie.stop()
        super().closeEvent(event)

    def reject(self) -> None:
        # Block Esc / window-X. Cancellation must go through the button.
        pass

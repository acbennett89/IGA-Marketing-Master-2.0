"""run_controls.py — bottom toolbar widgets.

Per ARCHITECTURE §8.2 the run controls expose:

- Extract — runs extraction on the queued PDFs. Gated on a non-empty
  pending-PDFs queue and no in-flight worker. (Added per gui-fix-2 #1
  so drag-and-drop / Add-PDFs queue rather than auto-extracting.)
- Begin Entry — gated on at least one approved field.
- Force Opus toggle — escalates Sonnet calls to Opus on the next extraction.
- Cancel / Abort.
- Resume — visible only while an entry session is paused.

The bar is a plain widget; it doesn't own the worker thread or the pause
state. The host wires button signals to its own slots.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QWidget,
)

__all__ = ["RunControlsBar"]


class RunControlsBar(QWidget):
    """Run-control toolbar.

    Signals:
        extract_clicked()
        begin_entry_clicked()
        cancel_clicked()
        resume_clicked()
        force_opus_toggled(bool)
    """

    extract_clicked = Signal()
    begin_entry_clicked = Signal()
    cancel_clicked = Signal()
    resume_clicked = Signal()
    force_opus_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self._approved_count: int = 0
        self._pending_pdf_count: int = 0
        self._is_entering: bool = False
        self._is_extracting: bool = False
        self._is_paused: bool = False
        self._refresh_button_states()

    def _build_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(8)

        self._status_label = QLabel("No client loaded.", self)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._status_label, 1)

        self._force_opus_check = QCheckBox("Force Opus", self)
        self._force_opus_check.setToolTip(
            "Escalate the next extraction call to Claude Opus 4.7 for higher accuracy."
        )
        self._force_opus_check.toggled.connect(self.force_opus_toggled)
        self._force_opus_check.setVisible(False)
        layout.addWidget(self._force_opus_check)

        self._cancel_btn = QPushButton("Cancel", self)
        self._cancel_btn.setObjectName("HeaderBtnSecondary")
        self._cancel_btn.clicked.connect(self.cancel_clicked)
        layout.addWidget(self._cancel_btn)

        self._resume_btn = QPushButton("Resume", self)
        self._resume_btn.setObjectName("HeaderBtnPrimary")
        self._resume_btn.clicked.connect(self.resume_clicked)
        layout.addWidget(self._resume_btn)

    # -- Public surface ----------------------------------------------------

    def set_status_text(self, text: str) -> None:
        self._status_label.setText(text)

    def set_approved_count(self, count: int) -> None:
        """Update the gating count for the Begin Entry button."""
        self._approved_count = max(0, int(count))
        self._refresh_button_states()

    def set_pending_pdf_count(self, count: int) -> None:
        """Update the gating count for the Extract button."""
        self._pending_pdf_count = max(0, int(count))
        self._refresh_button_states()

    def set_entering(self, is_entering: bool) -> None:
        self._is_entering = bool(is_entering)
        self._refresh_button_states()

    def set_extracting(self, is_extracting: bool) -> None:
        self._is_extracting = bool(is_extracting)
        self._refresh_button_states()

    def set_paused(self, is_paused: bool) -> None:
        self._is_paused = bool(is_paused)
        self._refresh_button_states()

    def is_force_opus(self) -> bool:
        return self._force_opus_check.isChecked()

    # -- Internal ----------------------------------------------------------

    def _refresh_button_states(self) -> None:
        any_run_active = self._is_entering or self._is_extracting or self._is_paused

        self._cancel_btn.setEnabled(any_run_active)

        self._resume_btn.setVisible(self._is_paused)
        self._resume_btn.setEnabled(self._is_paused)

"""run_controls.py — bottom toolbar widgets.

Per ARCHITECTURE §8.2 the run controls expose:

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
        begin_entry_clicked()
        cancel_clicked()
        resume_clicked()
        force_opus_toggled(bool)
    """

    begin_entry_clicked = Signal()
    cancel_clicked = Signal()
    resume_clicked = Signal()
    force_opus_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._build_ui()
        self._approved_count: int = 0
        self._is_entering: bool = False
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
        layout.addWidget(self._force_opus_check)

        self._begin_btn = QPushButton("Begin Entry", self)
        self._begin_btn.setStyleSheet("QPushButton { font-weight: bold; padding: 6px 14px; }")
        self._begin_btn.clicked.connect(self.begin_entry_clicked)
        layout.addWidget(self._begin_btn)

        self._cancel_btn = QPushButton("Cancel", self)
        self._cancel_btn.clicked.connect(self.cancel_clicked)
        layout.addWidget(self._cancel_btn)

        self._resume_btn = QPushButton("Resume", self)
        self._resume_btn.setStyleSheet("QPushButton { font-weight: bold; padding: 6px 14px; }")
        self._resume_btn.clicked.connect(self.resume_clicked)
        layout.addWidget(self._resume_btn)

    # -- Public surface ----------------------------------------------------

    def set_status_text(self, text: str) -> None:
        self._status_label.setText(text)

    def set_approved_count(self, count: int) -> None:
        """Update the gating count for the Begin Entry button."""
        self._approved_count = max(0, int(count))
        self._refresh_button_states()

    def set_entering(self, is_entering: bool) -> None:
        self._is_entering = bool(is_entering)
        self._refresh_button_states()

    def set_paused(self, is_paused: bool) -> None:
        self._is_paused = bool(is_paused)
        self._refresh_button_states()

    def is_force_opus(self) -> bool:
        return self._force_opus_check.isChecked()

    # -- Internal ----------------------------------------------------------

    def _refresh_button_states(self) -> None:
        # Centralized button-state logic. Every state-change setter calls
        # this so we never end up with, say, Begin Entry enabled while a
        # run is already in flight.
        can_begin = (
            not self._is_entering
            and not self._is_paused
            and self._approved_count > 0
        )
        self._begin_btn.setEnabled(can_begin)
        if self._approved_count == 0:
            self._begin_btn.setToolTip(
                "Approve at least one field before starting an entry run."
            )
        else:
            self._begin_btn.setToolTip(
                f"Start entering {self._approved_count} approved fields into EPIC."
            )

        # Cancel only meaningful while a run is in flight.
        self._cancel_btn.setEnabled(self._is_entering or self._is_paused)

        # Resume only visible while paused.
        self._resume_btn.setVisible(self._is_paused)
        self._resume_btn.setEnabled(self._is_paused)

        # Force-Opus toggle is disabled mid-run (no effect on an in-flight call).
        self._force_opus_check.setEnabled(not self._is_entering)

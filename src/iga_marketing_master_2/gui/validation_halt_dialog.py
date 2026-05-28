"""validation_halt_dialog.py — Proceed/Cancel modal shown when EPIC errors.

Triggered by ``epic_steps.validation_check.check_and_halt``. Displays:

* The exact error text EPIC produced.
* An "Expecting…" line describing what the script was about to do.
* The screen code (EPIC footer) and finding sequence.
* A thumbnail of the captured screenshot.

Buttons:

* **Proceed** — the operator has fixed the issue in EPIC manually (e.g.
  dismissed the modal, filled the required field) and wants the run to
  continue. Returns ``"proceed"``.
* **Cancel** — abort the run. Returns ``"cancel"``.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


ValidationHaltChoice = Literal["proceed", "cancel"]


class ValidationHaltDialog(QDialog):
    """Halt-for-human modal on validation error."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        sequence: int,
        error_text: str,
        expecting: str,
        screen_code: str,
        screenshot_path: Path | None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Validation Error #{sequence}")
        self.setModal(True)
        self.setMinimumSize(620, 520)
        self._choice: ValidationHaltChoice = "cancel"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        # Header
        title = QLabel(f"EPIC reported a validation error")
        title.setStyleSheet("font-size: 16px; font-weight: 700; color: #b91c1c;")
        layout.addWidget(title)

        if screen_code:
            screen_lbl = QLabel(f"Screen: <b>{screen_code}</b>  ·  Finding #{sequence}")
        else:
            screen_lbl = QLabel(f"Finding #{sequence}")
        screen_lbl.setTextFormat(Qt.TextFormat.RichText)
        screen_lbl.setStyleSheet("color: #475569; font-size: 12px;")
        layout.addWidget(screen_lbl)

        # Error text (read-only)
        err_lbl = QLabel("Error from EPIC:")
        err_lbl.setStyleSheet("font-weight: 600; margin-top: 6px;")
        layout.addWidget(err_lbl)
        err_box = QPlainTextEdit()
        err_box.setReadOnly(True)
        err_box.setPlainText(error_text)
        err_box.setMaximumHeight(80)
        err_box.setStyleSheet(
            "background: #fef2f2; color: #7f1d1d; "
            "border: 1px solid #fecaca; border-radius: 4px; padding: 6px;"
        )
        layout.addWidget(err_box)

        # Expecting line
        exp_lbl = QLabel("Script was about to:")
        exp_lbl.setStyleSheet("font-weight: 600; margin-top: 4px;")
        layout.addWidget(exp_lbl)
        exp_text = QPlainTextEdit()
        exp_text.setReadOnly(True)
        exp_text.setPlainText(expecting or "(no context provided)")
        exp_text.setMaximumHeight(60)
        exp_text.setStyleSheet(
            "background: #f1f5f9; color: #0f172a; "
            "border: 1px solid #cbd5e1; border-radius: 4px; padding: 6px;"
        )
        layout.addWidget(exp_text)

        # Screenshot thumbnail
        if screenshot_path is not None and screenshot_path.exists():
            shot_lbl = QLabel("Screenshot:")
            shot_lbl.setStyleSheet("font-weight: 600; margin-top: 4px;")
            layout.addWidget(shot_lbl)
            pix = QPixmap(str(screenshot_path))
            if not pix.isNull():
                scaled = pix.scaledToWidth(
                    560, Qt.TransformationMode.SmoothTransformation
                )
                # Hard-cap height so very tall captures don't push buttons off
                if scaled.height() > 220:
                    scaled = pix.scaledToHeight(
                        220, Qt.TransformationMode.SmoothTransformation
                    )
                img_lbl = QLabel()
                img_lbl.setPixmap(scaled)
                img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
                img_lbl.setStyleSheet("border: 1px solid #cbd5e1; padding: 2px;")
                img_lbl.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                layout.addWidget(img_lbl)

                path_lbl = QLabel(f"Saved: {screenshot_path}")
                path_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
                layout.addWidget(path_lbl)

        # Action hint
        hint = QLabel(
            "Fix the issue manually in EPIC (dismiss the modal, fill the missing "
            "field, etc.) and click Proceed to continue. Click Cancel to abort the run."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #334155; font-size: 12px; margin-top: 6px;")
        layout.addWidget(hint)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel Run")
        cancel_btn.setMinimumWidth(120)
        cancel_btn.clicked.connect(self._on_cancel)
        proceed_btn = QPushButton("Proceed")
        proceed_btn.setMinimumWidth(120)
        proceed_btn.setDefault(True)
        proceed_btn.setStyleSheet(
            "QPushButton { background: #16a34a; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: 600; }"
            "QPushButton:hover { background: #15803d; }"
        )
        proceed_btn.clicked.connect(self._on_proceed)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(proceed_btn)
        layout.addLayout(btn_row)

    def choice(self) -> ValidationHaltChoice:
        return self._choice

    def _on_proceed(self) -> None:
        self._choice = "proceed"
        self.accept()

    def _on_cancel(self) -> None:
        self._choice = "cancel"
        self.reject()

    def reject(self) -> None:
        # Esc / X = Cancel (treat as explicit abort, not just dismiss)
        self._choice = "cancel"
        super().reject()

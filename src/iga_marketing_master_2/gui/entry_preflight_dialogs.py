"""entry_preflight_dialogs.py — Begin Entry pre-flight prompts.

Two small Qt dialogs the entry flow uses after
``step_navigate_to_entry_start.run`` returns:

* :class:`MMSOpenConfirmDialog` — fires when EPIC is showing an MMS detail
  view. Asks the operator to confirm "Entering policy information on this
  MMS — Continue / Cancel".
* :class:`OpenMMSPromptDialog` — fires when EPIC is on Marketed Policies
  with no MMS open. Asks the operator to open the desired MMS manually,
  then click Continue. Re-probes EPIC each time Continue is clicked; only
  proceeds when an MMS is actually open. Cancel aborts the run.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class MMSOpenConfirmDialog(QDialog):
    """Confirm we're about to enter policy info into the currently-open MMS."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        account_name: str,
        lookup_code: str,
        screen_code: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Confirm MMS")
        self.setModal(True)
        self.setMinimumWidth(500)
        self._choice = "cancel"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        title = QLabel("Entering policy information on this MMS")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #0f172a;")
        layout.addWidget(title)

        detail_lines = [
            f"<b>Account:</b> {account_name or '(unknown)'}",
            f"<b>Lookup code:</b> {lookup_code or '(unknown)'}",
        ]
        if screen_code:
            detail_lines.append(f"<b>Screen:</b> {screen_code}")
        detail = QLabel("<br>".join(detail_lines))
        detail.setTextFormat(Qt.TextFormat.RichText)
        detail.setStyleSheet("color: #334155; font-size: 13px;")
        layout.addWidget(detail)

        hint = QLabel(
            "Click Continue to start entering policy information into this MMS. "
            "Cancel will abort the run."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #475569; font-size: 12px; margin-top: 6px;")
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumWidth(110)
        cancel_btn.clicked.connect(self._on_cancel)
        proceed_btn = QPushButton("Continue")
        proceed_btn.setMinimumWidth(110)
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

    def choice(self) -> str:
        return self._choice

    def _on_proceed(self) -> None:
        self._choice = "proceed"
        self.accept()

    def _on_cancel(self) -> None:
        self._choice = "cancel"
        self.reject()

    def reject(self) -> None:
        self._choice = "cancel"
        super().reject()


class OpenMMSPromptDialog(QDialog):
    """Prompt operator to open the desired MMS manually, then click Continue.

    On Continue this dialog just returns ``"proceed"``; the caller (worker
    thread) is responsible for re-probing EPIC and re-showing the dialog if
    the operator clicked Continue without actually opening an MMS. We can't
    re-probe from inside this dialog because Playwright's sync API is
    thread-bound to the worker thread that started it.

    ``hint_message`` is shown above the buttons and updates on retries —
    e.g. "Still on Marketed Policies — please open the MMS first."
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        account_name: str,
        lookup_code: str,
        hint_message: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open the MMS to enter")
        self.setModal(True)
        self.setMinimumWidth(540)
        self._choice = "cancel"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        title = QLabel("Open the MMS to enter into")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #0f172a;")
        layout.addWidget(title)

        detail = QLabel(
            f"<b>Account:</b> {account_name or '(unknown)'}<br>"
            f"<b>Lookup code:</b> {lookup_code or '(unknown)'}"
        )
        detail.setTextFormat(Qt.TextFormat.RichText)
        detail.setStyleSheet("color: #334155; font-size: 13px;")
        layout.addWidget(detail)

        hint = QLabel(
            "EPIC is on Marketed Policies. Double-click the MMS you want to "
            "enter into, then click Continue.\n\n"
            "If you'd rather start fresh, cancel this dialog and re-run "
            "Begin Entry with “Setup Marketing Submission” checked."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #475569; font-size: 12px; margin-top: 6px;")
        layout.addWidget(hint)

        if hint_message:
            status = QLabel(hint_message)
            status.setWordWrap(True)
            status.setStyleSheet(
                "color: #b45309; font-size: 12px; font-weight: 600; "
                "background: #fef3c7; border: 1px solid #fde68a; "
                "border-radius: 4px; padding: 6px;"
            )
            layout.addWidget(status)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumWidth(110)
        cancel_btn.clicked.connect(self._on_cancel)
        continue_btn = QPushButton("Continue")
        continue_btn.setMinimumWidth(110)
        continue_btn.setDefault(True)
        continue_btn.setStyleSheet(
            "QPushButton { background: #16a34a; color: white; padding: 6px 14px; "
            "border-radius: 4px; font-weight: 600; }"
            "QPushButton:hover { background: #15803d; }"
        )
        continue_btn.clicked.connect(self._on_continue)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(continue_btn)
        layout.addLayout(btn_row)

    def choice(self) -> str:
        return self._choice

    def _on_continue(self) -> None:
        self._choice = "proceed"
        self.accept()

    def _on_cancel(self) -> None:
        self._choice = "cancel"
        self.reject()

    def reject(self) -> None:
        self._choice = "cancel"
        super().reject()

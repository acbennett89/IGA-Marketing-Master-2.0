"""sidebar.py — Dark navigation sidebar for IGA Marketing Master 2.0."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def _brand_logo_path() -> Path:
    # src/iga_marketing_master_2/gui/sidebar.py → repo root /assets/...
    return Path(__file__).resolve().parents[3] / "assets" / "IGA_Icon_Orange2x.png"

__all__ = ["SidebarNav"]


_NAV_ITEMS: list[tuple[str, str]] = [
    ("client",      "Client"),
    # QPushButton treats a single ``&`` as a mnemonic accelerator prefix
    # and strips it from the visible label (so "Data Extraction & Review"
    # rendered as "Data Extraction  Review"). Doubling it escapes the
    # mnemonic so the ampersand actually shows on screen.
    ("data_review", "Data Extraction && Review"),
    ("settings",    "Settings"),
    ("help",        "Help"),
]

_QSS = """
SidebarNav {
    background-color: #1e293b;
}
/* Ensure ALL child widgets stay dark — prevents main-window QSS cascade. */
SidebarNav QWidget {
    background-color: #1e293b;
}
SidebarNav QFrame {
    background-color: #1e293b;
}
QWidget#SidebarBrand {
    background-color: #1e293b;
}
QLabel#BrandBadge {
    background: transparent;
    padding: 0px;
}
QLabel#BrandBadgeFallback {
    color: #ffffff;
    font-size: 15px;
    font-weight: bold;
    background: #c2570b;
    border-radius: 6px;
    padding: 5px 10px;
}
QLabel#BrandName {
    color: #f1f5f9;
    font-size: 13px;
    font-weight: bold;
}
QLabel#BrandSub {
    color: #94a3b8;
    font-size: 10px;
}
QFrame#SidebarDivider {
    background: #334155;
    min-height: 1px;
    max-height: 1px;
    border: none;
}
QPushButton#NavBtn {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: #94a3b8;
    font-size: 13px;
    padding: 9px 14px;
    text-align: left;
}
QPushButton#NavBtn:hover {
    background: #334155;
    color: #f1f5f9;
}
QPushButton#NavBtn[active="true"] {
    background: #2563eb;
    color: #ffffff;
}
QPushButton#NavBtn:disabled {
    color: #475569;
    background: transparent;
}
QPushButton#NavBtn:disabled:hover {
    background: transparent;
    color: #475569;
}
QLabel#SidebarSectionLabel {
    color: #64748b;
    font-size: 10px;
    font-weight: bold;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 4px 14px 4px;
}
QPushButton#SidebarActionBtn {
    background: #334155;
    border: 1px solid #475569;
    border-radius: 6px;
    color: #f1f5f9;
    font-size: 12px;
    padding: 8px 12px;
    text-align: left;
}
QPushButton#SidebarActionBtn:hover {
    background: #475569;
    border-color: #64748b;
}
QPushButton#SidebarActionBtn:pressed {
    background: #1e293b;
}
QPushButton#CloseAppBtn {
    background: transparent;
    border: none;
    border-radius: 6px;
    color: #f87171;
    font-size: 13px;
    padding: 9px 14px;
    text-align: left;
}
QPushButton#CloseAppBtn:hover {
    background: #334155;
    color: #fca5a5;
    border: none;
}
QPushButton#CloseAppBtn:pressed {
    background: transparent;
    border: none;
}
QPushButton#CloseAppBtn:focus {
    background: transparent;
    border: none;
}
QLabel#EngineLabel {
    color: #4ade80;
    font-size: 11px;
    padding: 2px 16px 0px;
}
QLabel#EngineStatus {
    color: #94a3b8;
    font-size: 11px;
    padding: 0px 16px 14px;
}
"""


class SidebarNav(QWidget):
    """Left navigation sidebar.

    Signals:
        page_changed(str): emitted when a nav item is clicked.
            Values: ``"client"``, ``"data_review"``, ``"history"``, ``"settings"``, ``"help"``.
    """

    page_changed = Signal(str)
    launch_browser_clicked = Signal()
    close_app_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidebarNav")
        self.setFixedWidth(196)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(_QSS)

        self._nav_buttons: dict[str, QPushButton] = {}
        self._active: str = "client"

        self._build_ui()

    # -- Build UI ----------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Brand block
        brand = QWidget()
        brand.setObjectName("SidebarBrand")
        b_layout = QVBoxLayout(brand)
        b_layout.setContentsMargins(16, 22, 16, 18)
        b_layout.setSpacing(3)

        badge = QLabel()
        logo_path = _brand_logo_path()
        pix = QPixmap(str(logo_path)) if logo_path.is_file() else QPixmap()
        if not pix.isNull():
            badge.setObjectName("BrandBadge")
            scaled = pix.scaledToHeight(
                40,
                Qt.TransformationMode.SmoothTransformation,
            )
            badge.setPixmap(scaled)
            badge.setFixedSize(scaled.size())
        else:
            # Logo asset missing — fall back to text badge so layout still works.
            badge.setText("IGA")
            badge.setObjectName("BrandBadgeFallback")
        b_layout.addWidget(badge)

        name = QLabel("Marketing Master")
        name.setObjectName("BrandName")
        b_layout.addWidget(name)

        sub = QLabel("Insurance Data Extraction")
        sub.setObjectName("BrandSub")
        b_layout.addWidget(sub)

        layout.addWidget(brand)
        layout.addWidget(self._hr())

        # Nav items
        nav_box = QWidget()
        nav_layout = QVBoxLayout(nav_box)
        nav_layout.setContentsMargins(8, 10, 8, 10)
        nav_layout.setSpacing(2)

        for key, label in _NAV_ITEMS:
            btn = QPushButton(label)
            btn.setObjectName("NavBtn")
            btn.setProperty("active", "true" if key == self._active else "false")
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _=False, k=key: self._select(k))
            nav_layout.addWidget(btn)
            self._nav_buttons[key] = btn

        nav_layout.addStretch(1)
        layout.addWidget(nav_box, 1)

        # Actions block — launches helper subprocesses (e.g., a Chrome
        # window with remote debugging enabled so the EPIC entry script
        # can attach via Playwright/CDP).
        actions_box = QWidget()
        actions_layout = QVBoxLayout(actions_box)
        actions_layout.setContentsMargins(8, 4, 8, 12)
        actions_layout.setSpacing(6)

        actions_label = QLabel("Tools")
        actions_label.setObjectName("SidebarSectionLabel")
        actions_layout.addWidget(actions_label)

        self._launch_browser_btn = QPushButton("Launch Browser")
        self._launch_browser_btn.setObjectName("SidebarActionBtn")
        self._launch_browser_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._launch_browser_btn.setToolTip(
            "Start Chrome with remote debugging enabled so EPIC entry can "
            "attach to it. Sign in to EPIC inside the launched window."
        )
        self._launch_browser_btn.clicked.connect(self.launch_browser_clicked.emit)
        actions_layout.addWidget(self._launch_browser_btn)

        # Close App button — added to actions_layout so it sits inside the
        # same dark container as Launch Browser, inheriting the sidebar background.
        self._close_app_btn = QPushButton("⏻  Close App")
        self._close_app_btn.setObjectName("CloseAppBtn")
        self._close_app_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_app_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._close_app_btn.setToolTip("Log out of EPIC (if active) and close IGA Marketing Master.")
        self._close_app_btn.clicked.connect(self.close_app_clicked.emit)
        actions_layout.addWidget(self._close_app_btn)

        layout.addWidget(actions_box)

        # Bottom engine status
        layout.addWidget(self._hr())

        self._engine_dot = QLabel("● Extraction Engine")
        self._engine_dot.setObjectName("EngineLabel")
        layout.addWidget(self._engine_dot)

        self._engine_status = QLabel("Ready")
        self._engine_status.setObjectName("EngineStatus")
        layout.addWidget(self._engine_status)

    @staticmethod
    def _hr() -> QFrame:
        line = QFrame()
        line.setObjectName("SidebarDivider")
        line.setFrameShape(QFrame.Shape.HLine)
        return line

    # -- Public API --------------------------------------------------------

    def set_active_page(self, page_key: str) -> None:
        """Highlight ``page_key`` without emitting ``page_changed``."""
        if page_key in self._nav_buttons:
            self._set_active(page_key)

    def set_button_enabled(
        self,
        page_key: str,
        enabled: bool,
        *,
        tooltip: str = "",
    ) -> None:
        """Enable/disable a nav button. Disabled buttons can't be clicked.

        When disabled, ``tooltip`` is shown on hover (use it to tell the
        operator what's blocking the gate, e.g. "Type a Named Insured first").
        """
        btn = self._nav_buttons.get(page_key)
        if btn is None:
            return
        btn.setEnabled(enabled)
        btn.setToolTip(tooltip if not enabled else "")
        btn.setCursor(
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        )

    def set_engine_status(self, active: bool, message: str = "") -> None:
        """Update the engine status dot + label at the bottom of the sidebar."""
        color = "#facc15" if active else "#4ade80"
        self._engine_dot.setStyleSheet(
            f"color: {color}; font-size: 11px; padding: 2px 16px 0px;"
        )
        self._engine_status.setText(message or ("Running..." if active else "Ready"))

    # -- Internal ----------------------------------------------------------

    def _select(self, key: str) -> None:
        if key == self._active:
            return
        self._set_active(key)
        self.page_changed.emit(key)

    def _set_active(self, key: str) -> None:
        old_btn = self._nav_buttons.get(self._active)
        if old_btn:
            old_btn.setProperty("active", "false")
            old_btn.style().unpolish(old_btn)
            old_btn.style().polish(old_btn)

        self._active = key
        new_btn = self._nav_buttons.get(key)
        if new_btn:
            new_btn.setProperty("active", "true")
            new_btn.style().unpolish(new_btn)
            new_btn.style().polish(new_btn)

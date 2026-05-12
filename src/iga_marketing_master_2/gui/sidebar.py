"""sidebar.py — Dark navigation sidebar for IGA Marketing Master 2.0."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

__all__ = ["SidebarNav"]


_NAV_ITEMS: list[tuple[str, str]] = [
    ("data_review", "Data Review"),
    ("history",     "History"),
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
    color: #ffffff;
    font-size: 15px;
    font-weight: bold;
    background: #2563eb;
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
            Values: ``"data_review"``, ``"history"``, ``"settings"``, ``"help"``.
    """

    page_changed = Signal(str)
    launch_browser_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidebarNav")
        self.setFixedWidth(196)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setStyleSheet(_QSS)

        self._nav_buttons: dict[str, QPushButton] = {}
        self._active: str = "data_review"

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

        badge = QLabel("IGA")
        badge.setObjectName("BrandBadge")
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

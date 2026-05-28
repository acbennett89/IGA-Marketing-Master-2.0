"""settings_page.py — Settings page widget for IGA Marketing Master 2.0.

Three credential sections, each saved independently to Windows Credential
Manager via secret_store:
  - Claude API Key
  - EPIC Usercode + Password
  - Working Library path (config.json via save_user_config)
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..config import Settings, save_user_config
from ..secret_store import (
    SecretStoreError,
    get_anthropic_api_key,
    get_epic_credentials,
    set_anthropic_api_key,
    set_epic_credentials,
)

__all__ = ["build_settings_page"]

_QSS = """
QWidget#SettingsPage {
    background: #f8fafc;
}
QWidget#SettingsScroll {
    background: #f8fafc;
}
QLabel#SectionTitle {
    font-size: 13px;
    font-weight: bold;
    color: #1e293b;
    background: transparent;
}
QFrame#SectionDivider {
    background: #e2e8f0;
    min-height: 1px;
    max-height: 1px;
    border: none;
}
QLabel#FieldLabel {
    color: #475569;
    font-size: 12px;
    min-width: 90px;
    background: transparent;
}
QLineEdit#SettingsField {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 12px;
    color: #0f172a;
    min-width: 280px;
}
QLineEdit#SettingsField:focus {
    border-color: #2563eb;
}
QPushButton#ShowBtn {
    background: transparent;
    color: #64748b;
    border: none;
    font-size: 11px;
    padding: 0 4px;
}
QPushButton#ShowBtn:hover {
    color: #2563eb;
}
QPushButton#SaveBtn {
    background: #2563eb;
    color: #ffffff;
    border: none;
    border-radius: 4px;
    padding: 6px 18px;
    font-size: 12px;
}
QPushButton#SaveBtn:hover {
    background: #1d4ed8;
}
QPushButton#BrowseBtn {
    background: #f1f5f9;
    color: #334155;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 5px 12px;
    font-size: 12px;
}
QPushButton#BrowseBtn:hover {
    background: #e2e8f0;
}
QLabel#StatusOk {
    color: #16a34a;
    font-size: 11px;
}
QLabel#StatusErr {
    color: #dc2626;
    font-size: 11px;
}
"""


def _divider() -> QFrame:
    d = QFrame()
    d.setObjectName("SectionDivider")
    d.setFrameShape(QFrame.Shape.HLine)
    return d


def _section_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("SectionTitle")
    return lbl


def _password_row(field: QLineEdit) -> QHBoxLayout:
    """Wrap a password QLineEdit with a Show/Hide toggle button."""
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(4)
    row.addWidget(field)
    btn = QPushButton("Show")
    btn.setObjectName("ShowBtn")
    btn.setCheckable(True)

    def _toggle(checked: bool) -> None:
        if checked:
            field.setEchoMode(QLineEdit.EchoMode.Normal)
            btn.setText("Hide")
        else:
            field.setEchoMode(QLineEdit.EchoMode.Password)
            btn.setText("Show")

    btn.toggled.connect(_toggle)
    row.addWidget(btn)
    return row


def _make_field(placeholder: str = "", password: bool = False) -> QLineEdit:
    f = QLineEdit()
    f.setObjectName("SettingsField")
    f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    if placeholder:
        f.setPlaceholderText(placeholder)
    if password:
        f.setEchoMode(QLineEdit.EchoMode.Password)
    return f


def _status_label() -> QLabel:
    lbl = QLabel("")
    lbl.setObjectName("StatusOk")
    return lbl


def _flash(lbl: QLabel, text: str, ok: bool) -> None:
    lbl.setObjectName("StatusOk" if ok else "StatusErr")
    lbl.setStyleSheet("color: #16a34a;" if ok else "color: #dc2626;")
    lbl.setText(text)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------

def _build_api_key_section(parent: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(8)
    layout.addWidget(_section_title("Claude API"))
    layout.addWidget(_divider())

    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setSpacing(8)

    field = _make_field("sk-ant-...", password=True)
    # Pre-populate with masked placeholder if key is already stored.
    existing = get_anthropic_api_key()
    if existing:
        field.setPlaceholderText("(key stored — enter new value to change)")

    pw_row = _password_row(field)
    pw_widget = QWidget()
    pw_widget.setLayout(pw_row)

    lbl = QLabel("API Key")
    lbl.setObjectName("FieldLabel")
    form.addRow(lbl, pw_widget)

    status = _status_label()
    save_btn = QPushButton("Save")
    save_btn.setObjectName("SaveBtn")

    def _save() -> None:
        val = field.text().strip()
        if not val:
            _flash(status, "Enter a key value first.", False)
            return
        try:
            set_anthropic_api_key(val)
            field.clear()
            field.setPlaceholderText("(key stored — enter new value to change)")
            _flash(status, "API key saved.", True)
        except SecretStoreError as exc:
            _flash(status, f"Save failed: {exc}", False)

    save_btn.clicked.connect(_save)

    btn_row = QHBoxLayout()
    btn_row.setContentsMargins(0, 0, 0, 0)
    btn_row.addStretch()
    btn_row.addWidget(status)
    btn_row.addWidget(save_btn)

    layout.addLayout(form)
    layout.addLayout(btn_row)
    return layout


def _build_epic_credentials_section() -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(8)
    layout.addWidget(_section_title("EPIC Credentials"))
    layout.addWidget(_divider())

    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setSpacing(8)

    usercode_field = _make_field("EPIC usercode")
    password_field = _make_field("EPIC password", password=True)

    existing = get_epic_credentials()
    if existing:
        usercode_field.setPlaceholderText("(stored — enter to change)")
        password_field.setPlaceholderText("(stored — enter to change)")

    lbl_u = QLabel("Usercode")
    lbl_u.setObjectName("FieldLabel")
    form.addRow(lbl_u, usercode_field)

    pw_row = _password_row(password_field)
    pw_widget = QWidget()
    pw_widget.setLayout(pw_row)
    lbl_p = QLabel("Password")
    lbl_p.setObjectName("FieldLabel")
    form.addRow(lbl_p, pw_widget)

    status = _status_label()
    save_btn = QPushButton("Save")
    save_btn.setObjectName("SaveBtn")

    def _save() -> None:
        u = usercode_field.text().strip()
        p = password_field.text().strip()
        if not u or not p:
            _flash(status, "Both Usercode and Password are required.", False)
            return
        try:
            set_epic_credentials(u, p)
            usercode_field.clear()
            password_field.clear()
            usercode_field.setPlaceholderText("(stored — enter to change)")
            password_field.setPlaceholderText("(stored — enter to change)")
            _flash(status, "EPIC credentials saved.", True)
        except SecretStoreError as exc:
            _flash(status, f"Save failed: {exc}", False)

    save_btn.clicked.connect(_save)

    btn_row = QHBoxLayout()
    btn_row.setContentsMargins(0, 0, 0, 0)
    btn_row.addStretch()
    btn_row.addWidget(status)
    btn_row.addWidget(save_btn)

    layout.addLayout(form)
    layout.addLayout(btn_row)
    return layout


def _build_working_library_section(settings: Settings) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(8)
    layout.addWidget(_section_title("Working Library"))
    layout.addWidget(_divider())

    path_field = _make_field()
    path_field.setText(str(settings.working_library))
    path_field.setReadOnly(True)

    browse_btn = QPushButton("Browse…")
    browse_btn.setObjectName("BrowseBtn")

    def _browse() -> None:
        chosen = QFileDialog.getExistingDirectory(
            None,
            "Select Working Library folder",
            path_field.text(),
        )
        if chosen:
            path_field.setText(chosen)

    browse_btn.clicked.connect(_browse)

    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    row.addWidget(path_field)
    row.addWidget(browse_btn)

    status = _status_label()
    save_btn = QPushButton("Save")
    save_btn.setObjectName("SaveBtn")

    def _save() -> None:
        val = path_field.text().strip()
        if not val:
            _flash(status, "Path cannot be empty.", False)
            return
        try:
            from dataclasses import replace as dc_replace
            updated = dc_replace(settings, working_library=Path(val))
            save_user_config(updated)
            _flash(status, "Working Library path saved.", True)
        except Exception as exc:  # noqa: BLE001
            _flash(status, f"Save failed: {exc}", False)

    save_btn.clicked.connect(_save)

    btn_row = QHBoxLayout()
    btn_row.setContentsMargins(0, 0, 0, 0)
    btn_row.addStretch()
    btn_row.addWidget(status)
    btn_row.addWidget(save_btn)

    form = QFormLayout()
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    lbl = QLabel("Path")
    lbl.setObjectName("FieldLabel")
    form.addRow(lbl, row)

    layout.addLayout(form)
    layout.addLayout(btn_row)
    return layout


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def build_settings_page(settings: Settings) -> QWidget:
    """Build and return the Settings page widget."""
    outer = QWidget()
    outer.setObjectName("SettingsPage")
    outer.setStyleSheet(_QSS)

    # Scrollable content area.
    scroll = QScrollArea(outer)
    scroll.setObjectName("SettingsScroll")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)

    content = QWidget()
    content.setObjectName("SettingsScroll")
    main_layout = QVBoxLayout(content)
    main_layout.setContentsMargins(40, 32, 40, 32)
    main_layout.setSpacing(28)
    main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

    heading = QLabel("Settings")
    heading.setStyleSheet(
        "font-size: 20px; font-weight: bold; color: #1e293b; background: transparent;"
    )
    main_layout.addWidget(heading)

    main_layout.addLayout(_build_api_key_section(content))
    main_layout.addLayout(_build_epic_credentials_section())
    main_layout.addLayout(_build_working_library_section(settings))
    main_layout.addStretch()

    scroll.setWidget(content)

    outer_layout = QVBoxLayout(outer)
    outer_layout.setContentsMargins(0, 0, 0, 0)
    outer_layout.addWidget(scroll)

    return outer

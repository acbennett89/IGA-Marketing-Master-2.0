"""client_page.py — Client info page for IGA Marketing Master 2.0.

State schema
------------
::

    state["insured"] = {
        "client_format":    "BUSINESS" | "INDIVIDUAL",
        "lookup_code":      str,
        "named_insured":    str,
        "fein":             str,
        "agency":           "IGA" | "VA",
        "branch":           str,        # branch code; valid set depends on agency
        "street_address":   str,
        "city":             str,
        "state":            str,
        "zip_code":         str,
        "physical_address": str,        # single line, kept for marketing use
        "business_phone":   str,
        "website":          str,
        "naics":            str,
        "sic":              str,
    }
    state["contacts"] = [
        {"first_name", "last_name", "title", "email", "phone"}, ...
    ]

Notes on the EPIC create flow
-----------------------------
- The GUI exposes only ``IGA`` and ``VA`` as agencies even though EPIC has
  more — see :data:`step_account_create.GUI_AGENCY_CHOICES`.
- Branch options are filtered by selected agency
  (see :data:`step_account_create.AGENCY_BRANCHES`). Branch is disabled until
  Agency is set.
- ``Type of Business`` (the Lines-of-Business "Commercial" checkbox in EPIC)
  and ``Client Type`` (Prospect) are hardcoded in the create step — operator
  doesn't pick them here.

Persistence runs on ``editingFinished`` for line edits and on ``toggled``
/ ``currentTextChanged`` for radios and combos. Saves go through the same
``save_callback`` the main window wires in (``_safe_state_save`` →
``state.save_atomic``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..epic_steps.step_account_create import (
    AGENCY_BRANCHES,
    BRANCH_NAMES,
    BUSINESS_TYPES,
    GUI_AGENCY_CHOICES,
)

__all__ = ["ClientPage", "build_client_page"]


_QSS = """
QWidget#ClientPage, QWidget#ClientScroll {
    background: #f8fafc;
}
QLabel#ClientPageTitle {
    font-size: 18px;
    font-weight: bold;
    color: #0f172a;
}
QLabel#ClientPageSub {
    color: #64748b;
    font-size: 12px;
}
QLabel#SectionTitle {
    font-size: 13px;
    font-weight: bold;
    color: #1e293b;
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
    min-width: 160px;
    background: transparent;
}
/* Section titles, dividers, and the radio row need explicit transparent
   backgrounds for the same reason — without it Qt's default style paints
   labels with the system window color (white on Windows), leaving each
   field label sitting on a stark white plate that doesn't match the
   page's #f8fafc fill. */
QLabel#SectionTitle {
    background: transparent;
}
QLabel#ClientPageTitle {
    background: transparent;
}
QLabel#ClientPageSub {
    background: transparent;
}
QLineEdit#ClientField {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 12px;
    color: #0f172a;
    min-width: 320px;
}
QLineEdit#ClientField:focus {
    border-color: #2563eb;
}
QLineEdit#ClientField:disabled {
    background: #f1f5f9;
    color: #94a3b8;
}
QComboBox#ClientCombo {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
    color: #0f172a;
    min-width: 320px;
}
QComboBox#ClientCombo:focus {
    border-color: #2563eb;
}
QComboBox#ClientCombo:disabled {
    background: #f1f5f9;
    color: #94a3b8;
}
QRadioButton#ClientRadio {
    color: #0f172a;
    font-size: 12px;
    padding-right: 14px;
    spacing: 6px;
}
QRadioButton#ClientRadio:disabled {
    color: #94a3b8;
}
/* Radio indicator: a 16px ring that stays the same physical size
   whether checked or not. Going from 1px border (unchecked) to a 4px
   border (checked) used to leave Qt unable to re-round the corners, so
   the "checked" state rendered as a blue square with a white center.
   We now keep the border weight constant at 1.5px and use a radial
   gradient as the background to draw the inner dot — gives the classic
   ring+dot look without changing the indicator's geometry. */
QRadioButton#ClientRadio::indicator {
    width: 16px;
    height: 16px;
    border: 1.5px solid #94a3b8;
    border-radius: 8px;
    background: #ffffff;
}
QRadioButton#ClientRadio::indicator:hover {
    border-color: #2563eb;
}
QRadioButton#ClientRadio::indicator:checked {
    border: 1.5px solid #2563eb;
    background: qradialgradient(
        cx: 0.5, cy: 0.5, radius: 0.5,
        fx: 0.5, fy: 0.5,
        stop: 0    #2563eb,
        stop: 0.45 #2563eb,
        stop: 0.55 #ffffff,
        stop: 1    #ffffff
    );
}
QRadioButton#ClientRadio::indicator:disabled {
    border-color: #cbd5e1;
    background: #f1f5f9;
}
QTableWidget#ContactsTable {
    background: #ffffff;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    gridline-color: #e2e8f0;
    font-size: 12px;
}
QTableWidget#ContactsTable QHeaderView::section {
    background: #f1f5f9;
    color: #334155;
    font-weight: bold;
    border: none;
    border-right: 1px solid #e2e8f0;
    border-bottom: 1px solid #cbd5e1;
    padding: 6px 8px;
}
QPushButton#ClientActionBtn {
    background: #f1f5f9;
    color: #334155;
    border: 1px solid #cbd5e1;
    border-radius: 4px;
    padding: 5px 12px;
    font-size: 12px;
}
QPushButton#ClientActionBtn:hover {
    background: #e2e8f0;
}
QPushButton#ClientActionBtn:disabled {
    color: #94a3b8;
    background: #f8fafc;
}
"""


# Plain text fields — order drives display order under the Insured section.
# (state_key, label, placeholder, max_length)
_INSURED_TEXT_FIELDS: list[tuple[str, str, str, int | None]] = [
    ("lookup_code",      "Lookup Code",       "EPIC account lookup code (filled after EPIC create)", 10),
    ("named_insured",    "Named Insured",     "Legal entity name",                                   None),
    ("fein",             "FEIN",              "##-#######",                                          None),
    # — Agency / Branch combo go here (built separately) —
    ("street_address",   "Street Address",    "Street — autocompletes city/state/ZIP in EPIC",        None),
    ("city",             "City",              "Auto-fills from street autocomplete",                  None),
    ("state",            "State",             "2-letter (e.g. TN)",                                   4),
    ("zip_code",         "ZIP Code",          "5- or 9-digit",                                       12),
    ("physical_address", "Physical Address",  "Leave blank if same as mailing",                       None),
    ("business_phone",   "Business Phone",    "(615) 555-0100",                                       None),
    ("website",          "Website",           "https://",                                             None),
    ("naics",            "NAICS",             "Optional",                                             None),
    ("sic",              "SIC",               "Optional",                                             None),
]

_CONTACT_COLUMNS: list[tuple[str, str]] = [
    ("first_name",   "First Name"),
    ("last_name",    "Last Name"),
    ("title",        "Title"),
    ("email",        "Email"),
    ("phone",        "Phone Number"),
]

# Format radio choices: state value → display label.
_FORMAT_CHOICES: list[tuple[str, str]] = [
    ("BUSINESS",   "Business"),
    ("INDIVIDUAL", "Individual"),
]
_FORMAT_DEFAULT = "BUSINESS"

# Sentinel for the "no selection yet" entry in the agency combo.
_NO_AGENCY = ""
_NO_BRANCH = ""
_NO_BUSINESS_TYPE = ""


def _divider() -> QFrame:
    d = QFrame()
    d.setObjectName("SectionDivider")
    d.setFrameShape(QFrame.Shape.HLine)
    return d


def _section_title(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("SectionTitle")
    return lbl


def _field(placeholder: str = "", max_length: int | None = None) -> QLineEdit:
    f = QLineEdit()
    f.setObjectName("ClientField")
    f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    if placeholder:
        f.setPlaceholderText(placeholder)
    if max_length is not None:
        f.setMaxLength(max_length)
    return f


def _combo() -> QComboBox:
    c = QComboBox()
    c.setObjectName("ClientCombo")
    c.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return c


class ClientPage(QWidget):
    """Client info page bound to the active client's ``state.json``.

    Signals:
        insured_changed(str, str): emitted after a persisted Insured edit,
            with ``(field_key, new_value)`` so the window can refresh
            dependent UI without re-reading state.
        contacts_changed(): emitted after any persisted contact mutation.
    """

    insured_changed = Signal(str, str)
    contacts_changed = Signal()
    # Header buttons — wiring intentionally left to a follow-up task. The
    # signals exist now so a host can connect them at any time without us
    # having to ship the button rewrite again.
    import_new_client_clicked = Signal()
    select_existing_client_clicked = Signal()
    clear_client_clicked = Signal()

    def __init__(
        self,
        *,
        save_callback: Callable[[dict, Any], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._client: Any | None = None
        self._save_callback = save_callback
        self._suspend_persist = False  # guard while we load state programmatically

        self.setObjectName("ClientPage")
        self.setStyleSheet(_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Header: title block on the left + Import/Select buttons on the right,
        # mirroring the Data Extraction & Review page's Extract/Begin-Entry layout.
        header = QWidget()
        header.setObjectName("ClientPageHeader")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(28, 22, 28, 18)
        h_layout.setSpacing(12)

        title_block = QWidget()
        tb = QVBoxLayout(title_block)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(2)
        title = QLabel("Client")
        title.setObjectName("ClientPageTitle")
        self._sub = QLabel("Named insured details and contacts.")
        self._sub.setObjectName("ClientPageSub")
        tb.addWidget(title)
        tb.addWidget(self._sub)
        h_layout.addWidget(title_block, 1)

        # Action buttons (right-aligned). These reuse the main window's
        # HeaderBtnSecondary / HeaderBtnPrimary QSS so they match the buttons
        # on the Data Extraction & Review page exactly. Click signals are
        # exposed but intentionally unwired here — the host wires them.
        self.clear_client_btn = QPushButton("Clear Client")
        self.clear_client_btn.setObjectName("HeaderBtnSecondary")
        self.clear_client_btn.setToolTip(
            "Drop the loaded client and switch back to the draft. "
            "Hidden when already on the draft."
        )
        self.clear_client_btn.clicked.connect(self.clear_client_clicked)
        self.clear_client_btn.hide()  # host shows it when a non-draft client is active
        h_layout.addWidget(self.clear_client_btn)

        self.import_new_client_btn = QPushButton("Import New Client")
        self.import_new_client_btn.setObjectName("HeaderBtnSecondary")
        self.import_new_client_btn.setToolTip(
            "Start a new client by importing PDFs and running extraction."
        )
        self.import_new_client_btn.clicked.connect(self.import_new_client_clicked)
        h_layout.addWidget(self.import_new_client_btn)

        self.select_existing_client_btn = QPushButton("Select Existing Client")
        self.select_existing_client_btn.setObjectName("HeaderBtnPrimary")
        self.select_existing_client_btn.setToolTip(
            "Pick a client folder from the Working Library."
        )
        self.select_existing_client_btn.clicked.connect(self.select_existing_client_clicked)
        h_layout.addWidget(self.select_existing_client_btn)

        outer.addWidget(header)

        scroll = QScrollArea()
        scroll.setObjectName("ClientScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        body = QWidget()
        body.setObjectName("ClientScroll")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(28, 4, 28, 28)
        body_layout.setSpacing(22)

        body_layout.addLayout(self._build_insured_section())
        body_layout.addLayout(self._build_contacts_section())
        body_layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        self._refresh_enabled_state()

    # -- UI sections --------------------------------------------------------

    def _build_insured_section(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(_section_title("Insured"))
        layout.addWidget(_divider())

        form = QFormLayout()
        form.setLabelAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        form.setSpacing(10)

        # 1. Individual / Business radio.
        # NOTE: We connect each button's own ``toggled`` signal directly
        # rather than ``QButtonGroup.buttonToggled`` — that signal has an
        # overload pair in PySide6 (``(int, bool)`` and
        # ``(QAbstractButton, bool)``) that fails to resolve without the
        # explicit ``[QAbstractButton, bool]`` selector.
        self._format_group = QButtonGroup(self)
        self._format_group.setExclusive(True)
        radio_row = QWidget()
        radio_row_layout = QHBoxLayout(radio_row)
        radio_row_layout.setContentsMargins(0, 0, 0, 0)
        radio_row_layout.setSpacing(16)
        for value, label_text in _FORMAT_CHOICES:
            btn = QRadioButton(label_text)
            btn.setObjectName("ClientRadio")
            btn.setProperty("formatValue", value)
            btn.toggled.connect(
                lambda checked, b=btn: self._on_format_toggled(b, checked)
            )
            self._format_group.addButton(btn)
            radio_row_layout.addWidget(btn)
        radio_row_layout.addStretch(1)
        fmt_label = QLabel("Individual or Business")
        fmt_label.setObjectName("FieldLabel")
        form.addRow(fmt_label, radio_row)

        # 2. Plain text fields (lookup_code, named_insured, fein) up to the
        # agency/branch split.
        self._insured_fields: dict[str, QLineEdit] = {}

        def add_text_field(key: str) -> None:
            spec = next((s for s in _INSURED_TEXT_FIELDS if s[0] == key), None)
            if spec is None:
                return
            _, label_text, placeholder, max_len = spec
            field = _field(placeholder, max_length=max_len)
            field.editingFinished.connect(
                lambda k=key, f=field: self._on_insured_committed(k, f)
            )
            self._insured_fields[key] = field
            lbl = QLabel(label_text)
            lbl.setObjectName("FieldLabel")
            form.addRow(lbl, field)

        for key in ("lookup_code", "named_insured", "fein"):
            add_text_field(key)

        # Business Type combo — EPIC's exact contact "Business Type" labels so
        # the value always matches a real EPIC dropdown row at entry time.
        self._business_type_combo = _combo()
        self._business_type_combo.addItem("— Select business type —", _NO_BUSINESS_TYPE)
        for bt in BUSINESS_TYPES:
            self._business_type_combo.addItem(bt, bt)
        self._business_type_combo.currentIndexChanged.connect(self._on_business_type_changed)
        bt_label = QLabel("Business Type")
        bt_label.setObjectName("FieldLabel")
        form.addRow(bt_label, self._business_type_combo)

        # 3. Agency + Branch combos.
        self._agency_combo = _combo()
        self._agency_combo.addItem("— Select agency —", _NO_AGENCY)
        for code in GUI_AGENCY_CHOICES:
            self._agency_combo.addItem(code, code)
        self._agency_combo.currentIndexChanged.connect(self._on_agency_changed)
        agency_label = QLabel("Agency")
        agency_label.setObjectName("FieldLabel")
        form.addRow(agency_label, self._agency_combo)

        self._branch_combo = _combo()
        self._branch_combo.addItem("— Select agency first —", _NO_BRANCH)
        self._branch_combo.setEnabled(False)
        self._branch_combo.currentIndexChanged.connect(self._on_branch_changed)
        branch_label = QLabel("Branch")
        branch_label.setObjectName("FieldLabel")
        form.addRow(branch_label, self._branch_combo)

        # 4. The rest of the text fields (address split, contact, identifiers).
        for key in (
            "street_address", "city", "state", "zip_code",
            "physical_address", "business_phone", "website",
            "naics", "sic",
        ):
            add_text_field(key)

        layout.addLayout(form)
        return layout

    def _build_contacts_section(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.addWidget(_section_title("Contacts"))
        layout.addWidget(_divider())

        table = QTableWidget(0, len(_CONTACT_COLUMNS))
        table.setObjectName("ContactsTable")
        table.setHorizontalHeaderLabels([h for _, h in _CONTACT_COLUMNS])
        table.verticalHeader().setVisible(False)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.SelectedClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        table.setAlternatingRowColors(False)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(220)
        table.itemChanged.connect(self._on_contact_item_changed)
        self._contacts_table = table

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)

        self._add_contact_btn = QPushButton("+ Add Contact")
        self._add_contact_btn.setObjectName("ClientActionBtn")
        self._remove_contact_btn = QPushButton("Remove Selected")
        self._remove_contact_btn.setObjectName("ClientActionBtn")
        self._remove_contact_btn.setEnabled(False)

        self._add_contact_btn.clicked.connect(self._on_add_contact)
        self._remove_contact_btn.clicked.connect(self._on_remove_contact)
        table.itemSelectionChanged.connect(self._update_remove_enabled)

        btn_row.addWidget(self._add_contact_btn)
        btn_row.addWidget(self._remove_contact_btn)
        btn_row.addStretch(1)

        layout.addWidget(table)
        layout.addLayout(btn_row)
        return layout

    # -- Public API ---------------------------------------------------------

    def set_client(self, client: Any | None) -> None:
        """Bind to ``client`` (a ``_ClientContext``) or clear when None."""
        self._client = client
        self._suspend_persist = True
        try:
            if client is None:
                for f in self._insured_fields.values():
                    self._set_text_silently(f, "")
                self._set_format_silently(_FORMAT_DEFAULT)
                self._set_agency_silently(_NO_AGENCY)
                self._set_branch_silently(_NO_BRANCH)
                self._set_business_type_silently(_NO_BUSINESS_TYPE)
                self._reload_contacts_table([])
                self._sub.setText("No client selected.")
            else:
                state = client.state if isinstance(client.state, dict) else {}
                insured = self._read_insured(state)

                # Format radio (default Business if missing).
                fmt = str(insured.get("client_format") or _FORMAT_DEFAULT).upper()
                self._set_format_silently(fmt if fmt in {v for v, _ in _FORMAT_CHOICES} else _FORMAT_DEFAULT)

                # Plain text fields.
                for key, field in self._insured_fields.items():
                    self._set_text_silently(field, str(insured.get(key) or ""))

                # Agency / Branch — populate agency first so branch options are correct.
                agency = str(insured.get("agency") or _NO_AGENCY)
                self._set_agency_silently(agency)
                branch = str(insured.get("branch") or _NO_BRANCH)
                self._set_branch_silently(branch)
                self._set_business_type_silently(
                    str(insured.get("business_type") or _NO_BUSINESS_TYPE)
                )

                contacts = state.get("contacts") or []
                self._reload_contacts_table(
                    contacts if isinstance(contacts, list) else []
                )
                self._sub.setText(f"Client: {client.name}")
        finally:
            self._suspend_persist = False
        self._refresh_enabled_state()
        self._update_remove_enabled()

    # -- Internal helpers ---------------------------------------------------

    @staticmethod
    def _set_text_silently(field: QLineEdit, value: str) -> None:
        blocked = field.blockSignals(True)
        try:
            field.setText(value)
        finally:
            field.blockSignals(blocked)

    def _set_format_silently(self, value: str) -> None:
        for btn in self._format_group.buttons():
            blocked = btn.blockSignals(True)
            try:
                btn.setChecked(btn.property("formatValue") == value)
            finally:
                btn.blockSignals(blocked)

    def _set_agency_silently(self, code: str) -> None:
        blocked = self._agency_combo.blockSignals(True)
        try:
            idx = self._agency_combo.findData(code)
            self._agency_combo.setCurrentIndex(idx if idx >= 0 else 0)
            self._refresh_branch_choices(code, preserve=_NO_BRANCH)
        finally:
            self._agency_combo.blockSignals(blocked)

    def _set_branch_silently(self, code: str) -> None:
        blocked = self._branch_combo.blockSignals(True)
        try:
            idx = self._branch_combo.findData(code) if code else 0
            self._branch_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._branch_combo.blockSignals(blocked)

    def _set_business_type_silently(self, value: str) -> None:
        blocked = self._business_type_combo.blockSignals(True)
        try:
            idx = self._business_type_combo.findData(value) if value else 0
            self._business_type_combo.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._business_type_combo.blockSignals(blocked)

    def _refresh_branch_choices(self, agency_code: str, *, preserve: str) -> None:
        """Repopulate the branch combo based on the chosen *agency_code*.

        Called from agency-change handlers and from set_client. Does not emit
        change signals (caller controls signal blocking when needed).
        """
        self._branch_combo.clear()
        branches = AGENCY_BRANCHES.get(agency_code, [])
        if not branches:
            self._branch_combo.addItem("— Select agency first —", _NO_BRANCH)
            self._branch_combo.setEnabled(False)
            return
        self._branch_combo.addItem("— Select branch —", _NO_BRANCH)
        for code in branches:
            display = f"{code} — {BRANCH_NAMES.get(code, code)}"
            self._branch_combo.addItem(display, code)
        self._branch_combo.setEnabled(True)
        if preserve and preserve in branches:
            idx = self._branch_combo.findData(preserve)
            if idx >= 0:
                self._branch_combo.setCurrentIndex(idx)

    def _refresh_enabled_state(self) -> None:
        bound = self._client is not None
        for f in self._insured_fields.values():
            f.setEnabled(bound)
        for btn in self._format_group.buttons():
            btn.setEnabled(bound)
        self._business_type_combo.setEnabled(bound)
        self._agency_combo.setEnabled(bound)
        if not bound:
            self._branch_combo.setEnabled(False)
        else:
            # Branch enabled-ness depends on whether an agency is currently set.
            self._branch_combo.setEnabled(bool(self._agency_combo.currentData()))
        self._contacts_table.setEnabled(bound)
        self._add_contact_btn.setEnabled(bound)
        if not bound:
            self._remove_contact_btn.setEnabled(False)

    def _update_remove_enabled(self) -> None:
        if self._client is None:
            self._remove_contact_btn.setEnabled(False)
            return
        self._remove_contact_btn.setEnabled(
            bool(self._contacts_table.selectionModel().selectedRows())
        )

    @staticmethod
    def _read_insured(state: dict) -> dict:
        """Return a dict view of state["insured"], honoring legacy fields.

        Legacy migration: if a state file still has the old single-line
        ``mailing_address`` and no ``street_address``, drop the whole string
        into ``street_address`` so the operator doesn't lose anything on
        first load — they can then re-split it manually.
        """
        insured = state.get("insured")
        if isinstance(insured, dict):
            migrated = dict(insured)
            legacy_mailing = migrated.pop("mailing_address", None)
            if legacy_mailing and not migrated.get("street_address"):
                migrated["street_address"] = legacy_mailing
            return migrated
        # Legacy: top-level lookup_code only.
        return {"lookup_code": str(state.get("lookup_code") or "")}

    def _ensure_insured_dict(self, state: dict) -> dict:
        """Return ``state['insured']`` as a mutable dict, creating if needed."""
        insured = state.get("insured")
        if not isinstance(insured, dict):
            insured = {}
            state["insured"] = insured
        state.pop("lookup_code", None)
        return insured

    # -- Insured persistence ------------------------------------------------

    def _persist_insured_key(self, key: str, new_value: str) -> bool:
        """Write ``insured[key] = new_value`` if changed. Returns True if written.

        Centralises the read-current → compare → save → roll-back pattern
        used by every Insured input.
        """
        if self._suspend_persist or self._client is None or self._save_callback is None:
            return False
        state = self._client.state
        if not isinstance(state, dict):
            return False
        insured = self._ensure_insured_dict(state)
        prior = str(insured.get(key) or "")
        if new_value == prior:
            return False
        insured[key] = new_value
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self._save_callback(state, self._client.path)
        except Exception:
            insured[key] = prior
            raise
        self.insured_changed.emit(key, new_value)
        return True

    def _on_insured_committed(self, key: str, field: QLineEdit) -> None:
        new_value = field.text().strip()
        try:
            self._persist_insured_key(key, new_value)
        except Exception:
            # Roll the field text back to the prior persisted value.
            if self._client is not None and isinstance(self._client.state, dict):
                prior = str(
                    self._read_insured(self._client.state).get(key) or ""
                )
                self._set_text_silently(field, prior)
            raise

    def _on_format_toggled(self, button, checked: bool) -> None:  # noqa: ANN001
        if not checked:
            return
        value = button.property("formatValue") or _FORMAT_DEFAULT
        try:
            self._persist_insured_key("client_format", str(value))
        except Exception:
            # Roll back to whatever's in state.
            if self._client is not None and isinstance(self._client.state, dict):
                prior = str(
                    self._read_insured(self._client.state).get("client_format")
                    or _FORMAT_DEFAULT
                )
                self._set_format_silently(prior)
            raise

    def _on_agency_changed(self, _index: int) -> None:
        code = str(self._agency_combo.currentData() or _NO_AGENCY)
        # Rebuild branch options — preserve the currently-selected branch only
        # if it's still valid for the new agency (otherwise drop it).
        previously_selected = str(self._branch_combo.currentData() or _NO_BRANCH)
        keep = (
            previously_selected
            if previously_selected and previously_selected in AGENCY_BRANCHES.get(code, [])
            else _NO_BRANCH
        )

        blocked = self._branch_combo.blockSignals(True)
        try:
            self._refresh_branch_choices(code, preserve=keep)
        finally:
            self._branch_combo.blockSignals(blocked)

        # Persist agency + reconcile branch state.
        try:
            self._persist_insured_key("agency", code)
            # If the previously-selected branch is no longer valid, clear it.
            if keep != previously_selected:
                self._persist_insured_key("branch", keep)
        except Exception:
            if self._client is not None and isinstance(self._client.state, dict):
                prior_agency = str(
                    self._read_insured(self._client.state).get("agency") or _NO_AGENCY
                )
                self._set_agency_silently(prior_agency)
            raise

    def _on_branch_changed(self, _index: int) -> None:
        code = str(self._branch_combo.currentData() or _NO_BRANCH)
        try:
            self._persist_insured_key("branch", code)
        except Exception:
            if self._client is not None and isinstance(self._client.state, dict):
                prior = str(
                    self._read_insured(self._client.state).get("branch") or _NO_BRANCH
                )
                self._set_branch_silently(prior)
            raise

    def _on_business_type_changed(self, _index: int) -> None:
        value = str(self._business_type_combo.currentData() or _NO_BUSINESS_TYPE)
        try:
            self._persist_insured_key("business_type", value)
        except Exception:
            if self._client is not None and isinstance(self._client.state, dict):
                prior = str(
                    self._read_insured(self._client.state).get("business_type")
                    or _NO_BUSINESS_TYPE
                )
                self._set_business_type_silently(prior)
            raise

    # -- Contacts persistence -----------------------------------------------

    def _reload_contacts_table(self, contacts: list) -> None:
        table = self._contacts_table
        blocked = table.blockSignals(True)
        try:
            table.setRowCount(0)
            for row_data in contacts:
                if not isinstance(row_data, dict):
                    continue
                row = table.rowCount()
                table.insertRow(row)
                for col, (key, _) in enumerate(_CONTACT_COLUMNS):
                    table.setItem(
                        row, col, QTableWidgetItem(str(row_data.get(key) or ""))
                    )
        finally:
            table.blockSignals(blocked)

    def _serialize_contacts_table(self) -> list[dict]:
        table = self._contacts_table
        out: list[dict] = []
        for row in range(table.rowCount()):
            entry: dict[str, str] = {}
            for col, (key, _) in enumerate(_CONTACT_COLUMNS):
                item = table.item(row, col)
                entry[key] = (item.text().strip() if item is not None else "")
            out.append(entry)
        return out

    def _persist_contacts(self, *, revert_payload: list[dict] | None = None) -> None:
        if self._suspend_persist or self._client is None or self._save_callback is None:
            return
        state = self._client.state
        if not isinstance(state, dict):
            return
        prior = state.get("contacts")
        new_contacts = self._serialize_contacts_table()
        state["contacts"] = new_contacts
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            self._save_callback(state, self._client.path)
        except Exception:
            state["contacts"] = revert_payload if revert_payload is not None else prior
            self._reload_contacts_table(
                state["contacts"] if isinstance(state["contacts"], list) else []
            )
            raise
        self.contacts_changed.emit()

    def _on_contact_item_changed(self, _item: QTableWidgetItem) -> None:
        self._persist_contacts()

    def _on_add_contact(self) -> None:
        if self._client is None:
            return
        table = self._contacts_table
        blocked = table.blockSignals(True)
        try:
            row = table.rowCount()
            table.insertRow(row)
            for col in range(len(_CONTACT_COLUMNS)):
                table.setItem(row, col, QTableWidgetItem(""))
        finally:
            table.blockSignals(blocked)
        self._persist_contacts()
        table.selectRow(row)
        first_item = table.item(row, 0)
        if first_item is not None:
            table.editItem(first_item)

    def _on_remove_contact(self) -> None:
        if self._client is None:
            return
        table = self._contacts_table
        rows = sorted(
            {idx.row() for idx in table.selectedIndexes()}, reverse=True
        )
        if not rows:
            return
        blocked = table.blockSignals(True)
        try:
            for r in rows:
                table.removeRow(r)
        finally:
            table.blockSignals(blocked)
        self._persist_contacts()


def build_client_page(
    *,
    save_callback: Callable[[dict, Any], None] | None = None,
    parent: QWidget | None = None,
) -> ClientPage:
    """Factory wrapper around ``ClientPage`` construction."""
    return ClientPage(save_callback=save_callback, parent=parent)

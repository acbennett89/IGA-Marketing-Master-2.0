"""section_forms.py — Purpose-built form widgets for each insurance coverage section."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import section_forms_layout as layout
from .section_table import confidence_color

__all__ = ["SectionFormBase", "make_section_form", "invalidate_field_map_cache"]


# Default minimum height for repeatable tables — sized to show ~6 rows
# before scrolling kicks in. Header (~28px) + 6 rows × ~28px ≈ 200px.
_TBL_HEIGHT_6_ROWS: int = 200


# ---------------------------------------------------------------------------
# Field Map cache
# ---------------------------------------------------------------------------
# Each section form needs to resolve (screen, name) -> domain_tag, which
# requires the Field Map. Loading it on every form construction is wasteful;
# we cache here at module level. Cleared by `invalidate_field_map_cache()`
# when the annotator or another writer modifies the Field Map.

_FIELD_MAP_CACHE = None


def _get_field_map():
    global _FIELD_MAP_CACHE
    if _FIELD_MAP_CACHE is None:
        from .. import field_map
        _FIELD_MAP_CACHE = field_map.load()
    return _FIELD_MAP_CACHE


def invalidate_field_map_cache() -> None:
    """Drop the cached Field Map. Next lookup will reload from disk."""
    global _FIELD_MAP_CACHE
    _FIELD_MAP_CACHE = None


def _resolve_column_tags(
    columns: tuple[layout.ColumnSpec, ...]
) -> list[str | None]:
    """Resolve each ColumnSpec to its verified domain_tag (or None)."""
    from .. import field_map
    fm = _get_field_map()
    return [field_map.tag_for_field(fm, c.screen, c.name) for c in columns]


def _resolve_tag(screen: str, name: str) -> str:
    """Resolve one (screen, EPIC name) pair to the verified domain_tag.

    Returns empty string on miss, which makes ``_add_text`` / ``_add_combo``
    render as a blank input rather than crashing — useful when the Field Map
    is partially annotated.
    """
    from .. import field_map
    fm = _get_field_map()
    return field_map.tag_for_field(fm, screen, name) or ""

# ---------------------------------------------------------------------------
# Stylesheet
# ---------------------------------------------------------------------------

_QSS = """
QScrollArea#SectionFormScroll,
QScrollArea#SectionFormScroll > QWidget,
QScrollArea#SectionFormScroll > QWidget > QWidget,
QWidget#SectionFormInner {
    background: white;
}
QScrollArea#SectionFormScroll {
    border: none;
}
QLabel#FieldLabel {
    color: #374151;
    font-size: 12px;
    min-width: 150px;
}
QLabel#GroupHeader {
    font-size: 13px;
    font-weight: bold;
    color: #0f172a;
}
QLineEdit#FieldInput {
    background: white;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 12px;
    color: #1e293b;
    min-width: 100px;
    max-width: 320px;
}
QLineEdit#FieldInput:focus { border-color: #2563eb; }
QComboBox#FieldCombo {
    background: white;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 12px;
    color: #1e293b;
    min-width: 130px;
    max-width: 320px;
}
QComboBox#FieldCombo:focus { border-color: #2563eb; }
QFrame#HSep {
    background: #e2e8f0;
    min-height: 1px;
    max-height: 1px;
    border: none;
}
QTableWidget#SectionTable {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 4px;
    gridline-color: #f1f5f9;
    outline: none;
    font-size: 12px;
}
QTableWidget#SectionTable QHeaderView::section {
    background: #f8fafc;
    border: none;
    border-bottom: 1px solid #e2e8f0;
    border-right: 1px solid #e2e8f0;
    padding: 5px 8px;
    font-size: 11px;
    font-weight: bold;
    color: #475569;
}
QTableWidget#SectionTable::item {
    padding: 4px 8px;
    color: #1e293b;
}
QTableWidget#SectionTable::item:selected {
    background: #eff6ff;
    color: #1e293b;
}
QPushButton#AddRowBtn {
    background: transparent;
    border: 1px solid #94a3b8;
    border-radius: 4px;
    color: #64748b;
    font-size: 11px;
    padding: 4px 12px;
}
QPushButton#AddRowBtn:hover {
    border-color: #2563eb;
    color: #2563eb;
    background: #eff6ff;
}
QPlainTextEdit#NotesInput {
    background: white;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    padding: 8px;
    font-size: 12px;
    color: #1e293b;
}
QPlainTextEdit#NotesInput:focus { border-color: #2563eb; }
"""

_US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _val(state: dict | None, tag: str) -> str:
    if not state:
        return ""
    rec = (state.get("fields") or {}).get(tag)
    if not isinstance(rec, dict):
        return ""
    v = rec.get("value")
    return str(v) if v is not None else ""


def _rep(state: dict | None, group: str) -> list[dict]:
    if not state:
        return []
    items = (state.get("repeatables") or {}).get(group, [])
    return items if isinstance(items, list) else []


_TAG_FALLBACKS: dict[str, list[str]] = {
    # When the canonical tag isn't populated, try these in order. Set per-tag
    # by section forms when the same logical concept can land on different
    # legacy tag names depending on which dec page the field came from.
    "account.named_insured.name": [
        "account.named_insured.fni_name",
    ],
    "location.building_description": [
        "location.address",
        "location.description",
    ],
}


def _rep_or_singleton_row(state: dict | None, namespace_prefix: str) -> list[dict]:
    """Return rows for a repeatable group, or a synthesized single row from
    matching singleton fields if the repeatable group has no items.

    Used by section forms whose tab is "naturally" repeatable (named insureds,
    locations) but where Claude sometimes emits the data as flat singletons
    in `state.fields` (one named insured, no explicit repeatable wrapping).
    Surfacing a single synthetic row lets the table show the data instead
    of going blank.

    Also applies tag fallbacks: if a row is missing the canonical tag but
    has data under a known alternate tag, copy the alternate's value into
    the canonical key so the table column finds it.
    """
    items = _rep(state, namespace_prefix)
    if not items and state:
        fields = state.get("fields") or {}
        prefix = namespace_prefix + "."
        matching = {tag: rec for tag, rec in fields.items() if tag.startswith(prefix)}
        if matching:
            items = [matching]
    if not items:
        return []
    # Apply fallbacks per row.
    for item in items:
        if not isinstance(item, dict):
            continue
        for canonical, alternates in _TAG_FALLBACKS.items():
            if canonical in item:
                rec = item[canonical]
                if isinstance(rec, dict) and rec.get("value"):
                    continue  # canonical already has a value
            for alt in alternates:
                if alt in item:
                    item[canonical] = item[alt]
                    break
    return items


def _hr() -> QFrame:
    f = QFrame()
    f.setObjectName("HSep")
    f.setFrameShape(QFrame.Shape.HLine)
    f.setMaximumHeight(1)
    return f


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class SectionFormBase(QScrollArea):
    """Scrollable per-section form.

    Signals:
        field_changed(domain_tag, value)
            ``domain_tag`` starting with ``"__add:<group>"`` means the operator
            clicked an "Add …" button for a repeatable group.
    """

    field_changed = Signal(str, object)

    def __init__(self, state: dict | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SectionFormScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet(_QSS)
        # Ensure viewport is white (no OS-default grey bleed-through).
        self.viewport().setStyleSheet("background: white;")

        self._state = state
        self._inputs: dict[str, QWidget] = {}

        inner = QWidget()
        inner.setObjectName("SectionFormInner")
        self.setWidget(inner)
        self._root = QVBoxLayout(inner)
        self._root.setContentsMargins(24, 20, 24, 20)
        self._root.setSpacing(14)

        self._build_form()
        # Subclasses add final widget with stretch=1 — no trailing addStretch here.

    # -- Subclass interface --------------------------------------------------

    def _build_form(self) -> None:
        raise NotImplementedError

    def _refresh_tables(self, state: dict | None) -> None:
        """Override to refresh QTableWidget subwidgets."""

    # -- Layout helpers ------------------------------------------------------

    def _hdr(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("GroupHeader")
        return lbl

    def _grid(self, cols: int = 2) -> QGridLayout:
        g = QGridLayout()
        g.setHorizontalSpacing(40)
        g.setVerticalSpacing(10)
        # Single trailing filler absorbs leftover space so each label+input
        # pair stays anchored to its label instead of stretching across the pane.
        g.setColumnStretch(cols * 2, 1)
        return g

    def _add_text(self, grid: QGridLayout, row: int, col: int,
                  label: str, tag: str, placeholder: str = "") -> QLineEdit:
        lbl = QLabel(label)
        lbl.setObjectName("FieldLabel")
        inp = QLineEdit()
        inp.setObjectName("FieldInput")
        if placeholder:
            inp.setPlaceholderText(placeholder)
        v = _val(self._state, tag)
        if v:
            inp.setText(v)

        def _done(t: str = tag, w: QLineEdit = inp) -> None:
            text = w.text().strip()
            self.field_changed.emit(t, text if text else None)

        inp.editingFinished.connect(_done)
        self._inputs[tag] = inp
        gc = col * 2
        grid.addWidget(lbl, row, gc, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(inp, row, gc + 1)
        return inp

    def _add_combo(self, grid: QGridLayout, row: int, col: int,
                   label: str, tag: str, options: list[str]) -> QComboBox:
        lbl = QLabel(label)
        lbl.setObjectName("FieldLabel")
        combo = QComboBox()
        combo.setObjectName("FieldCombo")
        combo.addItem("")
        combo.addItems(options)
        v = _val(self._state, tag)
        if v:
            idx = combo.findText(v)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        def _changed(t: str = tag, w: QComboBox = combo) -> None:
            text = w.currentText().strip()
            self.field_changed.emit(t, text if text else None)

        combo.currentTextChanged.connect(_changed)
        self._inputs[tag] = combo
        gc = col * 2
        grid.addWidget(lbl, row, gc, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(combo, row, gc + 1)
        return combo

    def _add_checkbox_group(
        self,
        grid: QGridLayout,
        row: int,
        col: int,
        label: str,
        items: list[tuple[str, str]],
        *,
        col_span: int = 1,
    ) -> dict[str, QCheckBox]:
        """Add a labeled row of checkboxes that each persist via field_changed.

        :param items: list of ``(checkbox_label, domain_tag)`` pairs.
        :param col_span: how many grid columns the checkbox row should span
            (each grid column = label + value, so col_span=2 spans the
            full width on a 2-column grid).
        :returns: dict mapping domain_tag → QCheckBox for refresh.
        """
        lbl = QLabel(label)
        lbl.setObjectName("FieldLabel")
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(14)

        checkboxes: dict[str, QCheckBox] = {}
        for cb_label, tag in items:
            cb = QCheckBox(cb_label)
            v = _val(self._state, tag)
            if v and v.lower() in {"true", "yes", "1", "checked", "on"}:
                cb.setChecked(True)

            def _changed(checked: bool, t: str = tag) -> None:
                self.field_changed.emit(t, "Yes" if checked else "No")

            cb.toggled.connect(_changed)
            self._inputs[tag] = cb
            checkboxes[tag] = cb
            row_layout.addWidget(cb)
        row_layout.addStretch(1)

        gc = col * 2
        grid.addWidget(lbl, row, gc, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(row_widget, row, gc + 1, 1, col_span * 2 - 1)
        return checkboxes

    def _add_period(self, grid: QGridLayout, row: int, col: int,
                    eff_tag: str, exp_tag: str) -> None:
        lbl = QLabel("Policy Period")
        lbl.setObjectName("FieldLabel")
        period = QWidget()
        pl = QHBoxLayout(period)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.setSpacing(6)
        from_lbl = QLabel("From")
        from_lbl.setObjectName("FieldLabel")
        from_inp = QLineEdit()
        from_inp.setObjectName("FieldInput")
        from_inp.setPlaceholderText("MM/DD/YYYY")
        from_inp.setMaximumWidth(115)
        v_eff = _val(self._state, eff_tag)
        if v_eff:
            from_inp.setText(v_eff)
        to_lbl = QLabel("To")
        to_lbl.setObjectName("FieldLabel")
        to_inp = QLineEdit()
        to_inp.setObjectName("FieldInput")
        to_inp.setPlaceholderText("MM/DD/YYYY")
        to_inp.setMaximumWidth(115)
        v_exp = _val(self._state, exp_tag)
        if v_exp:
            to_inp.setText(v_exp)
        pl.addWidget(from_lbl)
        pl.addWidget(from_inp)
        pl.addWidget(to_lbl)
        pl.addWidget(to_inp)
        pl.addStretch(1)

        def _eff(t: str = eff_tag, w: QLineEdit = from_inp) -> None:
            self.field_changed.emit(t, w.text().strip() or None)

        def _exp(t: str = exp_tag, w: QLineEdit = to_inp) -> None:
            self.field_changed.emit(t, w.text().strip() or None)

        from_inp.editingFinished.connect(_eff)
        to_inp.editingFinished.connect(_exp)
        self._inputs[eff_tag] = from_inp
        self._inputs[exp_tag] = to_inp
        gc = col * 2
        grid.addWidget(lbl, row, gc, Qt.AlignmentFlag.AlignTop)
        grid.addWidget(period, row, gc + 1)

    def _table(self, cols: int, headers: list[str], *,
               editable: bool = True) -> QTableWidget:
        """Create a table. Editable by default — operators need to fix
        extracted values before EPIC entry. Pass ``editable=False`` only
        for genuinely read-only tables."""
        t = QTableWidget(0, cols)
        t.setObjectName("SectionTable")
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.AnyKeyPressed
            if editable else QTableWidget.EditTrigger.NoEditTriggers
        )
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        return t

    def _wire_table_edits(
        self,
        table: QTableWidget,
        group: str,
        tags: list[str | None],
        *,
        offset: int = 0,
    ) -> None:
        """Connect ``cellChanged`` so edits propagate to the host's state.

        Edits are emitted via ``field_changed`` using a special encoding
        ``"__rep:<group>:<index>:<tag>"`` that the host parses and routes
        to the repeatable-update path. Programmatic ``setItem`` calls
        during refresh are filtered with a guard flag.

        :param offset: Index column offset (1 if the first column is a
            row-number, 0 otherwise).
        """
        def on_cell_changed(row: int, col: int) -> None:
            # Skip events generated by our own _populate_row.
            if getattr(table, "_iga_block", False):
                return
            tag_idx = col - offset
            if tag_idx < 0 or tag_idx >= len(tags):
                return
            tag = tags[tag_idx]
            if tag is None:
                return
            item = table.item(row, col)
            value = item.text().strip() if item else ""
            self.field_changed.emit(
                f"__rep:{group}:{row}:{tag}", value or None
            )
        try:
            table.cellChanged.disconnect()
        except (RuntimeError, TypeError):
            pass
        table.cellChanged.connect(on_cell_changed)

        # Right-click → "Delete Row" context menu.
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        try:
            table.customContextMenuRequested.disconnect()
        except (RuntimeError, TypeError):
            pass

        def on_context_menu(pos, _tbl=table, _g=group) -> None:
            index = _tbl.indexAt(pos)
            if not index.isValid():
                return
            menu = QMenu(_tbl)
            # If the cell has conflicts, offer "Use: <value>" items first.
            resolve_actions: list[tuple] = []  # (QAction, tag, value)
            cell_item = _tbl.item(index.row(), index.column())
            user_data = cell_item.data(Qt.ItemDataRole.UserRole) if cell_item else None
            if isinstance(user_data, dict):
                cfls = user_data.get("conflicts") or []
                tag = user_data.get("tag")
                if cfls and tag:
                    for cf in cfls:
                        if not isinstance(cf, dict):
                            continue
                        cf_val = cf.get("value") or ""
                        cf_doc = (cf.get("source") or {}).get("doc_id", "?")
                        cf_conf = cf.get("confidence", 0)
                        lbl = f'Use: "{cf_val}"  ({cf_doc}, {cf_conf:.0%})'
                        act = menu.addAction(lbl)
                        resolve_actions.append((act, tag, cf_val))
                    menu.addSeparator()
            del_act = menu.addAction("Delete Row")
            chosen = menu.exec(_tbl.viewport().mapToGlobal(pos))
            if chosen is del_act:
                self.field_changed.emit(f"__del:{_g}:{index.row()}", None)
            elif chosen is not None:
                for act, tag, val in resolve_actions:
                    if chosen is act:
                        self.field_changed.emit(f"__rep:{_g}:{index.row()}:{tag}", val)
                        # Update the cell appearance immediately without
                        # waiting for a full state reload.
                        _tbl._iga_block = True
                        cell_item.setText(val)
                        cell_item.setBackground(QColor("white"))
                        cell_item.setForeground(QColor("#1e293b"))
                        cell_item.setToolTip("")
                        cell_item.setData(Qt.ItemDataRole.UserRole, None)
                        _tbl._iga_block = False
                        break

        table.customContextMenuRequested.connect(on_context_menu)

    def _add_row_btn(self, label: str, group: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setObjectName("AddRowBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(
            lambda _=False, g=group: self.field_changed.emit(f"__add:{g}", None)
        )
        return btn

    def _section_row(self, title: str, add_label: str = "",
                     group: str = "") -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(self._hdr(title))
        row.addStretch(1)
        if add_label and group:
            row.addWidget(self._add_row_btn(add_label, group))
        return row

    def _autofit_columns(self, table: QTableWidget) -> None:
        """Resize all columns to fit content, then switch to Interactive so
        the user can further adjust.  Safe to call on empty tables — Qt uses
        the header text as the minimum.
        Switch all columns to Interactive *before* resizeColumnsToContents()
        so Qt doesn't skip Stretch-managed columns."""
        hdr = table.horizontalHeader()
        for c in range(table.columnCount()):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        table.resizeColumnsToContents()

    def _populate_row(self, table: QTableWidget, row: int, tags: list[str | None],
                      item: dict, *, row_num: bool = False) -> None:
        # Block cellChanged events during programmatic population so we
        # don't spuriously emit edit events back to the host.
        prev_block = getattr(table, "_iga_block", False)
        table._iga_block = True
        try:
            offset = 0
            if row_num:
                cell = QTableWidgetItem(str(row + 1))
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, 0, cell)
                offset = 1
            for c, tag in enumerate(tags):
                v = ""
                bg: QColor | None = None
                fg: QColor | None = None
                tip: str = ""
                user_data: dict | None = None
                if tag is not None:
                    rec = item.get(tag)
                    if isinstance(rec, dict):
                        v = str(rec.get("value") or "")
                        cfls = rec.get("conflicts") or []
                        if cfls:
                            # Conflict takes priority over confidence tinting.
                            bg = QColor("#fff7ed")
                            fg = QColor("#c2410c")
                            user_data = {"tag": tag, "conflicts": cfls}
                            src0 = (rec.get("source") or [{}])[0]
                            src_doc = src0.get("doc_id", "?") if isinstance(src0, dict) else "?"
                            lines: list[str] = []
                            for cf in cfls:
                                if not isinstance(cf, dict):
                                    continue
                                cf_doc = (cf.get("source") or {}).get("doc_id", "?")
                                lines.append(
                                    f"• {cf.get('value')!r}  "
                                    f"({cf_doc}, {cf.get('confidence', 0):.0%})"
                                )
                            tip = (
                                f"Current: {rec.get('value')!r}  (from {src_doc})\n\n"
                                f"Also extracted:\n" + "\n".join(lines)
                            )
                        else:
                            conf = float(rec.get("confidence") or 0.0)
                            status = str(rec.get("status") or "")
                            tint = confidence_color(conf, status)
                            if tint is not None:
                                bg = tint
                                src0 = (rec.get("source") or [{}])[0]
                                src_doc = src0.get("doc_id", "?") if isinstance(src0, dict) else "?"
                                label = "Low confidence" if conf < 0.6 else "Moderate confidence"
                                tip = f"{label}: {conf:.0%}  (from {src_doc})"
                    else:
                        v = str(rec) if rec is not None else ""
                cell = QTableWidgetItem(v)
                if bg is not None:
                    cell.setBackground(bg)
                if fg is not None:
                    cell.setForeground(fg)
                if user_data is not None:
                    cell.setData(Qt.ItemDataRole.UserRole, user_data)
                if tip:
                    cell.setToolTip(tip)
                table.setItem(row, c + offset, cell)
        finally:
            table._iga_block = prev_block

    # -- Shared section helpers (append HR + header + return table) ----------

    def _add_cov_table(self, lob_namespace: str) -> QTableWidget:
        """Append the Additional Coverages subsection; returns the table.

        :param lob_namespace: ``policy.<lob>`` — the repeatable group is
            ``f"{lob_namespace}.additional_coverage"``.

        Captures endorsement-style coverages (EPLI, EBL, Equipment Breakdown,
        Hired/Non-Owned Auto, etc.) — distinct from `policy_form` which
        tracks attached form numbers without their limit detail.
        """
        self._cov_group = f"{lob_namespace}.additional_coverage"
        # Code intentionally excluded from UI — Claude tends to confuse it
        # with form_number, and the dec page is the authoritative source.
        # The tag still exists in state.json if Claude emits it.
        self._cov_tags: list[str | None] = [
            f"{self._cov_group}.name",
            f"{self._cov_group}.each_claim_limit",
            f"{self._cov_group}.aggregate_limit",
            f"{self._cov_group}.deductible",
            f"{self._cov_group}.deductible_type",
            f"{self._cov_group}.retroactive_date",
            f"{self._cov_group}.form_number",
        ]
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row(
                "Additional Coverages", "+ Add Coverage", self._cov_group
            )
        )
        tbl = self._table(
            7,
            ["Coverage Name", "Each Claim", "Aggregate",
             "Deductible", "Ded Type", "Retro Date", "Form #"],
        )
        # Ensure all columns are interactively resizable. Setting Interactive
        # mode on every column means the user can drag any divider to resize.
        # The first column is wide by default but stays movable.
        header = tbl.horizontalHeader()
        for c in range(7):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        tbl.setColumnWidth(0, 240)
        tbl.setColumnWidth(1, 100)
        tbl.setColumnWidth(2, 100)
        tbl.setColumnWidth(3, 100)
        tbl.setColumnWidth(4, 110)
        tbl.setColumnWidth(5, 100)
        tbl.setColumnWidth(6, 130)
        tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(tbl, self._cov_group, self._cov_tags)
        return tbl

    def _refresh_cov_table(self, state: dict | None) -> None:
        if not hasattr(self, "_add_cov_tbl"):
            return
        items = _rep(state, getattr(self, "_cov_group", ""))
        self._add_cov_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._add_cov_tbl, r, self._cov_tags, item)
        self._autofit_columns(self._add_cov_tbl)

    def _add_ai_section(
        self,
        lob_namespace: str,
        *,
        subject_ref_label: str | None = None,
        subject_ref_leaf: str | None = None,
    ) -> QTableWidget:
        """Append an Additional Interests subsection; returns the table.

        :param lob_namespace: ``policy.<lob>`` — the repeatable group is
            ``f"{lob_namespace}.additional_interest"``.
        :param subject_ref_label: optional column header for an LOB-specific
            subject reference (e.g., 'Veh #' for Auto, 'Item #' for IM,
            'Subject #' for Property). Inserted before the Interest column.
        :param subject_ref_leaf: leaf tag for the subject reference field.
            Required when ``subject_ref_label`` is set.
        """
        self._ai_group = f"{lob_namespace}.additional_interest"
        cols: list[tuple[str, str]] = [
            ("Type",     f"{self._ai_group}.interest"),
            ("Name",     f"{self._ai_group}.name"),
            ("Address",  f"{self._ai_group}.primary_address.line_1"),
        ]
        if subject_ref_label and subject_ref_leaf:
            cols.append((subject_ref_label, f"{self._ai_group}.{subject_ref_leaf}"))
        cols.append(("Interest", f"{self._ai_group}.reason_for_int"))

        self._ai_tags = [t for _lbl, t in cols]
        headers = [lbl for lbl, _t in cols]

        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row(
                "Additional Interests", "+ Add Interest", self._ai_group
            )
        )
        tbl = self._table(len(cols), headers)
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tbl.setColumnWidth(0, 130)
        tbl.setColumnWidth(len(cols) - 1, 180)
        if subject_ref_label and subject_ref_leaf:
            tbl.setColumnWidth(3, 70)
        tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(tbl, self._ai_group, self._ai_tags)
        return tbl

    def _add_forms_section(self, lob_namespace: str) -> QTableWidget:
        """Append a Policy Forms & Endorsements subsection; returns the table.

        :param lob_namespace: ``policy.<lob>`` — the parent coverage's
            namespace. The repeatable group is
            ``f"{lob_namespace}.policy_form"``.
        """
        self._forms_group = f"{lob_namespace}.policy_form"
        self._forms_tags = [
            f"{self._forms_group}.number",
            f"{self._forms_group}.name",
            f"{self._forms_group}.edition",
        ]
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row(
                "Policy Forms & Endorsements", "+ Add Form", self._forms_group
            )
        )
        tbl = self._table(3, ["Form Number", "Form Name", "Edition Date"])
        tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        tbl.setColumnWidth(0, 120)
        tbl.setColumnWidth(2, 110)
        tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(tbl, self._forms_group, self._forms_tags)
        return tbl

    def _refresh_ai_table(self, state: dict | None) -> None:
        if not hasattr(self, "_ai_tbl"):
            return
        items = _rep(state, getattr(self, "_ai_group", ""))
        self._ai_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._ai_tbl, r, self._ai_tags, item)
        self._autofit_columns(self._ai_tbl)

    def _refresh_forms_table(self, state: dict | None) -> None:
        if not hasattr(self, "_forms_tbl"):
            return
        items = _rep(state, getattr(self, "_forms_group", ""))
        # Try both '.edition' and '.edition_date' since EPIC scrapes vary.
        # If first row has neither, fall back to the alternate.
        tags = list(self._forms_tags)
        if items and isinstance(items[0], dict):
            edition_tag = tags[2]
            alt_tag = edition_tag.replace(".edition", ".edition_date") \
                if edition_tag.endswith(".edition") else edition_tag.replace(".edition_date", ".edition")
            if edition_tag not in items[0] and alt_tag in items[0]:
                tags[2] = alt_tag
        self._forms_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._forms_tbl, r, tags, item)
        self._autofit_columns(self._forms_tbl)

    # -- Public API ----------------------------------------------------------

    def refresh(self, state: dict | None) -> None:
        self._state = state
        state_fields = (state.get("fields") or {}) if state else {}
        for tag, widget in self._inputs.items():
            v = _val(state, tag)
            widget.blockSignals(True)
            if isinstance(widget, QLineEdit):
                widget.setText(v)
                rec = state_fields.get(tag)
                cfls = (rec.get("conflicts") or []) if isinstance(rec, dict) else []
                if cfls:
                    widget.setStyleSheet(
                        "QLineEdit#FieldInput { border: 1px solid #f97316;"
                        " background: #fff7ed; }"
                    )
                    lines = [
                        f"• {cf.get('value')!r}  "
                        f"(from {(cf.get('source') or {}).get('doc_id', '?')})"
                        for cf in cfls if isinstance(cf, dict)
                    ]
                    widget.setToolTip("Conflict — also extracted:\n" + "\n".join(lines))
                else:
                    widget.setStyleSheet("")
                    widget.setToolTip("")
            elif isinstance(widget, QComboBox):
                idx = widget.findText(v) if v else 0
                widget.setCurrentIndex(max(idx, 0))
            elif isinstance(widget, _SymbolWidget):
                widget.setText(v)
            widget.blockSignals(False)
        self._refresh_tables(state)


# ---------------------------------------------------------------------------
# Reusable sub-widgets
# ---------------------------------------------------------------------------


_CB_STYLE = (
    "QCheckBox { font-size: 11px; spacing: 4px; color: #374151; }"
    "QCheckBox::indicator {"
    "  width: 15px; height: 15px;"
    "  border: 1.5px solid #94a3b8;"
    "  border-radius: 3px;"
    "  background: white;"
    "}"
    "QCheckBox::indicator:hover { border-color: #2563eb; }"
    "QCheckBox::indicator:checked {"
    "  background: #2563eb;"
    "  border-color: #2563eb;"
    "}"
)


class _SymbolWidget(QWidget):
    """3×3 checkbox grid for EPIC auto coverage symbols 1–9, plus an
    'Other' checkbox with a free-text field for non-standard symbols.

    Mimics the QLineEdit interface (``setText`` / ``text``) so the base-class
    ``refresh()`` can update it like any other input widget.
    """

    _SYMBOLS = ["1", "2", "3", "4", "5", "6", "7", "8", "9"]

    def __init__(self, tag: str, emit_cb, parent=None) -> None:
        super().__init__(parent)
        self._tag = tag
        self._emit_cb = emit_cb

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # 3×3 grid for standard symbols.
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        self._boxes: dict[str, QCheckBox] = {}
        for i, s in enumerate(self._SYMBOLS):
            cb = QCheckBox(s)
            cb.setStyleSheet(_CB_STYLE)
            self._boxes[s] = cb
            grid.addWidget(cb, i // 3, i % 3)
            cb.stateChanged.connect(self._on_change)
        outer.addWidget(grid_w)

        # "Other" row — checkbox + free-text field.
        other_row = QHBoxLayout()
        other_row.setContentsMargins(0, 0, 0, 0)
        other_row.setSpacing(6)
        self._other_cb = QCheckBox("Other")
        self._other_cb.setStyleSheet(_CB_STYLE)
        self._other_inp = QLineEdit()
        self._other_inp.setObjectName("FieldInput")
        self._other_inp.setPlaceholderText("custom symbols")
        self._other_inp.setMaximumWidth(110)
        self._other_inp.setEnabled(False)
        self._other_cb.stateChanged.connect(
            lambda s: self._other_inp.setEnabled(bool(s))
        )
        self._other_cb.stateChanged.connect(self._on_change)
        self._other_inp.editingFinished.connect(self._on_change)
        other_row.addWidget(self._other_cb)
        other_row.addWidget(self._other_inp)
        other_row.addStretch(1)
        outer.addLayout(other_row)

    def _on_change(self) -> None:
        parts = [s for s in self._SYMBOLS if self._boxes[s].isChecked()]
        if self._other_cb.isChecked():
            extra = self._other_inp.text().strip()
            if extra:
                parts.append(extra)
        self._emit_cb(self._tag, ",".join(parts) if parts else None)

    def setText(self, v: str) -> None:
        tokens = [s.strip() for s in v.split(",")] if v else []
        standard = set(self._SYMBOLS)
        other_vals = [t for t in tokens if t not in standard]
        for s, cb in self._boxes.items():
            cb.blockSignals(True)
            cb.setChecked(s in tokens)
            cb.blockSignals(False)
        self._other_cb.blockSignals(True)
        self._other_inp.blockSignals(True)
        if other_vals:
            self._other_cb.setChecked(True)
            self._other_inp.setEnabled(True)
            self._other_inp.setText(",".join(other_vals))
        else:
            self._other_cb.setChecked(False)
            self._other_inp.setEnabled(False)
            self._other_inp.setText("")
        self._other_cb.blockSignals(False)
        self._other_inp.blockSignals(False)

    def text(self) -> str:
        parts = [s for s in self._SYMBOLS if self._boxes[s].isChecked()]
        if self._other_cb.isChecked():
            extra = self._other_inp.text().strip()
            if extra:
                parts.append(extra)
        return ",".join(parts)


# ---------------------------------------------------------------------------
# Section forms
# ---------------------------------------------------------------------------


class NamedInsuredsForm(SectionFormBase):
    """Path B: a single repeatable group ``account.named_insured.*`` with a
    ``type`` discriminator. Columns are resolved from
    :data:`section_forms_layout.NAMED_INSUREDS_COLUMNS` via the Field Map.
    """

    REPEATABLE_GROUP = "account.named_insured"

    def _build_form(self) -> None:
        self._root.addLayout(
            self._section_row(
                "Named Insureds", "+ Add Named Insured", self.REPEATABLE_GROUP
            )
        )
        cols = layout.NAMED_INSUREDS_COLUMNS
        self._ni_tbl = self._table(len(cols), [c.label for c in cols])
        # Stretch columns 0 (Entity Name) and 4 (Address) for the wide bits.
        self._ni_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._ni_tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._ni_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._root.addWidget(self._ni_tbl, 1)
        # Cache resolved tags so refresh doesn't re-query the Field Map per row.
        self._ni_tags = _resolve_column_tags(cols)
        self._wire_table_edits(self._ni_tbl, self.REPEATABLE_GROUP, self._ni_tags)
        self._refresh_tables(self._state)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_ni_tbl"):
            return
        # Falls back to scanning singleton fields when Claude emitted only
        # one named insured as flat tags (typical case on a single-NI policy).
        items = _rep_or_singleton_row(state, self.REPEATABLE_GROUP)
        self._ni_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._ni_tbl, r, self._ni_tags, item)
        self._autofit_columns(self._ni_tbl)


class LocationsForm(SectionFormBase):
    """Locations table — columns sourced from the Field Map. Falls back to
    scanning singleton `location.*` fields when the repeatable group is empty
    (typical when only one location was extracted)."""

    REPEATABLE_GROUP = "location"

    def _build_form(self) -> None:
        self._root.addLayout(
            self._section_row("Locations", "+ Add Location", self.REPEATABLE_GROUP)
        )
        cols = layout.LOCATIONS_COLUMNS
        self._loc_tbl = self._table(len(cols), [c.label for c in cols])
        # Stretch the description column.
        self._loc_tbl.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._loc_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._root.addWidget(self._loc_tbl, 1)
        self._loc_tags = _resolve_column_tags(cols)
        self._wire_table_edits(self._loc_tbl, self.REPEATABLE_GROUP, self._loc_tags)
        self._refresh_tables(self._state)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_loc_tbl"):
            return
        items = _rep_or_singleton_row(state, self.REPEATABLE_GROUP)

        # Deduplicate: same address + same bldg# extracted from multiple PDFs
        # should collapse to one row. Keep the item with the most non-null fields.
        # Rows with the same address but different bldg# are distinct buildings —
        # do NOT merge them.
        _addr_tag = self._loc_tags[2] if len(self._loc_tags) > 2 else None
        _bldg_tag = self._loc_tags[1] if len(self._loc_tags) > 1 else None
        _seen: dict[tuple[str, str], int] = {}   # key → index in deduped
        _deduped: list[dict] = []
        for item in items:
            addr_v = ""
            bldg_v = ""
            if _addr_tag:
                r = item.get(_addr_tag)
                addr_v = str(r.get("value") or "") if isinstance(r, dict) else ""
            if _bldg_tag:
                r = item.get(_bldg_tag)
                bldg_v = str(r.get("value") or "") if isinstance(r, dict) else ""
            key = (addr_v.lower().strip(), bldg_v.lower().strip())
            if key == ("", ""):
                _deduped.append(item)
            elif key in _seen:
                # Keep whichever has more non-null values.
                existing = _deduped[_seen[key]]
                if sum(1 for v in item.values() if isinstance(v, dict) and v.get("value")) \
                        > sum(1 for v in existing.values() if isinstance(v, dict) and v.get("value")):
                    _deduped[_seen[key]] = item
            else:
                _seen[key] = len(_deduped)
                _deduped.append(item)
        items = _deduped

        # Fallback: if there's exactly one location and Loc#/Bldg# weren't
        # extracted, default to "1" / "1". Most dec pages with a single
        # location omit explicit numbering since it's implicit.
        if len(items) == 1:
            item = items[0]
            loc_tag = self._loc_tags[0] if self._loc_tags else None
            bldg_tag = self._loc_tags[1] if len(self._loc_tags) > 1 else None
            for default_tag, default_val in (
                (loc_tag, "1"),
                (bldg_tag, "1"),
            ):
                if default_tag and default_tag not in item:
                    item[default_tag] = {"value": default_val, "status": "defaulted"}
        self._loc_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._loc_tbl, r, self._loc_tags, item)
        self._autofit_columns(self._loc_tbl)


class GeneralLiabilityForm(SectionFormBase):
    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("General Liability"))
        # Resolve all GL singleton tags upfront so the rest of the build is
        # mechanical lookups.
        cov = "General Liability > Coverages"
        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Limits"))
        lg = self._grid(2)
        self._add_text(lg, 0, 0, "General Aggregate",
                       _resolve_tag(cov, "streGenAggrAppLimit"), "$")
        self._add_text(lg, 0, 1, "Products – Comp/Ops Aggregate",
                       _resolve_tag(cov, "streProdOperLimit"), "$")
        self._add_text(lg, 1, 0, "Personal & Advertising Injury",
                       _resolve_tag(cov, "strePersAdvInjLimit"), "$")
        self._add_text(lg, 1, 1, "Each Occurrence Limit",
                       _resolve_tag(cov, "streEachOccLimit"), "$")
        self._add_text(lg, 2, 0, "Damage to Premises Rented",
                       _resolve_tag(cov, "streDamPremLimit"), "$")
        self._add_text(lg, 2, 1, "Medical Payments Limit",
                       _resolve_tag(cov, "streMedLimit"), "$")
        self._add_text(lg, 3, 0, "BI Deductible",
                       _resolve_tag(cov, "streBIDed"), "$")
        self._add_text(lg, 3, 1, "PD Deductible",
                       _resolve_tag(cov, "strePDDed"), "$")
        self._root.addLayout(lg)

        # "Aggregate Applies To" — three plain QCheckBoxes. Use OS-default
        # styling so the indicator square is clearly visible. Custom
        # checkbox::indicator QSS in the past hid the square; native style
        # is the most reliable way to render an obviously-clickable checkbox.
        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Aggregate Applies To"))
        agg_row = QHBoxLayout()
        agg_row.setContentsMargins(0, 4, 0, 8)
        agg_row.setSpacing(28)
        self._agg_checkboxes: dict[str, QCheckBox] = {}
        for label_text, ep_field, fallback_tag in (
            ("Policy",   "GLCOVERchkAppPolicy",   "policy.gl.aggregate_applies_to.policy"),
            ("Location", "GLCOVERchkAppLocation", "policy.gl.aggregate_applies_to.location"),
            ("Project",  "GLCOVERchkAppProject",  "policy.gl.aggregate_applies_to.project"),
        ):
            tag = _resolve_tag(cov, ep_field) or fallback_tag
            cb = QCheckBox(label_text)
            # Explicit checkbox indicator styling — the form's parent QSS
            # otherwise renders some indicators invisibly on Windows. We
            # draw an unambiguous bordered square that fills with blue
            # when checked, so unchecked state is always visible.
            cb.setStyleSheet("""
                QCheckBox { font-size:13px; spacing:8px; padding:4px 0; }
                QCheckBox::indicator {
                    width:18px; height:18px;
                    border:2px solid #94a3b8;
                    border-radius:3px;
                    background:white;
                }
                QCheckBox::indicator:hover {
                    border-color:#2563eb;
                }
                QCheckBox::indicator:checked {
                    border-color:#2563eb;
                    background:#2563eb;
                    image:url(none);
                }
            """)
            v = _val(self._state, tag)
            if v and v.lower() in {"true", "yes", "1", "checked", "on"}:
                cb.setChecked(True)

            def _on_toggle(checked: bool, t: str = tag) -> None:
                self.field_changed.emit(t, "Yes" if checked else "No")

            cb.toggled.connect(_on_toggle)
            self._inputs[tag] = cb
            self._agg_checkboxes[tag] = cb
            agg_row.addWidget(cb)
        agg_row.addStretch(1)
        agg_row_w = QWidget()
        agg_row_w.setLayout(agg_row)
        self._root.addWidget(agg_row_w)

        # ------------------------------------------------------------------
        # Employee Benefits Liability — dedicated sub-section
        # ------------------------------------------------------------------
        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Employee Benefits Liability"))
        ebl = self._grid(2)
        # Each-claim limit comes from streEmpBenLimit on Coverages screen;
        # the EBL retro date and deductible live on the Claims-Made/EBL screen.
        self._add_text(ebl, 0, 0, "Each Claim Limit",
                       _resolve_tag(cov, "streEmpBenLimit"), "$")
        # Aggregate limit isn't on a single EPIC field today — we surface
        # it as a synthetic singleton so operators can enter it and so
        # extraction can populate it when present on the dec page.
        self._add_text(ebl, 0, 1, "Aggregate Limit",
                       "policy.gl.ebl.aggregate_limit", "$")
        ebl_screen = "General Liability > ClaimsMadeEmployeeBenefits"
        self._add_text(ebl, 1, 0, "Deductible (Each Claim)",
                       _resolve_tag(ebl_screen, "streDeductiblePerClaim"), "$")
        self._add_text(ebl, 1, 1, "Retroactive Date",
                       _resolve_tag(ebl_screen, "dteRetroactiveDate"),
                       "MM/DD/YYYY")
        self._add_text(ebl, 2, 0, "Number of Employees",
                       _resolve_tag(ebl_screen, "inteNumberOfEmployees"))
        self._add_text(ebl, 2, 1, "Employees Covered",
                       _resolve_tag(ebl_screen, "inteNumberOfEmployeesCovered"))
        self._root.addLayout(ebl)

        self._haz_group = "policy.gl.hazard"
        self._haz_tags: list[str | None] = [
            f"{self._haz_group}.location_number",
            f"{self._haz_group}.building_number",
            f"{self._haz_group}.class_code",
            f"{self._haz_group}.classification",
            f"{self._haz_group}.premium_basis",
            f"{self._haz_group}.exposure",
        ]
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Hazard Schedule", "+ Add Hazard", self._haz_group)
        )
        self._haz_tbl = self._table(
            6, ["Loc #", "Bldg #", "Class Code", "Description",
                "Exposure Basis", "Exposure Amount"],
        )
        self._haz_tbl.setColumnWidth(0, 60)
        self._haz_tbl.setColumnWidth(1, 60)
        self._haz_tbl.setColumnWidth(2, 90)
        self._haz_tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._haz_tbl.setColumnWidth(4, 120)
        self._haz_tbl.setColumnWidth(5, 120)
        self._haz_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._haz_tbl, self._haz_group, self._haz_tags)
        self._root.addWidget(self._haz_tbl, 1)

        # Order requested: Hazard → Forms → Additional Coverages → Additional Interests.
        self._forms_tbl = self._add_forms_section("policy.gl")
        self._root.addWidget(self._forms_tbl)
        self._add_cov_tbl = self._add_cov_table("policy.gl")
        self._root.addWidget(self._add_cov_tbl)
        self._ai_tbl = self._add_ai_section(
            "policy.gl",
            subject_ref_label="Loc #",
            subject_ref_leaf="location_number",
        )
        self._root.addWidget(self._ai_tbl)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_haz_tbl"):
            return
        items = _rep(state, self._haz_group)
        self._haz_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._haz_tbl, r, self._haz_tags, item)
        self._autofit_columns(self._haz_tbl)
        self._refresh_ai_table(state)
        self._refresh_forms_table(state)
        self._refresh_cov_table(state)


class PropertyForm(SectionFormBase):
    """Property — coverage subjects are repeatable in EPIC. Each subject row
    is one (location, building, coverage type) tuple with its own limit,
    valuation, form, and description. The GUI surfaces the canonical
    `policy.property.subject.*` repeatable directly so what extraction
    captures is exactly what the operator reviews and what entry will write.
    """

    REPEATABLE_GROUP = "policy.property.subject"

    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("Property"))
        g = self._grid(2)
        # Policy-level singletons. Coinsurance is verified;
        # `policy.property.valuation` is the policy-default valuation method.
        self._add_text(g, 0, 0, "Coinsurance", "policy.property.coinsurance", "%")
        self._add_text(g, 0, 1, "Default Valuation",
                       "policy.property.valuation", "ACV / RC / Other")
        self._root.addLayout(g)

        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Coverage Subjects",
                              "+ Add Subject", self.REPEATABLE_GROUP)
        )
        self._prop_tags: list[str | None] = [
            f"{self.REPEATABLE_GROUP}.location_number",
            f"{self.REPEATABLE_GROUP}.building_number",
            f"{self.REPEATABLE_GROUP}.subject",
            f"{self.REPEATABLE_GROUP}.amount",
            f"{self.REPEATABLE_GROUP}.valuation1",
            f"{self.REPEATABLE_GROUP}.form_number",
            f"{self.REPEATABLE_GROUP}.description",
        ]
        self._prop_tbl = self._table(
            7, ["Loc #", "Bldg #", "Coverage", "Limit",
                "Valuation", "Form #", "Description"],
            editable=True,
        )
        header = self._prop_tbl.horizontalHeader()
        for c in range(7):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        self._prop_tbl.setColumnWidth(0, 50)
        self._prop_tbl.setColumnWidth(1, 55)
        self._prop_tbl.setColumnWidth(2, 130)
        self._prop_tbl.setColumnWidth(3, 110)
        self._prop_tbl.setColumnWidth(4, 90)
        self._prop_tbl.setColumnWidth(5, 110)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._prop_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._prop_tbl, self.REPEATABLE_GROUP, self._prop_tags)
        self._root.addWidget(self._prop_tbl, 1)

        # Order: schedule → Forms → Additional Coverages → Additional Interests.
        self._forms_tbl = self._add_forms_section("policy.property")
        self._root.addWidget(self._forms_tbl)
        self._add_cov_tbl = self._add_cov_table("policy.property")
        self._root.addWidget(self._add_cov_tbl)
        self._ai_tbl = self._add_ai_section(
            "policy.property",
            subject_ref_label="Loc #",
            subject_ref_leaf="location_number",
        )
        self._root.addWidget(self._ai_tbl)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_prop_tbl"):
            return
        items = _rep(state, self.REPEATABLE_GROUP)
        self._prop_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._prop_tbl, r, self._prop_tags, item)
        self._autofit_columns(self._prop_tbl)
        self._refresh_ai_table(state)
        self._refresh_forms_table(state)
        self._refresh_cov_table(state)


class BusinessAutoForm(SectionFormBase):
    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("Business Auto"))
        # Policy Number / Period / premiums intentionally omitted per UX request.

        cov = "Business Auto > CoverageTN"

        # Grid layout mirroring the EPIC coverage form:
        #   Col 0: coverage name (bold, spans all limit rows for that coverage)
        #   Col 1: "Sym" header (first limit row only)
        #   Col 2: symbol input (spans all limit rows, narrow)
        #   Col 3: limit type label (one per limit row)
        #   Col 4: limit amount input (one per limit row)
        #   Col 5: trailing filler
        self._root.addWidget(self._hdr("Coverages"))
        ba_grid = QGridLayout()
        ba_grid.setContentsMargins(0, 4, 0, 4)
        ba_grid.setHorizontalSpacing(12)
        ba_grid.setVerticalSpacing(4)
        ba_grid.setColumnMinimumWidth(0, 180)
        ba_grid.setColumnMinimumWidth(2, 70)
        ba_grid.setColumnMinimumWidth(3, 160)
        ba_grid.setColumnStretch(5, 1)

        _gr = [0]  # mutable grid-row counter

        def _row(
            name: str,
            symbol_field: str,
            limit_pairs: list[tuple[str, str]],
        ) -> None:
            start = _gr[0]
            n = len(limit_pairs)

            # Coverage name spans all limit sub-rows.
            cov_lbl = QLabel(name)
            cov_lbl.setStyleSheet("font-weight: bold; font-size: 12px; color: #0f172a;")
            ba_grid.addWidget(cov_lbl, start, 0, n, 1,
                              Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

            # "Sym" header label — first row only.
            sym_hdr = QLabel("Sym")
            sym_hdr.setStyleSheet("color: #94a3b8; font-size: 10px;")
            ba_grid.addWidget(sym_hdr, start, 1, Qt.AlignmentFlag.AlignBottom)

            # Symbol checkbox grid spans all limit sub-rows.
            tag = _resolve_tag(cov, symbol_field)
            sym_w = _SymbolWidget(tag, self.field_changed.emit)
            sym_v = _val(self._state, tag)
            if sym_v:
                sym_w.setText(sym_v)
            self._inputs[tag] = sym_w
            ba_grid.addWidget(sym_w, start, 2, n, 1, Qt.AlignmentFlag.AlignTop)

            # One grid row per limit type.
            for i, (lbl_text, field_name) in enumerate(limit_pairs):
                ltag = _resolve_tag(cov, field_name)
                ll = QLabel(lbl_text)
                ll.setStyleSheet("color: #374151; font-size: 12px;")
                ba_grid.addWidget(ll, start + i, 3, Qt.AlignmentFlag.AlignVCenter)

                inp = QLineEdit()
                inp.setObjectName("FieldInput")
                inp.setPlaceholderText("$")
                inp.setMaximumWidth(160)
                v = _val(self._state, ltag)
                if v:
                    inp.setText(v)

                def _ldone(t: str = ltag, w: QLineEdit = inp) -> None:
                    self.field_changed.emit(t, w.text().strip() or None)

                inp.editingFinished.connect(_ldone)
                self._inputs[ltag] = inp
                ba_grid.addWidget(inp, start + i, 4, Qt.AlignmentFlag.AlignVCenter)

            _gr[0] += n

            # Thin separator after each coverage block.
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("background: #e2e8f0; max-height: 1px; border: none;")
            ba_grid.addWidget(sep, _gr[0], 0, 1, 5)
            _gr[0] += 1

        _row("Liability",                "chkLiability1",      [
            ("CSL",                "streLiabilityCSLLimit1"),
            ("BI / Each Person",   "streLiabilityBILimit2"),
            ("BI / Each Accident", "streLiabilityBILimit1"),
            ("Property Damage",    "streLiabilityPDLimit1"),
        ])
        _row("Medical Payments",         "chkMedical2",        [
            ("Limit",              "streMedicalLimit1"),
        ])
        _row("Uninsured / Underinsured", "chkUninsured2",      [
            ("CSL",                "streUninsuredCSLLimit1"),
            ("BI / Each Person",   "streUninsuredBILimit2"),
            ("BI / Each Accident", "streUninsuredBILimit1"),
            ("PD / Each Accident", "streUninsuredPDEachAccident"),
            ("PD Deductible",      "streUninsuredPDDeductible"),
        ])
        _row("Comprehensive",            "chkComprehensive2",  [
            ("Deductible",         "streComprehensiveDeductible1"),
        ])
        _row("Specified Causes of Loss", "chkCauseOfLoss2",    [
            ("Deductible",         "streCauseOfLossDeductible1"),
        ])
        _row("Collision",                "chkCollision2",      [
            ("Deductible",         "streCollisionDeductible1"),
        ])
        _row("Towing & Labor",           "chkTowing3",         [
            ("Limit",              "streTowingLimit1"),
        ])
        _row("Personal Injury Protection", "chkPersonalInjury2", [
            ("Limit",              "strePersonalInjuryLimit1"),
        ])

        ba_wrap = QWidget()
        ba_wrap.setLayout(ba_grid)
        self._root.addWidget(ba_wrap)

        self._veh_group = "policy.auto.vehicle"
        self._veh_tags: list[str | None] = [
            f"{self._veh_group}.year",
            f"{self._veh_group}.make",
            f"{self._veh_group}.model",
            f"{self._veh_group}.vin",
            f"{self._veh_group}.body_type",
            f"{self._veh_group}.garage_address.line_1",
            f"{self._veh_group}.comprehensive_deductible",
            f"{self._veh_group}.collision_deductible",
        ]
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Vehicles Schedule", "+ Add Vehicle", self._veh_group)
        )
        self._veh_tbl = self._table(
            9, ["#", "Year", "Make", "Model", "VIN", "Type",
                "Garaging Address", "Comp Ded", "Coll Ded"]
        )
        self._veh_tbl.setColumnWidth(0, 36)
        self._veh_tbl.setColumnWidth(1, 50)
        self._veh_tbl.setColumnWidth(2, 80)
        self._veh_tbl.setColumnWidth(3, 100)
        self._veh_tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._veh_tbl.setColumnWidth(5, 90)
        self._veh_tbl.setColumnWidth(6, 140)
        self._veh_tbl.setColumnWidth(7, 80)
        self._veh_tbl.setColumnWidth(8, 80)
        self._veh_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._veh_tbl, self._veh_group, self._veh_tags, offset=1)
        self._root.addWidget(self._veh_tbl)

        self._drv_group = "policy.auto.driver"
        self._drv_tags: list[str | None] = [
            f"{self._drv_group}.name",
            f"{self._drv_group}.drivers_license_number",
            f"{self._drv_group}.state",
            f"{self._drv_group}.birth",
            f"{self._drv_group}.driver_type",
        ]
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Driver Schedule", "+ Add Driver", self._drv_group)
        )
        self._drv_tbl = self._table(
            6, ["#", "Driver Name", "License #", "State", "Date of Birth", "Type"]
        )
        self._drv_tbl.setColumnWidth(0, 36)
        self._drv_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._drv_tbl.setColumnWidth(2, 110)
        self._drv_tbl.setColumnWidth(3, 55)
        self._drv_tbl.setColumnWidth(4, 110)
        self._drv_tbl.setColumnWidth(5, 90)
        self._drv_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._drv_tbl, self._drv_group, self._drv_tags, offset=1)
        self._root.addWidget(self._drv_tbl, 1)

        # Order: vehicles → drivers → Forms → Additional Coverages → Additional Interests.
        self._forms_tbl = self._add_forms_section("policy.auto")
        self._root.addWidget(self._forms_tbl)
        self._add_cov_tbl = self._add_cov_table("policy.auto")
        self._root.addWidget(self._add_cov_tbl)
        self._ai_tbl = self._add_ai_section(
            "policy.auto",
            subject_ref_label="Veh #",
            subject_ref_leaf="vehicle_number",
        )
        self._root.addWidget(self._ai_tbl)
        self._refresh_tables(self._state)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_veh_tbl"):
            return
        veh = _rep(state, self._veh_group)
        self._veh_tbl.setRowCount(len(veh))
        for r, item in enumerate(veh):
            self._populate_row(self._veh_tbl, r, self._veh_tags, item, row_num=True)
        self._autofit_columns(self._veh_tbl)

        drv = _rep(state, self._drv_group)
        self._drv_tbl.setRowCount(len(drv))
        for r, item in enumerate(drv):
            self._populate_row(self._drv_tbl, r, self._drv_tags, item, row_num=True)
        self._autofit_columns(self._drv_tbl)
        self._refresh_ai_table(state)
        self._refresh_forms_table(state)
        self._refresh_cov_table(state)


class InlandMarineForm(SectionFormBase):
    """Inland Marine — scheduled and unscheduled item repeatables use the
    canonical ``policy.inland_marine.scheduled_item`` and
    ``policy.inland_marine.unscheduled_item`` namespaces.
    """

    SCHED_GROUP = "policy.inland_marine.scheduled_item"
    UNSCHED_GROUP = "policy.inland_marine.unscheduled_item"

    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("Inland Marine"))
        g = self._grid(2)
        # Policy Number / Period / premiums intentionally omitted per UX request.
        self._add_text(g, 0, 0, "Total Scheduled Amount",
                       "policy.inland_marine.total_scheduled_amount", "$")
        self._add_text(g, 0, 1, "Deductible",
                       "policy.inland_marine.acv_replacement_cost_deductible", "$")
        self._add_text(g, 1, 0, "Coinsurance %",
                       "policy.inland_marine.coins_percent", "%")
        self._root.addLayout(g)

        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Scheduled Items", "+ Add Item", self.SCHED_GROUP)
        )
        self._sched_tags: list[str | None] = [
            f"{self.SCHED_GROUP}.item_number",
            f"{self.SCHED_GROUP}.model_year",
            f"{self.SCHED_GROUP}.manufacturer",
            f"{self.SCHED_GROUP}.model",
            f"{self.SCHED_GROUP}.description",
            f"{self.SCHED_GROUP}.serial_number",
            f"{self.SCHED_GROUP}.deductible",
            f"{self.SCHED_GROUP}.amt_insurance",
        ]
        self._sched_tbl = self._table(
            8, ["Item #", "Year", "Make", "Model", "Item Description", "Serial Number / ID", "Deductible", "Limit"]
        )
        self._sched_tbl.setColumnWidth(0, 55)
        self._sched_tbl.setColumnWidth(1, 50)
        self._sched_tbl.setColumnWidth(2, 120)
        self._sched_tbl.setColumnWidth(3, 100)
        self._sched_tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._sched_tbl.setColumnWidth(5, 155)
        self._sched_tbl.setColumnWidth(6, 90)
        self._sched_tbl.setColumnWidth(7, 90)
        self._sched_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._sched_tbl, self.SCHED_GROUP, self._sched_tags)
        self._root.addWidget(self._sched_tbl)

        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Unscheduled Equipment", "+ Add Equipment",
                              self.UNSCHED_GROUP)
        )
        self._unsched_tags: list[str | None] = [
            f"{self.UNSCHED_GROUP}.description",
            f"{self.UNSCHED_GROUP}.maximum",
            f"{self.UNSCHED_GROUP}.amt_insurance",
            f"{self.UNSCHED_GROUP}.co_insurance",
        ]
        self._unsched_tbl = self._table(
            5, ["#", "Equipment Description", "Per-Item Max", "Total Limit", "Coinsurance"]
        )
        self._unsched_tbl.setColumnWidth(0, 36)
        self._unsched_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._unsched_tbl.setColumnWidth(2, 110)
        self._unsched_tbl.setColumnWidth(3, 110)
        self._unsched_tbl.setColumnWidth(4, 100)
        self._unsched_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._unsched_tbl, self.UNSCHED_GROUP,
                               self._unsched_tags, offset=1)
        self._root.addWidget(self._unsched_tbl, 1)

        # Order: schedules → Forms → Additional Coverages → Additional Interests.
        self._forms_tbl = self._add_forms_section("policy.inland_marine")
        self._root.addWidget(self._forms_tbl)
        self._add_cov_tbl = self._add_cov_table("policy.inland_marine")
        self._root.addWidget(self._add_cov_tbl)
        self._ai_tbl = self._add_ai_section(
            "policy.inland_marine",
            subject_ref_label="Item #",
            subject_ref_leaf="item_number",
        )
        self._root.addWidget(self._ai_tbl)
        self._refresh_tables(self._state)

    @staticmethod
    def _strip_ymm(desc: str, year: str, make: str, model: str) -> str:
        """Strip leading year/make and trailing 'Model <model>' from description."""
        import re
        s = desc.strip()
        # Strip leading "{year} {make} "
        if year and make:
            prefix = re.escape(year.strip()) + r"\s+" + re.escape(make.strip()) + r"\s+"
            s = re.sub(r"^" + prefix, "", s, flags=re.IGNORECASE)
        elif year:
            s = re.sub(r"^" + re.escape(year.strip()) + r"\s+", "", s, flags=re.IGNORECASE)
        elif make:
            s = re.sub(r"^" + re.escape(make.strip()) + r"\s+", "", s, flags=re.IGNORECASE)
        # Strip trailing " Model {model}" or " {model}" if model present
        if model:
            s = re.sub(r"\s+Model\s+" + re.escape(model.strip()) + r"\s*$", "", s, flags=re.IGNORECASE)
            s = re.sub(r"\s+" + re.escape(model.strip()) + r"\s*$", "", s, flags=re.IGNORECASE)
        return s.strip() or desc.strip()

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_sched_tbl"):
            return
        sched = _rep(state, self.SCHED_GROUP)
        self._sched_tbl.setRowCount(len(sched))
        for r, item in enumerate(sched):
            self._populate_row(self._sched_tbl, r, self._sched_tags, item)
            # Post-process description col (index 4): strip year/make/model prefix
            # (autofit applied after all rows processed — see end of loop)
            desc_rec = item.get(f"{self.SCHED_GROUP}.description")
            raw_desc = str(desc_rec.get("value") or "") if isinstance(desc_rec, dict) else ""
            if raw_desc:
                year_rec = item.get(f"{self.SCHED_GROUP}.model_year")
                make_rec = item.get(f"{self.SCHED_GROUP}.manufacturer")
                model_rec = item.get(f"{self.SCHED_GROUP}.model")
                yr = str(year_rec.get("value") or "") if isinstance(year_rec, dict) else ""
                mk = str(make_rec.get("value") or "") if isinstance(make_rec, dict) else ""
                md = str(model_rec.get("value") or "") if isinstance(model_rec, dict) else ""
                clean = self._strip_ymm(raw_desc, yr, mk, md)
                prev = getattr(self._sched_tbl, "_iga_block", False)
                self._sched_tbl._iga_block = True
                self._sched_tbl.setItem(r, 4, QTableWidgetItem(clean))
                self._sched_tbl._iga_block = prev

        self._autofit_columns(self._sched_tbl)

        # Auto-assign item numbers if none were extracted (display-only;
        # persists when the user edits any cell, or on the next extraction).
        _num_tag = f"{self.SCHED_GROUP}.item_number"
        _all_blank = all(
            not (isinstance(item.get(_num_tag), dict) and
                 str(item.get(_num_tag, {}).get("value") or "").strip())
            for item in sched
        )
        if _all_blank and sched:
            self._sched_tbl._iga_block = True
            for r in range(self._sched_tbl.rowCount()):
                self._sched_tbl.setItem(r, 0, QTableWidgetItem(str(r + 1)))
            self._sched_tbl._iga_block = False

        # Compute and display total scheduled amount from limit column.
        import re as _re
        _total = 0.0
        for item in sched:
            _amt_rec = item.get(f"{self.SCHED_GROUP}.amt_insurance")
            if isinstance(_amt_rec, dict):
                _raw = str(_amt_rec.get("value") or "")
                _num = _re.sub(r"[^\d.]", "", _raw)
                try:
                    _total += float(_num)
                except ValueError:
                    pass
        if _total > 0:
            _inp = self._inputs.get("policy.inland_marine.total_scheduled_amount")
            if isinstance(_inp, QLineEdit):
                _inp.blockSignals(True)
                _inp.setText(f"{int(_total):,}")
                _inp.blockSignals(False)

        unsched = _rep(state, self.UNSCHED_GROUP)
        self._unsched_tbl.setRowCount(len(unsched))
        for r, item in enumerate(unsched):
            self._populate_row(self._unsched_tbl, r, self._unsched_tags, item, row_num=True)
        self._autofit_columns(self._unsched_tbl)
        self._refresh_ai_table(state)
        self._refresh_forms_table(state)
        self._refresh_cov_table(state)


class WorkersCompForm(SectionFormBase):
    """Worker's Compensation — no Additional Coverages, no Additional Interests."""

    REPEATABLE_GROUP = "policy.workers_comp.class_code"

    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("Worker's Compensation"))
        g = self._grid(2)
        # Policy Number / Period / premiums intentionally omitted per UX request.
        # Statutory limits is encoded in EPIC as `part1_states` (Part One
        # statutory states list); we surface it as a free-text input so the
        # operator can confirm the state list directly.
        self._add_text(g, 0, 0, "Statutory States (Part One)",
                       "policy.workers_comp.part1_states")
        self._add_text(g, 0, 1, "Deductible",
                       "policy.workers_comp.deductibles", "$")
        self._root.addLayout(g)

        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Employer Liability"))
        el = self._grid(2)
        self._add_text(el, 0, 0, "Each Accident",
                       "policy.workers_comp.each_accident", "$")
        self._add_text(el, 0, 1, "Disease – Policy Limit",
                       "policy.workers_comp.disease_policy_limit", "$")
        self._add_text(el, 1, 0, "Disease – Each Employee",
                       "policy.workers_comp.disease_each_employee", "$")
        self._root.addLayout(el)

        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Experience Rating"))
        exp_g = self._grid(2)
        self._add_text(exp_g, 0, 0, "Experience Mod",
                       "policy.workers_comp.experience_mod", "e.g. 0.87")
        self._root.addLayout(exp_g)

        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Class Codes", "+ Add Class Code", self.REPEATABLE_GROUP)
        )
        # State column included so multi-state policies can be captured.
        self._cc_tags: list[str | None] = [
            f"{self.REPEATABLE_GROUP}.state",
            f"{self.REPEATABLE_GROUP}.class_code",
            f"{self.REPEATABLE_GROUP}.description_code",
            f"{self.REPEATABLE_GROUP}.payroll",
        ]
        self._cc_tbl = self._table(
            5, ["#", "State", "Class Code", "Description", "Payroll"]
        )
        self._cc_tbl.setColumnWidth(0, 36)
        self._cc_tbl.setColumnWidth(1, 55)
        self._cc_tbl.setColumnWidth(2, 90)
        self._cc_tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._cc_tbl.setColumnWidth(4, 120)
        self._cc_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._cc_tbl, self.REPEATABLE_GROUP,
                               self._cc_tags, offset=1)
        self._root.addWidget(self._cc_tbl, 1)

        # Order: class codes → Forms → Additional Coverages.
        self._forms_tbl = self._add_forms_section("policy.workers_comp")
        self._root.addWidget(self._forms_tbl)
        self._add_cov_tbl = self._add_cov_table("policy.workers_comp")
        self._root.addWidget(self._add_cov_tbl)
        self._refresh_tables(self._state)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_cc_tbl"):
            return
        items = _rep(state, self.REPEATABLE_GROUP)
        self._cc_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._cc_tbl, r, self._cc_tags, item, row_num=True)
        self._autofit_columns(self._cc_tbl)
        self._refresh_forms_table(state)
        self._refresh_cov_table(state)


class UmbrellaExcessForm(SectionFormBase):
    """Umbrella / Excess. Each underlying coverage type (Auto, GL, EL) is
    rendered as its own labeled sub-section with all EPIC-mapped fields.
    The ``Other`` coverage type is a repeatable table for additional policies.
    """

    OTHER_GROUP = "policy.umbrella.underlying.other"

    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("Umbrella / Excess"))
        g = self._grid(2)
        self._add_text(g, 0, 0, "Each Occurrence Limit",
                       "policy.umbrella.occurrence_limit", "$")
        self._add_text(g, 0, 1, "Retained Limit (SIR)",
                       "policy.umbrella.retained_limit", "$")
        self._add_text(g, 1, 0, "EBL — Each Claim",
                       "policy.umbrella.insurance_ebl_limit", "$")
        self._add_text(g, 1, 1, "EBL — Aggregate",
                       "policy.umbrella.aggregate_ebl_limit", "$")
        self._root.addLayout(g)

        # ── Underlying Auto ──────────────────────────────────────────────────
        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Underlying — Auto"))
        auto_g = self._grid(2)
        self._add_text(auto_g, 0, 0, "CSL / Occurrence Limit",
                       "policy.umbrella.underlying.auto.csl_acc_limit", "$")
        self._add_text(auto_g, 0, 1, "BI Each Accident",
                       "policy.umbrella.underlying.auto.bi_acc_limit", "$")
        self._add_text(auto_g, 1, 0, "BI Each Person",
                       "policy.umbrella.underlying.auto.bi_pers_limit", "$")
        self._add_text(auto_g, 1, 1, "PD Each Accident",
                       "policy.umbrella.underlying.auto.pd_acc_limit", "$")
        self._root.addLayout(auto_g)

        # ── Underlying GL ────────────────────────────────────────────────────
        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Underlying — General Liability"))
        gl_g = self._grid(2)
        self._add_text(gl_g, 0, 0, "Each Occurrence Limit",
                       "policy.umbrella.underlying.gl.occ_limit", "$")
        self._add_text(gl_g, 0, 1, "General Aggregate",
                       "policy.umbrella.underlying.gl.gen_aggr_limit", "$")
        self._add_text(gl_g, 1, 0, "Products & Comp Ops Aggregate",
                       "policy.umbrella.underlying.gl.pro_comp_ops_limit", "$")
        self._add_text(gl_g, 1, 1, "Personal & Adv Injury",
                       "policy.umbrella.underlying.gl.pers_adv_inj_limit", "$")
        self._add_text(gl_g, 2, 0, "Damage to Rented Premises",
                       "policy.umbrella.underlying.gl.damage_rent_limit", "$")
        self._add_text(gl_g, 2, 1, "Medical Expense",
                       "policy.umbrella.underlying.gl.med_exp_limit", "$")
        self._root.addLayout(gl_g)

        # ── Underlying EL ────────────────────────────────────────────────────
        self._root.addWidget(_hr())
        self._root.addWidget(self._hdr("Underlying — Employer's Liability"))
        el_g = self._grid(2)
        self._add_text(el_g, 0, 0, "Each Accident Limit",
                       "policy.umbrella.underlying.el.acc_limit", "$")
        self._add_text(el_g, 0, 1, "Disease – Each Employee",
                       "policy.umbrella.underlying.el.disease_emp_limit", "$")
        self._add_text(el_g, 1, 0, "Disease – Policy Limit",
                       "policy.umbrella.underlying.el.disease_pol_limit", "$")
        self._root.addLayout(el_g)

        # ── Underlying Other (repeatable) ────────────────────────────────────
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Underlying — Other Policies", "+ Add Underlying",
                              self.OTHER_GROUP)
        )
        self._other_tags: list[str | None] = [
            f"{self.OTHER_GROUP}.desc",
            f"{self.OTHER_GROUP}.carrier",
            f"{self.OTHER_GROUP}.pol_num",
            f"{self.OTHER_GROUP}.eff_date",
            f"{self.OTHER_GROUP}.exp_date",
            f"{self.OTHER_GROUP}.limit",
        ]
        self._other_tbl = self._table(
            6, ["Description", "Carrier", "Policy #",
                "Effective", "Expiration", "Limit"],
        )
        self._other_tbl.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._other_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._other_tbl.setColumnWidth(2, 130)
        self._other_tbl.setColumnWidth(3, 100)
        self._other_tbl.setColumnWidth(4, 100)
        self._other_tbl.setColumnWidth(5, 120)
        self._other_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._wire_table_edits(self._other_tbl, self.OTHER_GROUP, self._other_tags)
        self._root.addWidget(self._other_tbl, 1)

        # Order: underlying → Forms → Additional Coverages → Additional Interests.
        self._forms_tbl = self._add_forms_section("policy.umbrella")
        self._root.addWidget(self._forms_tbl)
        self._add_cov_tbl = self._add_cov_table("policy.umbrella")
        self._root.addWidget(self._add_cov_tbl)
        self._ai_tbl = self._add_ai_section("policy.umbrella")
        self._root.addWidget(self._ai_tbl)
        self._refresh_tables(self._state)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_other_tbl"):
            return
        # Singleton inputs are refreshed by the base refresh() call.
        # Refresh the Other repeatable table.
        other_items = _rep(state, self.OTHER_GROUP)
        self._other_tbl.setRowCount(len(other_items))
        for r, item in enumerate(other_items):
            self._populate_row(self._other_tbl, r, self._other_tags, item)
        self._autofit_columns(self._other_tbl)
        self._refresh_ai_table(state)
        self._refresh_forms_table(state)
        self._refresh_cov_table(state)


class UnclassifiedFormsForm(SectionFormBase):
    """Standalone Unclassified Forms tab — no Additional Coverages."""

    def _build_form(self) -> None:
        self._root.addLayout(
            self._section_row("Unclassified Forms & Endorsements", "+ Add Form", "policy_form")
        )
        self._forms_tbl = self._table(3, ["Form Number", "Form Name", "Edition Date"],
                                      editable=True)
        self._forms_tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._forms_tbl.setColumnWidth(0, 120)
        self._forms_tbl.setColumnWidth(2, 110)
        self._forms_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._root.addWidget(self._forms_tbl, 1)
        self._refresh_tables(self._state)

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_forms_tbl"):
            return
        items = _rep(state, "policy_form")
        self._forms_tbl.setRowCount(len(items))
        tags = ["policy_form.form_number", "policy_form.form_name",
                "policy_form.edition_date"]
        for r, item in enumerate(items):
            self._populate_row(self._forms_tbl, r, tags, item)


class NotesForm(SectionFormBase):
    """Notes — no Additional Coverages."""

    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("Notes"))
        self._notes = QPlainTextEdit()
        self._notes.setObjectName("NotesInput")
        self._notes.setPlaceholderText("Enter notes about this submission…")
        v = _val(self._state, "notes.text")
        if v:
            self._notes.setPlainText(v)
        self._notes.textChanged.connect(self._on_notes_changed)
        self._root.addWidget(self._notes, 1)

    def _on_notes_changed(self) -> None:
        text = self._notes.toPlainText().strip()
        self.field_changed.emit("notes.text", text if text else None)

    def refresh(self, state: dict | None) -> None:
        self._state = state
        v = _val(state, "notes.text")
        self._notes.blockSignals(True)
        self._notes.setPlainText(v)
        self._notes.blockSignals(False)
        self._refresh_tables(state)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

_FORM_MAP: dict[str, type[SectionFormBase]] = {
    "account":              NamedInsuredsForm,
    "location":             LocationsForm,
    "policy.gl":            GeneralLiabilityForm,
    "policy.property":      PropertyForm,
    "policy.auto":          BusinessAutoForm,
    "policy.inland_marine": InlandMarineForm,
    "policy.workers_comp":  WorkersCompForm,
    "policy.umbrella":      UmbrellaExcessForm,
    "forms":                UnclassifiedFormsForm,
    "notes":                NotesForm,
}


def make_section_form(
    tab_key: str,
    state: dict | None,
    parent: QWidget | None = None,
) -> SectionFormBase | None:
    """Return a dedicated form for ``tab_key``, or ``None`` if none defined."""
    cls = _FORM_MAP.get(tab_key)
    if cls is None:
        return None
    return cls(state, parent)

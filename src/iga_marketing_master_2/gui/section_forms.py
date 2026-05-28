"""section_forms.py — Purpose-built form widgets for each insurance coverage section."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import section_forms_layout as layout
from .section_table import confidence_color, is_audit_exempt_tag

__all__ = ["SectionFormBase", "make_section_form", "invalidate_field_map_cache"]


# Default minimum height for repeatable tables — sized to show ~6 rows
# before scrolling kicks in. Header (~28px) + 6 rows × ~28px ≈ 200px.
_TBL_HEIGHT_6_ROWS: int = 200


# ---------------------------------------------------------------------------
# Currency display formatting
# ---------------------------------------------------------------------------
# All IM currency columns (limits, deductibles, premiums, etc.) display
# in a single canonical format: ``$X,XX0``. The underlying state.json
# value can be anything ("5000", "5,000", "$5,000" — extraction varies),
# and EPIC entry strips back to bare digits via norm_currency / Strip
# currency in the step files. Display formatting is GUI-only and
# non-destructive — we do not write back the formatted form.


def _fmt_currency_display(raw: str) -> str:
    """Return *raw* formatted as ``$X,XX0`` (or ``$X,XX0.YY`` if non-integer).

    Empty / blank stays empty. Values that don't parse as numeric pass
    through unchanged so we don't mangle text the operator may have
    intentionally typed (e.g., free-text notes mixed into the column).
    """
    import re as _re_cur
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    digits = _re_cur.sub(r"[^\d.]", "", s)
    if not digits:
        return s
    try:
        v = float(digits)
    except ValueError:
        return s
    if v == int(v):
        return f"${int(v):,}"
    return f"${v:,.2f}"


# ---------------------------------------------------------------------------
# Currency tag detection
# ---------------------------------------------------------------------------
# Whether a domain_tag represents a dollar value. We use lowercased keyword
# patterns against the *tag* itself (which mirrors EPIC's terse field names —
# streLimit, cureDeductible, streAmtInsurance, strePayroll, etc.). Phone /
# fax / extension / count / number / year / age / mod fields are excluded
# explicitly so digits-only non-currency values pass through untouched.

_CURRENCY_KEYWORDS = (
    "limit", "ded", "amt", "amount", "premium", "aggr", "aggregate",
    "payroll", "exposure", "salary", "revenue", "sales", "occ",
    "med", "claim", "retro", "retained", "sir",
    "cure",  # cure* tags are always currency in EPIC (cureLimit / curePremium)
)
_CURRENCY_NEGATIVE = (
    "phone", "fax", "ext", "count", "num", "number", "year", "age", "mod",
    "code", "zip", "fein", "vin", "naics", "sic", "rate", "pct", "percent",
    "id", "uuid", "date", "ref", "loc", "bldg", "site",
    "name", "addr", "street", "city", "state", "country",
)


def _is_currency_tag(tag: str | None) -> bool:
    """Heuristic: does this domain_tag represent a dollar amount?

    Examples that return True::

        streLimit, streEachOccLimit, streGenAggrAppLimit, cureDeductible,
        streAmtInsurance, strePayroll, streDamPremLimit, streBIDed.

    Examples that return False::

        synthetic_LocationNumber, inteBuildingNumber, strePhone, streZip,
        streFEIN, cboBasis, cboNameType, streStreetLine.

    Empty/None returns False so an unresolved column isn't formatted.
    """
    if not tag:
        return False
    last = tag.rsplit(".", 1)[-1].lower()
    if any(kw in last for kw in _CURRENCY_NEGATIVE):
        return False
    return any(kw in last for kw in _CURRENCY_KEYWORDS)


def _reformat_currency_cells(
    table: QTableWidget, columns: list[int],
) -> None:
    """Walk every row of *table* and rewrite the listed currency columns
    to the canonical ``$X,XX0`` display form. Blocks ``cellChanged``
    while writing so the persistence path doesn't fire on programmatic
    edits."""
    prev_block = getattr(table, "_iga_block", False)
    table._iga_block = True
    try:
        for r in range(table.rowCount()):
            for c in columns:
                cell = table.item(r, c)
                if cell is None:
                    continue
                fmtd = _fmt_currency_display(cell.text())
                if fmtd != cell.text():
                    cell.setText(fmtd)
    finally:
        table._iga_block = prev_block


# ---------------------------------------------------------------------------
# Cross-section row moves (Inland Marine)
# ---------------------------------------------------------------------------
# Operators frequently realize an item was extracted into the wrong IM
# subsection — e.g., a discrete piece of equipment landed under Additional
# Coverages, or vice versa. The right-click "Move to..." menu walks the
# selected rows through a per-pair field map. Where the destination has
# fewer fields than the source, the extra source fields concat into the
# destination's description-like field. If the result exceeds EPIC's
# 30-char Unscheduled cap, the operator gets a prompt to shorten it
# manually (no Claude here — moves should be deterministic).

_IM_SCHED_GROUP    = "policy.inland_marine.scheduled_item"
_IM_UNSCHED_GROUP  = "policy.inland_marine.unscheduled_item"
_IM_AC_GROUP       = "policy.inland_marine.additional_coverage"

# EPIC caps the Unscheduled description column at 30 chars. The Scheduled
# description and AC name fields have no hard cap we hit in practice.
_UNSCHED_DESC_MAX  = 30


def _val_of(item: dict, tag: str) -> str:
    """Extract the string value from a state record (or '' if missing)."""
    rec = item.get(tag)
    return str(rec.get("value") or "").strip() if isinstance(rec, dict) else ""


def _rec_of(value: str) -> dict | None:
    """Wrap a value in a state record dict; returns None for empty.

    Marks the record as ``status="approved"`` so the GUI's
    :func:`confidence_color` treats it as operator-confirmed (no red
    "low confidence" tint). The operator explicitly moved this value
    via the right-click "Move to..." action — that's an approval.
    """
    v = (value or "").strip()
    if not v:
        return None
    return {"value": v, "status": "approved", "confidence": 1.0}


def _concat_nonblank(*parts: str) -> str:
    """Space-join the truthy, non-whitespace parts."""
    return " ".join(p.strip() for p in parts if p and p.strip())


def transform_im_row(item: dict, src_group: str, dest_group: str) -> dict:
    """Build a destination-shaped row from a single source IM row.

    Returns a dict of ``{tag: record_or_None}`` keyed by destination tags.
    Caller drops None values before inserting into ``state.repeatables``.
    Unknown (src, dest) pairs return an empty dict.
    """
    g = lambda tag: _val_of(item, f"{src_group}.{tag}")

    if src_group == _IM_SCHED_GROUP and dest_group == _IM_UNSCHED_GROUP:
        # Concat year/make/model/desc so the Unsched row identifies the item.
        desc = _concat_nonblank(
            g("model_year"), g("manufacturer"), g("model"), g("description"),
        )
        return {
            f"{dest_group}.description":   _rec_of(desc),
            f"{dest_group}.amt_insurance": _rec_of(g("amt_insurance")),
        }
    if src_group == _IM_SCHED_GROUP and dest_group == _IM_AC_GROUP:
        name = _concat_nonblank(
            g("model_year"), g("manufacturer"), g("model"), g("description"),
        )
        return {
            f"{dest_group}.name":             _rec_of(name),
            f"{dest_group}.each_claim_limit": _rec_of(g("amt_insurance")),
            f"{dest_group}.deductible":       _rec_of(g("deductible")),
        }
    if src_group == _IM_UNSCHED_GROUP and dest_group == _IM_SCHED_GROUP:
        return {
            f"{dest_group}.description":   _rec_of(g("description")),
            f"{dest_group}.amt_insurance": _rec_of(g("amt_insurance")),
        }
    if src_group == _IM_UNSCHED_GROUP and dest_group == _IM_AC_GROUP:
        return {
            f"{dest_group}.name":             _rec_of(g("description")),
            f"{dest_group}.each_claim_limit": _rec_of(g("amt_insurance")),
        }
    if src_group == _IM_AC_GROUP and dest_group == _IM_SCHED_GROUP:
        return {
            f"{dest_group}.description":   _rec_of(g("name")),
            f"{dest_group}.amt_insurance": _rec_of(g("each_claim_limit")),
            f"{dest_group}.deductible":    _rec_of(g("deductible")),
        }
    if src_group == _IM_AC_GROUP and dest_group == _IM_UNSCHED_GROUP:
        return {
            f"{dest_group}.description":   _rec_of(g("name")),
            f"{dest_group}.amt_insurance": _rec_of(g("each_claim_limit")),
        }
    return {}


def _prompt_shorten(parent: QWidget, proposed: str, max_chars: int,
                    label_prefix: str) -> str | None:
    """Modal dialog: ask the operator to shorten *proposed* to ≤ *max_chars*.

    Returns the trimmed text on OK, or ``None`` if the user cancelled.
    Re-prompts until the result fits or the user cancels.
    """
    current = proposed
    while True:
        text, ok = QInputDialog.getText(
            parent,
            "Description too long",
            (
                f"{label_prefix}: this description is {len(current)} characters, "
                f"but the destination caps it at {max_chars}.\n\n"
                "Edit it to fit:"
            ),
            QLineEdit.Normal,
            current,
        )
        if not ok:
            return None
        trimmed = text.strip()
        if 0 < len(trimmed) <= max_chars:
            return trimmed
        current = trimmed


# ---------------------------------------------------------------------------
# Sortable tables
# ---------------------------------------------------------------------------
# Every section-form repeatable table is sortable by clicking its column
# header. Sorting is purely a display-time operation — it does NOT reorder
# state.repeatables in memory or on disk, so the natural "extraction order"
# is preserved across saves.
#
# Two pieces make this work:
#   1. ``_SortableTableItem`` overrides __lt__ so numeric / currency columns
#      ('$1,000,000' vs '$2,000') sort by numeric value rather than lexical
#      string order.
#   2. ``_STATE_INDEX_ROLE`` is stored on each row's column-0 cell so the
#      cellChanged handler in ``_wire_table_edits`` can translate the
#      visible row index back to the underlying state.repeatables index
#      (visible row N may not correspond to state index N after sorting).

_STATE_INDEX_ROLE: int = int(Qt.ItemDataRole.UserRole) + 100


def _parse_numeric_or_none(s: str) -> float | None:
    """Strip currency / percent / commas and parse as float. Returns None if
    the result isn't a number — caller falls back to lexical compare."""
    s = s.strip()
    if not s:
        return None
    s2 = s.replace(",", "").replace("$", "").strip()
    if s2.endswith("%"):
        s2 = s2[:-1].strip()
    try:
        return float(s2)
    except (ValueError, TypeError):
        return None


class _SortableTableItem(QTableWidgetItem):
    """Table-cell item with numeric-aware comparison.

    For columns like Limit, Premium, Coinsurance, Deductible, Exposure, etc.,
    compares the underlying numeric value rather than the raw string so
    '$1,000,000' sorts after '$2,000' instead of before. Empty cells sort to
    the bottom (in ascending direction). Non-numeric strings fall back to
    case-insensitive lexical compare so codes / descriptions sort sensibly.
    """

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if not isinstance(other, QTableWidgetItem):
            return NotImplemented
        a = (self.text() or "").strip()
        b = (other.text() or "").strip()
        if not a:
            return False  # Empty goes to the bottom in ascending sort.
        if not b:
            return True
        na = _parse_numeric_or_none(a)
        nb = _parse_numeric_or_none(b)
        if na is not None and nb is not None:
            return na < nb
        return a.casefold() < b.casefold()


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


def _record(state: dict | None, tag: str) -> dict | None:
    """Return the raw FieldRecord dict for *tag*, or ``None`` if absent."""
    if not state:
        return None
    rec = (state.get("fields") or {}).get(tag)
    return rec if isinstance(rec, dict) else None


# Reuse the same threshold used by ``count_low_confidence_in_tab`` so the
# badge and the singleton-input tinting can never get out of sync.
from .section_table import CONFIDENCE_HIGH_THRESHOLD as _CONF_HIGH


def _checkbox_attention_style(rec: dict | None) -> str:
    """Return a QSS snippet to flag a QCheckBox that still needs attention.

    Same trigger as :func:`_input_attention_style` (conflict or
    sub-threshold confidence with non-approved status), but the QSS targets
    the checkbox body + indicator rather than a QLineEdit border.
    """
    if not isinstance(rec, dict):
        return ""
    status = rec.get("status", "pending")
    if status in {"approved", "locked"}:
        return ""
    if rec.get("conflicts"):
        return (
            "QCheckBox { background: #fff7ed; border-radius: 3px;"
            " padding: 2px 4px; }"
            "QCheckBox::indicator { border: 1.5px solid #f97316; }"
        )
    try:
        conf = float(rec.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _CONF_HIGH:
        return (
            "QCheckBox { background: #fffbeb; border-radius: 3px;"
            " padding: 2px 4px; }"
            "QCheckBox::indicator { border: 1.5px solid #f59e0b; }"
        )
    return ""


def _attention_tooltip(rec: dict | None) -> str:
    """Operator-facing tooltip describing why a widget is flagged."""
    if not isinstance(rec, dict):
        return ""
    cfls = rec.get("conflicts") or []
    if cfls:
        lines = [
            f"• {cf.get('value')!r}  "
            f"(from {(cf.get('source') or {}).get('doc_id', '?')})"
            for cf in cfls if isinstance(cf, dict)
        ]
        return "Conflict — also extracted:\n" + "\n".join(lines)
    try:
        conf = float(rec.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return f"Low confidence: {conf:.0%}"


def _input_attention_style(rec: dict | None) -> str:
    """Return a QSS snippet to set the singleton input's border/background
    when it needs attention. Empty string when nothing is wrong.

    Two states:

    * ``conflicts`` present → orange border + pale-orange fill (matches the
      table-cell conflict tint in ``_populate_row``).
    * ``confidence < CONFIDENCE_HIGH_THRESHOLD`` and status not approved/
      locked → yellow border + pale-yellow fill (matches ``_TINT_MID``).
    * status approved/locked or ``confidence >= CONFIDENCE_HIGH_THRESHOLD``
      → empty string (no special styling).
    """
    if not isinstance(rec, dict):
        return ""
    status = rec.get("status", "pending")
    if status in {"approved", "locked"}:
        return ""
    if rec.get("conflicts"):
        return (
            "QLineEdit#FieldInput { border: 1px solid #f97316;"
            " background: #fff7ed; }"
        )
    try:
        conf = float(rec.get("confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < _CONF_HIGH:
        return (
            "QLineEdit#FieldInput { border: 1px solid #f59e0b;"
            " background: #fffbeb; }"
        )
    return ""


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
        # Populate repeatable tables from initial state. Singleton inputs are
        # filled inline by `_build_form` via `_val(self._state, tag)`, but
        # QTableWidgets created in `_build_form` are empty until something
        # calls `_refresh_tables`. Without this, hazards / vehicles / loss
        # payees / etc. don't appear until the operator triggers another
        # `_rebuild_tabs` pass (e.g., by editing a field). Mirroring what
        # `refresh()` does here means the first paint already shows data.
        self._refresh_tables(state)
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

    def _install_singleton_accept_menu(
        self,
        widget: "QLineEdit | QComboBox | QCheckBox",
        tag: str,
    ) -> None:
        """Attach a right-click menu that includes Accept on a singleton field.

        Builds on top of the widget's standard context menu (for QLineEdit
        we splice the standard Undo/Cut/Copy/etc. menu underneath ours; for
        QComboBox / QCheckBox there is no standard edit menu so we just
        show Accept).

        Accept marks the field's record as approved (status=approved,
        confidence=1.0, conflicts cleared) and drops the visual tint. No-op
        when the field isn't currently flagged.
        """
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        def on_menu(pos, w=widget, t=tag) -> None:
            from PySide6.QtWidgets import QMenu as _QMenu
            rec = _record(self._state, t)
            flagged = bool(_input_attention_style(rec) or _checkbox_attention_style(rec))
            # Build a menu starting with Accept (only when flagged), then
            # append the widget's standard menu if it has one.
            menu = _QMenu(w)
            accept_act = None
            if flagged:
                accept_act = menu.addAction("Accept")
                menu.addSeparator()
            else:
                # Always offer Accept as a no-op-able convenience; greyed out.
                accept_act = menu.addAction("Accept")
                accept_act.setEnabled(False)
                menu.addSeparator()

            # Splice the standard QLineEdit edit menu (Cut/Copy/Paste/etc.)
            # for line edits — operators expect those to still work.
            if isinstance(w, QLineEdit):
                std = w.createStandardContextMenu()
                for act in std.actions():
                    menu.addAction(act)

            chosen = menu.exec(w.mapToGlobal(pos))
            if chosen is accept_act and accept_act is not None and accept_act.isEnabled():
                # Re-emit the field's current value so the host's
                # ``_update_field`` runs and flips status → approved +
                # confidence → 1.0 (clearing conflicts in the process).
                if isinstance(w, QLineEdit):
                    text = w.text().strip()
                elif isinstance(w, QComboBox):
                    text = w.currentText().strip()
                elif isinstance(w, QCheckBox):
                    text = "Yes" if w.isChecked() else "No"
                else:
                    text = ""
                self.field_changed.emit(t, text or None)
                # Drop the local tint immediately so the operator sees
                # confirmation. The full refresh repaints the rest.
                w.setStyleSheet("")
                w.setToolTip("")

        widget.customContextMenuRequested.connect(on_menu)

    def _add_text(self, grid: QGridLayout, row: int, col: int,
                  label: str, tag: str, placeholder: str = "") -> QLineEdit:
        lbl = QLabel(label)
        lbl.setObjectName("FieldLabel")
        inp = QLineEdit()
        inp.setObjectName("FieldInput")
        if placeholder:
            inp.setPlaceholderText(placeholder)
        is_currency = _is_currency_tag(tag)
        rec = _record(self._state, tag)
        v = _val(self._state, tag)
        if v:
            inp.setText(_fmt_currency_display(v) if is_currency else v)
        # Visual attention indicator — matches the per-cell tint in
        # ``_populate_row`` so the tab badge and the singleton input
        # styling agree on what needs attention. Audit-exempt tags
        # (item_number, ...) skip this entirely.
        att = "" if is_audit_exempt_tag(tag) else _input_attention_style(rec)
        if att:
            inp.setStyleSheet(att)
            cfls = rec.get("conflicts") or [] if isinstance(rec, dict) else []
            if cfls:
                lines = [
                    f"• {cf.get('value')!r}  "
                    f"(from {(cf.get('source') or {}).get('doc_id', '?')})"
                    for cf in cfls if isinstance(cf, dict)
                ]
                inp.setToolTip("Conflict — also extracted:\n" + "\n".join(lines))
            elif rec is not None:
                try:
                    conf = float(rec.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    conf = 0.0
                inp.setToolTip(f"Low confidence: {conf:.0%}")

        def _done(t: str = tag, w: QLineEdit = inp, cur: bool = is_currency) -> None:
            text = w.text().strip()
            # Persist raw digits; reformat the box back into masked form so
            # the operator sees the canonical display after Tab/Enter.
            self.field_changed.emit(t, text if text else None)
            if cur and text:
                masked = _fmt_currency_display(text)
                if masked != text:
                    prev = w.blockSignals(True)
                    try:
                        w.setText(masked)
                    finally:
                        w.blockSignals(prev)
            # Operator edit = operator approval — clear any tint/tooltip.
            w.setStyleSheet("")
            w.setToolTip("")

        inp.editingFinished.connect(_done)
        self._install_singleton_accept_menu(inp, tag)
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
        self._install_singleton_accept_menu(combo, tag)
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
            # Mirror the singleton-input attention styling: yellow halo for
            # low confidence, orange for conflicts. This is the only signal
            # the operator has that a Yes/No checkbox isn't operator-blessed
            # yet — without it a 0.8-confidence checkbox is indistinguishable
            # from a fully-approved one.
            rec = _record(self._state, tag)
            cb_att = "" if is_audit_exempt_tag(tag) else _checkbox_attention_style(rec)
            if cb_att:
                cb.setStyleSheet(cb_att)
                cb.setToolTip(_attention_tooltip(rec))

            def _changed(checked: bool, t: str = tag, w: QCheckBox = cb) -> None:
                self.field_changed.emit(t, "Yes" if checked else "No")
                # Operator clicked = operator approved. Drop the tint.
                w.setStyleSheet("")
                w.setToolTip("")

            cb.toggled.connect(_changed)
            self._install_singleton_accept_menu(cb, tag)
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
        for genuinely read-only tables.

        Click-to-sort is enabled on every table; ``_populate_row`` stores
        the source state-index marker on column 0 so edits route to the
        correct state.repeatables row even after the operator sorts.
        """
        t = QTableWidget(0, cols)
        t.setObjectName("SectionTable")
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked | QTableWidget.EditTrigger.AnyKeyPressed
            if editable else QTableWidget.EditTrigger.NoEditTriggers
        )
        t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # ExtendedSelection lets the operator Ctrl/Shift-click multiple rows.
        # The context menu's "Delete Row(s)" item batches into one prompt
        # when 2+ rows are selected; right-click on a single row still works
        # as before.
        t.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        t.setSortingEnabled(True)
        # Qt's setSortingEnabled(True) defaults the sort indicator to
        # column 0 DESCENDING, which makes row-numbered tables (Vehicles,
        # Drivers, Class Codes, Scheduled Items) display in reverse order
        # — the operator opens the Vehicle Schedule and sees "37, 36, 36,
        # 35, ..." instead of "1, 2, 3, ...". Force the indicator to
        # column 0 ASCENDING so the initial view is the natural order.
        # The operator can still click any header to re-sort.
        t.horizontalHeader().setSortIndicator(0, Qt.SortOrder.AscendingOrder)
        t.horizontalHeader().setSortIndicatorShown(True)
        t.horizontalHeader().setSectionsClickable(True)
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
        def on_cell_changed(visible_row: int, col: int) -> None:
            # Skip events generated by our own _populate_row.
            if getattr(table, "_iga_block", False):
                return
            tag_idx = col - offset
            if tag_idx < 0 or tag_idx >= len(tags):
                return
            tag = tags[tag_idx]
            if tag is None:
                return
            item = table.item(visible_row, col)
            value = item.text().strip() if item else ""
            # Translate visible-row index back to the underlying
            # state.repeatables index. Sorting moves rows around in the
            # display, but the marker stays attached to the row's column-0
            # cell, so reading it here yields the correct destination index.
            state_idx = visible_row
            marker_cell = table.item(visible_row, 0)
            if marker_cell is not None:
                stored = marker_cell.data(_STATE_INDEX_ROLE)
                if isinstance(stored, int):
                    state_idx = stored
            self.field_changed.emit(
                f"__rep:{group}:{state_idx}:{tag}", value or None
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

        def _state_idx_for_row(_tbl, visible_row: int) -> int:
            """Translate visible row → underlying state.repeatables index.

            ``_populate_row`` writes the source index into column 0 via
            ``_STATE_INDEX_ROLE``; reading it back is sorting-safe.  Falls
            back to the visible row number if the marker isn't set (legacy
            tables / freshly inserted rows).
            """
            marker = _tbl.item(visible_row, 0)
            if marker is not None:
                stored = marker.data(_STATE_INDEX_ROLE)
                if isinstance(stored, int):
                    return stored
            return visible_row

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

            # "Accept" / "Accept Row" / "Accept N Rows" actions — mark the
            # cell or the whole row(s) as approved (status=approved,
            # confidence=1.0). Tint clears, tab badge ticks down.
            tag_idx = index.column() - offset
            accept_act = None
            accept_tag = None
            if 0 <= tag_idx < len(tags):
                accept_tag = tags[tag_idx]
            if accept_tag and cell_item is not None:
                accept_act = menu.addAction("Accept")

            # Row-level accept. Mirrors the delete pattern — multi-select
            # gets a batch action.
            selected_rows_for_accept = sorted({r.row() for r in _tbl.selectionModel().selectedRows()})
            if len(selected_rows_for_accept) >= 2 and index.row() in selected_rows_for_accept:
                accept_row_act = menu.addAction(f"Accept {len(selected_rows_for_accept)} Rows")
                accept_row_target_rows = selected_rows_for_accept
            else:
                accept_row_act = menu.addAction("Accept Row")
                accept_row_target_rows = [index.row()]
            menu.addSeparator()

            # Build the delete action.  If multiple rows are selected AND the
            # right-clicked row is one of them, the action deletes all of them
            # in a single confirmation.  Otherwise it deletes just the row
            # under the cursor (matches Qt's native delete-key behavior).
            selected_rows = sorted({r.row() for r in _tbl.selectionModel().selectedRows()})
            if len(selected_rows) >= 2 and index.row() in selected_rows:
                del_act = menu.addAction(f"Delete {len(selected_rows)} Rows")
                del_target_rows = selected_rows
            else:
                del_act = menu.addAction("Delete Row")
                del_target_rows = [index.row()]

            # "Move to..." submenu: only for tables that declare move
            # targets (set by the IM form). Mirrors the delete pattern —
            # multi-select moves go in one batch.
            move_actions: list[tuple] = []  # (QAction, dest_group, dest_label)
            move_target_rows = del_target_rows
            move_targets = getattr(_tbl, "_move_targets", None)
            if move_targets:
                move_menu = menu.addMenu(
                    f"Move {len(move_target_rows)} Row"
                    f"{'s' if len(move_target_rows) > 1 else ''} to"
                )
                for dest_group, dest_label in move_targets:
                    act = move_menu.addAction(dest_label)
                    move_actions.append((act, dest_group, dest_label))

            chosen = menu.exec(_tbl.viewport().mapToGlobal(pos))
            # Check move actions before delete so a Move pick doesn't trigger
            # the delete branch.
            for act, dest_group, dest_label in move_actions:
                if chosen is act:
                    state_idxs = sorted(
                        {_state_idx_for_row(_tbl, r) for r in move_target_rows}
                    )
                    self._initiate_move(
                        src_group=_g,
                        dest_group=dest_group,
                        src_state_idxs=state_idxs,
                        dest_label=dest_label,
                    )
                    return
            if chosen is del_act:
                # Map every visible row to its underlying state index, then
                # encode as a comma-separated list for the host.
                state_idxs = sorted({_state_idx_for_row(_tbl, r) for r in del_target_rows})
                if len(state_idxs) == 1:
                    self.field_changed.emit(f"__del:{_g}:{state_idxs[0]}", None)
                else:
                    payload = ",".join(str(i) for i in state_idxs)
                    self.field_changed.emit(f"__del_many:{_g}:{payload}", None)
            elif accept_act is not None and chosen is accept_act:
                # Re-emit the cell's current value so the host's
                # `_on_repeatable_commit` path runs and flips status →
                # approved + confidence → 1.0. Clears any tint immediately
                # for visual feedback (full state reload polishes the rest).
                state_idx = _state_idx_for_row(_tbl, index.row())
                cur_value = cell_item.text().strip() if cell_item else ""
                self.field_changed.emit(
                    f"__rep:{_g}:{state_idx}:{accept_tag}",
                    cur_value or None,
                )
                _tbl._iga_block = True
                cell_item.setBackground(QColor("white"))
                cell_item.setForeground(QColor("#1e293b"))
                cell_item.setToolTip("")
                cell_item.setData(Qt.ItemDataRole.UserRole, None)
                _tbl._iga_block = False
            elif chosen is accept_row_act:
                # Bulk-approve every record in the selected row(s). One
                # encoded event covers single-row and multi-row alike;
                # host parses the comma-separated state indices and walks
                # each row's records.
                state_idxs = sorted({
                    _state_idx_for_row(_tbl, r) for r in accept_row_target_rows
                })
                if len(state_idxs) == 1:
                    self.field_changed.emit(
                        f"__accept_row:{_g}:{state_idxs[0]}", None,
                    )
                else:
                    payload = ",".join(str(i) for i in state_idxs)
                    self.field_changed.emit(
                        f"__accept_rows:{_g}:{payload}", None,
                    )
                # Clear cell tints across every cell in those visible rows
                # for immediate feedback. The full state-driven repaint
                # handles anything we miss.
                _tbl._iga_block = True
                try:
                    for vr in accept_row_target_rows:
                        for c in range(_tbl.columnCount()):
                            it = _tbl.item(vr, c)
                            if it is None:
                                continue
                            it.setBackground(QColor("white"))
                            it.setForeground(QColor("#1e293b"))
                            it.setToolTip("")
                            it.setData(Qt.ItemDataRole.UserRole, None)
                finally:
                    _tbl._iga_block = False
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

    def _initiate_move(
        self,
        *,
        src_group: str,
        dest_group: str,
        src_state_idxs: list[int],
        dest_label: str,
    ) -> None:
        """Build the destination rows for a move and emit the ``__move:`` signal.

        Reads each selected source row from ``self._state`` (current
        client snapshot), runs it through :func:`transform_im_row`, and
        for destinations that have a 30-char cap (Unscheduled) prompts
        the operator to shorten any over-limit description. Cancelling
        any prompt aborts the entire move.

        On success emits exactly one event:
            ``__move:<src>:<dest>:<json>``
        where ``<json>`` is a JSON-encoded list of
        ``{"src_idx": int, "row": {tag: record, ...}}`` entries. The
        host's repeatable-move handler pops the source rows in
        descending index order and appends the new rows to the
        destination.
        """
        import json as _json

        if not src_state_idxs:
            return
        state = getattr(self, "_state", None) or {}
        rep = (state.get("repeatables") or {}) if isinstance(state, dict) else {}
        src_items: list = rep.get(src_group) or []

        out_rows: list[dict] = []
        for src_idx in src_state_idxs:
            if not (0 <= src_idx < len(src_items)):
                continue
            src_item = src_items[src_idx]
            transformed = transform_im_row(src_item, src_group, dest_group)
            # Drop None records before length checks / persistence.
            dest_row = {k: v for k, v in transformed.items() if v is not None}

            # Only the Unscheduled destination has a hard cap we enforce.
            if dest_group == _IM_UNSCHED_GROUP:
                desc_tag = f"{dest_group}.description"
                rec = dest_row.get(desc_tag)
                proposed = rec.get("value", "") if isinstance(rec, dict) else ""
                if len(proposed) > _UNSCHED_DESC_MAX:
                    shortened = _prompt_shorten(
                        self, proposed, _UNSCHED_DESC_MAX,
                        label_prefix=f"Row from {src_group.rsplit('.', 1)[-1]}",
                    )
                    if shortened is None:
                        # User cancelled — abort the entire move.
                        return
                    dest_row[desc_tag] = {"value": shortened}

            out_rows.append({"src_idx": src_idx, "row": dest_row})

        if not out_rows:
            return

        payload = _json.dumps(out_rows, ensure_ascii=False)
        self.field_changed.emit(
            f"__move:{src_group}:{dest_group}:{payload}", None,
        )

    def _add_row_btn(self, label: str, group: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setObjectName("AddRowBtn")
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(
            lambda _=False, g=group: self.field_changed.emit(f"__add:{g}", None)
        )
        return btn

    def _section_row(self, title: str, add_label: str = "",
                     group: str = "", *, show_count: bool = False) -> QHBoxLayout:
        row = QHBoxLayout()
        label = self._hdr(title)
        row.addWidget(label)
        row.addStretch(1)
        if add_label and group:
            row.addWidget(self._add_row_btn(add_label, group))
        # Item count in parens — opt-in, only the three tables the operator
        # asked for show it (Vehicles Schedule, Driver Schedule, Scheduled
        # Items). For every other section the title stays clean.
        if group and show_count:
            if not hasattr(self, "_section_title_labels"):
                self._section_title_labels: dict[str, tuple[QLabel, str]] = {}
            self._section_title_labels[group] = (label, title)
            self._update_section_count(group)
        return row

    def _update_section_count(self, group: str) -> None:
        """Refresh one section title to show the current item count.

        Reads ``len(state.repeatables[group])`` and appends "(N)" to the
        title. Pure-display update — does not mutate state.
        """
        registry = getattr(self, "_section_title_labels", None)
        if not registry:
            return
        pair = registry.get(group)
        if pair is None:
            return
        label, base = pair
        items = _rep(self._state, group) if self._state else []
        label.setText(f"{base} ({len(items)})")

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
        # don't spuriously emit edit events back to the host. Sorting is
        # disabled during population too — Qt would otherwise reorder rows
        # after each setItem call, and the row-by-row population loop would
        # write to the wrong destination.
        prev_block = getattr(table, "_iga_block", False)
        prev_sort = table.isSortingEnabled()
        table._iga_block = True
        if prev_sort:
            table.setSortingEnabled(False)
        try:
            offset = 0
            if row_num:
                cell = _SortableTableItem(str(row + 1))
                cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                # State-index marker lives on column 0 so edits can
                # translate visible-row → state.repeatables index after sort.
                cell.setData(_STATE_INDEX_ROLE, row)
                table.setItem(row, 0, cell)
                offset = 1
            for c, tag in enumerate(tags):
                v = ""
                bg: QColor | None = None
                fg: QColor | None = None
                tip: str = ""
                user_data: dict | None = None
                # Audit-exempt tags (item_number, ...) skip every tint/badge
                # path — they're mechanical identifiers, not extracted data.
                tag_exempt = is_audit_exempt_tag(tag)
                if tag is not None:
                    rec = item.get(tag)
                    if isinstance(rec, dict):
                        v = str(rec.get("value") or "")
                        cfls = rec.get("conflicts") or []
                        if cfls and not tag_exempt:
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
                        elif not tag_exempt:
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
                # Apply currency formatting to the displayed text when this
                # column's tag is currency-shaped. Storage stays raw digits;
                # this is display-only.
                if v and _is_currency_tag(tag):
                    v = _fmt_currency_display(v)
                cell = _SortableTableItem(v)
                if bg is not None:
                    cell.setBackground(bg)
                if fg is not None:
                    cell.setForeground(fg)
                if user_data is not None:
                    cell.setData(Qt.ItemDataRole.UserRole, user_data)
                if tip:
                    cell.setToolTip(tip)
                # Store the source state-index marker on the leftmost data
                # column too (when row_num is False, column 0 has no row-#
                # cell and is the first data cell).
                if c == 0 and not row_num:
                    cell.setData(_STATE_INDEX_ROLE, row)
                table.setItem(row, c + offset, cell)
        finally:
            table._iga_block = prev_block
            if prev_sort:
                table.setSortingEnabled(True)

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
        subject_ref_columns: "list[tuple[str, str]] | None" = None,
        subject_ref_label: str | None = None,
        subject_ref_leaf: str | None = None,
    ) -> QTableWidget:
        """Append an Additional Interests subsection; returns the table.

        :param lob_namespace: ``policy.<lob>`` — the repeatable group is
            ``f"{lob_namespace}.additional_interest"``.
        :param subject_ref_columns: optional list of ``(label, leaf)`` tuples
            for LOB-specific subject reference columns inserted between the
            Address and Interest columns. Use this when an LOB needs multiple
            reference columns (Property needs both ``Loc #`` and ``Bldg #``).
        :param subject_ref_label: legacy single-column shortcut equivalent to
            ``subject_ref_columns=[(label, leaf)]``. Kept for backward compat
            with existing Auto / IM call sites.
        :param subject_ref_leaf: leaf tag matching ``subject_ref_label``.
        """
        # Coalesce the two ways of passing subject-ref columns into one list.
        ref_cols: list[tuple[str, str]] = []
        if subject_ref_columns:
            ref_cols.extend(subject_ref_columns)
        if subject_ref_label and subject_ref_leaf:
            ref_cols.append((subject_ref_label, subject_ref_leaf))

        self._ai_group = f"{lob_namespace}.additional_interest"
        cols: list[tuple[str, str]] = [
            ("Type",     f"{self._ai_group}.interest"),
            ("Name",     f"{self._ai_group}.name"),
            ("Address",  f"{self._ai_group}.primary_address.line_1"),
        ]
        for lbl, leaf in ref_cols:
            cols.append((lbl, f"{self._ai_group}.{leaf}"))
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
        # Subject-ref columns get a narrow fixed width (Loc # / Bldg # / Veh #
        # / Item # are all single digits in practice).
        for offset, _ in enumerate(ref_cols):
            tbl.setColumnWidth(3 + offset, 70)
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
                widget.setText(_fmt_currency_display(v) if (v and _is_currency_tag(tag)) else v)
                rec = state_fields.get(tag) if isinstance(state_fields, dict) else None
                att = "" if is_audit_exempt_tag(tag) else _input_attention_style(rec)
                if att:
                    widget.setStyleSheet(att)
                    cfls = rec.get("conflicts") or [] if isinstance(rec, dict) else []
                    if cfls:
                        lines = [
                            f"• {cf.get('value')!r}  "
                            f"(from {(cf.get('source') or {}).get('doc_id', '?')})"
                            for cf in cfls if isinstance(cf, dict)
                        ]
                        widget.setToolTip("Conflict — also extracted:\n" + "\n".join(lines))
                    elif rec is not None:
                        try:
                            conf = float(rec.get("confidence", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            conf = 0.0
                        widget.setToolTip(f"Low confidence: {conf:.0%}")
                else:
                    widget.setStyleSheet("")
                    widget.setToolTip("")
            elif isinstance(widget, QComboBox):
                idx = widget.findText(v) if v else 0
                widget.setCurrentIndex(max(idx, 0))
            elif isinstance(widget, _SymbolWidget):
                widget.setText(v)
            elif isinstance(widget, QCheckBox):
                widget.setChecked(bool(v) and v.lower() in {"true", "yes", "1", "checked", "on"})
                rec = state_fields.get(tag) if isinstance(state_fields, dict) else None
                cb_att = "" if is_audit_exempt_tag(tag) else _checkbox_attention_style(rec)
                if cb_att:
                    widget.setStyleSheet(cb_att)
                    widget.setToolTip(_attention_tooltip(rec))
                else:
                    widget.setStyleSheet("")
                    widget.setToolTip("")
            widget.blockSignals(False)
        self._refresh_tables(state)
        self._refresh_all_section_counts()

    def _refresh_all_section_counts(self) -> None:
        """Update every registered section title with its current count."""
        for group in getattr(self, "_section_title_labels", {}):
            self._update_section_count(group)


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


# 50 US states + DC, 2-letter USPS code and full name. The Workers Comp
# Active/Other state pickers and the Rating Information table both use this
# list. Sorted alphabetically by code so the popup renders predictably.
_US_STATES: tuple[tuple[str, str], ...] = (
    ("AL", "Alabama"), ("AK", "Alaska"), ("AZ", "Arizona"), ("AR", "Arkansas"),
    ("CA", "California"), ("CO", "Colorado"), ("CT", "Connecticut"),
    ("DC", "District of Columbia"), ("DE", "Delaware"), ("FL", "Florida"),
    ("GA", "Georgia"), ("HI", "Hawaii"), ("ID", "Idaho"), ("IL", "Illinois"),
    ("IN", "Indiana"), ("IA", "Iowa"), ("KS", "Kansas"), ("KY", "Kentucky"),
    ("LA", "Louisiana"), ("ME", "Maine"), ("MD", "Maryland"),
    ("MA", "Massachusetts"), ("MI", "Michigan"), ("MN", "Minnesota"),
    ("MS", "Mississippi"), ("MO", "Missouri"), ("MT", "Montana"),
    ("NE", "Nebraska"), ("NV", "Nevada"), ("NH", "New Hampshire"),
    ("NJ", "New Jersey"), ("NM", "New Mexico"), ("NY", "New York"),
    ("NC", "North Carolina"), ("ND", "North Dakota"), ("OH", "Ohio"),
    ("OK", "Oklahoma"), ("OR", "Oregon"), ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"), ("SC", "South Carolina"), ("SD", "South Dakota"),
    ("TN", "Tennessee"), ("TX", "Texas"), ("UT", "Utah"), ("VT", "Vermont"),
    ("VA", "Virginia"), ("WA", "Washington"), ("WV", "West Virginia"),
    ("WI", "Wisconsin"), ("WY", "Wyoming"),
)
_US_STATE_CODES: frozenset[str] = frozenset(c for c, _ in _US_STATES)
_US_STATE_NAME_BY_CODE: dict[str, str] = dict(_US_STATES)

# Sentinel value for the "All Other" pseudo-option on the Other States picker.
# Stored alongside real state codes in the comma-separated singleton value.
ALL_OTHER_STATES_TOKEN: str = "ALL OTHER"


class _StatesMultiSelectWidget(QWidget):
    """Multi-select state picker.

    Button shows selected codes (or a count when many are selected); clicking
    opens a popup with one checkbox per US state. Selected items render at
    the top of the popup. An optional special "All Other" pseudo-option is
    supported for the Other States picker.

    Mimics the QLineEdit interface (``setText`` / ``text``) so the base-class
    ``refresh()`` can update it like any other input widget — value is the
    comma-separated list of selected codes (e.g. ``"TN,KY,NC"``).

    A sibling widget can call ``set_excluded(codes)`` to suppress states that
    the user already chose elsewhere (Other excludes Active selections).

    Emits ``selection_changed(set[str])`` whenever the selection changes so
    the parent form can react (e.g., refresh a downstream rating table).
    """

    selection_changed = Signal(set)

    def __init__(
        self,
        tag: str,
        emit_cb,
        *,
        include_all_other: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._tag = tag
        self._emit_cb = emit_cb
        self._include_all_other = bool(include_all_other)
        self._selected: set[str] = set()
        self._excluded: set[str] = set()
        self._popup: QListWidget | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._btn = QPushButton("(none selected)")
        self._btn.setObjectName("StatesMultiSelectBtn")
        self._btn.setStyleSheet(
            "QPushButton#StatesMultiSelectBtn {"
            "  text-align: left; padding: 4px 8px; "
            "  border: 1px solid #cbd5e1; border-radius: 3px;"
            "  background: white; color: #0f172a; min-height: 22px;"
            "}"
            "QPushButton#StatesMultiSelectBtn:hover { border-color: #2563eb; }"
        )
        self._btn.clicked.connect(self._open_popup)
        layout.addWidget(self._btn, 1)

    # -- QLineEdit-like interface for SectionFormBase.refresh() --------------

    def setText(self, v: str) -> None:
        tokens = [t.strip().upper() for t in (v or "").split(",") if t.strip()]
        self._selected = set(tokens)
        self._update_button()

    def text(self) -> str:
        return ",".join(self._ordered_selected())

    # -- Sibling-widget coordination -----------------------------------------

    def set_excluded(self, excluded: set[str]) -> None:
        """States that are unavailable in this picker (already chosen elsewhere).

        Any state in ``excluded`` that is currently selected here is silently
        deselected — we don't want both pickers claiming the same state.
        """
        self._excluded = {c.upper() for c in excluded}
        removed = self._selected & self._excluded
        if removed:
            self._selected -= removed
            self._update_button()
            self._emit()

    # -- Internals -----------------------------------------------------------

    def _ordered_selected(self) -> list[str]:
        """Selected codes in canonical order: ALL OTHER first if present,
        then 2-letter codes alphabetically."""
        codes = sorted(c for c in self._selected if c in _US_STATE_CODES)
        if ALL_OTHER_STATES_TOKEN in self._selected:
            return [ALL_OTHER_STATES_TOKEN] + codes
        return codes

    def _update_button(self) -> None:
        ordered = self._ordered_selected()
        if not ordered:
            self._btn.setText("(none selected)")
        elif len(ordered) <= 6:
            # Show codes inline when the list fits.
            self._btn.setText(", ".join(
                "All Other" if c == ALL_OTHER_STATES_TOKEN else c
                for c in ordered
            ))
        else:
            self._btn.setText(f"{len(ordered)} selected")

    def _emit(self) -> None:
        value = ",".join(self._ordered_selected())
        self._emit_cb(self._tag, value or None)
        self.selection_changed.emit(set(self._selected))

    def _open_popup(self) -> None:
        if self._popup is not None:
            self._popup.close()
            self._popup = None

        popup = QListWidget()
        popup.setWindowFlags(Qt.WindowType.Popup)
        # Explicit colors at every state — Qt's default selected/hover style
        # inverts to white text, which made checked items vanish against
        # white backgrounds. Forcing dark foreground for normal, selected,
        # and hover states keeps the labels readable in all three states.
        popup.setStyleSheet(
            "QListWidget {"
            "  border: 1px solid #94a3b8;"
            "  background: white;"
            "  color: #0f172a;"
            "  outline: 0;"
            "}"
            "QListWidget::item {"
            "  padding: 4px 8px;"
            "  color: #0f172a;"
            "}"
            "QListWidget::item:selected {"
            "  background: #dbeafe;"
            "  color: #0f172a;"
            "}"
            "QListWidget::item:hover {"
            "  background: #eff6ff;"
            "  color: #0f172a;"
            "}"
        )

        # Ordering inside the popup: special "All Other" first (if enabled),
        # then selected states alphabetically, then remaining states.
        dark = QColor("#0f172a")
        if self._include_all_other:
            item = QListWidgetItem("All Other")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if ALL_OTHER_STATES_TOKEN in self._selected
                else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, ALL_OTHER_STATES_TOKEN)
            item.setForeground(dark)
            font = item.font()
            font.setBold(True)
            item.setFont(font)
            popup.addItem(item)

        available = [
            (code, name) for code, name in _US_STATES if code not in self._excluded
        ]
        selected_avail = [t for t in available if t[0] in self._selected]
        remaining = [t for t in available if t[0] not in self._selected]
        for code, name in selected_avail + remaining:
            item = QListWidgetItem(f"{code} — {name}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked
                if code in self._selected
                else Qt.CheckState.Unchecked
            )
            item.setData(Qt.ItemDataRole.UserRole, code)
            # Set foreground explicitly per-item so it survives any
            # cascading parent QSS that would otherwise blank the text.
            item.setForeground(dark)
            popup.addItem(item)

        def on_item_changed(item: QListWidgetItem) -> None:
            code = item.data(Qt.ItemDataRole.UserRole)
            checked = item.checkState() == Qt.CheckState.Checked
            if checked:
                self._selected.add(code)
            else:
                self._selected.discard(code)
            self._update_button()
            self._emit()

        popup.itemChanged.connect(on_item_changed)

        # Click-outside-to-close: previously relied on Qt.Popup's native
        # outside-click handling + a FocusOut filter as backup.  Both proved
        # unreliable on Windows when toggling checkboxes inside the popup
        # (the popup never loses focus, and Qt.Popup's mouse tracking can
        # miss clicks that land on app-internal widgets).  Robust fix: a
        # single application-wide event filter on MouseButtonPress that
        # closes the popup whenever the click lands outside its global
        # geometry.  Filter is removed when the popup closes.
        popup.installEventFilter(self)

        # Anchor under the button.
        gpos = self._btn.mapToGlobal(self._btn.rect().bottomLeft())
        popup.move(gpos)
        popup.resize(max(self._btn.width(), 240), 360)
        popup.show()
        popup.activateWindow()
        popup.setFocus(Qt.FocusReason.PopupFocusReason)
        self._popup = popup

        # Hook app-wide mouse-press detection while the popup is open.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._app_filter_installed = True

    def _close_popup(self) -> None:
        """Tear down the popup and remove the app-wide filter."""
        if self._popup is not None:
            self._popup.close()
            self._popup = None
        if getattr(self, "_app_filter_installed", False):
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._app_filter_installed = False

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        """Close the popup when:
          - It loses focus (FocusOut), OR
          - A mouse press lands outside the popup's geometry (app-wide filter).
        """
        if self._popup is None:
            return super().eventFilter(obj, event)
        # Local FocusOut filter (kept as belt-and-suspenders)
        if obj is self._popup and event.type() == QEvent.Type.FocusOut:
            self._close_popup()
            return super().eventFilter(obj, event)
        # App-wide mouse-press filter: any press outside the popup closes it.
        if event.type() == QEvent.Type.MouseButtonPress:
            # `event.globalPosition()` is a QPointF; convert to QPoint for
            # geometry containment.
            try:
                gp = event.globalPosition().toPoint()
            except AttributeError:
                # Older Qt versions fall back to globalPos()
                gp = event.globalPos()
            if not self._popup.frameGeometry().contains(gp):
                self._close_popup()
                # Don't consume the event — let the user's intended click go
                # through to whatever they actually targeted.
        return super().eventFilter(obj, event)


class _SymbolWidget(QWidget):
    """Checkbox grid for EPIC auto coverage symbols, plus an 'Other' free-text
    field for state-specific or rare endorsement-driven symbols.

    Each instance is configured with the symbol set that ISO CA 00 01 permits
    for its coverage row (Liability gets 1–4, 7–9, 19; Physical Damage gets
    2–4, 7–8; UM gets 2–4, 6, 7; etc.). Any symbol that arrives in state but
    isn't in the configured allow-list is surfaced via "Other" so operators
    see — and can correct — an out-of-scope value Claude extracted (e.g.,
    Symbol 1 on Comp/Collision, which ISO doesn't allow).

    Mimics the QLineEdit interface (``setText`` / ``text``) so the base-class
    ``refresh()`` can update it like any other input widget.
    """

    # Falls back to the full ISO 1–9 set when no allow-list is passed. New
    # callers should always pass the coverage-specific allow-list defined in
    # ``BusinessAutoForm._build_form``.
    _DEFAULT_SYMBOLS: tuple[str, ...] = ("1", "2", "3", "4", "5", "6", "7", "8", "9")

    def __init__(
        self,
        tag: str,
        emit_cb,
        allowed: list[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tag = tag
        self._emit_cb = emit_cb
        self._symbols: list[str] = (
            list(allowed) if allowed else list(self._DEFAULT_SYMBOLS)
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        # 3-column grid; row count derived from len(self._symbols). Sized so
        # Liability (8) fills 3 rows, Physical Damage / UM (5) fills 2 rows,
        # MedPay / Towing (4) fills 2 rows with a trailing gap.
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        self._boxes: dict[str, QCheckBox] = {}
        for i, s in enumerate(self._symbols):
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
        parts = [s for s in self._symbols if self._boxes[s].isChecked()]
        if self._other_cb.isChecked():
            extra = self._other_inp.text().strip()
            if extra:
                parts.append(extra)
        self._emit_cb(self._tag, ",".join(parts) if parts else None)

    def setText(self, v: str) -> None:
        tokens = [s.strip() for s in v.split(",")] if v else []
        standard = set(self._symbols)
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
        parts = [s for s in self._symbols if self._boxes[s].isChecked()]
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
        # NOTE: SectionFormBase.__init__ calls _refresh_tables(state) right
        # after _build_form() returns. Don't call it here too — a double
        # call combined with sorting-enabled tables causes phantom duplicate
        # rows (Qt re-sorts between the two passes and the second pass
        # writes to physical rows that the first pass had reshuffled).

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
        # Base __init__ runs _refresh_tables right after this — don't double-call.

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
        # Policy-level Coinsurance and Default Valuation singletons removed —
        # both are now captured per coverage subject below (one document can
        # specify different coinsurance percentages or valuation methods per
        # building, and rolling them up to a single policy-level value loses
        # information).

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
            f"{self.REPEATABLE_GROUP}.coinsurance",
            f"{self.REPEATABLE_GROUP}.deductible",
        ]
        self._prop_tbl = self._table(
            7, ["Loc #", "Bldg #", "Coverage", "Limit",
                "Valuation", "Coinsurance", "Deductible"],
            editable=True,
        )
        header = self._prop_tbl.horizontalHeader()
        for c in range(7):
            header.setSectionResizeMode(c, QHeaderView.ResizeMode.Interactive)
        self._prop_tbl.setColumnWidth(0, 50)
        self._prop_tbl.setColumnWidth(1, 55)
        self._prop_tbl.setColumnWidth(2, 160)
        self._prop_tbl.setColumnWidth(3, 110)
        self._prop_tbl.setColumnWidth(4, 110)
        self._prop_tbl.setColumnWidth(5, 100)
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
            subject_ref_columns=[
                ("Loc #",  "location_number"),
                ("Bldg #", "building_number"),
            ],
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

        # Per-coverage ISO CA 00 01 symbol allow-lists. Sources:
        #   - rnc-pro.com CA 00 01 form analysis
        #   - propertycasualty360.com "Business Auto Declarations and Coverage Symbols"
        #   - insurancejournal.com "BAC 2+8+9 Do NOT Equal Symbol 1"
        # Liability is the only coverage that gets Symbol 1 ("Any Auto") and
        # the only one that gets 9 (nonowned) or 19 (mobile equipment).
        # Symbol 5 is no-fault-only; Symbol 6 is UM-only.
        # "Other" stays on every row for state-specific symbols (TN, NY, MI
        # no-fault variants) and rare endorsement-driven codes.
        LIAB_SYMS:   list[str] = ["1", "2", "3", "4", "7", "8", "9", "19"]
        PHYS_SYMS:   list[str] = ["2", "3", "4", "7", "8"]   # Comp, SCoL, Collision
        UM_SYMS:     list[str] = ["2", "3", "4", "6", "7"]
        MEDPAY_SYMS: list[str] = ["2", "3", "4", "7"]
        PIP_SYMS:    list[str] = ["2", "3", "4", "5", "7"]
        TOWING_SYMS: list[str] = ["2", "3", "4", "7"]

        _gr = [0]  # mutable grid-row counter

        def _row(
            name: str,
            symbol_field: str,
            limit_pairs: list[tuple[str, str]],
            allowed_symbols: list[str],
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

            # Symbol checkbox grid spans all limit sub-rows. The allow-list
            # restricts which checkboxes render; out-of-set values from state
            # surface in the "Other" field so the operator can review them.
            tag = _resolve_tag(cov, symbol_field)
            sym_w = _SymbolWidget(tag, self.field_changed.emit, allowed=allowed_symbols)
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
                    # BAUT coverage rows are always currency limits — format
                    # the display the same way the rest of the singletons do.
                    inp.setText(_fmt_currency_display(v))

                def _ldone(t: str = ltag, w: QLineEdit = inp) -> None:
                    text = w.text().strip()
                    self.field_changed.emit(t, text or None)
                    if text:
                        masked = _fmt_currency_display(text)
                        if masked != text:
                            prev = w.blockSignals(True)
                            try:
                                w.setText(masked)
                            finally:
                                w.blockSignals(prev)

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
        ], LIAB_SYMS)
        _row("Medical Payments",         "chkMedical2",        [
            ("Limit",              "streMedicalLimit1"),
        ], MEDPAY_SYMS)
        _row("Uninsured / Underinsured", "chkUninsured2",      [
            ("CSL",                "streUninsuredCSLLimit1"),
            ("BI / Each Person",   "streUninsuredBILimit2"),
            ("BI / Each Accident", "streUninsuredBILimit1"),
            ("PD / Each Accident", "streUninsuredPDEachAccident"),
            ("PD Deductible",      "streUninsuredPDDeductible"),
        ], UM_SYMS)
        _row("Comprehensive",            "chkComprehensive2",  [
            ("Deductible",         "streComprehensiveDeductible1"),
        ], PHYS_SYMS)
        _row("Specified Causes of Loss", "chkCauseOfLoss2",    [
            ("Deductible",         "streCauseOfLossDeductible1"),
        ], PHYS_SYMS)
        _row("Collision",                "chkCollision2",      [
            ("Deductible",         "streCollisionDeductible1"),
        ], PHYS_SYMS)
        _row("Towing & Labor",           "chkTowing3",         [
            ("Limit",              "streTowingLimit1"),
        ], TOWING_SYMS)
        _row("Personal Injury Protection", "chkPersonalInjury2", [
            ("Limit",              "strePersonalInjuryLimit1"),
        ], PIP_SYMS)

        ba_wrap = QWidget()
        ba_wrap.setLayout(ba_grid)
        self._root.addWidget(ba_wrap)

        self._veh_group = "policy.auto.vehicle"
        # Type column dropped per 2026-05-26 UX pass — body_type isn't used
        # by the entry walker and rarely populated by extraction, so it just
        # added a wide empty column. Storage still holds the value; only the
        # display column was removed.
        self._veh_tags: list[str | None] = [
            f"{self._veh_group}.year",
            f"{self._veh_group}.make",
            f"{self._veh_group}.model",
            f"{self._veh_group}.vin",
            f"{self._veh_group}.garage_address.line_1",
            f"{self._veh_group}.comprehensive_deductible",
            f"{self._veh_group}.collision_deductible",
        ]
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Vehicles Schedule", "+ Add Vehicle", self._veh_group,
                              show_count=True)
        )
        self._veh_tbl = self._table(
            8, ["#", "Year", "Make", "Model", "VIN",
                "Garaging Address", "Comp Ded", "Coll Ded"]
        )
        self._veh_tbl.setColumnWidth(0, 36)
        self._veh_tbl.setColumnWidth(1, 50)
        self._veh_tbl.setColumnWidth(2, 80)
        self._veh_tbl.setColumnWidth(3, 100)
        self._veh_tbl.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._veh_tbl.setColumnWidth(5, 140)
        self._veh_tbl.setColumnWidth(6, 80)
        self._veh_tbl.setColumnWidth(7, 80)
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
            self._section_row("Driver Schedule", "+ Add Driver", self._drv_group,
                              show_count=True)
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
        # Base __init__ runs _refresh_tables right after this — don't double-call.

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
        sched_row = self._section_row(
            "Scheduled Items", "+ Add Item", self.SCHED_GROUP, show_count=True,
        )
        # "Possible duplicates" button, placed between the stretch and
        # the + Add Item button. Lights up (orange) when the
        # similarity-scored detector finds any flagged rows. Clicking
        # cycles through the flagged rows in the Scheduled table —
        # scrolls each into view and selects it so the operator can
        # review and resolve with the existing delete / move actions.
        self._dup_btn = QPushButton("Possible duplicates (0)")
        self._dup_btn.setObjectName("DupBtn")
        self._dup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dup_btn.setEnabled(False)
        self._dup_btn.clicked.connect(self._on_dup_btn_clicked)
        # Insert just before the + Add Item button (which is the last
        # item in the row layout, at position count()-1).
        sched_row.insertWidget(sched_row.count() - 1, self._dup_btn)
        self._root.addLayout(sched_row)
        self._dup_flagged_rows: list[int] = []
        self._dup_groups: list[list[int]] = []
        self._dup_filter_active: bool = False
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

        # Cross-section "Move to..." right-click targets. Read by the
        # context-menu builder in `_wire_table_edits`. Each tuple is
        # (destination_group, destination_label).
        self._sched_tbl._move_targets = [
            (_IM_UNSCHED_GROUP, "Unscheduled Equipment"),
            (_IM_AC_GROUP,      "Additional Coverages"),
        ]
        self._unsched_tbl._move_targets = [
            (_IM_SCHED_GROUP,   "Scheduled Equipment"),
            (_IM_AC_GROUP,      "Additional Coverages"),
        ]
        self._add_cov_tbl._move_targets = [
            (_IM_SCHED_GROUP,   "Scheduled Equipment"),
            (_IM_UNSCHED_GROUP, "Unscheduled Equipment"),
        ]
        # Base __init__ runs _refresh_tables right after this — don't double-call.

    def _on_dup_btn_clicked(self) -> None:
        """Toggle the duplicate-review filter on the Scheduled table.

        First click (filter ON): hide every row that isn't flagged as a
        possible duplicate, then reorder the visible rows so each
        duplicate group is contiguous (so all members of a group can
        be compared side by side). Button text becomes "Finish
        reviewing".

        Second click (filter OFF): re-run :meth:`refresh` which
        re-populates the table in canonical state-index order and
        un-hides all rows. Recomputes flagged-row set and group list
        for the next click. Button text reverts to "Show potential
        duplicates (N)".
        """
        if not self._dup_flagged_rows:
            return
        if self._dup_filter_active:
            # Restore: full refresh from state — re-populates rows in
            # canonical order, re-runs dedupe / dup detection / etc.
            # Re-enable sorting before the refresh: _apply_dup_filter
            # disabled it and _refresh_tables preserves the entry
            # state, so without this force-enable the table would stay
            # in "sorting disabled" mode after the refresh.
            self._sched_tbl.setSortingEnabled(True)
            self.refresh(self._state)
            return
        self._apply_dup_filter()

    def _apply_dup_filter(self) -> None:
        """Hide non-flagged rows and reorder flagged ones by group.

        Translates the state-index values in ``_dup_flagged_rows`` and
        ``_dup_groups`` to current visual row indices via the
        ``_STATE_INDEX_ROLE`` marker on column 0. The translation is
        required because Qt may have sorted the table after
        ``_refresh_tables`` populated it in state-index order — without
        translation, the reorder operates on the wrong visual positions
        and flagged rows aren't actually the ones at the top.
        """
        table = self._sched_tbl
        n = table.rowCount()

        # Build a state-index → current visual-row map.
        state_to_visual: dict[int, int] = {}
        for vis_r in range(n):
            cell = table.item(vis_r, 0)
            if cell is None:
                continue
            si = cell.data(_STATE_INDEX_ROLE)
            if isinstance(si, int):
                state_to_visual[si] = vis_r

        flagged_visual = {
            state_to_visual.get(si, si) for si in self._dup_flagged_rows
        }

        # Build visible_order in group order, translated to visual rows.
        visible_order: list[int] = []
        seen: set[int] = set()
        for group in self._dup_groups:
            for si in group:
                vis = state_to_visual.get(si, si)
                if vis in flagged_visual and vis not in seen:
                    visible_order.append(vis)
                    seen.add(vis)
        tail = [r for r in range(n) if r not in seen]
        new_order = visible_order + tail
        self._reorder_table_rows(table, new_order)
        n_visible = len(visible_order)
        for vis_r in range(n):
            table.setRowHidden(vis_r, vis_r >= n_visible)
        self._dup_filter_active = True
        if hasattr(self, "_dup_btn"):
            self._dup_btn.setText("Finish reviewing")

    @staticmethod
    def _reorder_table_rows(table: QTableWidget, new_order: list[int]) -> None:
        """Reorder rows of *table* so visual row i contains what was
        previously row ``new_order[i]``. Cells are physically moved
        (``takeItem`` + ``setItem``) so per-cell metadata (state-index
        marker, tooltips, backgrounds, sort keys) travels with them.

        Sorting is disabled on entry and **left disabled** on exit —
        re-enabling it would immediately re-sort by the active sort
        column and undo the manual ordering. The caller is responsible
        for re-enabling sort when appropriate (e.g., when exiting
        filter mode).
        """
        n = table.rowCount()
        if not new_order or len(new_order) != n:
            return
        cols = table.columnCount()
        prev_block = getattr(table, "_iga_block", False)
        table.setSortingEnabled(False)
        table._iga_block = True
        try:
            cells: list[list[QTableWidgetItem | None]] = [
                [table.takeItem(r, c) for c in range(cols)]
                for r in range(n)
            ]
            for new_r, old_r in enumerate(new_order):
                if not (0 <= old_r < n):
                    continue
                for c in range(cols):
                    item = cells[old_r][c]
                    if item is not None:
                        table.setItem(new_r, c, item)
        finally:
            table._iga_block = prev_block

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
        # Un-hide every row before populating. The duplicate-review
        # filter may have called setRowHidden on a prior refresh — if
        # we don't reset here, the new data lands in rows that are
        # still hidden and the table appears to show only a few items
        # (or none) even though state has many entries. setRowCount()
        # does NOT reset hidden flags for rows that survive the resize.
        for _r in range(self._sched_tbl.rowCount()):
            self._sched_tbl.setRowHidden(_r, False)
        # Disable sorting once for the entire post-processing pass — the
        # per-row `setItem` calls below would otherwise trigger Qt to
        # reorder rows after every assignment and corrupt the loop.
        _prev_sort_sched = self._sched_tbl.isSortingEnabled()
        if _prev_sort_sched:
            self._sched_tbl.setSortingEnabled(False)
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
                self._sched_tbl.setItem(r, 4, _SortableTableItem(clean))
                self._sched_tbl._iga_block = prev

        self._autofit_columns(self._sched_tbl)

        # Dedupe item numbers: any duplicate or blank gets the next unused
        # positive integer. Mirrors the EPIC-side _ensure_unique_item_numbers
        # so what the user sees is exactly what gets entered. Every change
        # is persisted back to state.json via ``field_changed`` so the GUI
        # and on-disk state stay in lockstep — no "display-only" drift.
        _num_tag = f"{self.SCHED_GROUP}.item_number"
        _used: set[str] = set()
        _next_free = 1
        for r, item in enumerate(sched):
            _rec = item.get(_num_tag)
            _raw = (str(_rec.get("value") or "").strip()
                    if isinstance(_rec, dict) else "")
            if _raw and _raw not in _used:
                _used.add(_raw)
                continue
            while str(_next_free) in _used:
                _next_free += 1
            _new = str(_next_free)
            _used.add(_new)
            _next_free += 1
            # Update the cell without firing cellChanged — we emit
            # field_changed explicitly so the persistence path runs once
            # per renumbered row with the correct state index. The
            # _STATE_INDEX_ROLE marker MUST be re-set on the new cell:
            # the duplicate-filter relies on this marker to translate
            # state indices to visual rows when the table is sorted,
            # and a fresh _SortableTableItem starts with no role data.
            _prev_block = getattr(self._sched_tbl, "_iga_block", False)
            self._sched_tbl._iga_block = True
            _new_cell = _SortableTableItem(_new)
            _new_cell.setData(_STATE_INDEX_ROLE, r)
            self._sched_tbl.setItem(r, 0, _new_cell)
            self._sched_tbl._iga_block = _prev_block
            self.field_changed.emit(
                f"__rep:{self.SCHED_GROUP}:{r}:{_num_tag}", _new,
            )

        # Possible-duplicate detection (rule-based, 2026-05-26).
        #
        # Two source documents on the same dec can produce two rows
        # for the same physical item — one with a serial and one
        # without, or with slight description variations. Operator
        # rules:
        #
        #   1. BOTH rows have a serial: compare ONLY the normalized
        #      serials (alphanumeric chars, uppercased). Match iff
        #      equal. Different serials = NOT a duplicate, regardless
        #      of how similar the other fields are.
        #
        #   2. ONE or NEITHER has a serial: year and amount MUST match
        #      exactly. Make and model use fuzzy match — alias map for
        #      "Chevy"/"Chevrolet", "JD"/"John Deere", etc.;
        #      corporate-suffix strip for "Topcon LTD" → "Topcon";
        #      token-prefix so "Topcon" ⊆ "Topcon Holdings".
        import re as _re

        _MAKE_ALIASES = {
            "chevy": "chevrolet",
            "chev": "chevrolet",
            "cat": "caterpillar",
            "jd": "john deere",
            "ih": "international harvester",
            "intl": "international",
            "mb": "mercedes-benz",
            "vw": "volkswagen",
            "kw": "kenworth",
            "fl": "freightliner",
        }
        _MAKE_SUFFIXES = frozenset({
            "ltd", "limited",
            "inc", "incorporated",
            "corp", "corporation",
            "co", "company",
            "llc",
            "holdings", "group", "partners",
            "sales", "services",
            "mfg", "manufacturing", "mfr",
            "division", "div", "dba",
        })

        def _norm_serial(s: str) -> str:
            return _re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()

        def _norm_make_tokens(s: str) -> list[str]:
            s2 = _re.sub(r"[^A-Za-z0-9 ]", " ", str(s or "")).lower()
            s2 = _re.sub(r"\s+", " ", s2).strip()
            if not s2:
                return []
            if s2 in _MAKE_ALIASES:
                s2 = _MAKE_ALIASES[s2]
            tokens = s2.split()
            while tokens and tokens[-1] in _MAKE_SUFFIXES:
                tokens.pop()
            return tokens

        def _norm_model(s: str) -> str:
            return _re.sub(r"[^A-Za-z0-9]", "", str(s or "")).upper()

        def _norm_amount(s: str) -> int | None:
            digits = _re.sub(r"[^\d.]", "", str(s or ""))
            if not digits:
                return None
            try:
                return int(float(digits))
            except ValueError:
                return None

        def _makes_match(a: str, b: str) -> bool:
            ta = _norm_make_tokens(a)
            tb = _norm_make_tokens(b)
            if not ta or not tb:
                return False
            short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            return short == long_[: len(short)]

        def _models_match(a: str, b: str) -> bool:
            na, nb = _norm_model(a), _norm_model(b)
            return bool(na) and bool(nb) and na == nb

        # Per-row features for matching + tooltip rendering.
        _feats: list[dict] = []
        for _r, _item in enumerate(sched):
            def _v_of(tag: str, _it=_item) -> str:
                rec = _it.get(f"{self.SCHED_GROUP}.{tag}")
                return str(rec.get("value") or "").strip() if isinstance(rec, dict) else ""
            _feats.append({
                "year":   _v_of("model_year"),
                "make":   _v_of("manufacturer"),
                "model":  _v_of("model"),
                "amount": _norm_amount(_v_of("amt_insurance")),
                "serial": _norm_serial(_v_of("serial_number")),
            })

        def _is_potential_dup(i: int, j: int) -> tuple[bool, str]:
            """Return (is_dup, reason_for_tooltip)."""
            fi, fj = _feats[i], _feats[j]
            si, sj = fi["serial"], fj["serial"]
            if si and sj:
                # Both have serials — strict serial-equality rule.
                if si == sj:
                    return True, "same serial number"
                return False, ""
            # At least one missing serial: exact year + exact amount
            # + fuzzy make + fuzzy model.
            if not fi["year"] or not fj["year"] or fi["year"] != fj["year"]:
                return False, ""
            if (fi["amount"] is None or fj["amount"] is None
                    or fi["amount"] != fj["amount"]):
                return False, ""
            if not _makes_match(fi["make"], fj["make"]):
                return False, ""
            if not _models_match(fi["model"], fj["model"]):
                return False, ""
            return True, "same year, make, model, and amount"

        # Pairwise check + union-find clustering (A↔B + B↔C → {A,B,C}).
        _flagged: set[int] = set()
        _row_best: dict[int, tuple[int, str]] = {}
        _parent = list(range(len(sched)))

        def _find(x: int) -> int:
            while _parent[x] != x:
                _parent[x] = _parent[_parent[x]]
                x = _parent[x]
            return x

        def _union(a: int, b: int) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                _parent[ra] = rb

        for i in range(len(sched)):
            for j in range(i + 1, len(sched)):
                ok, reason = _is_potential_dup(i, j)
                if not ok:
                    continue
                _flagged.add(i)
                _flagged.add(j)
                _union(i, j)
                _row_best.setdefault(i, (j, reason))
                _row_best.setdefault(j, (i, reason))

        # Cluster flagged rows into groups via union-find roots, then
        # sort groups by their lowest row index for stable display order.
        _groups_by_root: dict[int, list[int]] = {}
        for _r in _flagged:
            _root = _find(_r)
            _groups_by_root.setdefault(_root, []).append(_r)
        self._dup_groups = sorted(
            (sorted(g) for g in _groups_by_root.values()),
            key=lambda g: g[0],
        )

        # Stash the flagged rows + update the "Possible duplicates"
        # button. Any prior filter is cleared because this is a fresh
        # full refresh — all rows are visible and in canonical order.
        self._dup_flagged_rows = sorted(_flagged)
        self._dup_filter_active = False
        if hasattr(self, "_dup_btn"):
            _n = len(self._dup_flagged_rows)
            if _n > 0:
                self._dup_btn.setText(f"Show potential duplicates ({_n})")
                self._dup_btn.setEnabled(True)
                self._dup_btn.setStyleSheet(
                    "QPushButton#DupBtn { background: #fde68a; "
                    "color: #78350f; border: 1px solid #f59e0b; "
                    "font-weight: 600; padding: 6px 12px; "
                    "border-radius: 4px; }"
                    "QPushButton#DupBtn:hover { background: #fcd34d; }"
                )
            else:
                self._dup_btn.setText("Possible duplicates (0)")
                self._dup_btn.setEnabled(False)
                self._dup_btn.setStyleSheet("")

        _DUP_TINT = QColor("#fef08a")  # pale yellow — distinct from the
                                       # conflict #fff7ed (peach) tint.
        for _r in sorted(_flagged):
            _other_r, _reason = _row_best[_r]
            _other_cell = self._sched_tbl.item(_other_r, 0)
            _other_lbl = _other_cell.text() if _other_cell else str(_other_r + 1)
            _tip = (
                f"Possible duplicate of item #{_other_lbl} ({_reason}).\n"
                "Review and delete or merge if these represent the "
                "same equipment."
            )
            _prev_block = getattr(self._sched_tbl, "_iga_block", False)
            self._sched_tbl._iga_block = True
            _item_cell = self._sched_tbl.item(_r, 0)
            if _item_cell is not None:
                _item_cell.setBackground(_DUP_TINT)
                _item_cell.setToolTip(_tip)
            for _col in (1, 2, 3):
                _sig_cell = self._sched_tbl.item(_r, _col)
                if _sig_cell is not None:
                    _sig_cell.setToolTip(_tip)
            self._sched_tbl._iga_block = _prev_block

        if _prev_sort_sched:
            self._sched_tbl.setSortingEnabled(True)

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
                _inp.setText(f"${int(_total):,}")
                _inp.blockSignals(False)

        # Canonical currency display for the Scheduled table: col 6 =
        # Deductible, col 7 = Limit. (No row_num offset on this table.)
        _reformat_currency_cells(self._sched_tbl, [6, 7])

        unsched = _rep(state, self.UNSCHED_GROUP)
        self._unsched_tbl.setRowCount(len(unsched))
        # EPIC caps the Unscheduled description at 30 characters. The
        # canonical short form lives in state.json under the sibling
        # ``description_short`` tag (defined in
        # ``Library/Epic Field Map.json``). On refresh:
        #   1. If state already has description_short, display it.
        #   2. If not AND description > 30 chars, compute via Claude,
        #      persist to state via field_changed (so future refreshes
        #      across app restarts skip the API call), and display.
        #   3. If description ≤ 30, no short form needed — display the
        #      original verbatim.
        # The full description is always available on hover.
        _abbreviate = None
        try:
            from iga_marketing_master_2.claude_client import abbreviate_for_epic as _abbreviate
        except Exception:  # noqa: BLE001
            _abbreviate = None
        _short_tag = f"{self.UNSCHED_GROUP}.description_short"
        _desc_tag = f"{self.UNSCHED_GROUP}.description"
        for r, item in enumerate(unsched):
            self._populate_row(self._unsched_tbl, r, self._unsched_tags, item, row_num=True)
            _desc_rec = item.get(_desc_tag)
            if not isinstance(_desc_rec, dict):
                continue
            _full = str(_desc_rec.get("value") or "").strip()
            if not _full:
                continue
            _cell = self._unsched_tbl.item(r, 1)
            if _cell is None:
                continue
            _cell.setToolTip(_full)

            # 1. Cached?
            _short_rec = item.get(_short_tag)
            _cached_short = (
                str(_short_rec.get("value") or "").strip()
                if isinstance(_short_rec, dict) else ""
            )
            if _cached_short:
                _display = _cached_short
            elif len(_full) <= 30:
                _display = _full
            elif _abbreviate is not None:
                try:
                    _display = _abbreviate(_full, max_chars=30) or _full
                except Exception:  # noqa: BLE001
                    _display = _full
                # Persist for next time. Defer the emit one event-loop
                # tick because this refresh runs during the form's
                # __init__, before the host has connected the
                # field_changed signal. Without the defer the emit is
                # dropped and state.json never gets the cached value
                # — the API gets re-called on every GUI restart.
                # Default-arg binding captures loop variables by value
                # so each row's deferred callback fires with its own
                # row index and short text (not the loop's last
                # iteration values).
                if _display and _display != _full:
                    from PySide6.QtCore import QTimer
                    QTimer.singleShot(
                        0,
                        lambda _r=r, _v=_display, _t=_short_tag, _g=self.UNSCHED_GROUP:
                            self.field_changed.emit(f"__rep:{_g}:{_r}:{_t}", _v),
                    )
            else:
                _display = _full

            if _display != _cell.text():
                _prev_block = getattr(self._unsched_tbl, "_iga_block", False)
                self._unsched_tbl._iga_block = True
                _cell.setText(_display)
                self._unsched_tbl._iga_block = _prev_block
        # Canonical currency display for Unscheduled: col 2 = Per-Item
        # Max, col 3 = Total Limit. (row_num=True offsets data cols by 1.)
        _reformat_currency_cells(self._unsched_tbl, [2, 3])
        self._autofit_columns(self._unsched_tbl)
        self._refresh_ai_table(state)
        self._refresh_forms_table(state)
        self._refresh_cov_table(state)
        # Canonical currency display for the IM Additional Coverages
        # table: col 1 = Each Claim, col 2 = Aggregate, col 3 =
        # Deductible. (_add_cov_table doesn't use row_num.)
        if hasattr(self, "_add_cov_tbl"):
            _reformat_currency_cells(self._add_cov_tbl, [1, 2, 3])


class WorkersCompForm(SectionFormBase):
    """Worker's Compensation.

    Layout (top → bottom):
      - "Worker's Compensation" header
      - "Part 1 - Active States" sub-header + multi-select picker
      - "Employers Liability Limits" sub-header + three limit inputs
      - "Other States" sub-header + multi-select picker (with "All Other")
        that excludes any state already chosen as Active
      - "Rating Information" sub-header + per-state table (Experience Mod,
        Scheduled Rating, Deductible) that auto-syncs with the union of
        Active + Other selections
      - Class Codes table, Forms table, Additional Coverages table
    """

    REPEATABLE_GROUP = "policy.workers_comp.class_code"
    RATING_INFO_GROUP = "policy.workers_comp.rating_info"

    def _build_form(self) -> None:
        self._root.addWidget(self._hdr("Worker's Compensation"))

        # -- Part 1 — Active States ------------------------------------------
        self._root.addWidget(self._sub_hdr("Part 1 - Active States"))
        active_tag = "policy.workers_comp.part1_states"
        self._active_states_w = _StatesMultiSelectWidget(
            active_tag,
            self.field_changed.emit,
            include_all_other=False,
        )
        sv = _val(self._state, active_tag)
        if sv:
            self._active_states_w.setText(sv)
        self._inputs[active_tag] = self._active_states_w
        self._root.addWidget(self._active_states_w)

        # -- Employers Liability Limits --------------------------------------
        self._root.addWidget(_hr())
        self._root.addWidget(self._sub_hdr("Employers Liability Limits"))
        el = self._grid(2)
        self._add_text(el, 0, 0, "Each Accident",
                       "policy.workers_comp.each_accident", "$")
        self._add_text(el, 0, 1, "Disease – Policy Limit",
                       "policy.workers_comp.disease_policy_limit", "$")
        self._add_text(el, 1, 0, "Disease – Each Employee",
                       "policy.workers_comp.disease_each_employee", "$")
        self._root.addLayout(el)

        # -- Other States ----------------------------------------------------
        # Mapped to `policy.workers_comp.part3_states` — Part 3 in standard
        # WC parlance is "Other States Insurance", which is exactly what
        # this picker captures.
        self._root.addWidget(_hr())
        self._root.addWidget(self._sub_hdr("Other States"))
        other_tag = "policy.workers_comp.part3_states"
        self._other_states_w = _StatesMultiSelectWidget(
            other_tag,
            self.field_changed.emit,
            include_all_other=True,
        )
        ov = _val(self._state, other_tag)
        if ov:
            self._other_states_w.setText(ov)
        self._inputs[other_tag] = self._other_states_w
        self._root.addWidget(self._other_states_w)

        # Active selection drives Other's exclusion list. Initial sync from
        # the loaded state, then live updates as Active changes.
        self._sync_other_exclusion()
        self._active_states_w.selection_changed.connect(
            lambda _codes: self._sync_other_exclusion()
        )
        # Only Active States drive the Rating Information table contents.
        # Other States Insurance (Part 3) is a contingency placeholder and
        # doesn't carry its own experience mod / scheduled rating /
        # deductible, so changes to that picker don't refresh the table.
        self._active_states_w.selection_changed.connect(
            lambda _codes: self._refresh_rating_table()
        )

        # -- Rating Information ----------------------------------------------
        self._root.addWidget(_hr())
        self._root.addWidget(self._sub_hdr("Rating Information"))
        self._rating_tbl = self._table(
            4, ["State", "Experience Mod", "Scheduled Rating", "Deductible"],
        )
        self._rating_tbl.setColumnWidth(0, 90)
        self._rating_tbl.setColumnWidth(1, 130)
        self._rating_tbl.setColumnWidth(2, 130)
        self._rating_tbl.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Stretch
        )
        self._rating_tbl.setMinimumHeight(_TBL_HEIGHT_6_ROWS)
        self._rating_tbl.cellChanged.connect(self._on_rating_cell_changed)
        self._root.addWidget(self._rating_tbl, 1)

        # -- Class Codes (existing repeatable, unchanged) --------------------
        self._root.addWidget(_hr())
        self._root.addLayout(
            self._section_row("Class Codes", "+ Add Class Code", self.REPEATABLE_GROUP)
        )
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
        # Base __init__ runs _refresh_tables right after this — don't double-call.

    def _sub_hdr(self, text: str) -> QLabel:
        """Sub-header label (smaller than ``_hdr``, used inside a coverage
        section to label sub-groups like 'Part 1 - Active States')."""
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-weight: 600; font-size: 12px; color: #334155;"
            " padding-top: 4px; padding-bottom: 2px;"
        )
        return lbl

    def _selected_state_codes(self) -> list[str]:
        """States that appear in the Rating Information table.

        Only Active States (Part 1) are surfaced. Other States Insurance
        (Part 3) doesn't carry per-state rating data, and the "All Other"
        sentinel never gets its own rating row.
        """
        if not hasattr(self, "_active_states_w"):
            return []
        return [
            c for c in self._active_states_w._ordered_selected()
            if c in _US_STATE_CODES
        ]

    def _sync_other_exclusion(self) -> None:
        active_codes = {
            c for c in self._active_states_w._ordered_selected()
            if c in _US_STATE_CODES
        }
        self._other_states_w.set_excluded(active_codes)

    # -- Rating Information table -------------------------------------------

    _RATING_COLUMN_TAGS: tuple[str, str, str] = (
        "experience_mod",
        "scheduled_rating",
        "deductible",
    )

    def _refresh_rating_table(self) -> None:
        if not hasattr(self, "_rating_tbl"):
            return
        ordered = self._selected_state_codes()
        # Index existing rating-info items by state code so display rows can
        # pull pre-existing values.
        items = _rep(self._state, self.RATING_INFO_GROUP)
        by_state: dict[str, dict] = {}
        for item in items:
            rec = item.get(f"{self.RATING_INFO_GROUP}.state")
            if isinstance(rec, dict):
                code = str(rec.get("value") or "").strip().upper()
                if code:
                    by_state[code] = item

        self._rating_tbl._iga_block = True
        _prev_sort_rating = self._rating_tbl.isSortingEnabled()
        if _prev_sort_rating:
            self._rating_tbl.setSortingEnabled(False)
        try:
            self._rating_tbl.setRowCount(len(ordered))
            for r, code in enumerate(ordered):
                state_label = (
                    "All Other" if code == ALL_OTHER_STATES_TOKEN else code
                )
                state_cell = _SortableTableItem(state_label)
                state_cell.setFlags(state_cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                state_cell.setData(Qt.ItemDataRole.UserRole, code)
                self._rating_tbl.setItem(r, 0, state_cell)
                item = by_state.get(code, {})
                for c, suffix in enumerate(self._RATING_COLUMN_TAGS, start=1):
                    rec = item.get(f"{self.RATING_INFO_GROUP}.{suffix}")
                    v = ""
                    if isinstance(rec, dict):
                        v = str(rec.get("value") or "")
                    self._rating_tbl.setItem(r, c, _SortableTableItem(v))
        finally:
            self._rating_tbl._iga_block = False
            if _prev_sort_rating:
                self._rating_tbl.setSortingEnabled(True)
        self._autofit_columns(self._rating_tbl)

    def _on_rating_cell_changed(self, row: int, col: int) -> None:
        if getattr(self._rating_tbl, "_iga_block", False):
            return
        if col == 0:
            return  # State column is read-only display.
        if not (1 <= col <= 3):
            return
        state_cell = self._rating_tbl.item(row, 0)
        if state_cell is None:
            return
        state_code = state_cell.data(Qt.ItemDataRole.UserRole)
        if not state_code:
            return
        val_cell = self._rating_tbl.item(row, col)
        value = val_cell.text().strip() if val_cell else ""
        suffix = self._RATING_COLUMN_TAGS[col - 1]
        full_tag = f"{self.RATING_INFO_GROUP}.{suffix}"
        # Emit the upsert-by-state protocol so the host creates the row on
        # first edit if no rating_info item exists for this state yet.
        self.field_changed.emit(
            f"__upsert_by_state:{self.RATING_INFO_GROUP}:{state_code}:{full_tag}",
            value or None,
        )

    def _refresh_tables(self, state: dict | None) -> None:
        if not hasattr(self, "_cc_tbl"):
            return
        # Class codes (existing behavior).
        items = _rep(state, self.REPEATABLE_GROUP)
        self._cc_tbl.setRowCount(len(items))
        for r, item in enumerate(items):
            self._populate_row(self._cc_tbl, r, self._cc_tags, item, row_num=True)
        self._autofit_columns(self._cc_tbl)
        # Rating-info table tracks the multi-select widgets, which the base
        # `refresh()` has already updated via setText.
        self._sync_other_exclusion()
        self._refresh_rating_table()
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
        # Base __init__ runs _refresh_tables right after this — don't double-call.

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
        # Base __init__ runs _refresh_tables right after this — don't double-call.

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

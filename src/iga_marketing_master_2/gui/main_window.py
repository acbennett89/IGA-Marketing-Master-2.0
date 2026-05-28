"""main_window.py — ``IgaApp`` entry point + ``MainWindow``.

The main window stitches together the section tabs, repeatable panes, PDF
preview, audit log, and run-controls toolbar. State lives in
``state.json``; the window reads it via :func:`state.load` and writes
through :func:`state.save_atomic` after every mutation.

Tab derivation is driven by the populated ``state.fields`` and
``state.repeatables`` keys, mapped through :data:`TAB_LABELS`. See
``DECISION-MAP-gui-agent.md`` section 1 for the ruling on open item §14 #3.

References:
- ARCHITECTURE.md §8 (full GUI contract)
- DECISION-MAP-gui-agent.md
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QSettings, QSize, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QDesktopServices, QIcon, QKeySequence, QMovie
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import config as config_module
from .. import secret_store
from ..logger import get_logger
from .audit_log import AuditLogPane
from .operator_modal import (
    ApiKeyPromptDialog,
    ConflictResolutionDialog,
    EpicValidationPauseDialog,
    PauseChoice,
    RecoverInterruptedRunDialog,
    SelectorUnresolvedPauseDialog,
)
from .pending_pdfs_pane import PendingPdfsPane
from .repeatable_pane import RepeatablePane
from .sidebar import SidebarNav
from .upload_panel import UploadPanel
from .run_controls import RunControlsBar
from .section_table import (
    CONFIDENCE_HIGH_THRESHOLD,
    BulkActionBar,
    FieldRow,
    SectionTableModel,
    SectionTableView,
    is_audit_exempt_tag,
    is_low_confidence_row,
)
from .welcome_pane import WelcomePane

__all__ = [
    "IgaApp",
    "MainWindow",
    "TAB_LABELS",
    "TAB_ORDER",
    "build_tab_label",
    "count_low_confidence_in_tab",
    "count_tab_field_total",
    "load_recent_clients",
    "save_recent_clients",
    "update_recent_clients",
]


_logger = get_logger("gui.main_window")


# ---------------------------------------------------------------------------
# Persistence keys (QSettings) — centralized so #3 and #6 stay in sync.
# ---------------------------------------------------------------------------


_QSETTINGS_ORG: str = "IGA Marketing"
_QSETTINGS_APP: str = "IGA Marketing Master 2.0"

_QS_GEOMETRY: str = "ui/geometry"
_QS_WINDOW_STATE: str = "ui/windowState"
_QS_CENTER_SPLITTER: str = "ui/centerSplitter"
_QS_OUTER_SPLITTER: str = "ui/outerSplitter"
_QS_RECENT_CLIENTS: str = "session/recentClients"
_QS_VIEW_PDF_VISIBLE: str = "view/pdfPreviewVisible"

_RECENT_CLIENTS_MAX: int = 5

# ---------------------------------------------------------------------------
# Application-level stylesheet
# ---------------------------------------------------------------------------

_APP_QSS = """
/* ── Global reset — ensure light backgrounds everywhere ── */
QMainWindow, QWidget {
    background: white;
    color: #1e293b;
}

/* ── Context menus ── */
QMenu {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 4px;
    font-size: 12px;
    color: #1e293b;
}
QMenu::item {
    padding: 6px 16px;
    border-radius: 4px;
}
QMenu::item:selected {
    background: #eff6ff;
    color: #1d4ed8;
}
QMenu::separator {
    height: 1px;
    background: #e2e8f0;
    margin: 4px 8px;
}

/* ── Tooltips ── */
QToolTip {
    background: #fffbeb;
    color: #1e293b;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    padding: 6px 8px;
    font-size: 12px;
}
QMainWindow {
    background: #f1f5f9;
}
QWidget#MainContent {
    background: #f1f5f9;
}

/* ── Page containers ── */
QWidget#DataReviewPage,
QWidget#ContentArea {
    background: white;
}

/* ── Current-client banner (visible only when a non-draft client is active) ── */
QLabel#ClientBanner {
    background: #1e3a8a;
    color: #f8fafc;
    font-size: 13px;
    font-weight: 600;
    padding: 6px 22px;
    border-bottom: 1px solid #1e40af;
}

/* ── Page header ── */
QWidget#PageHeaderBar {
    background: white;
    border-bottom: 1px solid #e2e8f0;
}
QLabel#PageTitle {
    font-size: 20px;
    font-weight: bold;
    color: #0f172a;
}
QLabel#PageSubtitle {
    font-size: 12px;
    color: #64748b;
}

/* ── Header action buttons ── */
QPushButton#HeaderBtnPrimary {
    background: #2563eb;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 8px 18px;
    font-size: 13px;
    font-weight: bold;
    min-width: 90px;
}
QPushButton#HeaderBtnPrimary:hover  { background: #1d4ed8; }
QPushButton#HeaderBtnPrimary:disabled {
    background: #93c5fd;
    color: #dbeafe;
}
QPushButton#HeaderBtnSecondary {
    background: white;
    color: #374151;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 7px 18px;
    font-size: 13px;
    min-width: 80px;
}
QPushButton#HeaderBtnSecondary:hover {
    background: #f9fafb;
    border-color: #9ca3af;
}
QPushButton#HeaderBtnSecondary:disabled {
    color: #d1d5db;
    border-color: #e5e7eb;
}

/* ── Upload panel divider ── */
QFrame#PanelDivider {
    background: #e2e8f0;
    min-width: 1px;
    max-width: 1px;
    border: none;
}

/* ── Splitters ── */
QSplitter {
    background: white;
}
QSplitter::handle {
    background: #e2e8f0;
}
QSplitter::handle:horizontal {
    width: 1px;
}
QSplitter::handle:vertical {
    height: 1px;
}

/* ── Custom tab button bar ── */
QScrollArea#TabBarScroll,
QScrollArea#TabBarScroll > QWidget,
QScrollArea#TabBarScroll > QWidget > QWidget,
QWidget#TabBarInner {
    background: white;
}
QScrollArea#TabBarScroll {
    border: none;
    border-bottom: 1px solid #e2e8f0;
}
QScrollArea#TabBarScroll QScrollBar:horizontal {
    height: 8px;
    background: #f1f5f9;
    margin: 0;
    border-radius: 4px;
}
QScrollArea#TabBarScroll QScrollBar::handle:horizontal {
    background: #94a3b8;
    border-radius: 4px;
    min-width: 30px;
}
QScrollArea#TabBarScroll QScrollBar::handle:horizontal:hover {
    background: #64748b;
}
QScrollArea#TabBarScroll QScrollBar::add-line:horizontal,
QScrollArea#TabBarScroll QScrollBar::sub-line:horizontal {
    width: 0px;
}
QPushButton#TabBtn {
    background: transparent;
    border: none;
    border-bottom: 3px solid transparent;
    border-radius: 0px;
    padding: 9px 14px;
    margin: 0 1px;
    color: #64748b;
    font-size: 12px;
    min-height: 42px;
    max-height: 42px;
}
QPushButton#TabBtn:hover {
    color: #374151;
    background: #f8fafc;
}
QPushButton#TabBtn[active="true"] {
    color: #2563eb;
    border-bottom: 3px solid #2563eb;
    font-weight: bold;
}
QPushButton#TabBtn[dimmed="true"] {
    color: #cbd5e1;
}
/* ── Tab content stack ── */
QStackedWidget#TabStack {
    background: white;
}

/* ── File queue list (PendingPdfsPane) ── */
QListWidget {
    background: white;
    border: 1px solid #e2e8f0;
    border-radius: 4px;
    outline: none;
}
QListWidget::item {
    padding: 5px 8px;
    color: #374151;
    border-radius: 3px;
}
QListWidget::item:hover {
    background: #f1f5f9;
}
QListWidget::item:selected {
    background: #eff6ff;
    color: #1d4ed8;
}
QListWidget::item:alternate {
    background: #f8fafc;
}

/* ── Audit log ── */
QPlainTextEdit {
    background: white;
    border: none;
    border-top: 1px solid #e2e8f0;
    color: #374151;
    font-size: 11px;
}

/* ── Run-controls bar ── */
RunControlsBar {
    background: white;
    border-top: 1px solid #e2e8f0;
}

/* ── Section tables ── */
QTableView {
    background: white;
    gridline-color: #f1f5f9;
    border: none;
    selection-background-color: #eff6ff;
    selection-color: #1e293b;
}
QHeaderView::section {
    background: #f8fafc;
    border: none;
    border-bottom: 1px solid #e2e8f0;
    border-right: 1px solid #e2e8f0;
    padding: 5px 8px;
    color: #475569;
    font-size: 11px;
    font-weight: bold;
}

/* ── Placeholder pages ── */
QWidget#SettingsPage,
QWidget#HelpPage {
    background: #f8fafc;
}
QLabel#PlaceholderTitle {
    font-size: 18px;
    font-weight: bold;
    color: #374151;
    background: transparent;
}
QLabel#PlaceholderBody {
    font-size: 13px;
    color: #6b7280;
    background: transparent;
}

/* ── Status bar ── */
QStatusBar {
    background: #f8fafc;
    border-top: 1px solid #e2e8f0;
    color: #475569;
    font-size: 11px;
}
"""

# ---------------------------------------------------------------------------
# Page header bar (title + subtitle + primary action buttons)
# ---------------------------------------------------------------------------


class _PageHeaderBar(QWidget):
    """Sticky header strip at the top of the Data Review page.

    Contains the page title, a subtitle, and the Extract / Begin Entry action
    buttons so the operator can launch runs without scrolling to the bottom bar.
    """

    extract_clicked = Signal()
    begin_entry_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PageHeaderBar")
        self.setFixedHeight(68)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(20, 0, 20, 0)
        layout.setSpacing(12)

        # Title + subtitle block
        title_block = QWidget()
        tb = QVBoxLayout(title_block)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(2)

        title = QLabel("Data Extraction & Review")
        title.setObjectName("PageTitle")
        tb.addWidget(title)

        self._subtitle_default = (
            "Review and verify extracted insurance data from your PDFs."
        )
        self._subtitle_draft = (
            "Draft client — type the Insured Name on the Client tab; "
            "clicking Extract saves the file under that name."
        )
        self._subtitle = QLabel(self._subtitle_default)
        self._subtitle.setObjectName("PageSubtitle")
        self._subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        tb.addWidget(self._subtitle)

        layout.addWidget(title_block, 1)

        # Action buttons
        self.extract_btn = QPushButton("Extract")
        self.extract_btn.setObjectName("HeaderBtnSecondary")
        self.extract_btn.setToolTip("Run extraction on queued PDFs (Ctrl+E).")
        self.extract_btn.clicked.connect(self.extract_clicked)
        layout.addWidget(self.extract_btn)

        self.begin_btn = QPushButton("Begin Entry")
        self.begin_btn.setObjectName("HeaderBtnPrimary")
        self.begin_btn.setToolTip(
            "Start entering extracted fields into EPIC (Ctrl+Return)."
        )
        self.begin_btn.clicked.connect(self.begin_entry_clicked)
        layout.addWidget(self.begin_btn)

    def set_draft_mode(self, active: bool) -> None:
        """Swap the subtitle to flag draft-client mode (or back to default)."""
        self._subtitle.setText(self._subtitle_draft if active else self._subtitle_default)


# ---------------------------------------------------------------------------
# Tab-derivation vocabulary
# ---------------------------------------------------------------------------


TAB_LABELS: dict[str, str] = {
    # Default coverage sections (always shown)
    "account": "Named Insureds",
    "location": "Locations",
    "policy.gl": "General Liability",
    "policy.property": "Property",
    "policy.auto": "Business Auto",
    "policy.inland_marine": "Inland Marine",
    "policy.workers_comp": "Worker's Compensation",
    "policy.umbrella": "Umbrella/Excess",
    "forms": "Unclassified Forms",
    "notes": "Notes",
    # Extended sections (appear when data is present)
    "submission": "Submission",
    "producer": "Producer",
    "policy.crime": "Crime",
    "policy.cyber": "Cyber",
    "policy.professional": "Professional",
    "policy.directors_officers": "D&O",
    "policy.employment_practices": "EPL",
    "policy.pollution": "Pollution",
    "vehicle": "Vehicles",
    "driver": "Drivers",
    "additional_insured": "Additional Insureds",
    "prior_carrier": "Prior Carriers",
    "loss": "Loss History",
}

# Order in which tabs appear; namespaces not listed here append after, sorted.
TAB_ORDER: tuple[str, ...] = tuple(TAB_LABELS.keys())

# Tabs that are always visible regardless of whether client data exists.
DEFAULT_TAB_KEYS: tuple[str, ...] = (
    "account",
    "location",
    "policy.gl",
    "policy.property",
    "policy.auto",
    "policy.inland_marine",
    "policy.workers_comp",
    "policy.umbrella",
    "forms",
    "notes",
)

# Repeatable namespaces use the RepeatablePane variant; everything else is a
# plain SectionTableView over filtered state.fields.
REPEATABLE_NAMESPACES: frozenset[str] = frozenset(
    {"vehicle", "driver", "location", "loss_payee", "additional_insured", "prior_carrier", "loss"}
)

# Namespaces that should never appear as tabs even if data is extracted into
# them. The data is preserved in state.json (useful for diagnostics) but the
# operator-facing GUI hides it. Keep entries here in sync with the upcoming
# domain_tags registry.
HIDDEN_NAMESPACES: frozenset[str] = frozenset({"submission", "producer"})


def derive_tab_keys(state: dict | None) -> list[str]:
    """Return the ordered list of tab keys for ``state``.

    Always includes DEFAULT_TAB_KEYS so the coverage sections are visible
    even before any data is extracted. Additional keys (vehicles, etc.) are
    appended when present in the state. Namespaces in HIDDEN_NAMESPACES are
    suppressed unconditionally.
    """
    keys: set[str] = set(DEFAULT_TAB_KEYS)
    if state:
        fields_map = state.get("fields") or {}
        for tag in fields_map.keys():
            keys.add(_tab_key_for_tag(tag))
        # Repeatable groups get collapsed to their parent tab namespace
        # (e.g., 'policy.gl.hazard' rolls up to the 'policy.gl' tab where
        # the hazards are rendered as an embedded table). Without this
        # roll-up, every LOB-nested repeatable group would surface as its
        # own top-level tab.
        for group in (state.get("repeatables") or {}).keys():
            keys.add(_tab_key_for_tag(group))

    keys -= HIDDEN_NAMESPACES

    ordered = [k for k in TAB_ORDER if k in keys]
    leftovers = sorted(keys - set(TAB_ORDER))
    return ordered + leftovers


def _tab_key_for_tag(domain_tag: str) -> str:
    """Map a ``domain_tag`` to its tab key.

    Per DECISION-MAP §1 ruling: ``policy.<lob>.*`` rolls up to
    ``policy.<lob>``; everything else collapses to its first segment.
    """
    parts = domain_tag.split(".")
    if not parts:
        return "submission"
    if parts[0] == "policy" and len(parts) >= 2:
        return f"policy.{parts[1]}"
    return parts[0]


def label_for_tab_key(key: str) -> str:
    """Friendly label for a tab key; falls back to a title-cased default."""
    if key in TAB_LABELS:
        return TAB_LABELS[key]
    return key.replace("_", " ").replace(".", " · ").title()


# ---------------------------------------------------------------------------
# Tab-badge counting (UX-pass #4) — pure functions, easy to unit-test.
# ---------------------------------------------------------------------------


def count_tab_field_total(state: dict | None, tab_key: str) -> int:
    """Return the total field/item count surfaced on the named tab.

    For repeatable namespaces (vehicle, location, ...) the count is
    ``len(state.repeatables[tab_key])``. For singleton tabs it's the
    number of ``state.fields`` keys whose ``_tab_key_for_tag`` resolves to
    ``tab_key`` plus the count of repeatable items in any group whose
    name starts with ``tab_key`` (e.g., ``policy.gl.hazard`` rolls up
    into the ``policy.gl`` tab badge).
    """
    if not state:
        return 0
    if tab_key in REPEATABLE_NAMESPACES:
        items = (state.get("repeatables") or {}).get(tab_key, [])
        return len(items) if isinstance(items, list) else 0
    fields_map: dict = state.get("fields") or {}
    singleton_count = sum(
        1 for tag in fields_map.keys() if _tab_key_for_tag(tag) == tab_key
    )
    rep_map: dict = state.get("repeatables") or {}
    rep_count = 0
    prefix = tab_key + "."
    for group, items in rep_map.items():
        if group == tab_key or group.startswith(prefix):
            if isinstance(items, list):
                rep_count += len(items)
    return singleton_count + rep_count


def count_low_confidence_in_tab(state: dict | None, tab_key: str) -> int:
    """Return the number of fields on *tab_key* that still need attention.

    A field counts as flagged when ANY of these is true:

    * ``confidence < CONFIDENCE_HIGH_THRESHOLD`` (Claude wasn't sure), OR
    * ``conflicts`` is a non-empty list (two docs disagreed),

    AND its status isn't an operator-blessed terminal state
    (``approved`` / ``locked``). Including conflicts keeps the badge in
    sync with the orange table-cell tint conflict cells get — previously
    a tab could show conflict-tinted cells while reading ``0`` on the badge.

    Repeatable groups walk every record across every item.
    """
    if not state:
        return 0

    def _record_is_low(record: dict) -> bool:
        if not isinstance(record, dict):
            return False
        status = record.get("status", "pending")
        if status in {"approved", "locked"}:
            return False
        if record.get("conflicts"):
            return True
        try:
            conf = float(record.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return conf < CONFIDENCE_HIGH_THRESHOLD

    if tab_key in REPEATABLE_NAMESPACES:
        items = (state.get("repeatables") or {}).get(tab_key, [])
        if not isinstance(items, list):
            return 0
        return sum(
            1
            for item in items
            if isinstance(item, dict)
            for tag, record in item.items()
            if not is_audit_exempt_tag(tag) and _record_is_low(record)
        )

    fields_map: dict = state.get("fields") or {}
    singleton_low = sum(
        1
        for tag, record in fields_map.items()
        if _tab_key_for_tag(tag) == tab_key
        and not is_audit_exempt_tag(tag)
        and _record_is_low(record)
    )
    rep_map: dict = state.get("repeatables") or {}
    rep_low = 0
    prefix = tab_key + "."
    for group, items in rep_map.items():
        if (group == tab_key or group.startswith(prefix)) and isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                rep_low += sum(
                    1
                    for tag, record in item.items()
                    if not is_audit_exempt_tag(tag) and _record_is_low(record)
                )
    return singleton_low + rep_low


_CURRENCY_RE = __import__("re").compile(r"^\s*\$?\s*[\d,]+\s*$")


def _normalize_committed_value(value: object) -> object:
    """Strip ``$`` and ``,`` from currency-shaped values before storing.

    The GUI table cells display currency as ``$10,000`` via
    :func:`_fmt_currency_display`, but the canonical storage shape is bare
    digits (``10000``) so the entry walker doesn't need to strip symbols on
    its way into EPIC. Per the 2026-05-26 UX pass: "Currency will always be
    entered as a whole number, no decimal."

    Heuristic: anything matching ``^\\$?[\\d,]+$`` (optional ``$``, digits
    with commas) collapses to its digits-only form. Anything else
    (addresses with commas, free text, percent values, numerics already
    without symbols) passes through unchanged.
    """
    if value is None:
        return value
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return value
    if not _CURRENCY_RE.match(s):
        return value
    stripped = s.replace("$", "").replace(",", "").strip()
    # Defensive: if we somehow stripped down to nothing meaningful, keep
    # the original — never silently drop the operator's input.
    return stripped or value


def build_tab_label(state: dict | None, tab_key: str) -> str:
    """Return the user-visible tab label.

    Format:
      * ``"<Friendly Name>"`` when there are zero low-confidence fields
        (the common case once review is done) — clean chrome.
      * ``"<Friendly Name> (K!)"`` when ``K`` low-confidence fields exist
        — exclamation count surfaces what still needs attention.

    The previous behavior also showed total field counts (``(27)``);
    those were dropped per the 2026-05-26 UX pass since the total isn't
    actionable.
    """
    base = label_for_tab_key(tab_key)
    low = count_low_confidence_in_tab(state, tab_key)
    if low > 0:
        return f"{base} ({low}!)"
    return base


# ---------------------------------------------------------------------------
# Recent-clients persistence helpers (UX-pass #6) — QSettings-backed.
# Pure-ish helpers exposed at module scope so tests can drive QSettings via
# a temporary scope without pulling MainWindow into the picture.
# ---------------------------------------------------------------------------


def load_recent_clients(settings: QSettings) -> list[Path]:
    """Read the recent-clients list from ``QSettings``."""
    raw = settings.value(_QS_RECENT_CLIENTS, [])
    if isinstance(raw, str):
        # QSettings collapses single-element lists to a string on some
        # platforms; tolerate that.
        raw = [raw] if raw else []
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[Path] = []
    for item in raw:
        try:
            out.append(Path(str(item)))
        except (TypeError, ValueError):
            continue
    return out


def save_recent_clients(settings: QSettings, paths: list[Path]) -> None:
    """Persist the recent-clients list (capped to ``_RECENT_CLIENTS_MAX``)."""
    capped = [str(p) for p in paths[:_RECENT_CLIENTS_MAX]]
    settings.setValue(_QS_RECENT_CLIENTS, capped)


def update_recent_clients(existing: list[Path], new_path: Path) -> list[Path]:
    """Return a deduped, capped list with ``new_path`` at the front.

    Pure helper — does not touch QSettings.
    """
    resolved_new = _resolve_or_self(new_path)
    out: list[Path] = [resolved_new]
    seen: set[str] = {str(resolved_new).lower()}
    for p in existing:
        key = str(_resolve_or_self(p)).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
        if len(out) >= _RECENT_CLIENTS_MAX:
            break
    return out[:_RECENT_CLIENTS_MAX]


def _resolve_or_self(p: Path) -> Path:
    """``Path.resolve()`` with a self-fallback for missing folders."""
    try:
        return p.resolve()
    except OSError:
        return p


def humanize_seconds_ago(seconds: float) -> str:
    """Render an "X ago" timestamp suitable for the status bar.

    Pure function. Negative or zero seconds collapse to ``"just now"``.
    """
    if seconds <= 1:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


# ---------------------------------------------------------------------------
# Worker — wraps a long-running call (extraction or entry) in a QThread
# ---------------------------------------------------------------------------


class _CallableWorker(QObject):
    """Run a callable on a worker thread; emit signals for progress and completion.

    The callable receives the worker as its first argument so it can call
    :meth:`emit_progress` (message-only) or :meth:`emit_progress_full`
    (message plus completed/total counters) to stream status updates.

    The ``progress`` signal carries ``(message, current, total)``. When a
    counter is unavailable (e.g., extraction has no per-doc callback yet),
    the worker passes ``current=0`` and ``total=0`` and the host treats
    the progress bar as indeterminate.

    The host connects to ``progress`` / ``finished`` / ``failed`` to
    update the UI.
    """

    progress = Signal(str, int, int)
    finished = Signal(object)
    failed = Signal(str, str)  # (operator-readable, technical_detail)

    def __init__(self, func: Callable[..., Any], *args, **kwargs) -> None:
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs
        # Initialized False so ``hasattr(worker, "cancel_requested")`` is True
        # on a fresh worker. Without this, the cancel-button slots that gate
        # on ``hasattr`` never set the flag — the worker keeps running until
        # its own timeout fires (we saw a 90s wait on Begin Entry cancels
        # before this was fixed).
        self.cancel_requested: bool = False

    def emit_progress(self, message: str) -> None:
        """Emit ``message`` with no counter (indeterminate)."""
        self.progress.emit(message, 0, 0)

    def emit_progress_full(self, message: str, current: int, total: int) -> None:
        """Emit ``message`` with completed/total counters (determinate)."""
        self.progress.emit(message, int(current), int(total))

    def run(self) -> None:
        try:
            result = self._func(self, *self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("worker failed")
            self.failed.emit(str(exc) or exc.__class__.__name__, repr(exc))
            return
        self.finished.emit(result)


# ---------------------------------------------------------------------------
# Browser launch worker — runs Playwright outside the asyncio event loop
# ---------------------------------------------------------------------------


class _BrowserLaunchWorker(QThread):
    """Launch the Playwright persistent-context browser in a worker thread.

    Playwright's sync API raises an error when called from a thread that has
    a running asyncio event loop. PySide6 keeps one on the main thread, so we
    offload the entire launch + initial navigation + epic_steps sequence here.

    Signals:
        launched(object)  — emits the live BrowserContext on success
        failed(str)       — emits an operator-readable error message on failure
        status_update(str) — emits a sidebar status string mid-launch
    """

    launched = Signal(object)
    failed = Signal(str)
    status_update = Signal(str)

    def __init__(
        self,
        *,
        playwright_profile,
        epic_base_url: str,
        debug: bool,
        cdp_port,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._playwright_profile = playwright_profile
        self._epic_base_url = epic_base_url
        self._debug = debug
        self._cdp_port = cdp_port

    def run(self) -> None:
        from .. import epic_session as epic_session_module
        from ..epic_steps import step_enterprise_id, step_login

        try:
            ctx = epic_session_module.launch_with_persistent_context(
                self._playwright_profile,
                headed=True,
                debug=self._debug,
                cdp_port=self._cdp_port,
            )
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return

        # Navigate to EPIC and run startup steps. Failures here are logged
        # but don't prevent the context from being returned — the operator
        # can interact with the browser manually if a step fails.
        try:
            pages = ctx.pages
            page = pages[0] if pages else ctx.new_page()
            page.goto(self._epic_base_url, wait_until="domcontentloaded", timeout=30_000)
            step_enterprise_id.run(page)
            step_login.run(ctx)
            from ..epic_steps import step_database_select, step_session_conflict
            step_database_select.run(page, debug=self._debug)
            step_session_conflict.run(
                page,
                on_waiting=lambda msg: self.status_update.emit(msg),
            )
        except Exception as exc:  # noqa: BLE001
            _logger.warning("browser_launch_worker.navigate_failed: %s", exc)

        self.launched.emit(ctx)




# ---------------------------------------------------------------------------
# State helpers (thin wrappers — keep main_window decoupled from state.py
# implementation details that may still be in flight)
# ---------------------------------------------------------------------------


def _safe_state_load(client_path: Path) -> dict:
    """Load ``state.json`` and return a plain-dict view.

    Always returns a dict (not the ``State`` dataclass) so the rest of the
    GUI can use ``.get()`` / ``[]`` indexing uniformly. We round-trip
    through ``dataclasses.asdict`` when the state module is available so
    the schema-aware load path (with .bak / snapshot fallback) is still
    used; we keep an ad-hoc JSON fallback for early-build scenarios where
    ``state.py`` is still a stub.
    """
    try:
        from .. import state as state_module

        loader = getattr(state_module, "load", None)
        if callable(loader):
            from dataclasses import asdict, is_dataclass

            loaded = loader(client_path)
            if is_dataclass(loaded):
                return asdict(loaded)
            if isinstance(loaded, dict):
                return loaded
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        _logger.warning("state.load failed (%s) — falling back to direct read", exc)

    state_path = client_path / "state.json"
    if state_path.exists():
        import json

        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning("could not read state.json: %s", exc)
    return _empty_state(client_path.name)


def _safe_state_save(state: dict, client_path: Path) -> None:
    """Persist ``state`` (dict) via the state module's atomic write protocol.

    If ``state.py`` is fully implemented we reconstruct a ``State`` dataclass
    from the dict (using public state-module accessors only) and call
    ``state.save_atomic``. Otherwise we fall back to a direct atomic JSON
    write so the GUI remains usable while other modules ramp up.
    """
    try:
        from .. import state as state_module

        save_atomic = getattr(state_module, "save_atomic", None)
        State_cls = getattr(state_module, "State", None)
        if callable(save_atomic) and State_cls is not None:
            try:
                state_obj = _dict_to_state(state, state_module=state_module)
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "could not reconstruct State from dict (%s) — "
                    "falling back to direct JSON write",
                    exc,
                )
            else:
                save_atomic(state_obj, client_path)
                return
    except ImportError:
        pass

    # Fallback path — direct atomic JSON write.
    import json

    client_path.mkdir(parents=True, exist_ok=True)
    target = client_path / "state.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)


def _dict_to_state(state: dict, *, state_module) -> object:
    """Reconstruct a ``State`` dataclass from a plain dict.

    Uses only the public dataclass classes exposed by ``state.py`` (no
    private helpers). Tolerant of missing fields — defaults flow through
    the dataclass field defaults.
    """
    from datetime import datetime, timezone

    SourceRef = getattr(state_module, "SourceRef")
    ConflictCandidate = getattr(state_module, "ConflictCandidate")
    HistoryEntry = getattr(state_module, "HistoryEntry")
    FieldRecord = getattr(state_module, "FieldRecord")
    PendingPause = getattr(state_module, "PendingPause")
    PendingExtraction = getattr(state_module, "PendingExtraction")
    RunHistoryEntry = getattr(state_module, "RunHistoryEntry")
    Insured = getattr(state_module, "Insured")
    Contact = getattr(state_module, "Contact")
    State = getattr(state_module, "State")

    def src(d: dict | None) -> object | None:
        if not isinstance(d, dict):
            return None
        return SourceRef(
            doc_id=d.get("doc_id", ""),
            page=int(d.get("page", 0) or 0),
            quote=d.get("quote", "") or "",
        )

    def conflict(d: dict) -> object:
        return ConflictCandidate(
            value=d.get("value"),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            source=src(d.get("source")) or SourceRef(doc_id="", page=0, quote=""),
            observed_at=d.get("observed_at", "") or "",
            model_used=d.get("model_used"),
        )

    def history(d: dict) -> object:
        return HistoryEntry(
            run_id=d.get("run_id", ""),
            ts=d.get("ts", ""),
            user=d.get("user", ""),
            actor=d.get("actor", "gui"),
            action=d.get("action", "update"),
            prior=d.get("prior"),
            new=d.get("new", {}),
        )

    def field_record(d: dict) -> object:
        return FieldRecord(
            value=d.get("value"),
            confidence=float(d.get("confidence", 0.0) or 0.0),
            status=d.get("status", "pending"),
            source=[s for s in (src(x) for x in (d.get("source") or [])) if s is not None],
            conflicts=[conflict(c) for c in (d.get("conflicts") or []) if isinstance(c, dict)],
            history=[history(h) for h in (d.get("history") or []) if isinstance(h, dict)],
            needs_review=bool(d.get("needs_review", False)),
            model_used=d.get("model_used"),
        )

    def pending_pause(d: dict | None) -> object | None:
        if not isinstance(d, dict):
            return None
        return PendingPause(
            run_id=d.get("run_id", ""),
            paused_at=d.get("paused_at", ""),
            domain_tag=d.get("domain_tag", ""),
            screen_code=d.get("screen_code", ""),
            reason_code=d.get("reason_code", "validation_rejected"),
            reason_message=d.get("reason_message", ""),
            technical_detail=d.get("technical_detail", ""),
            repeatable_group=d.get("repeatable_group"),
            repeatable_index=d.get("repeatable_index"),
        )

    def pending_extraction(d: dict | None) -> object | None:
        if not isinstance(d, dict):
            return None
        return PendingExtraction(
            run_id=d.get("run_id", ""),
            started_at=d.get("started_at", ""),
            pdf_paths=list(d.get("pdf_paths") or []),
            completed_pdf_basenames=list(d.get("completed_pdf_basenames") or []),
            notes=d.get("notes"),
        )

    def run_history(d: dict) -> object:
        return RunHistoryEntry(
            run_id=d.get("run_id", ""),
            ts=d.get("ts", ""),
            user=d.get("user", ""),
            kind=d.get("kind", "manual_edit"),
            inputs=list(d.get("inputs") or []),
            outcome=d.get("outcome", "completed"),
            model_used=d.get("model_used"),
            forced_opus=d.get("forced_opus"),
            notes=d.get("notes"),
        )

    now = datetime.now(timezone.utc).isoformat()
    return State(
        client=state.get("client", ""),
        created_at=state.get("created_at", now),
        updated_at=state.get("updated_at", now),
        schema_version=int(state.get("schema_version", 1) or 1),
        run_history=[run_history(r) for r in (state.get("run_history") or []) if isinstance(r, dict)],
        fields={
            tag: field_record(rec)
            for tag, rec in (state.get("fields") or {}).items()
            if isinstance(rec, dict)
        },
        repeatables={
            group: [
                {
                    tag: field_record(rec)
                    for tag, rec in item.items()
                    if isinstance(rec, dict)
                }
                for item in items
                if isinstance(item, dict)
            ]
            for group, items in (state.get("repeatables") or {}).items()
        },
        pending_pause=pending_pause(state.get("pending_pause")),
        pending_extraction=pending_extraction(state.get("pending_extraction")),
        insured=_insured_from_dict(state, Insured),
        contacts=[
            Contact(
                first_name=str(c.get("first_name") or ""),
                last_name=str(c.get("last_name") or ""),
                title=str(c.get("title") or ""),
                email=str(c.get("email") or ""),
                phone=str(c.get("phone") or ""),
            )
            for c in (state.get("contacts") or [])
            if isinstance(c, dict)
        ],
    )


def _insured_from_dict(state: dict, Insured) -> object:
    """Build an ``Insured`` from the in-memory dict, with legacy fallback.

    Honors a legacy top-level ``lookup_code`` so older saved states don't
    drop the value when round-tripped through this bridge. Tolerant of
    missing newer fields (client_format / agency / branch / split address /
    business_phone / NAICS / SIC) — defaults are applied per the dataclass.

    Mirror of ``state._insured_from_dict``: the GUI's save path round-trips
    dict → Insured → save_atomic, and any field this builder forgets ends
    up cleared on disk. Keep this function in sync with the Insured
    dataclass and with state.py's loader.
    """
    src = state.get("insured")
    legacy_code = str(state.get("lookup_code") or "")
    if not isinstance(src, dict):
        return Insured(lookup_code=legacy_code)

    legacy_mailing = str(src.get("mailing_address") or "")
    street_addr = str(src.get("street_address") or "") or legacy_mailing

    fmt = str(src.get("client_format") or "BUSINESS").upper()
    if fmt not in ("BUSINESS", "INDIVIDUAL"):
        fmt = "BUSINESS"

    return Insured(
        lookup_code=str(src.get("lookup_code") or legacy_code or ""),
        named_insured=str(src.get("named_insured") or ""),
        fein=str(src.get("fein") or ""),
        business_type=str(src.get("business_type") or ""),
        client_format=fmt,
        agency=str(src.get("agency") or ""),
        branch=str(src.get("branch") or ""),
        street_address=street_addr,
        city=str(src.get("city") or ""),
        state=str(src.get("state") or ""),
        zip_code=str(src.get("zip_code") or ""),
        mailing_address=legacy_mailing,
        physical_address=str(src.get("physical_address") or ""),
        business_phone=str(src.get("business_phone") or ""),
        website=str(src.get("website") or ""),
        naics=str(src.get("naics") or ""),
        sic=str(src.get("sic") or ""),
    )


def _empty_state(client_name: str) -> dict:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": 1,
        "client": client_name,
        "created_at": now,
        "updated_at": now,
        "run_history": [],
        "fields": {},
        "repeatables": {},
        "pending_pause": None,
        "pending_extraction": None,
        "insured": {
            "lookup_code": "",
            "named_insured": "",
            "fein": "",
            "business_type": "",
            "mailing_address": "",
            "physical_address": "",
            "website": "",
        },
        "contacts": [],
    }


# ---------------------------------------------------------------------------
# Extraction busy dialog
# ---------------------------------------------------------------------------


def _busy_gif_path() -> Path:
    """Resolve assets/working.gif relative to this module so the lookup
    works whether the package is installed editable or copied into a
    build artifact."""
    here = Path(__file__).resolve()
    # main_window.py -> gui -> iga_marketing_master_2 -> src -> repo root
    return here.parent.parent.parent.parent / "assets" / "working.gif"


class _ExtractionBusyDialog(QDialog):
    """Modal popup shown while an extraction run is in flight.

    Plays ``assets/working.gif`` and shows a "Working..." label. The
    operator can't dismiss it manually — only the host's
    ``_on_extraction_finished`` / ``_on_extraction_failed`` slots call
    ``close()`` on it when the worker thread reports completion.

    The status-bar progress strip is unchanged; this dialog is the
    obvious "the program is doing something, please wait" indicator
    that sits inside the GUI on top of the main window.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Extracting")
        # No close button — the host owns the lifecycle.
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setModal(True)
        self.setFixedSize(320, 280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 18)
        layout.setSpacing(12)

        self._movie: QMovie | None = None
        gif_label = QLabel()
        gif_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        gif_path = _busy_gif_path()
        if gif_path.exists():
            self._movie = QMovie(str(gif_path))
            # Source is 800x600; scale down to fit the dialog while
            # preserving aspect ratio.
            self._movie.setScaledSize(QSize(240, 180))
            gif_label.setMovie(self._movie)
            self._movie.start()
        else:
            # Graceful fallback: if the asset is missing, fall back to text.
            gif_label.setText("⏳")
            gif_label.setStyleSheet("font-size: 48px;")
        layout.addWidget(gif_label)

        self._status_label = QLabel("Working...")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status_label.setStyleSheet(
            "font-size: 14px; font-weight: 600; color: #0f172a;"
        )
        layout.addWidget(self._status_label)

    def set_status_text(self, text: str) -> None:
        """Update the small status line under the GIF (e.g. 'Completed 3 of 7')."""
        self._status_label.setText(text)

    def closeEvent(self, event) -> None:
        # Stop the movie so its timer stops firing when the dialog is hidden.
        if self._movie is not None:
            self._movie.stop()
        super().closeEvent(event)

    def reject(self) -> None:
        # Ignore Esc / X / etc. — only the host calls close() / accept().
        pass


# ---------------------------------------------------------------------------
# Coverage picker (--debug mode only)
# ---------------------------------------------------------------------------


# Each entry: (namespace_prefix, display_label, always_show)
# always_show=True  — shown in picker regardless of extracted data (workflow steps)
# always_show=False — shown only when extracted data exists for that prefix
_COVERAGE_OPTIONS: tuple[tuple[str, str, bool], ...] = (
    ("account",                    "Setup Account",                True),
    ("additional_contacts",        "Additional Contacts",          True),
    ("submission",                 "Create Marketing Submission",  True),
    ("policy.commercial_ap",       "Commercial AP",                True),
    ("policy.gl",                  "General Liability",            False),
    ("policy.property",            "Property",                     False),
    ("policy.auto",                "Business Auto",                False),
    ("policy.inland_marine",       "Inland Marine",                False),
    ("policy.workers_comp",        "Workers Comp",                 False),
    ("policy.umbrella",            "Umbrella / Excess",            False),
    # ("create_carrier_submission",  "Create Carrier Submission",    True),
    # ^ Hidden 2026-05-22 per operator request — restore by uncommenting
    #   when the carrier-submission step is ready.
    # Less-common lines — shown only when extracted data is present
    ("producer",                   "Producer",                     False),
    ("policy.crime",               "Crime",                        False),
    ("policy.cyber",               "Cyber",                        False),
    ("policy.professional",        "Professional",                 False),
    ("policy.directors_officers",  "D&O",                         False),
    ("policy.employment_practices", "EPL",                        False),
    ("policy.pollution",           "Pollution",                    False),
    ("vehicle",                    "Vehicles (legacy)",            False),
    ("driver",                     "Drivers (legacy)",             False),
    ("prior_carrier",              "Prior Carriers",               False),
    ("loss",                       "Loss History",                 False),
    ("notes",                      "Notes",                        False),
)

# Hierarchy level for each prefix (1 = must run first).
# When a level-N item is selected without all level < N items also selected,
# a prerequisite warning is shown before entry begins.
_STEP_LEVEL: dict[str, int] = {
    "account":                   1,
    "submission":                2,
    "policy.commercial_ap":      3,
    "policy.gl":                 4,
    "policy.property":           4,
    "policy.auto":               4,
    "policy.inland_marine":      4,
    "policy.workers_comp":       4,
    "policy.umbrella":           4,
    "create_carrier_submission": 5,
}

# Text describing what the operator must have already done at each level.
_PREREQ_MESSAGES: dict[int, str] = {
    1: "the browser is navigated to the appropriate account in EPIC",
    2: "the Marketing Submission has been created",
    3: "Commercial AP has already been completed",
    4: "all selected lines of business have been entered",
}


def _build_additional_contacts_setup(entry_state: Any, state_dict: Any):
    """Build the Additional Contacts entry payload from client state.

    Individuals come from ``entry_state.contacts``; business named insureds
    come from the ``account.named_insured`` repeatable, resolved through the
    same Field-Map column mapping the Named Insureds tab uses
    (:data:`section_forms_layout.NAMED_INSUREDS_COLUMNS`). All rows are
    included — the entry step dedups against the live EPIC Contacts grid by
    name, so the seeded primary contact + main business contact are skipped.
    """
    from ..epic_steps.step_additional_contacts import (
        AdditionalContactsSetup,
        IndividualContact,
        BusinessContact,
    )
    from .section_forms import _rep_or_singleton_row, _resolve_column_tags
    from .section_forms_layout import NAMED_INSUREDS_COLUMNS

    individuals = [
        IndividualContact(
            first_name=(getattr(c, "first_name", "") or ""),
            last_name=(getattr(c, "last_name", "") or ""),
            title=(getattr(c, "title", "") or ""),
            email=(getattr(c, "email", "") or ""),
            phone=(getattr(c, "phone", "") or ""),
        )
        for c in (getattr(entry_state, "contacts", None) or [])
    ]

    # NAMED_INSUREDS_COLUMNS index map: 0 Entity, 1 Type, 2 BusinessType,
    # 3 FEIN, 4 Address, 5 State, 6 Email, 7 Website.
    tags = _resolve_column_tags(NAMED_INSUREDS_COLUMNS)
    rows = _rep_or_singleton_row(
        state_dict if isinstance(state_dict, dict) else {}, "account.named_insured"
    )

    def _v(row: dict, idx: int) -> str:
        tag = tags[idx] if idx < len(tags) else None
        if not tag:
            return ""
        rec = row.get(tag)
        if isinstance(rec, dict):
            return str(rec.get("value") or "")
        return str(rec or "")

    businesses = [
        BusinessContact(
            entity_name=_v(row, 0),
            business_type=_v(row, 2),
            fein=_v(row, 3),
            street_address=_v(row, 4),
            state=_v(row, 5),
            email=_v(row, 6),
            website=_v(row, 7),
        )
        for row in rows
    ]

    return AdditionalContactsSetup(individuals=individuals, businesses=businesses)


def _namespace_present(state: Any, prefix: str) -> bool:
    """Return True if state has any field or repeatable-row that starts
    with ``prefix + "."`` (or equals ``prefix`` exactly for short names)."""
    fields_map = getattr(state, "fields", None) or {}
    for tag in fields_map.keys():
        if tag == prefix or tag.startswith(prefix + "."):
            return True
    repeatables = getattr(state, "repeatables", None) or {}
    for group, items in repeatables.items():
        if not items:
            continue
        if group == prefix or group.startswith(prefix + "."):
            return True
    return False


class _BeginEntryDialog(QDialog):
    """Modal that captures everything Begin Entry needs before the walker
    runs: the operator-typed submission-setup values (Profit Center /
    Effective Date / Expiration Date), and the per-line checkboxes that
    scope the walk to a subset of LOBs.

    Agency and Branch are **not** asked for here — they come from
    ``state.insured`` on the Client page. The dialog shows them as
    read-only labels so the operator sees what will be used.

    Pre-populates Profit Center + dates from ``state.submission_setup`` if
    present so subsequent runs just re-confirm. On accept, the host calls
    :meth:`submission_setup` to read values back and persists them to
    ``state.json``.
    """

    def __init__(
        self,
        state_obj: Any,
        *,
        debug: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Begin Entry")
        self.setModal(True)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)

        # Local imports to keep the module's top-level import surface tight
        # (these widgets are only needed when the dialog is opened).
        from PySide6.QtWidgets import (
            QCheckBox,
            QComboBox,
            QDialogButtonBox,
            QFormLayout,
            QLineEdit,
        )
        from .. import config as config_module
        from ..epic_steps.step_account_create import BRANCH_NAMES, AGENCY_NAMES

        prior = getattr(state_obj, "submission_setup", None)

        # Keep a reference to state_obj — _on_accept reads insured.agency /
        # insured.branch back to build the SubmissionSetup.
        self._state_obj = state_obj

        # -- Section 1: submission setup --------------------------------
        hdr = QLabel("Submission setup")
        hdr.setStyleSheet("font-weight: 700; font-size: 13px; color: #0f172a;")
        layout.addWidget(hdr)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        # Agency / Branch come from the Client page (state.insured). Show
        # them as read-only labels so the operator can see what's being
        # used — if they need to change either, they go back to the
        # Client page and edit there. An empty value is rendered in muted
        # red so the operator notices something needs filling in.
        insured = getattr(state_obj, "insured", None)
        self._client_agency = (getattr(insured, "agency", "") or "").strip() if insured else ""
        self._client_branch = (getattr(insured, "branch", "") or "").strip() if insured else ""

        def _readonly_value_label(text: str, missing: bool) -> QLabel:
            lbl = QLabel(text if text else "— (set on Client page)")
            if missing:
                lbl.setStyleSheet(
                    "color: #b91c1c; font-style: italic; font-size: 12px;"
                )
            else:
                lbl.setStyleSheet("color: #0f172a; font-size: 12px;")
            return lbl

        agency_display = (
            f"{self._client_agency} — {AGENCY_NAMES[self._client_agency]}"
            if self._client_agency in AGENCY_NAMES
            else self._client_agency
        )
        branch_display = (
            f"{self._client_branch} — {BRANCH_NAMES[self._client_branch]}"
            if self._client_branch in BRANCH_NAMES
            else self._client_branch
        )
        form.addRow(
            "Agency:",
            _readonly_value_label(agency_display, missing=not self._client_agency),
        )
        form.addRow(
            "Branch:",
            _readonly_value_label(branch_display, missing=not self._client_branch),
        )

        self._profit_center_cb = QComboBox(self)
        self._profit_center_cb.addItems(
            list(config_module.SUBMISSION_PROFIT_CENTER_OPTIONS)
        )

        def _restore_combo(cb: QComboBox, prior_value: str | None) -> None:
            if not prior_value:
                return
            idx = cb.findText(prior_value)
            if idx >= 0:
                cb.setCurrentIndex(idx)
            else:
                # Persisted value isn't in the current option list (someone
                # edited config.py) — surface it as an editable extra so the
                # operator sees it and can adjust.
                cb.setEditable(True)
                cb.setCurrentText(prior_value)
                cb.setEditable(False)

        prior_pc = getattr(prior, "profit_center", None) if prior else None
        _restore_combo(self._profit_center_cb, prior_pc)

        self._eff_date_inp = QLineEdit(self)
        self._eff_date_inp.setPlaceholderText("MM/DD/YYYY")
        if prior and prior.effective_date:
            self._eff_date_inp.setText(prior.effective_date)

        self._exp_date_inp = QLineEdit(self)
        self._exp_date_inp.setPlaceholderText("MM/DD/YYYY")
        if prior and prior.expiration_date:
            self._exp_date_inp.setText(prior.expiration_date)

        form.addRow("Profit Center:", self._profit_center_cb)
        form.addRow("Effective Date:", self._eff_date_inp)
        form.addRow("Expiration Date:", self._exp_date_inp)

        layout.addLayout(form)

        # Fixed-value footnote (operator can see what's hard-coded).
        fixed_lbl = QLabel(
            f"Agency and Branch come from the Client page. "
            f"Department is always “{config_module.SUBMISSION_DEPARTMENT_FIXED}”; "
            f"Type of Business is always “{config_module.SUBMISSION_TYPE_OF_BUSINESS_FIXED}”."
        )
        fixed_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
        fixed_lbl.setWordWrap(True)
        layout.addWidget(fixed_lbl)

        # -- Section 2: coverage / LOB picker --------------------------------
        # Existing-account gate: every step below "Setup Account" assumes
        # EPIC already has the account (they navigate by lookup code). If
        # the Client page has no lookup_code AND "Setup Account" isn't
        # checked, those rows get disabled so the operator can't queue work
        # that has no account to run against.
        self._has_existing_lookup_code: bool = bool(
            (getattr(insured, "lookup_code", "") or "").strip()
            if insured else False
        )

        self._checkboxes: dict[str, QCheckBox] = {}
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("color: #e2e8f0;")
        layout.addWidget(sep)

        hdr2 = QLabel("Lines to enter")
        hdr2.setStyleSheet("font-weight: 700; font-size: 13px; color: #0f172a;")
        layout.addWidget(hdr2)

        intro = QLabel(
            "Only checked lines will be typed into EPIC during this run."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #334155;")
        layout.addWidget(intro)

        for prefix, label, always_show in _COVERAGE_OPTIONS:
            if not always_show and not _namespace_present(state_obj, prefix):
                continue
            cb = QCheckBox(label, self)
            # Default to *unchecked* — operator explicitly opts in to each
            # step. (Was True historically when rows were narrower; now
            # that Setup Account / Create Marketing Submission / each LOB
            # are all on the same list, defaulting to checked would let
            # Continue accidentally run the entire pipeline.)
            cb.setChecked(False)
            cb.setStyleSheet("padding: 2px 4px;")
            layout.addWidget(cb)
            self._checkboxes[prefix] = cb

        if not self._checkboxes:
            note = QLabel("(No extracted lines found in this client.)")
            note.setStyleSheet("color: #94a3b8; font-style: italic;")
            layout.addWidget(note)

        # Wire "Setup Account" → enable/disable downstream rows. Also do an
        # initial pass so disabled rows render correctly on first paint.
        account_cb = self._checkboxes.get("account")
        if account_cb is not None:
            account_cb.toggled.connect(self._refresh_downstream_gate)
        self._refresh_downstream_gate()

        # Optional hint when the account is missing and Setup Account is
        # off — clarifies why the lower rows are greyed out.
        self._gate_hint = QLabel("")
        self._gate_hint.setWordWrap(True)
        self._gate_hint.setStyleSheet(
            "color: #b91c1c; font-size: 11px; font-style: italic;"
        )
        layout.addWidget(self._gate_hint)
        self._refresh_gate_hint()

        helper_row = QHBoxLayout()
        sel_all = QPushButton("Select all", self)
        sel_all.clicked.connect(self._select_all)
        clr_all = QPushButton("Clear all", self)
        clr_all.clicked.connect(self._clear_all)
        helper_row.addWidget(sel_all)
        helper_row.addWidget(clr_all)
        helper_row.addStretch(1)
        layout.addLayout(helper_row)

        # -- OK / Cancel ------------------------------------------------
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self._buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            "Continue"
        )
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    # -- Internal ----------------------------------------------------------

    def _refresh_downstream_gate(self, _checked: bool = False) -> None:
        """Update the lookup-code hint based on Setup Account state.

        We no longer *disable* the downstream rows when the lookup code is
        missing — the operator may have just typed it on the Client page
        (the dialog's cached value would be stale) or be about to. Instead
        the hint is informational, and the hard check happens in
        :meth:`_on_accept` against fresh state.
        """
        # Keep the hint label in sync if it exists yet.
        if getattr(self, "_gate_hint", None) is not None:
            self._refresh_gate_hint()

    def _refresh_gate_hint(self) -> None:
        """Show a soft hint when the dialog opened without a lookup code
        AND Setup Account isn't currently checked. The hint is purely
        informational — rows stay clickable so the operator can proceed
        if they've since hand-keyed a lookup code on the Client page."""
        account_cb = self._checkboxes.get("account")
        account_checked = bool(account_cb.isChecked()) if account_cb else False
        if self._has_existing_lookup_code or account_checked:
            self._gate_hint.setText("")
            self._gate_hint.setVisible(False)
        else:
            self._gate_hint.setText(
                "No EPIC lookup code was set on the Client page when this "
                "dialog opened. If you haven't already, either type the "
                "existing lookup code on the Client page, or check Setup "
                "Account so the run creates the account first."
            )
            self._gate_hint.setVisible(True)

    def _on_accept(self) -> None:
        """Validate the form, refusing to close if anything is empty.

        Agency and Branch are sourced from ``state.insured`` (Client page),
        not from this dialog. If the operator hasn't set them there, point
        them back rather than silently saving with empty values.
        """
        from .. import state as state_module

        # Existing-account gate: at least one source of a lookup code must
        # be available (either currently set on the Client page, or Setup
        # Account is checked so the run creates the account). The cached
        # ``self._has_existing_lookup_code`` is the state at dialog-open
        # time; the operator may have hand-keyed a code on the Client page
        # since then, so re-read live state before deciding.
        account_cb = self._checkboxes.get("account")
        account_checked = bool(account_cb.isChecked()) if account_cb else False

        live_lookup_code = ""
        parent = self.parent()
        if parent is not None:
            # Walk up to the MainWindow that owns the client_page so we
            # can read the live in-memory state (catches edits not yet
            # committed via editingFinished — e.g. the lookup-code QLineEdit
            # still has focus).
            mw = parent
            while mw is not None and not hasattr(mw, "_client"):
                mw = mw.parent() if hasattr(mw, "parent") else None
            client_ctx = getattr(mw, "_client", None) if mw is not None else None
            state_dict = getattr(client_ctx, "state", None) if client_ctx else None
            if isinstance(state_dict, dict):
                insured_dict = state_dict.get("insured")
                if isinstance(insured_dict, dict):
                    live_lookup_code = str(insured_dict.get("lookup_code") or "").strip()
        if not live_lookup_code:
            # Fallback: trust the cached value (dialog-open snapshot).
            live_lookup_code = (
                self._has_existing_lookup_code and "<cached>" or ""
            )

        if not live_lookup_code and not account_checked:
            QMessageBox.warning(
                self,
                "No EPIC account to run against",
                "The Client page has no Lookup Code and Setup Account is "
                "not checked, so there is no EPIC account for the other "
                "steps to operate on. Either check Setup Account (to "
                "create the account during this run), or cancel, enter the "
                "existing lookup code on the Client page, and re-open "
                "Begin Entry.",
            )
            return

        missing: list[str] = []
        if not self._client_agency:
            missing.append("Agency (set on Client page)")
        if not self._client_branch:
            missing.append("Branch (set on Client page)")
        if not self._profit_center_cb.currentText().strip():
            missing.append("Profit Center")
        if missing:
            QMessageBox.warning(
                self,
                "Missing fields",
                "Please fill in: " + ", ".join(missing),
            )
            return

        # Cross-field rule: "Create Marketing Submission" needs at least one
        # real line of business so the MMS gets non-empty lines.
        # Commercial AP is *not* a line of coverage — it's a wrapper
        # applicable to every line — so checking it alone does NOT satisfy
        # the requirement. (Operator can still check it; we just don't
        # count it as the required LOB.)
        selected_prefixes = [
            prefix for prefix, cb in self._checkboxes.items() if cb.isChecked()
        ]
        if "submission" in selected_prefixes and not any(
            p.startswith("policy.") and p != "policy.commercial_ap"
            for p in selected_prefixes
        ):
            QMessageBox.warning(
                self,
                "Line of business required",
                "Create Marketing Submission requires at least one line "
                "of business (General Liability, Property, Business Auto, "
                "Workers Comp, etc.). Commercial AP is not a line of "
                "coverage on its own — check the actual line(s) you want "
                "on the submission and try again.",
            )
            return
        # Defer the SubmissionSetup construction to accept-time so we know
        # everything is filled before the host reads it back.
        self._setup = state_module.SubmissionSetup(
            agency=self._client_agency,
            branch=self._client_branch,
            profit_center=self._profit_center_cb.currentText().strip(),
            effective_date=self._eff_date_inp.text().strip(),
            expiration_date=self._exp_date_inp.text().strip(),
        )
        self.accept()

    def _select_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(True)

    def _clear_all(self) -> None:
        for cb in self._checkboxes.values():
            cb.setChecked(False)

    # -- Public read-back --------------------------------------------------

    def submission_setup(self) -> Any:
        """Return the SubmissionSetup the operator just confirmed.

        Only valid after the dialog was accepted; raises AttributeError
        if called after a Cancel.
        """
        return self._setup

    def selected_namespaces(self) -> list[str] | None:
        """Selected line/coverage prefixes.

        Returns ``None`` if no extracted lines were found in state (the picker
        was empty), so the host walks every enterable field.
        Returns a (possibly empty) list when the picker was shown.
        """
        if not self._checkboxes:
            return None
        return [prefix for prefix, cb in self._checkboxes.items() if cb.isChecked()]


# Backward-compat alias so any import of the old class name still works.
_CoveragePickerDialog = _BeginEntryDialog


# ---------------------------------------------------------------------------
# MainWindow
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class _ClientContext:
    """In-memory per-client snapshot that the window operates against."""

    name: str
    path: Path
    state: dict
    inputs_dir: Path
    # Tracked here (not on MainWindow) so multi-client switching can't
    # leak a stale "saved 3s ago" message across clients.
    last_save_at: datetime | None = None

    @property
    def state_path(self) -> Path:
        return self.path / "state.json"


class MainWindow(QMainWindow):
    """The application's primary window."""

    def __init__(
        self,
        settings: config_module.Settings,
        *,
        debug: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._debug = debug
        self._client: _ClientContext | None = None
        self._worker_thread: QThread | None = None
        self._worker: _CallableWorker | None = None
        # "Working..." modal that pops up while extraction is running.
        # None when idle; populated by _show_busy_dialog and torn down by
        # _close_busy_dialog in the worker's finished/failed slots.
        self._busy_dialog: _ExtractionBusyDialog | None = None
        # Entry-run busy dialog (working.gif + Cancel button + live status).
        # Created in _show_entry_busy_dialog, torn down in
        # _close_entry_busy_dialog from the entry-finished/failed slots.
        self._entry_busy_dialog: object | None = None  # EntryBusyDialog | None
        # Per-run cancel event + artifacts dir, set in _on_begin_entry and
        # read by the runtime context shared with step files via
        # epic_steps.runtime.set_runtime / get_runtime.
        self._entry_cancel_event: object | None = None  # threading.Event | None
        self._entry_artifacts_dir: object | None = None  # Path | None
        self._entry_runtime: object | None = None       # EntryRuntime | None
        # Persistent Playwright BrowserContext, owned by MainWindow.
        # _on_launch_browser_clicked launches it; _on_begin_entry reuses it.
        # Kept alive across multiple Begin Entry runs so the operator's
        # EPIC login + navigation survives between sessions. Torn down in
        # closeEvent when the IGA app itself shuts down.
        self._browser_context: object | None = None  # BrowserContext | None at runtime
        self._cdp_port: int | None = None  # CDP port for the current browser launch
        self._browser_launch_worker: _BrowserLaunchWorker | None = None
        # Tracks which kind of run is in flight so the progress strip and
        # run-controls can clear themselves correctly on finish/fail.
        self._active_run_kind: str | None = None  # "extract" | "entry" | None

        # Persistent settings (QSettings) — used for geometry, recent clients,
        # and view-menu checkable states. Wrapped in a try so headless test
        # environments without an organization registry still work.
        self._qsettings: QSettings = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)

        # Cache of QAction / QPushButton objects we need to enable/disable.
        # Values may be QAction *or* QPushButton — both have setEnabled().
        self._run_actions: dict = {}
        # The "Get started" empty-state widget; we reuse one instance.
        self._welcome_pane: WelcomePane | None = None
        # Outer splitter retained because audit-log pane lives in it (hidden).
        self._outer_split: QSplitter | None = None
        self._bottom_widget: QWidget | None = None
        # Sidebar + page stack (set during _build_ui)
        self._sidebar: SidebarNav | None = None
        self._pages: QStackedWidget | None = None
        self._page_index: dict[str, int] = {}
        # Upload panel (wraps PendingPdfsPane with nicer UI)
        self._upload_panel: UploadPanel | None = None
        # Client sidebar page — populated by _build_client_page during _build_ui.
        # Held so we can call .set_client(...) on every client transition.
        self._client_page: object | None = None
        # Page header (Extract / Begin Entry buttons in header bar)
        self._page_header: _PageHeaderBar | None = None
        # Custom tab bar (replaces QTabWidget)
        self._tab_bar_inner: QWidget | None = None
        self._tab_bar_layout: QHBoxLayout | None = None
        self._tab_stack: QStackedWidget | None = None
        self._tab_pages: dict[str, QWidget] = {}
        self._tab_buttons: dict[str, QPushButton] = {}
        self._active_tab_key: str | None = None

        self.setWindowTitle("IGA Marketing Master 2.0")
        # Load BOTH icon.ico (multi-resolution, what Windows taskbar
        # honors via the AppUserModelID registered in the run()
        # entrypoint) AND the high-res PNG (Qt fallback / scaled
        # title-bar) into a single QIcon. addFile lets QIcon discover
        # every embedded size in the .ico and use whichever matches
        # the request.
        _assets = Path(__file__).resolve().parents[3] / "assets"
        _icon = QIcon()
        _ico_path = _assets / "icon.ico"
        _png_path = _assets / "IGA_Icon_Orange2x.png"
        if _ico_path.is_file():
            _icon.addFile(str(_ico_path))
        if _png_path.is_file():
            _icon.addFile(str(_png_path))
        if not _icon.isNull():
            self.setWindowIcon(_icon)
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        self._build_ui()
        self._build_menu_bar()
        self._restore_persisted_layout()
        self._handle_first_run_and_api_key()
        self._maybe_seed_initial_client()
        # Draft client: if no --client was passed AND no client is currently
        # loaded, auto-create (or resume) a deterministic __draft__ folder
        # so the Client tab is immediately editable. Extract-click renames
        # the folder to the typed Insured name and proceeds normally.
        self._ensure_draft_client()
        # If neither path loaded a client, _rebuild_tabs paints the welcome
        # empty-state tab. Loading a client also calls _rebuild_tabs so this
        # is idempotent.
        if self._client is None:
            self._rebuild_tabs()
        self._refresh_run_controls()
        self._refresh_pending_pdfs_state()
        self._update_status_bar_idle()

        # Tick the "saved X ago" status text every 30s so the operator sees
        # it tick over without having to interact.
        self._status_timer: QTimer = QTimer(self)
        self._status_timer.setInterval(30_000)
        self._status_timer.timeout.connect(self._update_status_bar_idle)
        self._status_timer.start()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:  # noqa: C901
        # -- Sidebar -------------------------------------------------------
        self._sidebar = SidebarNav(self)
        self._sidebar.page_changed.connect(self._on_sidebar_page_changed)
        self._sidebar.launch_browser_clicked.connect(self._on_launch_browser_clicked)
        self._sidebar.close_app_clicked.connect(self._on_close_app_clicked)

        # -- Page stack ----------------------------------------------------
        self._pages = QStackedWidget(self)

        # == Data Review page ==============================================
        dr_widget = QWidget()
        dr_widget.setObjectName("DataReviewPage")
        dr_layout = QVBoxLayout(dr_widget)
        dr_layout.setContentsMargins(0, 0, 0, 0)
        dr_layout.setSpacing(0)

        # Page header: title + action buttons
        self._page_header = _PageHeaderBar(dr_widget)
        self._page_header.extract_clicked.connect(self._on_extract_clicked)
        self._page_header.begin_entry_clicked.connect(self._on_begin_entry)
        self._run_actions["header_extract"] = self._page_header.extract_btn
        self._run_actions["header_begin"] = self._page_header.begin_btn
        dr_layout.addWidget(self._page_header)

        # Content row: upload panel | divider | review area
        content = QWidget()
        content.setObjectName("ContentArea")
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        # Left: upload panel (drag-drop zone + file queue)
        self._upload_panel = UploadPanel(content)
        self._upload_panel.browse_clicked.connect(self._on_add_pdfs)
        self._upload_panel.paths_changed.connect(self._refresh_pending_pdfs_state)
        # Keep self._pending_pdfs_pane reference for all downstream handlers.
        self._pending_pdfs_pane = self._upload_panel.pdfs_pane
        content_layout.addWidget(self._upload_panel)

        # Thin vertical divider
        panel_div = QFrame()
        panel_div.setObjectName("PanelDivider")
        panel_div.setFrameShape(QFrame.Shape.VLine)
        content_layout.addWidget(panel_div)

        # Right: custom tab bar + content stack + PDF preview (center splitter).
        # The Edit-menu Find Field (Ctrl+F) was removed 2026-05-26 along with
        # the FindBar widget that backed it — operators know where each field
        # lives, so searching across tabs wasn't earning its keyboard slot.

        # Tab button bar — horizontally scrollable row of QPushButtons
        tab_bar_scroll = QScrollArea()
        tab_bar_scroll.setObjectName("TabBarScroll")
        tab_bar_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        tab_bar_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        tab_bar_scroll.setWidgetResizable(True)
        tab_bar_scroll.setFixedHeight(54)
        tab_bar_scroll.setFrameShape(QFrame.Shape.NoFrame)

        self._tab_bar_inner = QWidget()
        self._tab_bar_inner.setObjectName("TabBarInner")
        self._tab_bar_inner.setFixedHeight(46)  # 54px scroll area - 8px scrollbar track
        self._tab_bar_layout = QHBoxLayout(self._tab_bar_inner)
        self._tab_bar_layout.setContentsMargins(8, 0, 8, 0)
        self._tab_bar_layout.setSpacing(2)
        self._tab_bar_layout.addStretch(1)
        tab_bar_scroll.setWidget(self._tab_bar_inner)

        # Tab content pages
        self._tab_stack = QStackedWidget()
        self._tab_stack.setObjectName("TabStack")

        tabs_container = QWidget()
        tc_layout = QVBoxLayout(tabs_container)
        tc_layout.setContentsMargins(0, 0, 0, 0)
        tc_layout.setSpacing(0)
        tc_layout.addWidget(tab_bar_scroll)
        tc_layout.addWidget(self._tab_stack, 1)

        # Audit log + run controls beneath the tab area
        self._audit_log = AuditLogPane()
        self._audit_log.attach_logger("iga", level=logging.INFO)

        self._run_controls = RunControlsBar()
        self._run_controls.extract_clicked.connect(self._on_extract_clicked)
        self._run_controls.begin_entry_clicked.connect(self._on_begin_entry)
        self._run_controls.cancel_clicked.connect(self._on_cancel)

        # Outer vertical splitter (tabs area + audit)
        self._outer_split = QSplitter()
        self._outer_split.setOrientation(Qt.Orientation.Vertical)
        self._outer_split.setObjectName("OuterSplitter")
        self._outer_split.addWidget(tabs_container)
        self._outer_split.addWidget(self._audit_log)
        self._outer_split.setStretchFactor(0, 5)
        self._outer_split.setStretchFactor(1, 1)

        # Keep _bottom_widget pointing to the audit pane for toggle compat.
        self._bottom_widget = self._audit_log

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._outer_split, 1)
        right_layout.addWidget(self._run_controls)

        content_layout.addWidget(right_widget, 1)
        dr_layout.addWidget(content, 1)

        idx = self._pages.addWidget(dr_widget)
        self._page_index["data_review"] = idx

        # == Client page ===================================================
        idx = self._pages.addWidget(self._build_client_page())
        self._page_index["client"] = idx

        # History page removed 2026-05-26 — operator never used it.

        # == Settings page =================================================
        idx = self._pages.addWidget(self._build_settings_page())
        self._page_index["settings"] = idx

        # == Help page =====================================================
        idx = self._pages.addWidget(self._build_help_page())
        self._page_index["help"] = idx

        # Land on the Client page on launch (matches SidebarNav's default
        # active item). The stack otherwise shows index 0 = Data Review.
        client_idx = self._page_index.get("client")
        if client_idx is not None:
            self._pages.setCurrentIndex(client_idx)

        # Current-client banner: thin strip above the page stack showing
        # which client folder is active. Hidden in draft mode.
        self._client_banner = QLabel("")
        self._client_banner.setObjectName("ClientBanner")
        self._client_banner.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._client_banner.setVisible(False)

        # -- Outer container: sidebar | (banner over pages) ----------------
        main_content = QWidget()
        main_content.setObjectName("MainContent")
        mc_layout = QHBoxLayout(main_content)
        mc_layout.setContentsMargins(0, 0, 0, 0)
        mc_layout.setSpacing(0)
        mc_layout.addWidget(self._sidebar)
        right_col = QWidget()
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._client_banner)
        right_layout.addWidget(self._pages, 1)
        mc_layout.addWidget(right_col, 1)

        self.setCentralWidget(main_content)

        # Status bar
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setMaximumWidth(220)
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._progress_bar.setTextVisible(False)
        self.statusBar().addPermanentWidget(self._progress_bar)
        self.statusBar().showMessage("Ready.")

        # Apply app-level stylesheet
        self.setStyleSheet(_APP_QSS)

    # -- Sidebar-page helpers ----------------------------------------------

    def _on_sidebar_page_changed(self, page_key: str) -> None:
        idx = self._page_index.get(page_key, 0)
        if self._pages is not None:
            self._pages.setCurrentIndex(idx)

    def _on_launch_browser_clicked(self) -> None:
        """Launch the Playwright persistent-context Chromium in a worker thread.

        Playwright's sync API cannot run inside a running asyncio event loop
        (which PySide6 maintains on the main thread). All Playwright calls
        are therefore offloaded to a QThread. The resulting BrowserContext is
        returned via signal and stored on self._browser_context.
        """
        # Guard: launch already in progress — second click would spin up a second
        # worker on the same locked Chromium profile, causing a failed-launch signal
        # that wipes _browser_context after the first worker succeeds.
        if self._browser_launch_worker is not None and self._browser_launch_worker.isRunning():
            if self._sidebar is not None:
                self._sidebar.set_engine_status(False, "Browser launching…")
            return

        if self._browser_context is not None:
            # Probe whether the browser is still alive — the operator may have
            # closed the Chromium window manually, leaving a dead context.
            browser_alive = False
            try:
                pages = self._browser_context.pages
                if pages:
                    pages[0].bring_to_front()
                    browser_alive = True
            except Exception:  # noqa: BLE001
                pass

            if browser_alive:
                if self._sidebar is not None:
                    self._sidebar.set_engine_status(False, "Browser already running")
                return

            # Browser was closed externally — clean up the dead context.
            try:
                self._browser_context.close()
            except Exception:  # noqa: BLE001
                pass
            self._browser_context = None

        cdp_port: int = int(config_module.CDP_DEBUG_PORT) if config_module.CDP_DEBUG_PORT else 9222
        self._cdp_port = cdp_port

        if self._sidebar is not None:
            self._sidebar.set_engine_status(False, "Launching browser…")

        worker = _BrowserLaunchWorker(
            playwright_profile=self._settings.playwright_profile,
            epic_base_url=config_module.EPIC_BASE_URL,
            debug=self._debug,
            cdp_port=cdp_port,
            parent=self,
        )
        worker.launched.connect(self._on_browser_launched)
        worker.failed.connect(self._on_browser_launch_failed)
        worker.status_update.connect(self._on_browser_status_update)
        worker.start()
        # Keep a reference so it isn't GC'd before it finishes.
        self._browser_launch_worker = worker

    def _on_browser_launched(self, context: object) -> None:
        self._browser_context = context
        _logger.info(
            "browser.launched user_data_dir=%s",
            self._settings.playwright_profile,
        )
        if self._sidebar is not None:
            self._sidebar.set_engine_status(False, "Browser running")
        self._update_status_bar_idle()

    def _on_browser_status_update(self, msg: str) -> None:
        if self._sidebar is not None:
            self._sidebar.set_engine_status(True, msg)

    def _on_browser_launch_failed(self, error_msg: str) -> None:
        self._browser_context = None
        if self._sidebar is not None:
            self._sidebar.set_engine_status(True, "Ready")
        QMessageBox.critical(
            self,
            "Couldn't launch browser",
            f"Chromium failed to launch with the persistent profile.\n\n{error_msg}",
        )

    def _build_client_page(self) -> QWidget:
        from .client_page import ClientPage
        page = ClientPage(save_callback=_safe_state_save, parent=self)
        self._client_page = page
        # Gate the "Data Extraction & Review" sidebar tab on the Named Insured
        # field having a value. Connect to the QLineEdit's textChanged signal
        # for per-keystroke responsiveness — as soon as the operator types
        # one character, the tab unlocks; clearing the field re-locks it.
        ni_field = page._insured_fields.get("named_insured")
        if ni_field is not None:
            ni_field.textChanged.connect(self._refresh_data_review_gate)
        # insured_changed also re-evaluates so programmatic state loads
        # (e.g., switching clients) refresh the gate without depending on
        # a focus event.
        page.insured_changed.connect(self._on_insured_changed)
        # Wire the header "Select Existing" / "Import New" / "Clear Client"
        # buttons to the host's handlers. Defined as signals on ClientPage so
        # we connect them here at host wiring time.
        page.select_existing_client_clicked.connect(self._on_pick_client)
        # Import New Client is being redesigned to accept a structured Excel
        # file; the legacy "type a folder name" prompt no longer matches the
        # intended workflow. Show a coming-soon placeholder instead of
        # ``_on_create_client``. (The underlying ``_on_create_client`` method
        # is kept for any internal callers — see _maybe_seed_initial_client.)
        page.import_new_client_clicked.connect(self._on_import_new_client_placeholder)
        page.clear_client_clicked.connect(self._on_clear_client)
        return page

    def _on_import_new_client_placeholder(self) -> None:
        """Stand-in until the Excel-driven import flow ships."""
        QMessageBox.information(
            self,
            "Feature coming soon",
            "Import New Client is being redesigned to accept a structured "
            "Excel file. For now, type the Insured Name on the draft Client "
            "tab and click Extract — that creates the client folder for you.",
        )

    def _on_clear_client(self) -> None:
        """Drop the currently-loaded client and swap to a fresh draft.

        Used by the Client tab header's Clear Client button. The previous
        client's folder is left intact on disk — the operator can return to
        it via Select Existing Client. The draft is wiped + re-created.
        """
        if self._client is None:
            return
        # Already on a draft? No-op — nothing to clear.
        if self._client.name == self.DRAFT_DIRNAME:
            return
        self._client = None
        self._ensure_draft_client()

    def _refresh_clear_client_btn(self) -> None:
        """Show Clear Client only when a non-draft client is active."""
        if self._client_page is None:
            return
        btn = getattr(self._client_page, "clear_client_btn", None)
        if btn is None:
            return
        active_non_draft = (
            self._client is not None
            and self._client.name != self.DRAFT_DIRNAME
        )
        btn.setVisible(active_non_draft)

    def _on_insured_changed(self, field_key: str, _new_value: str) -> None:
        if field_key == "named_insured":
            self._refresh_data_review_gate()
            # Refresh the ClientBanner so the current-client label tracks
            # any rename / typo correction the operator just persisted.
            if self._client is not None:
                self._apply_draft_mode(self._client.name == self.DRAFT_DIRNAME)

    def _refresh_data_review_gate(self, *_args) -> None:
        """Enable Data Review nav iff the Named Insured field has a value.

        Reads the live QLineEdit text first (covers per-keystroke updates
        before save), then falls back to the persisted state for the initial
        post-load evaluation.
        """
        if self._sidebar is None:
            return
        name = ""
        page = self._client_page
        if page is not None:
            ni_field = page._insured_fields.get("named_insured") if hasattr(page, "_insured_fields") else None
            if ni_field is not None:
                name = (ni_field.text() or "").strip()
        if not name and self._client is not None:
            insured = (self._client.state or {}).get("insured") or {}
            name = str(insured.get("named_insured") or "").strip()
        self._sidebar.set_button_enabled(
            "data_review",
            bool(name),
            tooltip="Type a Named Insured on the Client tab to unlock Data Extraction & Review.",
        )

    def _build_settings_page(self) -> QWidget:
        from .settings_page import build_settings_page
        return build_settings_page(self._settings)

    def _build_help_page(self) -> QWidget:
        """Build the Help page (sidebar Nav → Help).

        Replaces the old menubar "&Help" submenu — the toolbar is being
        retired one menu at a time and Help moves first. Each action that
        previously lived under the menu becomes a button row here.
        """
        from PySide6.QtWidgets import QFrame as _QFrame

        w = QWidget()
        w.setObjectName("HelpPage")
        outer = QVBoxLayout(w)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(18)

        title = QLabel("Help")
        title.setStyleSheet(
            "font-size: 20px; font-weight: bold; color: #1e293b; "
            "background: transparent;"
        )
        outer.addWidget(title)

        intro = QLabel(
            "Documentation, troubleshooting, and external resources."
        )
        intro.setStyleSheet("color: #64748b; font-size: 12px; background: transparent;")
        outer.addWidget(intro)

        def _hr() -> _QFrame:
            line = _QFrame()
            line.setFrameShape(_QFrame.Shape.HLine)
            line.setStyleSheet("background: #e2e8f0; max-height: 1px; border: none;")
            return line
        outer.addWidget(_hr())

        def _row(label: str, btn_text: str, slot, *,
                 description: str = "") -> QHBoxLayout:
            row = QHBoxLayout()
            row.setSpacing(12)
            text_block = QVBoxLayout()
            text_block.setSpacing(2)
            row_lbl = QLabel(label)
            row_lbl.setStyleSheet(
                "color: #0f172a; font-size: 13px; font-weight: 600; "
                "background: transparent;"
            )
            text_block.addWidget(row_lbl)
            if description:
                desc = QLabel(description)
                desc.setStyleSheet(
                    "color: #64748b; font-size: 11px; background: transparent;"
                )
                desc.setWordWrap(True)
                text_block.addWidget(desc)
            row.addLayout(text_block, 1)
            btn = QPushButton(btn_text)
            btn.setStyleSheet(
                "QPushButton { background: #f1f5f9; color: #334155;"
                " border: 1px solid #cbd5e1; border-radius: 4px;"
                " padding: 6px 14px; font-size: 12px; }"
                "QPushButton:hover { background: #e2e8f0; }"
            )
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            row.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
            return row

        outer.addLayout(_row(
            "Troubleshooting",
            "Open TROUBLESHOOTING.md",
            self._on_open_troubleshooting,
            description="Local install troubleshooting reference.",
        ))
        outer.addLayout(_row(
            "Anthropic Console",
            "Open Console",
            lambda: QDesktopServices.openUrl(QUrl("https://console.anthropic.com/")),
            description="API keys, usage dashboard, model availability.",
        ))
        outer.addLayout(_row(
            "Report an Issue",
            "Open GitHub",
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/anthropics/claude-code/issues")
            ),
            description="File a bug or request a change.",
        ))
        outer.addWidget(_hr())
        outer.addLayout(_row(
            "About IGA Marketing Master",
            "About",
            self._on_about,
            description="Build commit, Python version, dependencies.",
        ))
        outer.addStretch(1)
        return w

    # -- Menu bar (UX-pass #1, #2) ------------------------------------------

    def _build_menu_bar(self) -> None:
        """Install the keyboard-shortcut QActions and hide the menubar.

        Menus were retired in stages between 2026-05-26 and 2026-05-26:
        Help → moved to sidebar Help page; Run / View / Edit / File →
        removed outright. Operator-facing actions are now reachable from
        header buttons, the bottom run-controls strip, the sidebar nav,
        and the Client page. The QActions below remain solely so the
        global keyboard shortcuts (Ctrl+E, Ctrl+Return, Esc) keep firing —
        they're parented to the main window via ``self.addAction`` so Qt
        routes the shortcuts even though no menu surfaces them. The
        QMenuBar widget itself is hidden so it doesn't render an empty
        strip across the top.
        """
        act_run_extract = QAction("Extract", self)
        act_run_extract.setShortcut(QKeySequence("Ctrl+E"))
        act_run_extract.triggered.connect(self._on_extract_clicked)
        self.addAction(act_run_extract)
        self._run_actions["menu_extract"] = act_run_extract

        act_run_begin = QAction("Begin Entry", self)
        act_run_begin.setShortcut(QKeySequence("Ctrl+Return"))
        act_run_begin.triggered.connect(self._on_begin_entry)
        self.addAction(act_run_begin)
        self._run_actions["menu_begin"] = act_run_begin

        act_run_cancel = QAction("Cancel Current Run", self)
        act_run_cancel.setShortcut(QKeySequence("Esc"))
        act_run_cancel.triggered.connect(self._on_cancel)
        self.addAction(act_run_cancel)
        self._run_actions["menu_cancel"] = act_run_cancel

        # Hide the (now empty) menubar widget so it doesn't paint a thin
        # strip across the top of the window.
        self.menuBar().setVisible(False)

    def _on_open_troubleshooting(self) -> None:
        # Look for TROUBLESHOOTING.md alongside the package's repo root.
        # When run from an editable install we walk up until we find it.
        candidate: Path | None = None
        cursor = Path(__file__).resolve()
        for parent in [cursor, *cursor.parents]:
            potential = parent / "TROUBLESHOOTING.md"
            if potential.exists():
                candidate = potential
                break
        if candidate is None:
            QMessageBox.information(
                self,
                "Couldn't find TROUBLESHOOTING.md",
                "TROUBLESHOOTING.md isn't on disk in the install tree.\n"
                "Try the GitHub repo instead.",
            )
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(candidate)))

    def _on_about(self) -> None:
        commit = _safe_git_commit()
        py = ".".join(str(x) for x in sys.version_info[:3])
        QMessageBox.about(
            self,
            "About IGA Marketing Master",
            (
                "<h3>IGA Marketing Master 2.0</h3>"
                f"<p>Build commit: <code>{commit}</code></p>"
                f"<p>Python: {py}</p>"
                "<p>Workflow docs: <code>docs/workflow/</code></p>"
                "<p>Anthropic Claude Agent SDK + PySide6.</p>"
            ),
        )

    # -- Recent clients (UX-pass #6) ----------------------------------------

    def _push_recent_client(self, path: Path) -> None:
        """Record this client open so later sessions can surface a recent list.

        The Recent Clients submenu was retired 2026-05-26, but we keep the
        QSettings list maintained so a future "recent clients" UI on the
        Client page or sidebar can consume it without a fresh data path.
        """
        existing = load_recent_clients(self._qsettings)
        updated = update_recent_clients(existing, path)
        save_recent_clients(self._qsettings, updated)

    # -- Layout persistence (UX-pass #3) ------------------------------------

    def _restore_persisted_layout(self) -> None:
        """Restore geometry and splitter sizes from QSettings.

        Tolerant of missing/corrupted values — falls back to defaults silently.
        """
        try:
            geometry = self._qsettings.value(_QS_GEOMETRY)
            if geometry:
                self.restoreGeometry(geometry)
            window_state = self._qsettings.value(_QS_WINDOW_STATE)
            if window_state:
                self.restoreState(window_state)
            outer = self._qsettings.value(_QS_OUTER_SPLITTER)
            if outer and self._outer_split is not None:
                self._outer_split.restoreState(outer)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("could not restore layout from QSettings: %s", exc)

        # Audit-log pane stays hidden — the Show Audit Log toggle was removed
        # 2026-05-26. The pane itself remains in the layout so the existing
        # ``append_event`` calls scattered across the codebase keep working
        # as an internal log; they just don't surface to the operator.
        if self._audit_log is not None:
            self._audit_log.setVisible(False)

    def _persist_layout(self) -> None:
        try:
            self._qsettings.setValue(_QS_GEOMETRY, self.saveGeometry())
            self._qsettings.setValue(_QS_WINDOW_STATE, self.saveState())
            if self._outer_split is not None:
                self._qsettings.setValue(_QS_OUTER_SPLITTER, self._outer_split.saveState())
        except Exception as exc:  # noqa: BLE001
            _logger.warning("could not persist layout to QSettings: %s", exc)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt-style)
        self._persist_layout()
        self._tear_down_browser()
        super().closeEvent(event)

    def _tear_down_browser(self) -> None:
        """Close the Playwright BrowserContext and stop the Playwright engine."""
        ctx = self._browser_context
        self._browser_context = None
        if ctx is not None:
            try:
                ctx.close()
            except Exception:  # noqa: BLE001
                pass
            pw = getattr(ctx, "_iga_playwright", None)
            if pw is not None:
                try:
                    pw.stop()
                except Exception:  # noqa: BLE001
                    pass

    def _on_close_app_clicked(self) -> None:
        self.close()

    # -- First-run / API key flow -------------------------------------------

    def _handle_first_run_and_api_key(self) -> None:
        """Run the first-run picker (if applicable) and ensure an API key is set."""
        if config_module.is_first_run(self._settings):
            picked = QFileDialog.getExistingDirectory(
                self,
                "Pick a folder for your Working Library",
                str(self._settings.working_library),
            )
            if picked:
                # Persist the choice via config_module.
                from dataclasses import replace as _replace

                new_settings = _replace(self._settings, working_library=Path(picked))
                try:
                    config_module.save_user_config(new_settings)
                    self._settings = new_settings
                except OSError as exc:
                    QMessageBox.warning(
                        self,
                        "Couldn't save settings",
                        f"Your Working Library choice couldn't be saved.\n\n{exc}",
                    )

        # Ensure the Working Library exists.
        try:
            self._settings.working_library.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _logger.warning("could not create working library: %s", exc)

        # API key — prompt only if missing (don't gate the GUI on a key).
        try:
            stored = secret_store.get_anthropic_api_key()
        except secret_store.SecretStoreError as exc:
            _logger.warning("secret store unavailable: %s", exc)
            stored = None
        if not stored:
            self._prompt_api_key(is_reprompt=False)

    def _prompt_api_key(self, *, is_reprompt: bool, technical_detail: str | None = None) -> None:
        dialog = ApiKeyPromptDialog(self, is_reprompt=is_reprompt, technical_detail=technical_detail)
        choice = dialog.exec_with_choice()
        if choice == "save":
            key = dialog.api_key()
            if not key:
                return
            try:
                secret_store.set_anthropic_api_key(key)
                self._audit_log.append_event("Anthropic API key saved.")
            except (secret_store.SecretStoreError, ValueError) as exc:
                QMessageBox.warning(self, "API key not saved", str(exc))

    # -- Client management --------------------------------------------------

    def _maybe_seed_initial_client(self) -> None:
        """Honor ``--client NAME`` if it was supplied on the CLI.

        Auto-creates the client folder when missing — important for the
        ``--fresh --client _DIAGNOSTIC`` test-iteration flow, which wipes
        the folder before the GUI starts, and for one-shot bootstrap of
        a new client from the CLI.

        Also honors ``--queue-pdfs PATH`` by populating the upload queue
        with every PDF found under PATH.
        """
        name = self._settings.cli_initial_client
        _logger.info("seed_initial_client: cli_initial_client=%r", name)
        if not name:
            return
        candidate = self._settings.working_library / name
        _logger.info("seed_initial_client: candidate=%s exists=%s",
                     candidate, candidate.exists())
        if not candidate.exists():
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                (candidate / "inputs").mkdir(exist_ok=True)
                _logger.info("auto-created client folder via --client: %s", candidate)
            except OSError as exc:
                _logger.error(
                    "could not auto-create client folder %s: %s", candidate, exc
                )
                return
        _logger.info("seed_initial_client: calling _load_client(%s)", candidate)
        self._load_client(candidate)
        self._maybe_queue_initial_pdfs()

    # -- Draft client (Client tab → Extract creates the folder) -------------
    #
    # When the operator launches the app without an explicit client, we
    # auto-create a deterministic "__draft__" folder so the Client tab is
    # immediately editable and the existing save flow works unchanged. On
    # Extract, the draft is renamed to the typed Insured name and the
    # extraction proceeds against the renamed client. See
    # ``C:\Users\Andrew\.claude\plans\eager-shimmying-hummingbird.md``.

    DRAFT_DIRNAME: str = "__draft__"
    _FOLDER_NAME_BAD_CHARS: str = r'/\:*?"<>|'

    def _ensure_draft_client(self) -> None:
        """Auto-create (or discard-and-recreate) the draft client.

        Skipped when ``_maybe_seed_initial_client`` already loaded a client
        (e.g., via ``--client`` CLI flag). Any pre-existing ``__draft__``
        folder is silently discarded — every launch starts with a clean
        draft. The Select Existing Client list is how operators pick up
        prior work; the draft is purely scratch space.
        """
        if self._client is not None:
            return
        draft_path = self._settings.working_library / self.DRAFT_DIRNAME
        if draft_path.exists():
            import shutil
            try:
                shutil.rmtree(draft_path)
                _logger.info("draft: discarded prior draft at %s", draft_path)
            except OSError as exc:
                _logger.warning("draft: rmtree failed (%s); reusing in place", exc)
        try:
            draft_path.mkdir(parents=True, exist_ok=True)
            (draft_path / "inputs").mkdir(exist_ok=True)
        except OSError as exc:
            _logger.error("draft: mkdir failed: %s", exc)
            return
        _logger.info("draft client: loading %s", draft_path)
        # _load_client toggles draft-mode chrome based on folder name.
        self._load_client(draft_path)
        # Optional test-harness pre-fill. When launched from the sandbox
        # launcher with --preload-acme, IGA_DRAFT_PREFILL_INSURED carries an
        # Insured Name string. We write it into the freshly-loaded draft so
        # the operator doesn't have to retype "Acme, LLC" before every
        # Group B (reconciliation) test. No-op outside the test sandbox.
        prefill = os.environ.get("IGA_DRAFT_PREFILL_INSURED", "").strip()
        if prefill and self._client is not None and self._client.name == self.DRAFT_DIRNAME:
            state_dict = self._client.state if isinstance(self._client.state, dict) else None
            if state_dict is not None:
                insured = state_dict.setdefault("insured", {})
                if isinstance(insured, dict) and not (insured.get("named_insured") or "").strip():
                    insured["named_insured"] = prefill
                    try:
                        _safe_state_save(state_dict, self._client.path)
                    except Exception as exc:  # noqa: BLE001
                        _logger.warning("draft prefill save failed: %s", exc)
                    # Push the new value into the Client tab UI + refresh gates.
                    self._sync_client_page()
        # --queue-pdfs from the CLI is honoured here too — without --client
        # the seed_initial_client path isn't taken, so the queue wouldn't be
        # populated otherwise. Used by the sandbox launcher's --preload-acme.
        self._maybe_queue_initial_pdfs()

    def _read_insured_name_from_state(self) -> str:
        """Read ``state.insured.named_insured`` from the in-memory client state."""
        if self._client is None:
            return ""
        try:
            insured = (self._client.state or {}).get("insured") or {}
            return str(insured.get("named_insured") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _sanitize_folder_name(self, name: str) -> str:
        """Strip illegal filesystem chars + whitespace from a folder name."""
        cleaned = "".join(ch for ch in (name or "") if ch not in self._FOLDER_NAME_BAD_CHARS)
        # Collapse internal whitespace runs to a single space; strip ends.
        cleaned = " ".join(cleaned.split())
        # Disallow trailing dot/space (Windows reserves these).
        return cleaned.rstrip(". ")

    def _promote_draft_to(self, target: Path) -> bool:
        """Rename the draft folder to ``target`` and reload ``_client`` there.

        Returns True on success. On failure (rename error, load error), the
        draft is left intact at its original path and an error dialog is shown.

        PDFs already queued from the draft's ``inputs/`` directory have
        their queue paths translated to point at the renamed folder — the
        files moved with the rename, so the queue must follow or extraction
        would error with "PDF paths do not exist".
        """
        if self._client is None:
            return False
        draft_path = self._client.path
        try:
            draft_path.rename(target)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Couldn't rename draft folder",
                f"{exc}\n\nThe draft is still at:\n{draft_path}",
            )
            return False
        # Translate any queued PDF paths that lived under the draft folder so
        # the upcoming extraction can find them at their new home.
        self._translate_pending_pdfs(old_root=draft_path, new_root=target)
        try:
            self._load_client(target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(
                self,
                "Couldn't open the renamed client",
                f"{exc}\n\nFolder rename succeeded but load failed.",
            )
            return False
        self._apply_draft_mode(False)
        return True

    def _translate_pending_pdfs(self, *, old_root: Path, new_root: Path) -> None:
        """Rewrite queued PDF paths that started with ``old_root``.

        Called after the draft folder rename so any PDFs uploaded while
        in draft mode (and copied into ``__draft__/inputs/``) still point
        at real files on disk. Paths outside ``old_root`` are left alone.
        """
        if self._pending_pdfs_pane is None:
            return
        try:
            old_resolved = old_root.resolve()
        except OSError:
            old_resolved = old_root
        try:
            new_resolved = new_root.resolve()
        except OSError:
            new_resolved = new_root
        current = self._pending_pdfs_pane.paths()
        if not current:
            return
        translated: list[Path] = []
        changed = False
        for p in current:
            try:
                rel = p.relative_to(old_resolved)
            except ValueError:
                translated.append(p)
                continue
            translated.append(new_resolved / rel)
            changed = True
        if not changed:
            return
        self._pending_pdfs_pane.clear_queue()
        self._pending_pdfs_pane.add_paths(translated)

    def _apply_draft_mode(self, active: bool) -> None:
        """Update header subtitle + window title chrome to reflect draft mode.

        Also toggles the global ClientBanner strip — visible only when a real
        (non-draft) client is loaded so the operator always knows which
        client they're working on.
        """
        try:
            if self._page_header is not None:
                self._page_header.set_draft_mode(active)
        except Exception:  # noqa: BLE001
            pass
        # Window title: prefix "[Draft] " when in draft mode so the operator
        # always knows their state isn't filed yet.
        title = self.windowTitle()
        if active and not title.startswith("[Draft] "):
            self.setWindowTitle(f"[Draft] {title}")
        elif not active and title.startswith("[Draft] "):
            self.setWindowTitle(title[len("[Draft] "):])
        # Client banner.
        banner = getattr(self, "_client_banner", None)
        if banner is not None:
            if active or self._client is None:
                banner.setVisible(False)
            else:
                insured_name = ""
                state_dict = self._client.state or {}
                insured = state_dict.get("insured") if isinstance(state_dict, dict) else None
                if isinstance(insured, dict):
                    insured_name = str(insured.get("named_insured") or "").strip()
                # Prefer the typed Insured name; fall back to the folder name
                # so a freshly-loaded client (no typed name yet) still surfaces.
                label = insured_name or self._client.name
                banner.setText(f"Current client:  {label}")
                banner.setVisible(True)

    def _maybe_queue_initial_pdfs(self) -> None:
        """Honor ``--queue-pdfs PATH`` by adding every PDF in PATH to the
        upload queue. Called once after the initial client is loaded.
        """
        src = self._settings.cli_queue_pdfs
        if src is None:
            return
        if not src.is_dir():
            _logger.warning("--queue-pdfs path is not a directory: %s", src)
            return
        pdfs = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
        if not pdfs:
            _logger.info("--queue-pdfs: no PDFs found in %s", src)
            return
        if self._upload_panel is not None:
            added = self._upload_panel.add_paths(pdfs)
            _logger.info("--queue-pdfs: queued %d PDF(s) from %s", added, src)

    def _on_pick_client(self) -> None:
        """Show the labelled client-picker dialog and load the chosen folder.

        We scan the working library for any subfolder containing a state.json
        and present them by typed Insured name + folder name + last-updated.
        Operators don't need to know the on-disk folder names — they pick by
        the Insured the client represents.
        """
        from .existing_clients_dialog import ExistingClientsDialog
        dlg = ExistingClientsDialog(
            self,
            working_library=self._settings.working_library,
        )
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = dlg.chosen_path()
        if chosen is None:
            return
        self._load_client(chosen)

    def _on_create_client(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(
            self,
            "New client",
            "Client name (used as folder name):",
        )
        if not ok or not name.strip():
            return
        client_path = self._settings.working_library / name.strip()
        if client_path.exists():
            QMessageBox.information(self, "Client exists", "A client with that name already exists.")
            return
        try:
            client_path.mkdir(parents=True, exist_ok=False)
            (client_path / "inputs").mkdir(exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Couldn't create client", str(exc))
            return
        self._load_client(client_path)

    def _load_client(self, client_path: Path) -> None:
        try:
            state = _safe_state_load(client_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "Couldn't open client", str(exc))
            return

        inputs_dir = client_path / "inputs"
        inputs_dir.mkdir(exist_ok=True)

        self._client = _ClientContext(
            name=client_path.name,
            path=client_path,
            state=state,
            inputs_dir=inputs_dir,
        )
        self.setWindowTitle(f"IGA Marketing Master 2.0 — {client_path.name}")
        # Toggle draft-mode chrome based on folder name. Re-applied here so
        # switching from the draft to a real client (via Pick Existing) clears
        # the banner, and switching back to the draft re-asserts it.
        self._apply_draft_mode(client_path.name == self.DRAFT_DIRNAME)
        self._audit_log.append_event(f"Loaded client: {client_path.name}")
        self._push_recent_client(client_path)
        self._rebuild_tabs()
        self._refresh_run_controls()
        self._update_status_bar_idle()

        # Recover-interrupted-run flow.
        pending = state.get("pending_extraction") if isinstance(state, dict) else None
        if pending:
            self._show_recover_interrupted_run(pending)

    # -- Tab derivation -----------------------------------------------------

    def _sync_client_page(self) -> None:
        """Push the current client into the Client sidebar page."""
        if self._client_page is not None:
            self._client_page.set_client(self._client)
        # State just changed — re-evaluate the Data Review gate. Cheap and
        # idempotent; safe to call even when nothing's gating right now.
        self._refresh_data_review_gate()
        self._refresh_clear_client_btn()

    def _rebuild_tabs(self) -> None:
        """Diff current tabs against target keys and apply the delta.

        The coverage tabs (DEFAULT_TAB_KEYS) are always shown so the operator
        can see the section structure even before a client is loaded. When no
        client is loaded, each tab shows an empty-state hint instead of data.
        """
        self._sync_client_page()
        target_keys = derive_tab_keys(self._client.state if self._client else None)
        current_keys = list(self._tab_pages.keys())

        if current_keys == target_keys:
            # In-place refresh — preserves the operator's tab + scroll position.
            state = self._client.state if self._client else None
            for key in target_keys:
                widget = self._tab_pages[key]
                self._refresh_tab_widget(widget, key)
                btn = self._tab_buttons.get(key)
                if btn is not None:
                    btn.setText(build_tab_label(state, key))
            return

        # Tab set changed — full rebuild.
        # Remove old buttons from bar layout.
        for btn in list(self._tab_buttons.values()):
            self._tab_bar_layout.removeWidget(btn)
            btn.setParent(None)
            btn.deleteLater()
        self._tab_buttons.clear()
        # Remove old pages from stack.
        while self._tab_stack.count():
            w = self._tab_stack.widget(0)
            self._tab_stack.removeWidget(w)
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._tab_pages.clear()

        state = self._client.state if self._client else None
        for key in target_keys:
            widget = self._build_tab_widget(key)
            widget.setProperty("tab_key", key)
            self._tab_stack.addWidget(widget)
            self._tab_pages[key] = widget
            label = build_tab_label(state, key)
            btn = self._make_tab_button(key, label)
            self._tab_buttons[key] = btn
            # Insert before the trailing stretch (last item).
            insert_pos = self._tab_bar_layout.count() - 1
            self._tab_bar_layout.insertWidget(insert_pos, btn)

        # Restore or default the active tab.
        if target_keys:
            key_to_select = (
                self._active_tab_key
                if self._active_tab_key in self._tab_pages
                else target_keys[0]
            )
            self._select_tab(key_to_select)

    def _refresh_tab_badges(self) -> None:
        """Update each tab button's text without rebuilding any widget.

        Called whenever a single record's status/confidence changes (e.g. the
        operator typed in a cell or clicked right-click → Accept). The
        ``count_low_confidence_in_tab`` count may have dropped; surface that
        on the badge immediately. A full :meth:`_rebuild_tabs` would also
        work but would tear down the QTableWidget the operator was just
        clicking in — far too jarring for a single-cell edit.
        """
        if not self._tab_buttons:
            return
        state = self._client.state if self._client else None
        for key, btn in self._tab_buttons.items():
            btn.setText(build_tab_label(state, key))

    def _make_tab_button(self, key: str, label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setObjectName("TabBtn")
        btn.setProperty("active", False)
        btn.setProperty("dimmed", False)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(lambda _=False, k=key: self._select_tab(k))
        # Right-click → context menu with "Accept All". Bulk-marks every
        # low-confidence field on this tab as ``approved``/confidence=1.0
        # so the badge clears in one action.
        btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        btn.customContextMenuRequested.connect(
            lambda pos, k=key, b=btn: self._show_tab_context_menu(b, k, pos)
        )
        return btn

    def _show_tab_context_menu(self, btn: QPushButton, tab_key: str, pos) -> None:
        """Right-click on a tab button: offer Accept All for any flagged fields."""
        from PySide6.QtWidgets import QMenu
        state = self._client.state if self._client else None
        flagged = count_low_confidence_in_tab(state, tab_key)
        menu = QMenu(btn)
        if flagged > 0:
            label = (
                f"Accept All ({flagged} item)"
                if flagged == 1
                else f"Accept All ({flagged} items)"
            )
            act = menu.addAction(label)
            act.triggered.connect(lambda _=False, k=tab_key: self._accept_all_on_tab(k))
        else:
            act = menu.addAction("Nothing to accept")
            act.setEnabled(False)
        menu.exec(btn.mapToGlobal(pos))

    def _accept_all_on_tab(self, tab_key: str) -> None:
        """Mark every low-confidence field on *tab_key* as approved.

        Walks both singleton fields and repeatable groups whose namespace
        rolls up to *tab_key*. For each low-confidence record (the same
        criteria :func:`count_low_confidence_in_tab` uses), we set
        ``status="approved"`` and ``confidence=1.0`` so the tab badge clears
        and table cells lose their tint.
        """
        if self._client is None:
            return
        state = self._client.state
        if not isinstance(state, dict):
            return
        changed = 0

        def _approve(record: dict) -> bool:
            # Mirror ``count_low_confidence_in_tab._record_is_low`` exactly so
            # the badge count and Accept All operate on the same set. The
            # earlier version skipped any record with confidence >= HIGH —
            # which left conflict-flagged records (often confidence 0.99 with
            # ``conflicts`` populated) untouched, so Accept All looked broken.
            status = record.get("status", "pending")
            if status in {"approved", "locked"}:
                return False
            has_conflict = bool(record.get("conflicts"))
            try:
                conf = float(record.get("confidence", 0.0) or 0.0)
            except (TypeError, ValueError):
                conf = 0.0
            if not has_conflict and conf >= CONFIDENCE_HIGH_THRESHOLD:
                return False
            record["status"] = "approved"
            record["confidence"] = 1.0
            record["needs_review"] = False
            # Clear the conflict list — the operator's "Accept All" choice
            # supersedes the alternates. Leaving them populated would keep
            # the orange conflict tint even after status flips to approved.
            if has_conflict:
                record["conflicts"] = []
            return True

        if tab_key in REPEATABLE_NAMESPACES:
            for item in (state.get("repeatables") or {}).get(tab_key, []) or []:
                if not isinstance(item, dict):
                    continue
                for tag, rec in item.items():
                    if is_audit_exempt_tag(tag):
                        continue
                    if isinstance(rec, dict) and _approve(rec):
                        changed += 1
        else:
            fields_map = state.get("fields") or {}
            for tag, rec in fields_map.items():
                if (
                    isinstance(rec, dict)
                    and _tab_key_for_tag(tag) == tab_key
                    and not is_audit_exempt_tag(tag)
                    and _approve(rec)
                ):
                    changed += 1
            rep_map = state.get("repeatables") or {}
            prefix = tab_key + "."
            for group, items in rep_map.items():
                if not (group == tab_key or group.startswith(prefix)):
                    continue
                for item in items or []:
                    if not isinstance(item, dict):
                        continue
                    for tag, rec in item.items():
                        if is_audit_exempt_tag(tag):
                            continue
                        if isinstance(rec, dict) and _approve(rec):
                            changed += 1

        if changed == 0:
            return
        self._persist_state()
        self._audit_log.append_event(
            f"Accept All on tab {tab_key!r}: {changed} field(s) marked approved."
        )
        self._rebuild_tabs()
        self._refresh_run_controls()

    def _select_tab(self, key: str) -> None:
        """Switch the active tab to ``key``, updating button styles + stack."""
        # Deactivate old button.
        if self._active_tab_key and self._active_tab_key in self._tab_buttons:
            old_btn = self._tab_buttons[self._active_tab_key]
            old_btn.setProperty("active", False)
            old_btn.style().unpolish(old_btn)
            old_btn.style().polish(old_btn)
        self._active_tab_key = key
        # Activate new button.
        if key in self._tab_buttons:
            new_btn = self._tab_buttons[key]
            new_btn.setProperty("active", True)
            new_btn.style().unpolish(new_btn)
            new_btn.style().polish(new_btn)
        # Switch stack page.
        if key in self._tab_pages and self._tab_stack is not None:
            self._tab_stack.setCurrentWidget(self._tab_pages[key])

    def _build_tab_widget(self, key: str) -> QWidget:
        from .section_forms import SectionFormBase, make_section_form

        state = self._client.state if self._client else None
        form = make_section_form(key, state, self)
        if form is not None:
            form.field_changed.connect(self._on_section_form_field_changed)
            return form
        if key in REPEATABLE_NAMESPACES:
            return self._build_repeatable_tab(key)
        return self._build_singleton_tab(key)

    def _refresh_tab_widget(self, widget: QWidget, key: str) -> None:
        from .section_forms import SectionFormBase

        form = widget if isinstance(widget, SectionFormBase) else None
        if form is not None:
            form.refresh(self._client.state if self._client else None)
            return
        if key in REPEATABLE_NAMESPACES:
            pane = widget.findChild(RepeatablePane)
            if pane is not None and self._client is not None:
                items = (self._client.state.get("repeatables") or {}).get(key, [])
                pane.set_items(items)
            return
        # Singleton tabs (extended sections without a dedicated form).
        view = widget.findChild(SectionTableView)
        if view is None:
            return
        model = view.model()
        if isinstance(model, SectionTableModel):
            model.set_rows(self._build_singleton_rows(key))

    def _on_section_form_field_changed(self, domain_tag: str, value: object) -> None:
        """Handle field edits from dedicated section forms."""
        if domain_tag.startswith("__add:"):
            group = domain_tag[len("__add:"):]
            self._on_repeatable_add(group)
            return
        if domain_tag.startswith("__del:"):
            payload = domain_tag[len("__del:"):]
            colon = payload.rfind(":")
            if colon == -1:
                return
            group = payload[:colon]
            try:
                idx = int(payload[colon + 1:])
            except ValueError:
                return
            self._on_repeatable_delete(group, idx)
            return
        if domain_tag.startswith("__del_many:"):
            # Encoded as ``__del_many:<group>:<i1>,<i2>,<i3>...`` — emitted by
            # section-form QTableWidgets when the operator right-clicks on a
            # multi-row selection and picks "Delete N Rows".
            payload = domain_tag[len("__del_many:"):]
            colon = payload.rfind(":")
            if colon == -1:
                return
            group = payload[:colon]
            idx_csv = payload[colon + 1:]
            try:
                idxs = [int(x) for x in idx_csv.split(",") if x.strip()]
            except ValueError:
                _logger.warning("malformed __del_many: token: %r", domain_tag)
                return
            if idxs:
                self._on_repeatable_delete_many(group, idxs)
            return
        if domain_tag.startswith("__accept_row:"):
            # ``__accept_row:<group>:<state_idx>`` — bulk-approve every
            # record in a single repeatable item.
            payload = domain_tag[len("__accept_row:"):]
            colon = payload.rfind(":")
            if colon == -1:
                return
            group = payload[:colon]
            try:
                idx = int(payload[colon + 1:])
            except ValueError:
                _logger.warning("malformed __accept_row: token: %r", domain_tag)
                return
            self._on_repeatable_accept_rows(group, [idx])
            return
        if domain_tag.startswith("__accept_rows:"):
            # ``__accept_rows:<group>:<i1>,<i2>,...`` — bulk-approve every
            # record across multiple repeatable items in one pass.
            payload = domain_tag[len("__accept_rows:"):]
            colon = payload.rfind(":")
            if colon == -1:
                return
            group = payload[:colon]
            idx_csv = payload[colon + 1:]
            try:
                idxs = [int(x) for x in idx_csv.split(",") if x.strip()]
            except ValueError:
                _logger.warning("malformed __accept_rows: token: %r", domain_tag)
                return
            if idxs:
                self._on_repeatable_accept_rows(group, idxs)
            return
        if domain_tag.startswith("__move:"):
            # Encoded as ``__move:<src_group>:<dest_group>:<json>`` where
            # <json> is a list of {"src_idx": int, "row": {tag: record, ...}}.
            # Section group strings don't contain ':' so split with
            # maxsplit=2 cleanly separates head fields from the JSON tail.
            payload = domain_tag[len("__move:"):]
            parts = payload.split(":", 2)
            if len(parts) != 3:
                _logger.warning("malformed __move: token: %r", domain_tag)
                return
            src_group, dest_group, json_blob = parts
            try:
                import json as _json
                moves = _json.loads(json_blob)
                if not isinstance(moves, list):
                    raise ValueError("payload not a list")
            except (ValueError, _json.JSONDecodeError) as exc:
                _logger.warning("malformed __move JSON: %s — token=%r", exc, domain_tag)
                return
            self._on_repeatable_move(src_group, dest_group, moves)
            return
        if domain_tag.startswith("__rep:"):
            # Encoded as ``__rep:<group>:<index>:<tag>`` (the tag may
            # contain '.', so split with maxsplit=3 from the left).
            payload = domain_tag[len("__rep:"):]
            parts = payload.split(":", 2)
            if len(parts) != 3:
                _logger.warning("malformed __rep: edit token: %r", domain_tag)
                return
            group, idx_str, tag = parts
            try:
                idx = int(idx_str)
            except ValueError:
                _logger.warning("__rep: edit has non-integer index: %r", domain_tag)
                return
            if self._client is None:
                return
            self._update_repeatable_field(group, idx, tag, value)
            self._refresh_run_controls()
            return
        if domain_tag.startswith("__upsert_by_state:"):
            # Encoded as ``__upsert_by_state:<group>:<state_code>:<full_tag>``.
            # Looks for an existing item in `state.repeatables[group]` whose
            # `{group}.state` value matches state_code (case-insensitive);
            # creates one if none exists, then sets `full_tag` to `value`.
            # Used by WorkersCompForm's per-state Rating Information table
            # where rows are keyed by state, not by index.
            payload = domain_tag[len("__upsert_by_state:"):]
            parts = payload.split(":", 2)
            if len(parts) != 3:
                _logger.warning("malformed __upsert_by_state: token: %r", domain_tag)
                return
            group, state_code, tag = parts
            if self._client is None:
                return
            self._upsert_repeatable_by_state(group, state_code, tag, value)
            self._refresh_run_controls()
            return
        if self._client is None:
            return
        self._update_field(domain_tag, value)
        self._refresh_run_controls()

    def _update_repeatable_field(
        self, group: str, index: int, tag: str, value: object
    ) -> None:
        """Update one field of one item in a repeatable group, persist state."""
        if self._client is None:
            return
        state = self._client.state
        reps = state.setdefault("repeatables", {})
        items = reps.setdefault(group, [])
        if not isinstance(items, list):
            return
        # Pad missing indices with empty dicts (defensive — shouldn't happen
        # in normal flow since rows are populated before edits).
        while len(items) <= index:
            items.append({})
        item = items[index]
        if not isinstance(item, dict):
            items[index] = {}
            item = items[index]
        # Edit the field record's value, preserving the rest of its metadata
        # (confidence, source, etc.) so audit info isn't blown away.
        rec = item.get(tag)
        if isinstance(rec, dict):
            rec["value"] = value
            rec["status"] = "edited"
            rec["conflicts"] = []
        else:
            item[tag] = {"value": value, "status": "edited", "conflicts": []}
        # Persist via the same path singleton edits use.
        self._persist_state()

    def _upsert_repeatable_by_state(
        self, group: str, state_code: str, tag: str, value: object
    ) -> None:
        """Upsert into a repeatable group keyed by ``{group}.state``.

        Used by Workers Comp's Rating Information table. The display has
        one row per selected state (Active ∪ Other ∪ "ALL OTHER"), but the
        underlying state.repeatables may not yet have a row for every
        selected state. When the operator edits any cell, this helper
        finds-or-creates the row keyed by state code and writes the value.
        """
        if self._client is None:
            return
        state = self._client.state
        reps = state.setdefault("repeatables", {})
        items = reps.setdefault(group, [])
        if not isinstance(items, list):
            return
        state_tag = f"{group}.state"
        wanted = (state_code or "").strip().upper()
        target_idx: int | None = None
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            rec = item.get(state_tag)
            if isinstance(rec, dict):
                v = str(rec.get("value") or "").strip().upper()
                if v == wanted:
                    target_idx = i
                    break
        if target_idx is None:
            # Create a new item carrying the state field, then drop in the
            # edited field. confidence=1.0 / status="edited" mirrors what
            # _update_repeatable_field does for operator-originated edits.
            new_item: dict = {
                state_tag: {
                    "value": state_code,
                    "status": "edited",
                    "confidence": 1.0,
                    "source": [],
                    "conflicts": [],
                    "history": [],
                },
            }
            items.append(new_item)
            target_idx = len(items) - 1
        item = items[target_idx]
        rec = item.get(tag)
        if isinstance(rec, dict):
            rec["value"] = value
            rec["status"] = "edited"
            rec["conflicts"] = []
        else:
            item[tag] = {"value": value, "status": "edited", "conflicts": []}
        self._persist_state()

    def _build_singleton_tab(self, key: str) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        rows = self._build_singleton_rows(key)
        model = SectionTableModel(rows, commit_callback=self._make_singleton_commit_callback())
        view = SectionTableView(container)
        view.setModel(model)

        model.source_clicked.connect(self._on_source_clicked)
        model.conflict_clicked.connect(self._on_conflict_clicked)
        view.focus_changed.connect(self._on_field_focus_changed)
        # When data changes, the run-controls' approved count may shift.
        model.dataChanged.connect(lambda *_: self._refresh_run_controls())

        # UX-pass #7: per-section bulk-action toolbar.
        bulk_bar = BulkActionBar(container)
        bulk_bar.action_requested.connect(
            lambda kind, k=key: self._on_bulk_action(kind, k)
        )
        layout.addWidget(bulk_bar)

        layout.addWidget(view)

        # Empty-state hint if the tab has no rows yet.
        if not rows:
            if self._client is None:
                msg = "Pick a client (Ctrl+O) or drop PDFs onto the window to get started."
            else:
                msg = "No fields extracted for this section yet.\nAdd PDFs and click Extract to populate."
            hint = QLabel(msg, container)
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("QLabel { color: #94a3b8; font-size: 13px; padding: 40px 24px; }")
            hint.setWordWrap(True)
            layout.addWidget(hint)

        return container

    def _build_singleton_rows(self, key: str) -> list[FieldRow]:
        if self._client is None:
            return []
        fields_map: dict = self._client.state.get("fields") or {}
        rows: list[FieldRow] = []
        for tag, record in sorted(fields_map.items()):
            if _tab_key_for_tag(tag) != key:
                continue
            if not isinstance(record, dict):
                continue
            label = self._friendly_label_for_tag(tag)
            rows.append(
                FieldRow.from_field_record(
                    domain_tag=tag,
                    label=label,
                    record=record,
                )
            )
        return rows

    def _build_repeatable_tab(self, key: str) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        items = []
        if self._client is not None:
            items = (self._client.state.get("repeatables") or {}).get(key, [])

        # UX-pass #7: bulk-action bar above the list+form pane.
        bulk_bar = BulkActionBar(container)
        bulk_bar.action_requested.connect(
            lambda kind, k=key: self._on_bulk_action(kind, k)
        )
        layout.addWidget(bulk_bar)

        pane = RepeatablePane(group=key, items=items, parent=container)
        pane.source_clicked.connect(self._on_source_clicked)
        pane.conflict_clicked.connect(self._on_conflict_clicked)
        pane.commit_requested.connect(self._on_repeatable_commit)
        pane.item_added.connect(lambda: self._on_repeatable_add(key))
        pane.item_deleted.connect(lambda idx: self._on_repeatable_delete(key, idx))
        pane.items_deleted.connect(lambda idxs: self._on_repeatable_delete_many(key, idxs))
        pane.focus_changed.connect(self._on_repeatable_focus)
        layout.addWidget(pane, 1)
        return container

    @staticmethod
    def _friendly_label_for_tag(domain_tag: str) -> str:
        last = domain_tag.split(".")[-1]
        return last.replace("_", " ").title()

    # -- Edit / persistence -------------------------------------------------

    def _make_singleton_commit_callback(self) -> Callable[[str, object], None]:
        def commit(domain_tag: str, value: object) -> None:
            if self._client is None:
                return
            self._update_field(domain_tag, value)

        return commit

    def _update_field(self, domain_tag: str, value: object) -> None:
        if self._client is None:
            return
        value = _normalize_committed_value(value)
        fields_map: dict = self._client.state.setdefault("fields", {})
        record: dict = fields_map.setdefault(domain_tag, {
            "value": None,
            "confidence": 0.0,
            "status": "pending",
            "source": [],
            "conflicts": [],
            "history": [],
            "model_used": None,
            "needs_review": False,
        })
        prior = {
            "value": record.get("value"),
            "status": record.get("status"),
            "confidence": record.get("confidence"),
        }
        record["value"] = value
        record["confidence"] = 1.0
        # Operator edit = operator approval, regardless of prior status. The
        # only exceptions are ``locked`` (explicit hard lock) and ``entered``
        # (already written to EPIC — re-editing here doesn't reset that).
        if record.get("status") not in {"locked", "entered"}:
            record["status"] = "approved"
        record["needs_review"] = False
        # History entry — the state module owns the canonical helper, but we
        # write a compatible-shape entry here as a fallback.
        new = {
            "value": value,
            "status": record["status"],
            "confidence": record["confidence"],
        }
        self._append_history_entry(domain_tag, "update", prior, new)
        self._persist_state()
        self._audit_log.append_event(f"Edited {domain_tag} → {value!r}")
        self._refresh_run_controls()
        self._refresh_tab_badges()

    def _append_history_entry(self, domain_tag: str, action: str, prior: dict, new: dict) -> None:
        from datetime import datetime, timezone
        from uuid import uuid4

        if self._client is None:
            return
        record = self._client.state["fields"].get(domain_tag)
        if not isinstance(record, dict):
            return
        history: list = record.setdefault("history", [])
        history.append({
            "run_id": str(uuid4()),
            "ts": datetime.now(timezone.utc).isoformat(),
            "user": _safe_user(),
            "actor": "gui",
            "action": action,
            "prior": prior,
            "new": new,
        })

    def _persist_state(self) -> None:
        if self._client is None:
            return

        self._client.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _safe_state_save(self._client.state, self._client.path)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Couldn't save state",
                "Your edits couldn't be saved to disk.\n\n"
                "The Working Library folder may have disconnected, or the disk is full.\n\n"
                f"{exc}",
            )
            return
        self._client.last_save_at = datetime.now(timezone.utc)
        # Surface the freshly-updated "saved Xs ago" tag in the status bar.
        self._update_status_bar_idle()

    # -- Repeatable group event handlers -----------------------------------

    def _on_repeatable_commit(self, group: str, item_index: int, domain_tag: str, value: object) -> None:
        if self._client is None:
            return
        value = _normalize_committed_value(value)
        repeatables: dict = self._client.state.setdefault("repeatables", {})
        items: list = repeatables.setdefault(group, [])
        if not 0 <= item_index < len(items):
            return
        record = items[item_index].setdefault(domain_tag, {
            "value": None,
            "confidence": 0.0,
            "status": "pending",
            "source": [],
            "conflicts": [],
            "history": [],
            "model_used": None,
            "needs_review": False,
        })
        record["value"] = value
        record["confidence"] = 1.0
        # Operator typed it = operator approved it. Even if the prior status
        # was something exotic (low_confidence, conflicted, etc.) the manual
        # edit clears it. Locked/entered are sticky exceptions.
        if record.get("status") not in {"locked", "entered"}:
            record["status"] = "approved"
        record["needs_review"] = False
        self._persist_state()
        self._audit_log.append_event(f"Edited {group}[{item_index}] {domain_tag} → {value!r}")
        self._refresh_run_controls()
        # Update only the badge text on each tab button — no widget rebuild.
        # A full ``_rebuild_tabs()`` would replace the table the operator is
        # mid-interaction with and steal focus/scroll position.
        self._refresh_tab_badges()

    def _on_repeatable_accept_rows(self, group: str, item_indices: list) -> None:
        """Mark every record in the listed repeatable items as approved.

        Walks each item's records and applies the same status/confidence
        adjustments :meth:`_on_repeatable_commit` does for a single edit:
        ``status = "approved"`` (unless locked/entered), ``confidence = 1.0``,
        ``needs_review = False``. Persists once at the end, refreshes the
        tab badges, and rebuilds tabs so the tinted cells repaint clean.
        """
        if self._client is None:
            return
        repeatables: dict = self._client.state.get("repeatables") or {}
        items: list = repeatables.get(group, [])
        valid = sorted({i for i in item_indices if isinstance(i, int) and 0 <= i < len(items)})
        if not valid:
            return
        changed = 0
        for idx in valid:
            item = items[idx]
            if not isinstance(item, dict):
                continue
            for rec in item.values():
                if not isinstance(rec, dict):
                    continue
                status = rec.get("status", "pending")
                if status in {"locked", "entered"}:
                    continue
                rec["status"] = "approved"
                rec["confidence"] = 1.0
                rec["needs_review"] = False
                if "conflicts" in rec:
                    rec["conflicts"] = []
                changed += 1
        if changed == 0:
            return
        self._persist_state()
        suffix = "row" if len(valid) == 1 else f"rows ({', '.join(f'#{i + 1}' for i in valid)})"
        self._audit_log.append_event(
            f"Accept {suffix} on {group}: {changed} field(s) marked approved."
        )
        self._refresh_run_controls()
        self._refresh_tab_badges()
        self._rebuild_tabs()

    def _on_repeatable_add(self, group: str) -> None:
        if self._client is None:
            return
        repeatables: dict = self._client.state.setdefault("repeatables", {})
        items: list = repeatables.setdefault(group, [])
        items.append({})
        self._persist_state()
        self._audit_log.append_event(f"Added empty {group} item.")
        self._rebuild_tabs()

    def _on_repeatable_delete(self, group: str, item_index: int) -> None:
        if self._client is None:
            return
        repeatables: dict = self._client.state.get("repeatables") or {}
        items: list = repeatables.get(group, [])
        if not 0 <= item_index < len(items):
            return
        confirm = QMessageBox.question(
            self,
            "Delete item",
            f"Delete {group} item #{item_index + 1}? This cannot be undone.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        items.pop(item_index)
        self._persist_state()
        self._audit_log.append_event(f"Deleted {group} item #{item_index + 1}.")
        self._rebuild_tabs()

    def _on_repeatable_delete_many(self, group: str, item_indices: list) -> None:
        """Bulk-delete multiple selected items from a repeatable group.

        Single confirmation prompt covering the whole set; deletions happen in
        descending index order so earlier positions remain valid as later ones
        are popped.
        """
        if self._client is None:
            return
        repeatables: dict = self._client.state.get("repeatables") or {}
        items: list = repeatables.get(group, [])
        # De-dupe + validate against current item count
        valid = sorted({i for i in item_indices if isinstance(i, int) and 0 <= i < len(items)})
        if not valid:
            return
        if len(valid) == 1:
            # Fall through to the single-item path so the prompt matches the
            # operator's intent (and no plural-vs-singular text confusion).
            self._on_repeatable_delete(group, valid[0])
            return
        human_nums = ", ".join(f"#{i + 1}" for i in valid)
        confirm = QMessageBox.question(
            self,
            "Delete items",
            f"Delete {len(valid)} {group} items ({human_nums})? This cannot be undone.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        for idx in reversed(valid):
            items.pop(idx)
        self._persist_state()
        self._audit_log.append_event(
            f"Deleted {len(valid)} {group} items ({human_nums})."
        )
        self._rebuild_tabs()

    def _on_repeatable_move(
        self,
        src_group: str,
        dest_group: str,
        moves: list,
    ) -> None:
        """Move rows from one repeatable group to another.

        *moves* is a list of dicts: ``{"src_idx": int, "row": {tag: rec, ...}}``.
        The transform is computed in the form (see
        ``section_forms.transform_im_row``) so the host just appends the
        provided ``row`` dicts to *dest_group* and pops the ``src_idx``
        positions from *src_group* in descending order.

        If either side is the Inland Marine Scheduled-Equipment group,
        the singleton ``policy.inland_marine.total_scheduled_amount``
        field is rewritten from the post-move Scheduled rows so state.json
        stays in lockstep with the visible total.
        """
        if self._client is None:
            return
        state = self._client.state
        repeatables: dict = state.setdefault("repeatables", {})
        src_items: list = repeatables.setdefault(src_group, [])
        dest_items: list = repeatables.setdefault(dest_group, [])

        # Validate src indices. De-dupe in case the form sent duplicates.
        seen: set[int] = set()
        valid_moves: list = []
        for m in moves:
            if not isinstance(m, dict):
                continue
            try:
                idx = int(m.get("src_idx"))
            except (TypeError, ValueError):
                continue
            if idx in seen or not (0 <= idx < len(src_items)):
                continue
            row = m.get("row")
            if not isinstance(row, dict):
                continue
            seen.add(idx)
            valid_moves.append((idx, row))
        if not valid_moves:
            return

        # Append the new destination rows in source order so the user's
        # visible ordering is preserved.
        for src_idx, row in sorted(valid_moves, key=lambda t: t[0]):
            dest_items.append(row)

        # Pop source rows in descending order so earlier indices stay valid.
        for src_idx, _ in sorted(valid_moves, key=lambda t: -t[0]):
            src_items.pop(src_idx)

        # Recompute the IM Scheduled-amount singleton when in/out of Sched.
        IM_SCHED = "policy.inland_marine.scheduled_item"
        TOTAL_TAG = "policy.inland_marine.total_scheduled_amount"
        if src_group == IM_SCHED or dest_group == IM_SCHED:
            import re as _re
            total = 0.0
            for item in repeatables.get(IM_SCHED, []):
                rec = item.get(f"{IM_SCHED}.amt_insurance")
                if isinstance(rec, dict):
                    raw = str(rec.get("value") or "")
                    num = _re.sub(r"[^\d.]", "", raw)
                    try:
                        total += float(num)
                    except ValueError:
                        pass
            fields: dict = state.setdefault("fields", {})
            existing = fields.get(TOTAL_TAG)
            new_value = f"{int(total):,}" if total > 0 else ""
            if isinstance(existing, dict):
                existing["value"] = new_value
            else:
                fields[TOTAL_TAG] = {"value": new_value}

        self._persist_state()
        n = len(valid_moves)
        src_short = src_group.rsplit(".", 1)[-1]
        dest_short = dest_group.rsplit(".", 1)[-1]
        self._audit_log.append_event(
            f"Moved {n} row{'s' if n != 1 else ''} from {src_short} to {dest_short}."
        )
        self._rebuild_tabs()

    def _on_repeatable_focus(self, group: str, item_index: int, domain_tag: str) -> None:
        if self._client is None:
            return
        items: list = (self._client.state.get("repeatables") or {}).get(group, [])
        if not 0 <= item_index < len(items):
            return
        record = items[item_index].get(domain_tag)
        if isinstance(record, dict):
            self._show_pdf_for_record(record)

    # -- Source / conflict handlers ----------------------------------------

    def _on_source_clicked(self, doc_id: str, page: int) -> None:
        pass  # PDF preview removed

    def _on_field_focus_changed(self, domain_tag: str) -> None:
        pass  # PDF preview removed

    def _show_pdf_for_record(self, record: dict) -> None:
        pass  # PDF preview removed

    def _on_conflict_clicked(self, domain_tag: str) -> None:
        if self._client is None:
            return
        fields_map = self._client.state.get("fields") or {}
        record = fields_map.get(domain_tag)
        if not isinstance(record, dict):
            return
        candidates = record.get("conflicts") or []
        if not candidates:
            return
        dialog = ConflictResolutionDialog(
            self,
            field_label=self._friendly_label_for_tag(domain_tag),
            current_value=record.get("value"),
            current_confidence=float(record.get("confidence", 0.0) or 0.0),
            candidates=candidates,
        )
        chosen = dialog.exec_with_choice()
        if not isinstance(chosen, dict):
            return
        prior = {
            "value": record.get("value"),
            "status": record.get("status"),
            "confidence": record.get("confidence"),
        }
        # Promote: chosen candidate becomes value; old canonical moves to conflicts.
        old_canonical = {
            "value": prior["value"],
            "confidence": prior["confidence"],
            "source": (record.get("source") or [{}])[0],
            "model_used": record.get("model_used"),
            "observed_at": record.get("updated_at") or "",
        }
        record["value"] = chosen.get("value")
        record["confidence"] = float(chosen.get("confidence", 1.0) or 1.0)
        record["model_used"] = chosen.get("model_used")
        if chosen.get("source"):
            record["source"] = [chosen["source"]]
        new_conflicts = [c for c in candidates if c is not chosen]
        if old_canonical["value"] is not None:
            new_conflicts.append(old_canonical)
        record["conflicts"] = new_conflicts
        self._append_history_entry(
            domain_tag,
            "resolve_conflict",
            prior,
            {"value": record["value"], "status": record["status"], "confidence": record["confidence"]},
        )
        self._persist_state()
        self._audit_log.append_event(f"Resolved conflict on {domain_tag} → {record['value']!r}")
        self._rebuild_tabs()
        self._refresh_run_controls()

    # -- PDF intake ---------------------------------------------------------

    def _on_add_pdfs(self) -> None:
        if self._client is None:
            QMessageBox.information(self, "No client loaded", "Pick or create a client first.")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "Select source PDFs",
            str(self._client.path),
            "PDF Files (*.pdf)",
        )
        if not files:
            return
        copied = self._copy_pdfs_to_inputs([Path(f) for f in files])
        if not copied:
            return
        added = self._pending_pdfs_pane.add_paths(copied)
        self._audit_log.append_event(
            f"Queued {added} PDF(s) for extraction (total queued: {len(self._pending_pdfs_pane.paths())})."
        )

    def dragEnterEvent(self, event) -> None:  # noqa: N802 (Qt-style)
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 (Qt-style)
        if self._client is None:
            QMessageBox.information(self, "No client loaded", "Pick or create a client first.")
            return
        urls = event.mimeData().urls()
        files = [Path(u.toLocalFile()) for u in urls if u.toLocalFile().lower().endswith(".pdf")]
        if not files:
            return
        copied = self._copy_pdfs_to_inputs(files)
        if not copied:
            return
        added = self._pending_pdfs_pane.add_paths(copied)
        self._audit_log.append_event(
            f"Queued {added} dropped PDF(s) for extraction "
            f"(total queued: {len(self._pending_pdfs_pane.paths())})."
        )

    def _copy_pdfs_to_inputs(self, files: list[Path]) -> list[Path]:
        """Copy ``files`` into ``<client>/inputs/`` and return the destination paths.

        Mirrors the prior inline behavior of ``_on_add_pdfs`` / ``dropEvent``.
        Source-quote deep-links from state.json reference paths under
        ``inputs/`` so we copy on intake — even though extraction now
        queues, the copy still happens at intake time so the queue holds
        canonical destination paths.
        """
        if self._client is None:
            return []
        import shutil

        copied: list[Path] = []
        for src in files:
            dest = self._client.inputs_dir / src.name
            try:
                if src.resolve() != dest.resolve():
                    shutil.copy2(src, dest)
                copied.append(dest)
            except OSError as exc:
                _logger.warning("could not copy %s: %s", src, exc)
        return copied

    def _refresh_pending_pdfs_state(self) -> None:
        """Sync the run-controls bar's view of queue size."""
        count = self._pending_pdfs_pane.count()
        self._run_controls.set_pending_pdf_count(count)
        # Keep the menu/toolbar Extract action gated on queue depth too.
        ready = self._count_enterable_fields(self._client.state) if self._client else 0
        self._sync_run_actions(ready=ready)

    def _on_extract_clicked(self) -> None:
        """Handler for the new Extract button. See gui-fix-2 #1."""
        if self._client is None:
            QMessageBox.information(self, "No client loaded", "Pick or create a client first.")
            return
        # Draft-client promotion: if the operator is editing the scratch
        # ``__draft__`` folder, rename it to the typed Insured name before
        # extracting. Aborts (without running extraction) if the name is
        # blank or already taken — operator must either fix the Client tab
        # or use Pick Existing Client.
        if self._client.name == self.DRAFT_DIRNAME:
            if not self._promote_draft_for_extract():
                return
        queued = self._pending_pdfs_pane.paths()
        if not queued:
            return
        # Snapshot the queue and clear it before launching — if the worker
        # spawn fails (e.g., extraction module not wired up) we restore.
        self._launch_extraction(queued)
        if self._worker_thread is not None:
            # The worker accepted the job — clear the queue.
            self._pending_pdfs_pane.clear_queue()

    def _promote_draft_for_extract(self) -> bool:
        """Validate + rename the draft folder using the typed Insured name.

        Returns True if the draft was successfully promoted (or if there's
        nothing to do); False if the operator should fix something before
        extraction can run.
        """
        typed = self._read_insured_name_from_state()
        if not typed:
            QMessageBox.warning(
                self,
                "Type a client name first",
                "Enter the Insured Name on the Client tab before clicking Extract.\n"
                "That name becomes the client folder.",
            )
            return False
        safe = self._sanitize_folder_name(typed)
        if not safe:
            QMessageBox.warning(
                self,
                "Invalid client name",
                "The Insured Name can't be used as a folder name "
                "(no usable characters after removing /\\:*?\"<>|). "
                "Edit the Insured Name on the Client tab and try again.",
            )
            return False
        target = self._settings.working_library / safe
        if target.exists():
            QMessageBox.warning(
                self,
                "Client folder already exists",
                f"A client folder named {safe!r} already exists.\n\n"
                "If you're re-running extraction on that account, "
                "click Pick Existing Client to open it.\n"
                "Otherwise rename the Insured on the Client tab to "
                "something unique.",
            )
            return False
        return self._promote_draft_to(target)

    def _launch_extraction(self, pdf_paths: list[Path]) -> None:
        if self._client is None or not pdf_paths:
            return
        if self._worker_thread is not None:
            QMessageBox.information(
                self,
                "Already running",
                "Another run is already in progress. Wait for it to finish or click Cancel.",
            )
            return
        # Late import — extract.py may still be a stub; we just want to surface
        # the failure mode cleanly.
        try:
            from .. import extract as extract_module
        except ImportError:
            QMessageBox.information(
                self,
                "Extraction not available yet",
                "The extraction module isn't wired up yet. PDFs were copied but no extraction was run.",
            )
            return
        run_extraction = getattr(extract_module, "run_extraction", None)
        if not callable(run_extraction):
            QMessageBox.information(
                self,
                "Extraction not available yet",
                "The extraction module isn't wired up yet. PDFs were copied but no extraction was run.",
            )
            return

        force_opus = self._run_controls.is_force_opus()
        pdf_count = len(pdf_paths)

        def task(worker: _CallableWorker) -> object:
            assert self._client is not None
            worker.emit_progress_full(
                f"Completed 0 of {pdf_count} — starting...", 0, pdf_count
            )

            def on_progress(message: str, current: int, total: int) -> None:
                worker.emit_progress_full(message, current, total)

            return run_extraction(
                self._client.name,
                pdf_paths,
                force_opus=force_opus,
                settings=self._settings,
                progress_callback=on_progress,
            )

        self._active_run_kind = "extract"
        self._run_controls.set_extracting(True)
        self._show_progress_determinate(
            f"Completed 0 of {pdf_count} — starting...", 0, pdf_count
        )
        self._show_busy_dialog(f"Working... (0 of {pdf_count})")
        self._spawn_worker(
            task,
            on_finished=self._on_extraction_finished,
            on_failed=self._on_extraction_failed,
        )

    def _on_extraction_finished(self, _result: object) -> None:
        self._run_controls.set_extracting(False)
        self._active_run_kind = None
        self._close_busy_dialog()
        # Reload state BEFORE the status-bar refresh fires, otherwise the
        # "last run $X.XXXX · total $Y.YYYY" debug segment reads stale
        # in-memory state (no cost_usd yet) and the operator doesn't see
        # the cost until the next 30s idle-tick.
        if self._client is not None:
            self._client.state = _safe_state_load(self._client.path)
        self._hide_progress("Extraction complete.")
        if self._client is None:
            return
        self._audit_log.append_event("Extraction complete.")
        # Post-extraction reconciliation: Client tab ↔ matched named_insureds
        # row. Surfaces conflicts in a popup and silently merges single-side
        # gaps. See eager-shimmying-hummingbird.md.
        self._run_client_ni_reconciliation()
        self._rebuild_tabs()
        self._refresh_run_controls()

    def _run_client_ni_reconciliation(self) -> None:
        """Compare Client tab Insured against the matching named_insured row.

        Fires at the end of every extraction. Three outcomes per field:
          * Both sides equal → no-op (dropped).
          * One side blank → silent merge into the blank side.
          * Both differ → operator picks in the ReconciliationDialog.
        Operator picks become canon (Insured.canon_fields + NI FieldRecord.pinned).
        """
        if self._client is None:
            return
        try:
            from .. import reconcile as reconcile_mod
            from .. import state as state_module
            from .reconciliation_dialog import ReconciliationDialog
        except Exception as exc:  # noqa: BLE001
            _logger.warning("reconciliation: imports failed: %s", exc)
            return
        try:
            st = state_module.load(self._client.path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("reconciliation: state load failed: %s", exc)
            return
        # Ensure the Client tab's primary insured is represented in the
        # named_insured repeatable. When extraction surfaced no matching row,
        # this creates one with low confidence + needs_review so the GUI
        # flags it as "operator-typed, not extraction-confirmed".
        created_primary = reconcile_mod.ensure_client_insured_in_ni(st)
        if created_primary:
            try:
                state_module.save_atomic(st, self._client.path)
            except Exception as exc:  # noqa: BLE001
                _logger.warning("reconciliation: post-create save failed: %s", exc)
            self._client.state = _safe_state_load(self._client.path)
        if reconcile_mod.find_matching_ni_row(st) is None:
            return  # still no matched NI row (Client tab blank) → nothing to reconcile
        deltas = reconcile_mod.compute_deltas(st)
        conflicts = [d for d in deltas if d.kind in ("conflict", "canon_extraction_diff")]
        silent_merges = [
            d for d in deltas
            if d.kind in ("silent_merge_to_insured", "silent_merge_to_ni")
        ]
        if not conflicts and not silent_merges:
            return  # everything already in sync
        if conflicts:
            dlg = ReconciliationDialog(
                self,
                entity_name=st.insured.named_insured,
                conflicts=conflicts,
                silent_merges=silent_merges,
            )
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return  # Cancel: drop silent merges too — no state change.
            reconcile_mod.apply_silent_merges(st, silent_merges)
            reconcile_mod.apply_resolutions(st, dlg.resolutions())
        else:
            # No conflicts, only silent gap-fills — apply without a popup.
            reconcile_mod.apply_silent_merges(st, silent_merges)
        try:
            state_module.save_atomic(st, self._client.path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("reconciliation: save_atomic failed: %s", exc)
            return
        self._client.state = _safe_state_load(self._client.path)
        self._audit_log.append_event(
            f"Reconciliation: merged {len(silent_merges)} gap(s), "
            f"resolved {len(conflicts)} conflict(s)."
        )

    def _on_extraction_failed(self, message: str, technical: str) -> None:
        self._run_controls.set_extracting(False)
        self._active_run_kind = None
        self._close_busy_dialog()
        # Partial-run failures still write a RunHistoryEntry with cost_usd
        # for whatever docs completed. Reload so the debug cost segment
        # reflects the partial spend.
        if self._client is not None:
            self._client.state = _safe_state_load(self._client.path)
        self._hide_progress("Extraction didn't finish.")
        QMessageBox.warning(
            self,
            "Extraction didn't finish",
            f"{message}\n\nYour edits are saved. Try again, or check the audit log for details.",
        )
        _logger.error("extraction failed: %s | %s", message, technical)

    # -- Busy dialog (extraction-in-progress popup) -------------------------

    def _show_busy_dialog(self, status_text: str = "Working...") -> None:
        """Pop up the working.gif modal dialog. Auto-closed by the
        extraction-finished / extraction-failed slots."""
        if self._busy_dialog is not None:
            # Already up (rare — would only happen on a second-click race).
            self._busy_dialog.set_status_text(status_text)
            return
        self._busy_dialog = _ExtractionBusyDialog(self)
        self._busy_dialog.set_status_text(status_text)
        # show() (not exec()) so the worker thread keeps running and our
        # finished-slot can call .close() on the dialog later.
        self._busy_dialog.show()

    def _close_busy_dialog(self) -> None:
        if self._busy_dialog is None:
            return
        dlg = self._busy_dialog
        self._busy_dialog = None
        dlg.close()
        dlg.deleteLater()

    # -- Entry-run busy dialog + cancel + validation halt -------------------

    def _show_entry_busy_dialog(self, status_text: str = "Entering...") -> None:
        """Pop up the entry-run busy modal with cancel button + live status."""
        from .entry_busy_dialog import EntryBusyDialog
        if self._entry_busy_dialog is not None:
            self._entry_busy_dialog.set_status_text(status_text)  # type: ignore[union-attr]
            return
        dlg = EntryBusyDialog(self)
        dlg.set_status_text(status_text)
        dlg.cancel_requested.connect(self._on_entry_cancel_requested)
        self._entry_busy_dialog = dlg
        dlg.show()

    def _close_entry_busy_dialog(self) -> None:
        if self._entry_busy_dialog is None:
            return
        dlg = self._entry_busy_dialog
        self._entry_busy_dialog = None
        try:
            dlg.close()  # type: ignore[union-attr]
            dlg.deleteLater()  # type: ignore[union-attr]
        except Exception:
            pass

    def _on_entry_cancel_requested(self) -> None:
        """Cancel button on the entry busy dialog was clicked.

        Sets the runtime cancel_event so step files raise ``EntryCancelled``
        at their next checkpoint. The busy dialog stays up; the worker's
        finished/failed slot closes it.
        """
        if self._entry_cancel_event is not None:
            try:
                self._entry_cancel_event.set()  # type: ignore[union-attr]
            except Exception:
                pass
        self._audit_log.append_event("Entry: cancel requested by operator.")
        if self._worker is not None and hasattr(self._worker, "cancel_requested"):
            try:
                self._worker.cancel_requested = True  # type: ignore[attr-defined]
            except Exception:
                pass

    def _on_validation_halt(self, finding: object) -> str:
        """Callback invoked by epic_steps.validation_check on EPIC validation error.

        Runs on the worker thread; marshals the modal onto the GUI thread
        via QTimer.singleShot(0, ...) and blocks the worker on a
        threading.Event until the operator picks Proceed or Cancel.
        Returns ``"proceed"`` or ``"cancel"`` per epic_steps.runtime.
        """
        from .validation_halt_dialog import ValidationHaltDialog

        result_holder: dict[str, str] = {"choice": "cancel"}

        def show_modal() -> None:
            dlg = ValidationHaltDialog(
                self,
                sequence=getattr(finding, "sequence", 0),
                error_text=getattr(finding, "error_text", "(unknown)"),
                expecting=getattr(finding, "expecting", ""),
                screen_code=getattr(finding, "screen_code", "") or "",
                screenshot_path=getattr(finding, "screenshot_path", None),
            )
            dlg.exec()
            result_holder["choice"] = dlg.choice()

        if QThread.currentThread() is self.thread():
            show_modal()
        else:
            import threading as _threading
            from PySide6.QtCore import QTimer
            _done = _threading.Event()

            def _runner() -> None:
                try:
                    show_modal()
                finally:
                    _done.set()

            QTimer.singleShot(0, self, _runner)
            _done.wait()

        return result_holder["choice"]

    def _on_preflight_confirm_mms_open(
        self,
        *,
        account_name: str,
        lookup_code: str,
        screen_code: str,
    ) -> str:
        """Worker → GUI: show MMSOpenConfirmDialog and return ``proceed``/``cancel``.

        Safe to call from the worker thread; marshals via QTimer.singleShot.
        """
        from .entry_preflight_dialogs import MMSOpenConfirmDialog
        result_holder: dict[str, str] = {"choice": "cancel"}

        def show_modal() -> None:
            dlg = MMSOpenConfirmDialog(
                self,
                account_name=account_name,
                lookup_code=lookup_code,
                screen_code=screen_code,
            )
            dlg.exec()
            result_holder["choice"] = dlg.choice()

        if QThread.currentThread() is self.thread():
            show_modal()
        else:
            import threading as _threading
            from PySide6.QtCore import QTimer
            _done = _threading.Event()
            def _runner() -> None:
                try:
                    show_modal()
                finally:
                    _done.set()
            QTimer.singleShot(0, self, _runner)
            _done.wait()
        return result_holder["choice"]

    def _on_preflight_prompt_open_mms(
        self,
        *,
        account_name: str,
        lookup_code: str,
        hint_message: str = "",
    ) -> str:
        """Worker → GUI: show OpenMMSPromptDialog and return ``proceed``/``cancel``."""
        from .entry_preflight_dialogs import OpenMMSPromptDialog
        result_holder: dict[str, str] = {"choice": "cancel"}

        def show_modal() -> None:
            dlg = OpenMMSPromptDialog(
                self,
                account_name=account_name,
                lookup_code=lookup_code,
                hint_message=hint_message,
            )
            dlg.exec()
            result_holder["choice"] = dlg.choice()

        if QThread.currentThread() is self.thread():
            show_modal()
        else:
            import threading as _threading
            from PySide6.QtCore import QTimer
            _done = _threading.Event()
            def _runner() -> None:
                try:
                    show_modal()
                finally:
                    _done.set()
            QTimer.singleShot(0, self, _runner)
            _done.wait()
        return result_holder["choice"]

    def _on_account_inline_dup_prompt(
        self,
        target_name: str,
        panel_text: str,
    ) -> str:
        """Worker → GUI: surface EPIC's inline 'Possible Duplicates' panel.

        Shows a plain message box asking the operator to dismiss the EPIC
        panel themselves (in the browser) and then click OK to proceed.
        Returns ``"proceed"`` on OK or ``"cancel"`` on Cancel/close.

        *panel_text* is logged for the audit trail but not shown in the
        popup — the operator is looking at the panel in EPIC directly.
        """
        _logger.info(
            "Inline dup panel surfaced for %r — panel text:\n%s",
            target_name, panel_text,
        )

        from PySide6.QtWidgets import QMessageBox

        result_holder: dict[str, str] = {"choice": "cancel"}

        def show_modal() -> None:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("Possible duplicate")
            box.setText(
                "EPIC is reporting a possible duplicate account.\n\n"
                "To correct, dismiss the possible Duplicate warning in EPIC, "
                "then press OK."
            )
            ok_btn = box.addButton(QMessageBox.StandardButton.Ok)
            cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(ok_btn)
            box.exec()
            result_holder["choice"] = (
                "proceed" if box.clickedButton() is ok_btn else "cancel"
            )

        if QThread.currentThread() is self.thread():
            show_modal()
        else:
            import threading as _threading
            from PySide6.QtCore import QTimer
            _done = _threading.Event()
            def _runner() -> None:
                try:
                    show_modal()
                finally:
                    _done.set()
            QTimer.singleShot(0, self, _runner)
            _done.wait()
        return result_holder["choice"]

    def _on_account_dup_prompt(
        self,
        *,
        target_name: str,
        outcome_label: str,
        matches: list,
    ) -> str:
        """Worker → GUI: surface a duplicate-name finding and return ``proceed``/``cancel``.

        *outcome_label* is the human label of the dup-check outcome ("Exact
        match" or "Possible matches"). *matches* is a list of
        :class:`step_account_dup_check.AccountResult` records to display.
        Operator picks Continue (create anyway) or Cancel (abort the run).
        """
        from PySide6.QtWidgets import QMessageBox
        result_holder: dict[str, str] = {"choice": "cancel"}

        rows_text = "\n".join(
            f"  • {m.lookup_code} — {m.account_name}"
            + (f" ({m.client_type})" if m.client_type else "")
            + (f" — {m.status}" if m.status else "")
            for m in matches[:20]
        ) or "  (none)"

        if outcome_label.lower().startswith("exact"):
            body = (
                f"An account named {target_name!r} already exists in EPIC:\n\n"
                f"{rows_text}\n\n"
                "Continue creating a new account anyway, or Cancel and use the existing one?"
            )
            icon = QMessageBox.Icon.Warning
        else:
            body = (
                f"{len(matches)} possible match(es) for {target_name!r}:\n\n"
                f"{rows_text}\n\n"
                "Continue creating a new account, or Cancel to review the matches?"
            )
            icon = QMessageBox.Icon.Question

        def show_modal() -> None:
            box = QMessageBox(self)
            box.setIcon(icon)
            box.setWindowTitle("Possible duplicate account")
            box.setText(body)
            proceed_btn = box.addButton("Continue (create anyway)", QMessageBox.ButtonRole.AcceptRole)
            cancel_btn  = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
            box.setDefaultButton(cancel_btn)
            box.exec()
            result_holder["choice"] = (
                "proceed" if box.clickedButton() is proceed_btn else "cancel"
            )

        if QThread.currentThread() is self.thread():
            show_modal()
        else:
            import threading as _threading
            from PySide6.QtCore import QTimer
            _done = _threading.Event()
            def _runner() -> None:
                try:
                    show_modal()
                finally:
                    _done.set()
            QTimer.singleShot(0, self, _runner)
            _done.wait()
        return result_holder["choice"]

    def _run_account_setup(
        self,
        *,
        page: Any,
        entry_state: Any,
        client_path: Any,
        worker: Any,
    ) -> str:
        """Drive Account Locate → name-search → dup-check → create.

        Returns the EPIC-assigned lookup code on success (also written back
        into ``entry_state.insured.lookup_code`` and persisted). Raises
        ``EntryCancelled`` if the operator backs out at the dup prompt, or
        ``RuntimeError`` for any other failure (e.g. missing required
        AccountSetup fields, EPIC validation errors).
        """
        from ..epic_steps import (
            step_account_create,
            step_account_dup_check,
            step_account_lookup_nav,
            step_account_search,
        )
        from ..epic_steps.runtime import EntryCancelled as _EC

        insured = getattr(entry_state, "insured", None)
        if insured is None:
            raise RuntimeError(
                "No Insured details on the Client page — fill them in before "
                "running Setup Account."
            )

        named = (getattr(insured, "named_insured", "") or "").strip()
        if not named:
            raise RuntimeError(
                "Named Insured is required on the Client page before "
                "running Setup Account."
            )

        agency = (getattr(insured, "agency", "") or "").strip()
        branch = (getattr(insured, "branch", "") or "").strip()
        if not agency or not branch:
            raise RuntimeError(
                "Agency and Branch are required on the Client page before "
                "running Setup Account."
            )

        # Primary contact (row 0 of contacts table) — feeds the EPIC
        # Primary Contact section. Missing contact rows are tolerated.
        contacts = getattr(entry_state, "contacts", None) or []
        primary = contacts[0] if contacts else None

        setup = step_account_create.AccountSetup(
            account_name=named,
            agency=agency,
            branch=branch,
            client_format=(getattr(insured, "client_format", "BUSINESS") or "BUSINESS"),
            # client_type stays at PROSPECT (AccountSetup default) per operator policy.
            fein=(getattr(insured, "fein", "") or None),
            business_type=(getattr(insured, "business_type", "") or None),
            street_address=(getattr(insured, "street_address", "") or None),
            business_phone=(getattr(insured, "business_phone", "") or None),
            business_email=None,                         # not on the Client GUI yet
            business_website=(getattr(insured, "website", "") or None),
            naics=(getattr(insured, "naics", "") or None),
            sic=(getattr(insured, "sic", "") or None),
            primary_first_name=(getattr(primary, "first_name", "") or None) if primary else None,
            primary_last_name=(getattr(primary, "last_name", "") or None) if primary else None,
            primary_phone=(getattr(primary, "phone", "") or None) if primary else None,
            primary_email=(getattr(primary, "email", "") or None) if primary else None,
            lines_of_business=["COMMERCIAL"],            # hardcoded per operator policy
        )

        # 1. Navigate to Account Locate.
        worker.emit_progress("Opening Account Locate...")
        if not step_account_lookup_nav.run(page):
            raise RuntimeError("Could not open the EPIC Account Locate screen.")

        # 2. Search by Account/Business Name to surface near-matches.
        worker.emit_progress(f"Searching EPIC for {named!r}...")
        if not step_account_search.run(
            page,
            search_by="Account/Business Name",
            search_term=named,
        ):
            raise RuntimeError(
                f"Could not search EPIC for {named!r} during dup-check."
            )

        # 3. Classify the results.
        worker.emit_progress("Checking for duplicate accounts...")
        dup = step_account_dup_check.run(page, target_name=named)
        _Outcome = step_account_dup_check.DupCheckOutcome

        if dup.outcome is _Outcome.ERROR:
            raise RuntimeError(
                dup.error or "Duplicate check failed to read the results grid."
            )

        if dup.outcome is _Outcome.EXACT:
            choice = self._on_account_dup_prompt(
                target_name=named,
                outcome_label="Exact match",
                matches=dup.matches,
            )
            if choice == "cancel":
                raise _EC("Operator cancelled at duplicate-account prompt.")
        elif dup.outcome is _Outcome.FUZZY_MATCHES:
            choice = self._on_account_dup_prompt(
                target_name=named,
                outcome_label="Possible matches",
                matches=dup.matches,
            )
            if choice == "cancel":
                raise _EC("Operator cancelled at near-match prompt.")
        # NO_MATCH falls through cleanly.

        # 4. Create the account.
        worker.emit_progress(f"Creating account {named!r} in EPIC...")
        result = step_account_create.run(page, setup=setup)
        _CreateOutcome = step_account_create.AccountCreateOutcome

        if result.outcome is _CreateOutcome.DUP_CANCELLED:
            raise _EC("Operator cancelled at EPIC's built-in dup popup.")
        if result.outcome is _CreateOutcome.VALIDATION_FAILED:
            raise RuntimeError(
                f"EPIC rejected the new account: {result.error or 'unknown validation error'}"
            )
        if result.outcome is _CreateOutcome.ERROR:
            raise RuntimeError(
                f"Account create failed: {result.error or 'unknown error'}"
            )

        # CREATED — write the EPIC-assigned code back to state.
        new_code = (result.lookup_code or "").strip()
        if new_code:
            insured.lookup_code = new_code
            from .. import state as state_module
            try:
                state_module.save_atomic(entry_state, client_path)
            except Exception as exc:  # noqa: BLE001
                worker.emit_progress(
                    f"Account created (code {new_code!r}), but state save failed: {exc}"
                )
            else:
                worker.emit_progress(
                    f"Account created — lookup code {new_code!r} stored."
                )
                # Mirror the write into the in-memory client dict (the GUI
                # reads from self._client.state, which is a SEPARATE copy
                # from entry_state). Without this the Lookup Code field on
                # the Client page would still show empty until the operator
                # switched clients and back.
                if self._client is not None and isinstance(self._client.state, dict):
                    ins = self._client.state.setdefault("insured", {})
                    if isinstance(ins, dict):
                        ins["lookup_code"] = new_code
                    # Schedule the Client page refresh on the GUI thread —
                    # worker can't call set_client() directly.
                    from PySide6.QtCore import QTimer
                    def _refresh_client_ui() -> None:
                        try:
                            self._sync_client_page()
                        except Exception:  # noqa: BLE001
                            pass
                    QTimer.singleShot(0, self, _refresh_client_ui)
            return new_code

        # Account was created but we couldn't read the lookup code off the
        # post-save screen (transient title, or EPIC hasn't fully loaded
        # the new account yet). Raise a clear message — the account does
        # exist in EPIC; the operator just needs to put its code on the
        # Client page and re-run.
        raise RuntimeError(
            f"Account {setup.account_name!r} was saved in EPIC, but its "
            "new lookup code could not be read off the page within the "
            "wait window. Look at the account in EPIC, copy the lookup "
            "code into the Client page's 'Lookup Code' field, uncheck "
            "'Setup Account' on the Begin Entry dialog, and re-run."
        )

    def _on_entry_status_update(self, message: str) -> None:
        """Called by step files via runtime.on_status to update the busy dialog."""
        if self._entry_busy_dialog is None:
            return
        # Marshal onto the GUI thread — runtime.on_status is invoked from worker.
        from PySide6.QtCore import QTimer
        def _setter() -> None:
            if self._entry_busy_dialog is not None:
                try:
                    self._entry_busy_dialog.set_status_text(message)  # type: ignore[union-attr]
                except Exception:
                    pass
        QTimer.singleShot(0, self, _setter)

    def _offer_send_run_report(self, outcome: str) -> None:
        """After an entry run ends, offer to email the artifacts folder.

        Only prompts when the run actually produced artifacts (findings,
        screenshots, or run log). Otherwise silently skips.
        """
        from .. import config as _cfg
        if not getattr(_cfg, "RUN_REPORT_ENABLED", True):
            # Feature disabled via config — still clear the artifacts ref so
            # a later run doesn't pick up this folder.
            self._entry_artifacts_dir = None
            return
        if self._entry_artifacts_dir is None:
            return
        from pathlib import Path as _Path
        artifacts_dir: _Path = self._entry_artifacts_dir  # type: ignore[assignment]
        # Clear our reference up front — even on early-return, we don't want to
        # send the same folder twice.
        self._entry_artifacts_dir = None

        runtime = self._entry_runtime
        findings = list(getattr(runtime, "findings", []) or [])
        # If nothing happened, skip — no need to bother the user.
        if not findings and not list(artifacts_dir.glob("*")):
            return

        from .. import config as config_module
        from .. import run_report as run_report_module

        # Confirm before sending — even though Outlook will pop a compose
        # window, asking first lets the user back out without any side effect.
        client_name = self._client.name if self._client else "Unknown Client"
        finding_lines = [
            f"#{f.sequence} [{getattr(f, 'screen_code', '') or '?'}] "
            f"{getattr(f, 'error_text', '')[:80]}"
            for f in findings
        ]
        prompt = (
            f"Run finished ({outcome}). {len(findings)} validation finding(s) recorded.\n\n"
            f"Send the run report (zipped screenshots + log) to "
            f"{config_module.RUN_REPORT_RECIPIENT}?"
        )
        box = QMessageBox(self)
        box.setWindowTitle("Send run report?")
        box.setText(prompt)
        send_btn = box.addButton("Send", QMessageBox.ButtonRole.AcceptRole)
        skip_btn = box.addButton("Skip", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(send_btn)
        box.exec()
        if box.clickedButton() is not send_btn:
            return

        ctx = run_report_module.ReportContext(
            run_id=getattr(runtime, "run_id", "unknown"),
            client_name=client_name,
            artifacts_dir=artifacts_dir,
            recipient=config_module.RUN_REPORT_RECIPIENT,
            finding_count=len(findings),
            finding_summary_lines=finding_lines,
            outcome=outcome,
        )
        ok = run_report_module.send_report(
            ctx, auto_send=config_module.OUTLOOK_AUTO_SEND,
        )
        if not ok:
            QMessageBox.warning(
                self,
                "Send failed",
                "Could not send the report via Outlook. "
                "The artifacts folder is preserved at:\n\n"
                f"{artifacts_dir}",
            )
            return
        # Compose-mode: leave the folder alone (user may still be editing the draft).
        # Silent-send mode: send_report already cleaned up. Either way, mark done.
        self._audit_log.append_event(
            f"Entry run report sent to {config_module.RUN_REPORT_RECIPIENT}."
        )

    # -- Run controls (Begin Entry / Cancel / Resume) -----------------------

    def _refresh_run_controls(self) -> None:
        if self._client is None:
            self._run_controls.set_status_text("No client loaded.")
            self._run_controls.set_approved_count(0)
            self._sync_run_actions(ready=0)
            return
        ready = self._count_enterable_fields(self._client.state)
        self._run_controls.set_status_text(
            f"{self._client.name} — {ready} field(s) ready."
        )
        self._run_controls.set_approved_count(ready)
        self._sync_run_actions(ready=ready)

    def _sync_run_actions(self, *, ready: int) -> None:
        """Mirror the bottom run-controls' enabled state onto menu/toolbar QActions.

        Keeps Ctrl+E / Ctrl+Enter / Esc in lockstep with the bottom buttons.
        """
        any_run_active = self._active_run_kind is not None
        queued = self._pending_pdfs_pane.count() if hasattr(self, "_pending_pdfs_pane") else 0
        can_extract = (queued > 0) and not any_run_active and self._client is not None
        can_begin = (ready > 0) and not any_run_active and self._client is not None
        can_cancel = any_run_active

        for key in ("header_extract", "menu_extract"):
            act = self._run_actions.get(key)
            if act is not None:
                act.setEnabled(can_extract)
        for key in ("header_begin", "menu_begin"):
            act = self._run_actions.get(key)
            if act is not None:
                act.setEnabled(can_begin)
        cancel_act = self._run_actions.get("menu_cancel")
        if cancel_act is not None:
            cancel_act.setEnabled(can_cancel)

    @staticmethod
    def _count_enterable_fields(state: dict) -> int:
        """Count fields that are ready to be entered into EPIC.

        Previously this required ``status in {"approved", "locked"}``,
        which gated Begin Entry behind a manual review pass the operator
        never asked for. Now any extracted field with a non-empty value
        that hasn't been entered yet counts — Begin Entry becomes clickable
        as soon as extraction lands data. The ``needs_review`` flag is
        preserved on individual records for the GUI to highlight; it no
        longer blocks the entry gate.
        """
        count = 0
        for record in (state.get("fields") or {}).values():
            if not isinstance(record, dict):
                continue
            if record.get("status") == "entered":
                continue
            if record.get("value") in (None, ""):
                continue
            count += 1
        for items in (state.get("repeatables") or {}).values():
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                for record in item.values():
                    if not isinstance(record, dict):
                        continue
                    if record.get("status") == "entered":
                        continue
                    if record.get("value") in (None, ""):
                        continue
                    count += 1
        return count

    # Backward-compat alias — kept so any leftover external caller keeps
    # working. The behaviour is now "enterable" (any with a value), not
    # "approved" (status-stamped).
    _count_approved_fields = _count_enterable_fields

    def _on_begin_entry(self) -> None:
        if self._client is None:
            return
        try:
            from .. import enter as enter_module
        except ImportError:
            QMessageBox.information(
                self,
                "Entry not available yet",
                "The entry module isn't wired up yet.",
            )
            return
        run_entry_session = getattr(enter_module, "run_entry_session", None)
        if not callable(run_entry_session):
            QMessageBox.information(
                self,
                "Entry not available yet",
                "The entry module isn't wired up yet.",
            )
            return

        # Note: we no longer gate dialog-open on ``self._browser_context``.
        # The operator can open the Begin Entry dialog and pick options
        # while the browser is still finishing its launch sequence (login,
        # database picker, etc., which takes ~10 s). When they click
        # Continue, the worker task does an inline wait for the browser
        # to become ready before proceeding — see the wait-loop near the
        # top of ``task()`` below.
        #
        # Auto-launch the browser now if neither a context nor an in-flight
        # launch worker exists. Otherwise the operator would have to dismiss
        # the modal Begin Entry dialog, click Launch Browser, and reopen
        # Begin Entry — the busy dialog later blocks any sidebar clicks,
        # so they couldn't recover without cancelling the entry run.
        if self._browser_context is None and (
            self._browser_launch_worker is None
            or not self._browser_launch_worker.isRunning()
        ):
            _logger.info(
                "Begin Entry: no browser running — auto-triggering Launch Browser"
            )
            self._on_launch_browser_clicked()

        from .. import field_map as field_map_module
        from .. import state as state_module

        try:
            entry_state = state_module.load(self._client.path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self,
                "Couldn't load state for entry",
                f"State.json couldn't be opened.\n\n{exc}",
            )
            return

        try:
            entry_field_map = field_map_module.load()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self,
                "Couldn't load field map for entry",
                f"The Field Map couldn't be loaded.\n\n{exc}",
            )
            return

        # Begin Entry dialog: captures operator-typed submission-setup
        # values (Agency / Branch / Profit Center / Eff Date / Exp Date)
        # always, plus per-coverage scope checkboxes in --debug mode.
        # Pre-populates from the persisted state.submission_setup so
        # subsequent runs just re-confirm.
        dlg = _BeginEntryDialog(entry_state, debug=self._debug, parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return  # operator cancelled
        submission_setup = dlg.submission_setup()
        include_namespaces = dlg.selected_namespaces()
        if include_namespaces is not None and not include_namespaces:
            QMessageBox.information(
                self,
                "No lines selected",
                "Pick at least one line to enter.",
            )
            return

        # Prerequisite check: warn when a higher-level step is selected
        # without the lower-level steps that must precede it.
        # Skipped in debug mode for faster iteration.
        if not self._debug and include_namespaces:
            selected_levels = {_STEP_LEVEL.get(ns, 0) for ns in include_namespaces}
            selected_levels.discard(0)
            if selected_levels:
                max_level = max(selected_levels)
                missing = [
                    lvl for lvl in range(1, max_level)
                    if lvl not in selected_levels and lvl in _PREREQ_MESSAGES
                ]
                if missing:
                    prereq_text = "\n".join(
                        f"• {_PREREQ_MESSAGES[lvl].capitalize()}"
                        for lvl in missing
                    )
                    warn_box = QMessageBox(self)
                    warn_box.setWindowTitle("Confirm prerequisites")
                    warn_box.setIcon(QMessageBox.Icon.Warning)
                    warn_box.setText(
                        "Before proceeding, please ensure:\n\n"
                        + prereq_text
                        + "\n\nThe browser should already be navigated to the "
                        "correct location in EPIC."
                    )
                    proceed_btn = warn_box.addButton(
                        "Proceed", QMessageBox.ButtonRole.AcceptRole
                    )
                    cancel_btn = warn_box.addButton(
                        "Cancel", QMessageBox.ButtonRole.RejectRole
                    )
                    warn_box.setDefaultButton(cancel_btn)
                    warn_box.exec()
                    if warn_box.clickedButton() is not proceed_btn:
                        return

        # Property Additional Interests: EPIC rejects rows whose loc# is set
        # without a bldg# ("A location number must be accompanied by a building
        # number"). Warn the operator at Begin Entry time so they can fix the
        # data or proceed knowing the automation will default bldg# to "1".
        # Only fires when Property is part of the entry scope.
        _prop_in_scope = include_namespaces is None or any(
            ns == "policy.property" or ns.startswith("policy.property.")
            for ns in (include_namespaces or set())
        )
        if _prop_in_scope:
            _ai_group = "policy.property.additional_interest"
            _loc_tag = f"{_ai_group}.location_number"
            _bldg_tag = f"{_ai_group}.building_number"
            _name_tag = f"{_ai_group}.name"
            _orphan_locs: list[str] = []
            for _item in entry_state.repeatables.get(_ai_group, []) or []:
                _loc_rec = _item.get(_loc_tag)
                _bldg_rec = _item.get(_bldg_tag)
                _name_rec = _item.get(_name_tag)
                _loc_v = (_loc_rec.value if _loc_rec else "") or ""
                _bldg_v = (_bldg_rec.value if _bldg_rec else "") or ""
                _name_v = (_name_rec.value if _name_rec else "") or ""
                if str(_loc_v).strip() and not str(_bldg_v).strip():
                    _orphan_locs.append(
                        f"{(_name_v or '(unnamed)')[:40]} — Loc #{_loc_v}"
                    )
            if _orphan_locs:
                _detail = "\n".join(f"• {s}" for s in _orphan_locs[:8])
                if len(_orphan_locs) > 8:
                    _detail += f"\n• …and {len(_orphan_locs) - 8} more"
                ai_warn = QMessageBox(self)
                ai_warn.setWindowTitle("Property Additional Interests")
                ai_warn.setIcon(QMessageBox.Icon.Warning)
                ai_warn.setText(
                    f"{len(_orphan_locs)} Property Additional Interest row(s) "
                    "have a Location # but no Building #.\n\n"
                    "EPIC requires both when a Location # is set "
                    "(\"A location number must be accompanied by a building number\").\n\n"
                    f"If you proceed, the automation will default Building # to "
                    "\"1\" for these rows:\n\n"
                    f"{_detail}"
                )
                ai_proceed = ai_warn.addButton("Proceed", QMessageBox.ButtonRole.AcceptRole)
                ai_cancel = ai_warn.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
                ai_warn.setDefaultButton(ai_cancel)
                ai_warn.exec()
                if ai_warn.clickedButton() is not ai_proceed:
                    return

        # Persist the just-confirmed submission setup so the next Begin
        # Entry click pre-populates from these values.
        entry_state.submission_setup = submission_setup
        try:
            state_module.save_atomic(entry_state, self._client.path)
        except Exception as exc:  # noqa: BLE001
            _logger.warning(
                "could not persist submission_setup before entry: %s", exc
            )
        # Mirror the change into the GUI's in-memory dict view too, so
        # the status bar / panes don't read a stale snapshot.
        self._client.state = _safe_state_load(self._client.path)

        browser_context = self._browser_context
        client_path = self._client.path
        settings = self._settings
        _local_cdp_port = self._cdp_port

        # ── Set up the per-run runtime context shared with step files ────────
        # validation_check / cancel checks live in epic_steps.runtime; we
        # publish a fresh runtime here so step files can consume it.
        import threading as _threading
        import uuid as _uuid
        from datetime import datetime as _dt2
        from ..epic_steps import runtime as _runtime_mod

        _entry_run_id = _uuid.uuid4().hex[:12]
        _run_ts = _dt2.now().strftime("%Y%m%d_%H%M%S")
        _artifacts_dir = client_path / "run_artifacts" / f"{_run_ts}_{_entry_run_id}"
        try:
            _artifacts_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        _cancel_event = _threading.Event()
        self._entry_cancel_event = _cancel_event
        self._entry_artifacts_dir = _artifacts_dir

        # Per-run timestamped log file. The shared iga.log in
        # user_data_dir/logs/ still gets everything, but a fresh file
        # under the run's artifacts dir makes it easy to grab the
        # exact log for ONE run (no grep-by-timestamp needed).
        # Detach any handler from a previous Begin Entry so we don't
        # duplicate-write across runs.
        _iga_root_logger = logging.getLogger("iga")
        _old_run_handler = getattr(self, "_entry_log_handler", None)
        if _old_run_handler is not None:
            try:
                _iga_root_logger.removeHandler(_old_run_handler)
                _old_run_handler.close()
            except Exception:  # noqa: BLE001
                pass
            self._entry_log_handler = None
        _run_log_path = _artifacts_dir / "run.log"
        try:
            _run_handler = logging.FileHandler(_run_log_path, encoding="utf-8")
            _run_handler.setLevel(logging.DEBUG)
            _run_handler.setFormatter(logging.Formatter(
                "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s %(message)s",
                datefmt="%H:%M:%S",
            ))
            _iga_root_logger.addHandler(_run_handler)
            self._entry_log_handler = _run_handler
            _banner = (
                "=" * 72 + "\n"
                + f"  Begin Entry run started\n"
                + f"  Run ID    : {_entry_run_id}\n"
                + f"  Log file  : {_run_log_path}\n"
                + f"  Artifacts : {_artifacts_dir}\n"
                + "=" * 72
            )
            for _ln in _banner.split("\n"):
                _logger.info(_ln)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("could not attach per-run log handler: %s", exc)

        _runtime = _runtime_mod.EntryRuntime(
            run_id=_entry_run_id,
            artifacts_dir=_artifacts_dir,
            cancel_event=_cancel_event,
            on_validation_halt=self._on_validation_halt,
            on_status=self._on_entry_status_update,
            on_inline_dup_prompt=self._on_account_inline_dup_prompt,
        )
        self._entry_runtime = _runtime
        _runtime_mod.set_runtime(_runtime)

        def task(worker: _CallableWorker) -> object:
            worker.emit_progress("Entry session starting...")

            # Wait for the browser to finish its launch sequence (login,
            # database picker, session-conflict dismissal) if it's still in
            # flight. The Begin Entry dialog was allowed to open before the
            # browser was ready so the operator can pre-fill the form; this
            # is where Continue actually blocks until it's safe to proceed.
            #
            # Timeout: 90s — covers slow logins + the post-login settle.
            # Cancel: handled via ``worker.cancel_requested`` so the
            # operator can back out from the busy dialog.
            import time as _wait_time
            _wait_deadline = _wait_time.time() + 90.0
            _wait_announced = False
            while self._browser_context is None:
                if getattr(worker, "cancel_requested", False):
                    from ..epic_steps.runtime import (
                        EntryCancelled as _WaitCancelled,
                    )
                    raise _WaitCancelled(
                        "operator cancelled while waiting for browser"
                    )
                if _wait_time.time() >= _wait_deadline:
                    raise RuntimeError(
                        "Browser launch did not complete within 90 seconds. "
                        "Click 'Launch Browser' in the sidebar to start it "
                        "(or check the sidebar status for launch errors), "
                        "then try Begin Entry again."
                    )
                if not _wait_announced:
                    worker.emit_progress("Waiting for browser to finish launching...")
                    _wait_announced = True
                _wait_time.sleep(0.25)
            if _wait_announced:
                worker.emit_progress("Browser ready — continuing entry.")

            # In debug mode, reload all epic_steps modules so script edits
            # are picked up without restarting the app.
            #
            # Skip ``runtime``: it holds the live ``_runtime`` global that we
            # just published via ``set_runtime`` two blocks up. Reloading
            # would reset it to None and every step file's
            # ``get_runtime()`` would return None — breaking the
            # validation-halt and cancel-event plumbing for the rest of the
            # run. (Hit this in the inline-dup-panel handler 2026-05-22.)
            if self._debug:
                import importlib as _importlib
                import sys as _sys
                _prefix = "iga_marketing_master_2.epic_steps"
                _skip = {f"{_prefix}.runtime"}
                for _mod_name in list(_sys.modules):
                    if _mod_name.startswith(_prefix) and _mod_name not in _skip:
                        try:
                            _importlib.reload(_sys.modules[_mod_name])
                        except Exception:
                            pass

            # Belt-and-suspenders: re-publish the runtime here, after any
            # reload pass. ``set_runtime`` was already called on the GUI
            # thread before spawning this worker, but if anything *did*
            # reset the module-level ``_runtime`` global (older builds
            # reloaded ``runtime`` itself; future code may import it for
            # other reasons), this guarantees step files see the live
            # runtime when they call ``get_runtime()``.
            try:
                _runtime_mod.set_runtime(self._entry_runtime)
            except Exception:
                pass

            # ── Pre-step: Create Master Marketing Submission ─────────────────
            # If the operator has the MKADMSTR form open, fill and submit it
            # before the field-level entry walk begins.
            from datetime import datetime as _dt
            from ..epic_steps.step_mms_create import (
                LineSpec as _LineSpec,
                MmsSetup as _MmsSetup,
                run as _mms_run,
            )

            # Keys must match the state.json namespace prefixes that Claude
            # extracts into.  Values are the EPIC Add New Line combo codes.
            _NS_TO_LOB: dict[str, str] = {
                "policy.gl":            "GLIA",  # General Liability
                "policy.auto":          "BAUT",  # Business Auto
                "policy.property":      "PROP",  # Commercial Property
                "policy.workers_comp":  "WCOM",  # Workers' Compensation
                "policy.umbrella":      "CUMB",  # Commercial Umbrella
                "policy.inland_marine": "IM",    # Inland Marine
                "policy.bop":           "BOP",   # Business Owners Policy
                "policy.prof":          "PROF",  # Professional Liability
                "policy.epli":          "EPLI",  # Employment Practices
                "policy.cyb":           "CYB",   # Cyber
                "policy.truc":          "TRUC",  # Truckers
            }
            _ns = include_namespaces or []
            _lines = [
                _LineSpec(line_code=lob)
                for ns_prefix, lob in _NS_TO_LOB.items()
                if any(
                    n == ns_prefix or n.startswith(ns_prefix + ".")
                    for n in _ns
                )
            ]
            _client_name = self._client.name if self._client else "Unknown Client"
            _mms_name = f"{_dt.now().year} New Business MMS - {_client_name}"
            _mms_setup = _MmsSetup(
                name=_mms_name,
                effective=submission_setup.effective_date or "",
                expiration=submission_setup.expiration_date or "",
                agency=submission_setup.agency or "IGA",
                branch=submission_setup.branch or "002",
                department="CL",
                lines=_lines,
            )
            # Helpers for reading field values out of state by domain tag.
            def _sv(tag: str) -> str:
                fields = getattr(entry_state, "fields", None) or {}
                rec = fields.get(tag)
                if rec is None:
                    return ""
                v = rec.get("value") if isinstance(rec, dict) else getattr(rec, "value", None)
                return str(v) if v is not None else ""

            def _rv(row, tag: str) -> str:  # noqa: ANN001
                rec = row.get(tag) if isinstance(row, dict) else getattr(row, tag, None)
                if rec is None:
                    return ""
                v = rec.get("value") if isinstance(rec, dict) else getattr(rec, "value", None)
                return str(v) if v is not None else ""

            _rep = getattr(entry_state, "repeatables", None) or {}

            # Playwright's sync API is greenlet-bound to the thread where
            # sync_playwright().start() was called.  The browser was launched
            # on _BrowserLaunchWorker's thread (which has since exited), so we
            # create a fresh Playwright handle on this worker thread via CDP.
            from playwright.sync_api import sync_playwright as _sync_pw
            _pw2 = _sync_pw().start()
            try:
                _cdp_browser = _pw2.chromium.connect_over_cdp(
                    f"http://localhost:{_local_cdp_port}"
                )
                _cdp_ctx = _cdp_browser.contexts[0] if _cdp_browser.contexts else None
                _pages = _cdp_ctx.pages if _cdp_ctx else []

                # ── Pre-flight: navigate to a known-good starting point ──────────
                # Detect where EPIC is now and, if needed, get to Marketed
                # Policies for the right client. After this block the page is
                # either on Marketed Policies (caller prompts to open an MMS)
                # or on an MMS detail view (caller confirms before entering).
                # MMS-create flow then runs unchanged if "submission" was checked.
                from ..epic_steps import step_navigate_to_entry_start as _preflight
                from ..epic_steps.runtime import EntryCancelled as _EntryCancelled

                _lookup_code = ""
                _insured = getattr(entry_state, "insured", None)
                if _insured is not None:
                    _lookup_code = (getattr(_insured, "lookup_code", "") or "").strip()
                _expected_name = self._client.name if self._client else ""

                if not _pages:
                    raise RuntimeError(
                        "No active EPIC tab — launch the browser before Begin Entry."
                    )
                _page0 = _pages[0]

                # ── Step 0: Setup Account ────────────────────────────────────────
                # Runs *before* preflight because preflight assumes the lookup
                # code already exists. Account create writes the new code into
                # state["insured"]["lookup_code"]; preflight then uses it.
                #
                # Behaviour:
                #   - Skipped if lookup_code is already non-empty (existing
                #     account — operator chose Setup Account by mistake or is
                #     re-running the flow).
                #   - Pre-flights navigation to Account Locate, name-search,
                #     and dup-check. If a duplicate is found, asks the operator
                #     to Continue (create anyway) or Cancel.
                #   - On CREATED, writes back the EPIC-assigned lookup code so
                #     downstream preflight + entry steps run against the new
                #     account.
                _account_just_created = False
                if "account" in _ns:
                    if _lookup_code:
                        worker.emit_progress(
                            f"Lookup code {_lookup_code!r} already set — "
                            "skipping Setup Account."
                        )
                    else:
                        _lookup_code = self._run_account_setup(
                            page=_page0,
                            entry_state=entry_state,
                            client_path=client_path,
                            worker=worker,
                        )
                        # Refresh the bound _insured ref now that we wrote
                        # the new code into state.
                        _insured = getattr(entry_state, "insured", None)
                        _account_just_created = True

                    # If Setup Account was the only thing checked, we're done.
                    # EPIC is sitting on the new account's screen; no further
                    # navigation, MMS create, or entry walk is needed.
                    _remaining = [n for n in _ns if n != "account"]
                    if not _remaining:
                        from ..enter import EntryResult as _EntryResult
                        worker.emit_progress(
                            f"Setup Account finished — lookup code "
                            f"{_lookup_code!r}. No other steps were "
                            "checked; entry run complete."
                        )
                        return _EntryResult(
                            run_id="account-setup-only",
                            fields_entered=0,
                            fields_skipped=0,
                            fields_paused=0,
                            outcome="completed",
                        )

                worker.emit_progress("Locating account in EPIC...")
                _nav_outcome = _preflight.run(
                    _page0,
                    lookup_code=_lookup_code,
                    expected_account_name=_expected_name,
                )
                _NavStatus = _preflight.NavStatus

                if _nav_outcome.status == _NavStatus.LOGIN_REQUIRED:
                    raise RuntimeError(
                        _nav_outcome.error or "EPIC is not past login. Sign in and try again."
                    )
                if _nav_outcome.status == _NavStatus.FAILED:
                    raise RuntimeError(
                        _nav_outcome.error or "Could not navigate to Marketed Policies."
                    )

                # ── Additional Contacts (account-level) ──────────────────────────
                # Runs after preflight (the correct account is loaded, so the
                # account sidebar with "Contacts" is available) and BEFORE any
                # MMS handling — it operates on the account Contacts screen and
                # needs no MMS. Best-effort: a failure here doesn't abort the run.
                if "additional_contacts" in _ns:
                    from ..epic_steps import step_additional_contacts as _addl
                    worker.emit_progress("Adding additional contacts / named insureds...")
                    _ac_setup = _build_additional_contacts_setup(
                        entry_state,
                        self._client.state if self._client else None,
                    )
                    try:
                        _ac_res = _addl.run(_page0, _ac_setup)
                        worker.emit_progress(
                            f"Additional Contacts: {_ac_res.added} added, "
                            f"{_ac_res.skipped} already present, {_ac_res.failed} failed."
                        )
                    except _EntryCancelled:
                        raise
                    except Exception as _ac_exc:  # noqa: BLE001
                        worker.emit_progress(
                            f"Additional Contacts step error (continuing): {_ac_exc}"
                        )

                # Short-circuit when nothing MMS-dependent remains (account
                # and/or additional_contacts only) — those are account-level and
                # don't need an MMS open. Mirrors the Setup-Account-only return.
                _mms_dependent = [
                    n for n in _ns if n not in ("account", "additional_contacts")
                ]
                if not _mms_dependent:
                    from ..enter import EntryResult as _EntryResult
                    worker.emit_progress(
                        "Account-level steps complete; no MMS-dependent steps "
                        "were selected — entry run complete."
                    )
                    return _EntryResult(
                        run_id="account-level-only",
                        fields_entered=0,
                        fields_skipped=0,
                        fields_paused=0,
                        outcome="completed",
                    )

                _mms_setup_requested = "submission" in _ns
                if _mms_setup_requested:
                    # Operator wants to create a new MMS — proceed regardless of
                    # whether one is currently open. The existing MMS create
                    # step will open the Add MMS dialog from the Marketed
                    # Policies frame.
                    if _nav_outcome.status == _NavStatus.MMS_OPEN:
                        worker.emit_progress(
                            "An MMS is open, but Setup Marketing Submission "
                            "was checked — proceeding to create a new one."
                        )
                else:
                    # No MMS create — we need an MMS to be open before entry.
                    if _nav_outcome.status == _NavStatus.ON_MARKETED_POLICIES:
                        # Loop: prompt operator to open MMS, then re-probe.
                        _hint = ""
                        while True:
                            _choice = self._on_preflight_prompt_open_mms(
                                account_name=_nav_outcome.loaded_account_name,
                                lookup_code=_nav_outcome.loaded_lookup_code,
                                hint_message=_hint,
                            )
                            if _choice == "cancel":
                                raise _EntryCancelled(
                                    "operator cancelled MMS-open prompt"
                                )
                            _re_probe = _preflight.probe_state(_page0)
                            if _re_probe.status == _NavStatus.MMS_OPEN:
                                _nav_outcome = _re_probe
                                break
                            _hint = (
                                "Still on Marketed Policies — please "
                                "double-click the MMS first, then click Continue."
                            )
                    # By now status must be MMS_OPEN — confirm.
                    if _nav_outcome.status == _NavStatus.MMS_OPEN:
                        _choice = self._on_preflight_confirm_mms_open(
                            account_name=_nav_outcome.loaded_account_name,
                            lookup_code=_nav_outcome.loaded_lookup_code,
                            screen_code=_nav_outcome.screen_code,
                        )
                        if _choice == "cancel":
                            raise _EntryCancelled(
                                "operator cancelled MMS-open confirmation"
                            )

                # ── Step 1: Create Marketing Submission ──────────────────────────
                if "submission" in _ns and _pages:
                    worker.emit_progress("Creating Master Marketing Submission...")
                    _mms_run(_pages[0], _mms_setup)

                # ── Step 2: Commercial AP ────────────────────────────────────────
                if "policy.commercial_ap" in _ns and _pages:
                    from ..epic_steps.step_commercial_ap import (
                        CommercialApSetup as _CapSetup,
                        NamedInsuredSpec as _CapNI,
                        PremiseSpec as _CapPremise,
                        run as _cap_run,
                    )
                    _cap_ni = [
                        _CapNI(
                            name      =_rv(row, "policy.commercial_ap.named_insured.name"),
                            name_type =_rv(row, "policy.commercial_ap.named_insured.name_type"),
                        )
                        for row in (_rep.get("policy.commercial_ap.named_insured") or [])
                        if _rv(row, "policy.commercial_ap.named_insured.name")
                    ]
                    # Premises come from the shared `location` repeatable.
                    # Pass ALL rows (including Loc 1, all buildings) — _fill_premises
                    # groups by location_number and handles edit-Loc1 / add-Loc2+ /
                    # vlvwBuilding_add internally, matching v1 behaviour.
                    def _parse_loc_address(desc: str):
                        """Parse 'Street, City, ST Zip' into (street, city, state, zip) best-effort."""
                        parts = [p.strip() for p in desc.split(",")]
                        if len(parts) < 2:
                            return desc, "", "", ""
                        street = parts[0]
                        state_zip_str = parts[-1].strip()
                        sv = state_zip_str.split()
                        state = sv[0] if sv else ""
                        zip_code = sv[1] if len(sv) > 1 else ""
                        city = parts[-2].strip() if len(parts) >= 3 else ""
                        return street, city, state, zip_code

                    _cap_prems: list = []
                    for _loc_row in (_rep.get("location") or []):
                        _loc_num_str = _rv(_loc_row, "location.location_number")
                        _bldg_num_str = _rv(_loc_row, "location.building_number")
                        try:
                            _loc_num = int(_loc_num_str or "0")
                            _bldg_num = int(_bldg_num_str or "1")
                        except ValueError:
                            continue
                        if _loc_num < 1:
                            continue
                        _bldg_desc = _rv(_loc_row, "location.building_description") or ""
                        _street, _city, _state, _zip = _parse_loc_address(_bldg_desc)
                        _cap_prems.append(_CapPremise(
                            location_number=_loc_num,
                            building_number=_bldg_num,
                            street  =_street,
                            city    =_city,
                            state   =_state,
                            zip_code=_zip,
                        ))

                    worker.emit_progress("Entering Commercial AP...")
                    _cap_run(_pages[0], _CapSetup(named_insureds=_cap_ni, premises=_cap_prems))

                # ── Step 3: General Liability ────────────────────────────────────
                if "policy.gl" in _ns and _pages:
                    from ..epic_steps.step_general_liability import (
                        AdditionalCoverageSpec as _GlAddlCov,
                        ContractorsSpec as _GlContractors,
                        EblSpec as _GlEbl,
                        GlCoveragesSpec as _GlCoverages,
                        GeneralLiabilitySetup as _GlSetup,
                        HazardSpec as _GlHazard,
                        run as _gl_run,
                    )
                    _gl_setup = _GlSetup(
                        coverages=_GlCoverages(
                            each_occ_limit     =_sv("policy.gl.each_occ_limit"),
                            gen_aggr_limit     =_sv("policy.gl.gen_aggr_app_limit"),
                            pers_adv_inj_limit =_sv("policy.gl.pers_adv_inj_limit"),
                            prod_oper_limit    =_sv("policy.gl.prod_oper_limit"),
                            med_limit          =_sv("policy.gl.med_limit") or "5000",
                            dam_prem_limit     =_sv("policy.gl.dam_prem_limit"),
                            emp_ben_limit      =_sv("policy.gl.ebl.each_claim_limit"),
                            occurrence_type    =_sv("policy.gl.pnl4"),
                        ),
                        hazards=[
                            _GlHazard(
                                class_code     =_rv(row, "policy.gl.hazard.class_code"),
                                exposure       =_rv(row, "policy.gl.hazard.exposure"),
                                premium_basis  =_rv(row, "policy.gl.hazard.premium_basis"),
                                classification =_rv(row, "policy.gl.hazard.classification"),
                                loc_num        =int(_rv(row, "policy.gl.hazard.location_number") or "1"),
                                bldg_num       =int(_rv(row, "policy.gl.hazard.building_number") or "1"),
                            )
                            for row in (_rep.get("policy.gl.hazard") or [])
                            if _rv(row, "policy.gl.hazard.class_code")
                        ],
                        ebl=_GlEbl(
                            retroactive_date     =_sv("policy.gl.ebl.retroactive_date"),
                            deductible_per_claim =_sv("policy.gl.ebl.deductible_each_claim"),
                        ),
                        contractors=_GlContractors(
                            num_full_time      =_sv("policy.gl.contractors.num_full_time"),
                            num_part_time      =_sv("policy.gl.contractors.num_part_time"),
                            percent_subcontract=_sv("policy.gl.contractors.percent_subcontract"),
                            dollars_subcontract=_sv("policy.gl.contractors.dollars_subcontract"),
                        ),
                        additional_coverages=[
                            _GlAddlCov(
                                description =_rv(row, "policy.gl.additional_coverage.name"),
                                code        =_rv(row, "policy.gl.additional_coverage.code"),
                                each_claim  =_rv(row, "policy.gl.additional_coverage.each_claim_limit"),
                                aggregate   =_rv(row, "policy.gl.additional_coverage.aggregate_limit"),
                                deductible  =_rv(row, "policy.gl.additional_coverage.deductible"),
                                retro_date  =_rv(row, "policy.gl.additional_coverage.retroactive_date"),
                                form_number =_rv(row, "policy.gl.additional_coverage.form_number"),
                            )
                            for row in (_rep.get("policy.gl.additional_coverage") or [])
                            if _rv(row, "policy.gl.additional_coverage.name")
                        ],
                    )
                    worker.emit_progress("Entering General Liability...")
                    _gl_run(_pages[0], _gl_setup)

                # ── Step 4: Property ─────────────────────────────────────────────
                if "policy.property" in _ns and _pages:
                    from ..epic_steps.step_property import (
                        AdditionalInterestSpec as _PropAI,
                        PropertyCoverageSpec   as _PropCov,
                        PropertyFormSpec       as _PropForm,
                        PropertySetup          as _PropSetup,
                        SubjectSpec            as _PropSubject,
                        run as _prop_run,
                    )
                    _prop_subjects = [
                        _PropSubject(
                            subject_type    =_rv(row, "policy.property.subject.subject"),
                            location_number =_rv(row, "policy.property.subject.location_number") or "1",
                            building_number =_rv(row, "policy.property.subject.building_number") or "1",
                            description     =_rv(row, "policy.property.subject.description"),
                            amount          =_rv(row, "policy.property.subject.amount"),
                            valuation       =_rv(row, "policy.property.subject.valuation1"),
                            form_number     =_rv(row, "policy.property.subject.form_number"),
                            cause_of_loss   =_sv("policy.property.applicable_causes_of_loss"),
                            coinsurance     =_rv(row, "policy.property.subject.coinsurance"),
                            deductible      =_rv(row, "policy.property.subject.deductible"),
                        )
                        for row in (_rep.get("policy.property.subject") or [])
                        if _rv(row, "policy.property.subject.subject")
                    ]
                    _prop_ais = [
                        _PropAI(
                            name            =_rv(row, "policy.property.additional_interest.name"),
                            interest_type   =_rv(row, "policy.property.additional_interest.interest_type"),
                            street          =_rv(row, "policy.property.additional_interest.street"),
                            city            =_rv(row, "policy.property.additional_interest.city"),
                            state           =_rv(row, "policy.property.additional_interest.state"),
                            zip_code        =_rv(row, "policy.property.additional_interest.zip_code"),
                            location_number =_rv(row, "policy.property.additional_interest.location_number"),
                            building_number =_rv(row, "policy.property.additional_interest.building_number"),
                            loan_number     =_rv(row, "policy.property.additional_interest.loan_number"),
                        )
                        for row in (_rep.get("policy.property.additional_interest") or [])
                        if _rv(row, "policy.property.additional_interest.name")
                    ]
                    # Skip scraped forms from state.json — only the IGA standard
                    # "Property Extension Endorsement" (defined in PROPERTY_STANDARD_FORMS)
                    # is added on every submission.
                    _prop_forms: list = []
                    _prop_covs = [
                        _PropCov(
                            description =_rv(row, "policy.property.additional_coverage.name"),
                            limit1      =_rv(row, "policy.property.additional_coverage.each_claim_limit"),
                            limit2      =_rv(row, "policy.property.additional_coverage.aggregate_limit"),
                            deductible  =_rv(row, "policy.property.additional_coverage.deductible"),
                            form_number =_rv(row, "policy.property.additional_coverage.form_number"),
                        )
                        for row in (_rep.get("policy.property.additional_coverage") or [])
                        if _rv(row, "policy.property.additional_coverage.name")
                    ]
                    worker.emit_progress("Entering Property...")
                    _prop_run(_pages[0], _PropSetup(
                        subjects             =_prop_subjects,
                        additional_interests =_prop_ais,
                        forms                =_prop_forms,
                        coverages            =_prop_covs,
                        add_all_premises     =True,
                    ))

                # ── Step 5: Business Auto ────────────────────────────────────────
                # BAUT lines are state-suffixed; we pick the first available
                # state line on the submission for this pass.  Operator can
                # re-run with additional states if needed.
                if "policy.auto" in _ns and _pages:
                    from ..epic_steps.step_business_auto import (
                        AutoAdditionalCoverageSpec as _BAutoAC,
                        AutoAdditionalInterestSpec as _BAutoAI,
                        AutoCoverageSpec           as _BAutoCov,
                        BusinessAutoSetup          as _BAutoSetup,
                        VehicleSpec                as _BAutoVeh,
                        list_available_baut_states,
                        run as _baut_run,
                    )
                    _baut_cov = _BAutoCov(
                        liability_symbols        =_sv("policy.auto.liability.symbols"),
                        liability_csl_limit1     =_sv("policy.auto.liability_csl_limit1"),
                        liability_bi_limit1      =_sv("policy.auto.liability_bi_limit1"),
                        liability_bi_limit2      =_sv("policy.auto.liability_bi_limit2"),
                        liability_pd_limit1      =_sv("policy.auto.liability_pd_limit1"),
                        medical_symbols          =_sv("policy.auto.medical.symbols"),
                        medical_limit1           =_sv("policy.auto.medical_limit1"),
                        uninsured_symbols        =_sv("policy.auto.uninsured.symbols"),
                        uninsured_csl_limit1     =_sv("policy.auto.uninsured_csl_limit1"),
                        uninsured_bi_limit1      =_sv("policy.auto.uninsured_bi_limit1"),
                        uninsured_bi_limit2      =_sv("policy.auto.uninsured_bi_limit2"),
                        uninsured_pd_each_accident=_sv("policy.auto.uninsured_pd_each_accident"),
                        uninsured_pd_deductible  =_sv("policy.auto.uninsured_pd_deductible"),
                        towing_symbols           =_sv("policy.auto.towing.symbols"),
                        towing_limit1            =_sv("policy.auto.towing_limit1"),
                        comprehensive_symbols    =_sv("policy.auto.comprehensive.symbols"),
                        comprehensive_deductible1=_sv("policy.auto.comprehensive_deductible1"),
                        cause_of_loss_symbols    =_sv("policy.auto.specified_causes_loss.symbols"),
                        cause_of_loss_deductible1=_sv("policy.auto.cause_of_loss_deductible1"),
                        collision_symbols        =_sv("policy.auto.collision.symbols"),
                        collision_deductible1    =_sv("policy.auto.collision_deductible1"),
                    )
                    _baut_veh_rows = [
                        row for row in (_rep.get("policy.auto.vehicle") or [])
                        if _rv(row, "policy.auto.vehicle.vin")
                        or _rv(row, "policy.auto.vehicle.year")
                    ]
                    _baut_vehicles = [
                        _BAutoVeh(
                            vehicle_num   =str(idx),
                            year          =_rv(row, "policy.auto.vehicle.year"),
                            make          =_rv(row, "policy.auto.vehicle.make"),
                            model         =_rv(row, "policy.auto.vehicle.model"),
                            vin           =_rv(row, "policy.auto.vehicle.vin"),
                            body_type     =_rv(row, "policy.auto.vehicle.body_type"),
                            garage_address=_rv(row, "policy.auto.vehicle.garage_address.line_1"),
                            cost_new      =_rv(row, "policy.auto.vehicle.cost_new"),
                            class_code    =_rv(row, "policy.auto.vehicle.class_code"),
                            valuation_type=_rv(row, "policy.auto.vehicle.valuation_type"),
                            comprehensive_deductible=_rv(row, "policy.auto.vehicle.comprehensive_deductible"),
                            collision_deductible    =_rv(row, "policy.auto.vehicle.collision_deductible"),
                        )
                        for idx, row in enumerate(_baut_veh_rows, start=1)
                    ]
                    _baut_ais = [
                        _BAutoAI(
                            name          =_rv(row, "policy.auto.additional_interest.name"),
                            interest_type =_rv(row, "policy.auto.additional_interest.interest"),
                            vehicle_number=_rv(row, "policy.auto.additional_interest.vehicle_number"),
                            address_line_1=_rv(row, "policy.auto.additional_interest.primary_address.line_1"),
                        )
                        for row in (_rep.get("policy.auto.additional_interest") or [])
                        if _rv(row, "policy.auto.additional_interest.name")
                    ]
                    _baut_acs = [
                        _BAutoAC(
                            description=_rv(row, "policy.auto.additional_coverage.name"),
                            code       =_rv(row, "policy.auto.additional_coverage.code"),
                            limit1     =_rv(row, "policy.auto.additional_coverage.each_claim_limit"),
                            deductible =_rv(row, "policy.auto.additional_coverage.deductible"),
                        )
                        for row in (_rep.get("policy.auto.additional_coverage") or [])
                        if _rv(row, "policy.auto.additional_coverage.name")
                    ]
                    _baut_states = list_available_baut_states(_pages[0])
                    if _baut_states:
                        worker.emit_progress(
                            f"Entering Business Auto ({_baut_states[0]})..."
                        )
                        _baut_run(_pages[0], _BAutoSetup(
                            state               =_baut_states[0],
                            coverages           =_baut_cov,
                            vehicles            =_baut_vehicles,
                            additional_interests=_baut_ais,
                            additional_coverages=_baut_acs,
                        ))
                    else:
                        worker.emit_progress(
                            "Business Auto: no BAUT line on the submission — skipping."
                        )

                # ── Step 6: Workers' Compensation ───────────────────────────────
                # WCOM is also state-suffixed.  Build one location per unique
                # state in the class_code rows; address falls back to the
                # account mailing address when no per-state address exists.
                if "policy.workers_comp" in _ns and _pages:
                    from ..epic_steps.step_workers_comp import (
                        WcClassCodeSpec   as _WcClass,
                        WcLocationSpec    as _WcLoc,
                        WcRatingInfoSpec  as _WcRating,
                        WorkersCompSetup  as _WcSetup,
                        list_available_wcom_states,
                        run as _wc_run,
                    )
                    from ..epic_steps.step_business_auto import _split_us_address as _wc_split_addr

                    _wc_rating = [
                        _WcRating(
                            state         =_rv(row, "policy.workers_comp.rating_info.state"),
                            experience_mod=_rv(row, "policy.workers_comp.rating_info.experience_mod"),
                            deductible    =_rv(row, "policy.workers_comp.rating_info.deductible"),
                        )
                        for row in (_rep.get("policy.workers_comp.rating_info") or [])
                        if _rv(row, "policy.workers_comp.rating_info.state")
                    ]
                    _wc_classes = [
                        _WcClass(
                            state       =_rv(row, "policy.workers_comp.class_code.state"),
                            class_code  =_rv(row, "policy.workers_comp.class_code.class_code"),
                            description =_rv(row, "policy.workers_comp.class_code.description_code"),
                            payroll     =_rv(row, "policy.workers_comp.class_code.payroll"),
                        )
                        for row in (_rep.get("policy.workers_comp.class_code") or [])
                        if _rv(row, "policy.workers_comp.class_code.class_code")
                    ]
                    _wc_fb_addr = _sv("account.named_insured.mailing_address.line_1")
                    _wc_street, _wc_city, _wc_addr_state, _wc_zip = _wc_split_addr(_wc_fb_addr or "")
                    _wc_states_seen: list[str] = []
                    for _cc in _wc_classes:
                        _s = (_cc.state or "").upper()
                        if _s and _s not in _wc_states_seen:
                            _wc_states_seen.append(_s)
                    _wc_locations = [
                        _WcLoc(
                            loc_num=str(idx), state=_s,
                            street=_wc_street, city=_wc_city, zip_code=_wc_zip,
                        )
                        for idx, _s in enumerate(_wc_states_seen, start=1)
                    ]
                    _wc_states_available = list_available_wcom_states(_pages[0])
                    if _wc_states_available:
                        _wc_target = _wc_states_available[0]
                        worker.emit_progress(
                            f"Entering Workers' Compensation ({_wc_target})..."
                        )
                        _wc_run(_pages[0], _WcSetup(
                            state                =_wc_target,
                            each_accident        =_sv("policy.workers_comp.each_accident"),
                            disease_each_employee=_sv("policy.workers_comp.disease_each_employee"),
                            disease_policy_limit =_sv("policy.workers_comp.disease_policy_limit"),
                            part1_states         =_sv("policy.workers_comp.part1_states"),
                            part3_states         =_sv("policy.workers_comp.part3_states"),
                            locations            =_wc_locations,
                            rating_info          =_wc_rating,
                            class_codes          =_wc_classes,
                        ))
                    else:
                        worker.emit_progress(
                            "Workers' Comp: no WCOM line on the submission — skipping."
                        )

                # ── Step 7: Inland Marine ───────────────────────────────────────
                if "policy.inland_marine" in _ns and _pages:
                    from ..epic_steps.step_inland_marine import (
                        IMAdditionalCoverageSpec  as _IMAC,
                        IMAdditionalInterestSpec  as _IMAI,
                        IMScheduledItemSpec       as _IMSched,
                        IMUnscheduledItemSpec     as _IMUnsched,
                        InlandMarineSetup         as _IMSetup,
                        list_available_im_lines,
                        run as _im_run,
                    )
                    _im_sched = [
                        _IMSched(
                            item_number  =_rv(row, "policy.inland_marine.scheduled_item.item_number"),
                            type         =_rv(row, "policy.inland_marine.scheduled_item.type"),
                            manufacturer =_rv(row, "policy.inland_marine.scheduled_item.manufacturer"),
                            model        =_rv(row, "policy.inland_marine.scheduled_item.model"),
                            model_year   =_rv(row, "policy.inland_marine.scheduled_item.model_year"),
                            description  =_rv(row, "policy.inland_marine.scheduled_item.description"),
                            serial_number=_rv(row, "policy.inland_marine.scheduled_item.serial_number"),
                            amt_insurance=_rv(row, "policy.inland_marine.scheduled_item.amt_insurance"),
                            deductible   =_rv(row, "policy.inland_marine.scheduled_item.deductible"),
                        )
                        for row in (_rep.get("policy.inland_marine.scheduled_item") or [])
                        if _rv(row, "policy.inland_marine.scheduled_item.description")
                        or _rv(row, "policy.inland_marine.scheduled_item.amt_insurance")
                    ]
                    _im_unsched = [
                        _IMUnsched(
                            description  =_rv(row, "policy.inland_marine.unscheduled_item.description"),
                            amt_insurance=_rv(row, "policy.inland_marine.unscheduled_item.amt_insurance"),
                        )
                        for row in (_rep.get("policy.inland_marine.unscheduled_item") or [])
                        if _rv(row, "policy.inland_marine.unscheduled_item.description")
                    ]
                    _im_ais = [
                        _IMAI(
                            name           =_rv(row, "policy.inland_marine.additional_interest.name"),
                            interest_type  =_rv(row, "policy.inland_marine.additional_interest.interest"),
                            address_line_1 =_rv(row, "policy.inland_marine.additional_interest.primary_address.line_1"),
                            reason_for_int =_rv(row, "policy.inland_marine.additional_interest.reason_for_int"),
                        )
                        for row in (_rep.get("policy.inland_marine.additional_interest") or [])
                        if _rv(row, "policy.inland_marine.additional_interest.name")
                    ]
                    _im_acs = [
                        _IMAC(
                            description=_rv(row, "policy.inland_marine.additional_coverage.name"),
                            code       =_rv(row, "policy.inland_marine.additional_coverage.code"),
                            each_claim =_rv(row, "policy.inland_marine.additional_coverage.each_claim_limit"),
                            deductible =_rv(row, "policy.inland_marine.additional_coverage.deductible"),
                        )
                        for row in (_rep.get("policy.inland_marine.additional_coverage") or [])
                        if _rv(row, "policy.inland_marine.additional_coverage.name")
                    ]
                    _im_lines = list_available_im_lines(_pages[0])
                    if _im_lines:
                        _im_target = (_im_lines[0].get("state") or "") if isinstance(_im_lines[0], dict) else ""
                        worker.emit_progress("Entering Inland Marine...")
                        _im_run(_pages[0], _IMSetup(
                            state                          =_im_target,
                            total_scheduled_amount         =_sv("policy.inland_marine.total_scheduled_amount"),
                            acv_replacement_cost_deductible=_sv("policy.inland_marine.acv_replacement_cost_deductible"),
                            scheduled_items                =_im_sched,
                            unscheduled_items              =_im_unsched,
                            additional_interests           =_im_ais,
                            additional_coverages           =_im_acs,
                        ))
                    else:
                        worker.emit_progress(
                            "Inland Marine: no IM line on the submission — skipping."
                        )

                # ── Step 8: Commercial Umbrella ─────────────────────────────────
                if "policy.umbrella" in _ns and _pages:
                    from ..epic_steps.step_umbrella import (
                        UmbrellaAdditionalCoverageSpec as _UmbAC,
                        UmbrellaAdditionalInterestSpec as _UmbAI,
                        UmbrellaSetup                  as _UmbSetup,
                        UmbrellaUnderlyingSpec         as _UmbUL,
                        list_available_umbrella_lines,
                        run as _umb_run,
                    )
                    _umb_underlying = [
                        _UmbUL(
                            carrier=_rv(row, "policy.umbrella.underlying.other.carrier"),
                            desc   =_rv(row, "policy.umbrella.underlying.other.desc"),
                            limit  =_rv(row, "policy.umbrella.underlying.other.limit"),
                        )
                        for row in (_rep.get("policy.umbrella.underlying.other") or [])
                        if _rv(row, "policy.umbrella.underlying.other.carrier")
                        or _rv(row, "policy.umbrella.underlying.other.desc")
                    ]
                    # State.json's diagnostic data has no Umbrella-specific AI / AC
                    # repeatables — leave empty; the step accepts empty lists.
                    _umb_ais: list = []
                    _umb_acs: list = []
                    _umb_lines = list_available_umbrella_lines(_pages[0])
                    if _umb_lines:
                        worker.emit_progress("Entering Commercial Umbrella...")
                        _umb_run(_pages[0], _UmbSetup(
                            expiring_pol_num    =_sv("policy.umbrella.expiring_pol_num"),
                            occurrence_limit    =_sv("policy.umbrella.occurrence_limit"),
                            retained_limit      =_sv("policy.umbrella.retained_limit"),
                            underlying          =_umb_underlying,
                            additional_interests=_umb_ais,
                            additional_coverages=_umb_acs,
                        ))
                    else:
                        worker.emit_progress(
                            "Commercial Umbrella: no umbrella line on the submission — skipping."
                        )

                # ── Generic field walk for remaining namespaces ──────────────────
                # The dedicated steps above handle their LOBs completely.
                # Exclude those namespaces so the generic walker doesn't attempt
                # to enter the same fields a second time.
                _dedicated = {
                    "submission",
                    "policy.commercial_ap",
                    "policy.gl",
                    "policy.property",
                    "policy.auto",
                    "policy.workers_comp",
                    "policy.inland_marine",
                    "policy.umbrella",
                }
                _generic_ns = (
                    [ns for ns in _ns if ns not in _dedicated]
                    if _ns else None
                )

                # If every selected namespace was a dedicated step, skip the
                # generic walker — there's nothing left to walk.
                if _generic_ns is not None and not _generic_ns:
                    from ..enter import EntryResult as _EntryResult
                    return _EntryResult(
                        run_id="dedicated-steps-only",
                        fields_entered=0,
                        fields_skipped=0,
                        fields_paused=0,
                        outcome="completed",
                    )

                def progress(domain_tag: str, completed: int, total: int) -> None:
                    worker.emit_progress_full(
                        f"Entering {domain_tag} ({completed + 1}/{total})...",
                        completed + 1,
                        total,
                    )

                return run_entry_session(
                    entry_state,
                    entry_field_map,
                    _cdp_ctx,
                    on_pause_callback=self._on_pause_callback,
                    on_progress_callback=progress,
                    settings=settings,
                    client_path=client_path,
                    include_namespaces=_generic_ns,
                )
            finally:
                try:
                    _pw2.stop()
                except Exception:
                    pass

        self._active_run_kind = "entry"
        self._run_controls.set_entering(True)
        self._show_progress_indeterminate("Entry session starting...")
        self._show_entry_busy_dialog("Entry session starting...")
        self._audit_log.append_event("Entry session started.")
        self._spawn_worker(
            task,
            on_finished=self._on_entry_finished,
            on_failed=self._on_entry_failed,
        )

    def _on_cancel(self) -> None:
        if self._worker_thread is None:
            return
        # Best-effort cancellation; the worker callable itself owns the
        # graceful-stop semantics. We just signal interest.
        if self._worker is not None and hasattr(self._worker, "cancel_requested"):
            try:
                self._worker.cancel_requested = True  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        self._audit_log.append_event("Cancel requested.")

    def _on_pause_callback(self, payload: object) -> str:
        """Bridge from worker thread → main thread modal → return value.

        Called by ``enter.run_entry_session`` when EPIC needs operator
        intervention. ``payload`` is a :class:`enter.PauseInfo` dataclass
        (per the entry-agent contract); we read ``reason_code`` to pick
        the right modal. We tolerate dict-shaped payloads too so test
        doubles can drive this without instantiating the dataclass.
        """
        # The modal must run on the GUI thread. If we're already on it, run
        # directly. Otherwise marshal via a single-shot QTimer onto the main
        # thread and block the worker until the operator picks an answer.
        # enter.run_entry_session expects "resume" | "skip" | "abort"; the
        # dialog family returns "resume" | "skip" | "cancel". Map cancel→abort
        # at the boundary so neither side has to know about the other's
        # vocabulary.
        def _read(name: str, default: object = None) -> object:
            if isinstance(payload, dict):
                return payload.get(name, default)
            return getattr(payload, name, default)

        result_holder: dict[str, str] = {"choice": "abort"}

        def show_modal() -> None:
            reason = _read("reason_code", "validation_rejected")
            field_label = _read("field_label", "field") or "field"
            screen_label = _read("screen_label", "screen") or "screen"
            tech = _read("technical_detail")
            if reason == "selector_unresolved":
                dialog = SelectorUnresolvedPauseDialog(
                    self,
                    field_label=field_label,
                    screen_label=screen_label,
                    technical_detail=tech,
                )
            else:
                dialog = EpicValidationPauseDialog(
                    self,
                    field_label=field_label,
                    attempted_value=_read("attempted_value"),
                    screen_label=screen_label,
                    technical_detail=tech,
                )
            choice = dialog.exec_with_choice()
            if isinstance(choice, PauseChoice):
                raw = choice.value
            elif isinstance(choice, str):
                raw = choice
            else:
                raw = PauseChoice.CANCEL.value
            # PauseChoice.CANCEL.value == "cancel"; enter.py expects "abort".
            result_holder["choice"] = "abort" if raw == "cancel" else raw

        # Thread-safety dance: Qt forbids touching widgets from a worker
        # thread, but enter.run_entry_session is called *from* a worker. We
        # marshal the modal onto the GUI thread via QTimer.singleShot(0,...)
        # and block the worker on a local QEventLoop until the operator
        # picks an answer. From the worker's perspective it just looks like
        # on_pause_callback returned.
        if QThread.currentThread() is self.thread():
            show_modal()
        else:
            import threading as _threading
            from PySide6.QtCore import QTimer

            _done = _threading.Event()

            def _runner() -> None:
                try:
                    show_modal()
                finally:
                    _done.set()

            # Pass `self` as context so Qt posts _runner to the main thread
            # (the thread that owns self), not the calling worker thread.
            # Then block the worker thread with a plain threading.Event until
            # the modal closes. QEventLoop on a worker thread is not safe —
            # it can process application-level events including close events.
            QTimer.singleShot(0, self, _runner)
            _done.wait()

        return result_holder["choice"]

    def _on_entry_finished(self, _result: object) -> None:
        self._run_controls.set_entering(False)
        self._active_run_kind = None
        self._hide_progress("Entry session complete.")
        self._close_entry_busy_dialog()
        # Clear runtime before any state-reload — step files won't run further.
        try:
            from ..epic_steps import runtime as _runtime_mod
            _runtime_mod.clear_runtime()
        except Exception:
            pass
        self._audit_log.append_event("Entry session complete.")
        # Offer send-report (no-op when no artifacts were produced).
        self._offer_send_run_report(outcome="completed")
        self._entry_runtime = None
        self._entry_cancel_event = None
        if self._client is not None:
            self._client.state = _safe_state_load(self._client.path)
            self._rebuild_tabs()
            self._refresh_run_controls()

    def _on_entry_failed(self, message: str, technical: str) -> None:
        self._run_controls.set_entering(False)
        self._active_run_kind = None
        self._hide_progress("Entry didn't finish.")
        self._close_entry_busy_dialog()
        try:
            from ..epic_steps import runtime as _runtime_mod
            _runtime_mod.clear_runtime()
        except Exception:
            pass
        # Distinguish user cancel from a real failure for the operator.
        cancelled = "EntryCancelled" in (technical or "") or "cancelled" in (message or "").lower()
        outcome = "cancelled" if cancelled else "failed"
        if cancelled:
            self._audit_log.append_event("Entry session cancelled by operator.")
        else:
            QMessageBox.warning(self, "Entry didn't finish", message)
            _logger.error("entry failed: %s | %s", message, technical)
        # Offer the report regardless — even cancelled runs may have produced
        # screenshots worth sending.
        self._offer_send_run_report(outcome=outcome)
        self._entry_runtime = None
        self._entry_cancel_event = None

    # -- Recover-interrupted-run --------------------------------------------

    def _show_recover_interrupted_run(self, pending: dict) -> None:
        completed = pending.get("completed_pdf_basenames") or []
        all_paths = pending.get("pdf_paths") or []
        remaining = [Path(p).name for p in all_paths if Path(p).name not in set(completed)]
        dialog = RecoverInterruptedRunDialog(
            self,
            completed_count=len(completed),
            total_count=len(all_paths),
            remaining_basenames=remaining,
            started_at=pending.get("started_at", "(unknown)"),
        )
        choice = dialog.exec_with_choice()
        if choice == "resume":
            self._audit_log.append_event(
                f"Resuming interrupted run ({len(remaining)} PDFs left)."
            )
            if self._client is not None:
                self._launch_extraction([self._client.inputs_dir / name for name in remaining])
        elif choice == "discard":
            if self._client is not None:
                self._client.state["pending_extraction"] = None
                self._persist_state()
                self._audit_log.append_event("Discarded interrupted run.")

    # -- Worker management --------------------------------------------------

    def _spawn_worker(
        self,
        task: Callable[[_CallableWorker], object],
        *,
        on_finished: Callable[[object], None],
        on_failed: Callable[[str, str], None],
    ) -> None:
        if self._worker_thread is not None:
            QMessageBox.information(
                self,
                "Already running",
                "Another run is already in progress. Wait for it to finish or click Cancel.",
            )
            return
        thread = QThread(self)
        worker = _CallableWorker(task)
        worker.moveToThread(thread)

        # The user-supplied callbacks run on the main thread via QueuedConnection.
        # Both signals carry data only (no Qt resources), so this is safe.
        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(on_failed, Qt.ConnectionType.QueuedConnection)

        # Canonical Qt thread-cleanup pattern: the thread terminates itself
        # asynchronously via signals — no thread.wait() calls anywhere.
        # Order of effects per Qt's deferred-deletion semantics:
        #   1. worker emits finished/failed → user callback runs on main thread
        #   2. thread.quit() posts a quit event into the worker's event loop
        #   3. worker's event loop exits → thread emits finished
        #   4. worker.deleteLater() and thread.deleteLater() are scheduled
        #   5. self._on_worker_thread_finished() clears our references
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._on_worker_thread_finished)

        thread.started.connect(worker.run)

        self._worker_thread = thread
        self._worker = worker
        thread.start()

    def _on_worker_thread_finished(self) -> None:
        """Clear worker references after the QThread has terminated cleanly."""
        self._worker_thread = None
        self._worker = None

    def _on_worker_progress(self, message: str, current: int, total: int) -> None:
        """Receive progress events from the active worker.

        Updates the status-bar text + progress bar, mirrors the message
        into the audit log, and keeps the busy-dialog status line in
        sync so the operator sees "Working... (3 of 7)" or whatever
        phase the worker is reporting.
        """
        self._audit_log.append_event(message)
        if total > 0:
            self._show_progress_determinate(message, current, total)
        else:
            self._show_progress_indeterminate(message)
        if self._busy_dialog is not None:
            if total > 0:
                self._busy_dialog.set_status_text(
                    f"Working... ({current} of {total})"
                )
            else:
                self._busy_dialog.set_status_text(message or "Working...")

    # -- Progress strip ----------------------------------------------------

    def _show_progress_indeterminate(self, message: str) -> None:
        """Show the progress bar in indeterminate mode + status-bar message."""
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setVisible(True)
        self.statusBar().showMessage(message)
        if self._sidebar is not None:
            self._sidebar.set_engine_status(True, message)

    def _show_progress_determinate(self, message: str, current: int, total: int) -> None:
        """Show the progress bar with a known total."""
        if total <= 0:
            self._show_progress_indeterminate(message)
            return
        self._progress_bar.setRange(0, total)
        self._progress_bar.setValue(min(current, total))
        self._progress_bar.setVisible(True)
        self.statusBar().showMessage(message)

    def _hide_progress(self, idle_message: str = "Ready.") -> None:
        """Hide the progress bar and reset the status-bar message."""
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 0)
        # Show the brief completion message; it'll be replaced by the rich
        # idle status on the next timer tick or state-change event.
        self.statusBar().showMessage(idle_message)
        if self._sidebar is not None:
            self._sidebar.set_engine_status(False)
        self._update_status_bar_idle()

    def _cleanup_worker(self) -> None:
        """Best-effort early termination (e.g., user clicked Cancel).

        Normal completion is handled by `_on_worker_thread_finished` via the
        thread.finished signal chain — this method is only used when we
        want to ask the worker to stop immediately. We do NOT call
        thread.wait() (which would raise "thread tried to wait on itself"
        if the call somehow originated from the worker thread).
        """
        if self._worker_thread is not None:
            self._worker_thread.requestInterruption()
            self._worker_thread.quit()
            # _on_worker_thread_finished will clear _worker_thread/_worker
            # when the thread actually terminates.

    # -- Status bar (UX-pass #5) -------------------------------------------

    def _update_status_bar_idle(self) -> None:
        """Refresh the idle status-bar message.

        Called on a timer (every 30s) and on every state change so the
        "saved X ago" tag stays current. While a run is in flight the
        progress strip owns the message; we leave it alone in that case.
        """
        if self._active_run_kind is not None:
            return  # progress strip is driving the status bar
        if self._client is None:
            self.statusBar().showMessage("No client loaded.")
            return
        total_fields = len((self._client.state.get("fields") or {}))
        repeatables = self._client.state.get("repeatables") or {}
        total_items = sum(
            len(v) for v in repeatables.values() if isinstance(v, list)
        )
        if self._client.last_save_at is not None:
            delta = (datetime.now(timezone.utc) - self._client.last_save_at).total_seconds()
            saved_str = f"saved {humanize_seconds_ago(delta)}"
        else:
            saved_str = "not yet saved"
        # Debug-mode cost segment. Shows the most recent extraction-run cost
        # plus the cumulative client total. Hidden in production to keep the
        # status line tidy for operators who don't need to think about cost.
        cost_tag = ""
        if self._debug:
            last_run_cost = self._latest_extraction_cost_usd()
            total_cost = float(self._client.state.get("total_cost_usd") or 0.0)
            if last_run_cost is not None or total_cost > 0:
                parts: list[str] = []
                if last_run_cost is not None:
                    parts.append(f"last run ${last_run_cost:.4f}")
                if total_cost > 0:
                    parts.append(f"total ${total_cost:.4f}")
                cost_tag = " · " + " · ".join(parts)
        msg = (
            f"Client: {self._client.name} · "
            f"{total_fields} fields · {total_items} items · {saved_str}"
            f"{cost_tag}"
        )
        self.statusBar().showMessage(msg)

    def _latest_extraction_cost_usd(self) -> float | None:
        """Return the cost_usd of the most recent extraction run, or None."""
        if self._client is None:
            return None
        history = self._client.state.get("run_history") or []
        for entry in reversed(history):
            if not isinstance(entry, dict):
                continue
            if entry.get("kind") != "extraction":
                continue
            cost = entry.get("cost_usd")
            if cost is None:
                continue
            try:
                return float(cost)
            except (TypeError, ValueError):
                return None
        return None

    # -- Bulk actions (UX-pass #7) -----------------------------------------

    def _on_bulk_action(self, action: str, tab_key: str) -> None:
        """Apply ``action`` to every field in ``tab_key``.

        Called from per-section :class:`BulkActionBar` buttons and from the
        Edit-menu actions. The single dispatcher means there's exactly one
        place to update if the action vocabulary grows.
        """
        if self._client is None:
            return
        changed = self._apply_bulk_action_to_state(self._client.state, action, tab_key)
        if changed == 0:
            self.statusBar().showMessage(f"No fields affected ({action}).", 3_000)
            return
        self._persist_state()
        self._audit_log.append_event(
            f"Bulk action '{action}' on '{tab_key}' affected {changed} field(s)."
        )
        self._rebuild_tabs()
        self._refresh_run_controls()

    @staticmethod
    def _apply_bulk_action_to_state(state: dict, action: str, tab_key: str) -> int:
        """Mutate ``state`` in place; return the number of records touched.

        Pure-ish (mutates ``state`` only) so unit tests can drive it without
        a Qt window. The ``action`` vocabulary matches what
        :class:`BulkActionBar` emits.
        """
        if not isinstance(state, dict):
            return 0
        affected = 0

        def _touch(record: dict) -> bool:
            if not isinstance(record, dict):
                return False
            if action == "approve_all":
                if record.get("status") == "locked":
                    return False  # locked fields are immutable
                record["status"] = "approved"
                return True
            if action == "reject_all":
                if record.get("status") == "locked":
                    return False
                record["status"] = "pending"
                record["value"] = None
                return True
            if action == "lock_all_approved":
                if record.get("status") == "approved":
                    record["status"] = "locked"
                    return True
                return False
            return False

        if tab_key in REPEATABLE_NAMESPACES:
            items = (state.get("repeatables") or {}).get(tab_key, [])
            if not isinstance(items, list):
                return 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                for record in item.values():
                    if _touch(record):
                        affected += 1
            return affected

        fields_map: dict = state.get("fields") or {}
        for tag, record in fields_map.items():
            if _tab_key_for_tag(tag) != tab_key:
                continue
            if _touch(record):
                affected += 1
        return affected


def _safe_user() -> str:
    """``os.getlogin()`` with a fallback that won't raise on detached terminals."""
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def _safe_git_commit() -> str:
    """Return a short git rev or "dev" if git isn't available.

    Used by Help → About. Best-effort: never raises.
    """
    import subprocess

    try:
        repo_root = Path(__file__).resolve()
        for parent in [repo_root, *repo_root.parents]:
            if (parent / ".git").exists():
                cmd = ["git", "-C", str(parent), "rev-parse", "--short", "HEAD"]
                result = subprocess.run(  # noqa: S603 — no shell=True, fixed args
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                )
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                break
    except (OSError, subprocess.SubprocessError):
        pass
    return "dev"


def _to_bool(value: object) -> bool:
    """Coerce a QSettings-stored value (might be str "true"/"false") to bool."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


# ---------------------------------------------------------------------------
# IgaApp — application entry point
# ---------------------------------------------------------------------------


class IgaApp:
    """Application entry point. See ARCHITECTURE.md §8.1."""

    @classmethod
    def run(
        cls,
        *,
        debug: bool = False,
        settings: "config_module.Settings | None" = None,
    ) -> int:
        """Construct ``QApplication``, show the main window, run the loop.

        Returns the exit code from ``QApplication.exec()``. Called from
        ``cli.py``'s ``main()``.

        :param debug: Whether to enable debug-mode behaviors.
        :param settings: Pre-built ``Settings`` object from the CLI. Pass
            this to preserve CLI overrides like ``--client`` and
            ``--queue-pdfs``. If ``None``, settings are loaded from disk
            with only ``debug`` as an override (legacy entry path).
        """
        from ..logger import configure_logging

        if settings is None:
            settings = config_module.load_settings(
                cli_overrides={"debug": debug} if debug else None,
            )
            configure_logging(settings)

        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName(config_module.APP_NAME)

        # Windows taskbar uses AppUserModelID to group windows + pick an icon.
        # Without this, Python's generic icon shows in the taskbar even though
        # the window icon is set correctly.
        if sys.platform == "win32":
            try:
                import ctypes  # noqa: PLC0415
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "IGA.MarketingMaster.2"
                )
            except Exception:  # noqa: BLE001
                pass

        # Both .ico (multi-resolution; what the Windows taskbar
        # uses via the AppUserModelID) and the high-res PNG go into
        # a single QIcon. Setting it on QApplication makes it the
        # default for every window the app spawns.
        _assets_dir = Path(__file__).resolve().parents[3] / "assets"
        _app_icon = QIcon()
        _ico = _assets_dir / "icon.ico"
        _png = _assets_dir / "IGA_Icon_Orange2x.png"
        if _ico.is_file():
            _app_icon.addFile(str(_ico))
        if _png.is_file():
            _app_icon.addFile(str(_png))
        if not _app_icon.isNull():
            app.setWindowIcon(_app_icon)

        window = MainWindow(settings, debug=debug)
        window.show()
        return int(app.exec())

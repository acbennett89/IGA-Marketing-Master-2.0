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
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QMovie
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
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
from .find_bar import FindBar
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
_QS_VIEW_AUDIT_VISIBLE: str = "view/auditLogVisible"
_QS_VIEW_LOW_CONF_FILTER: str = "view/lowConfidenceFilter"

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
QWidget#HistoryPage,
QWidget#SettingsPage,
QWidget#HelpPage {
    background: #f8fafc;
}
QLabel#PlaceholderTitle {
    font-size: 18px;
    font-weight: bold;
    color: #374151;
}
QLabel#PlaceholderBody {
    font-size: 13px;
    color: #6b7280;
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

        title = QLabel("Data Review")
        title.setObjectName("PageTitle")
        tb.addWidget(title)

        sub = QLabel("Review and verify extracted insurance data from your PDFs.")
        sub.setObjectName("PageSubtitle")
        sub.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        tb.addWidget(sub)

        layout.addWidget(title_block, 1)

        # Action buttons
        self.extract_btn = QPushButton("Extract")
        self.extract_btn.setObjectName("HeaderBtnSecondary")
        self.extract_btn.setToolTip("Run extraction on queued PDFs (Ctrl+E).")
        self.extract_btn.clicked.connect(self.extract_clicked)
        layout.addWidget(self.extract_btn)

        self.begin_btn = QPushButton("Begin Entry")
        self.begin_btn.setObjectName("HeaderBtnPrimary")
        self.begin_btn.setToolTip("Start entering approved fields into EPIC (Ctrl+Return).")
        self.begin_btn.clicked.connect(self.begin_entry_clicked)
        layout.addWidget(self.begin_btn)


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
    """Return the number of low-confidence fields on the named tab.

    A field counts as low-confidence when ``confidence < CONFIDENCE_HIGH_THRESHOLD``
    AND its status isn't an operator-blessed terminal state (``approved`` /
    ``locked``). Repeatable groups walk every record across every item.
    """
    if not state:
        return 0

    def _record_is_low(record: dict) -> bool:
        if not isinstance(record, dict):
            return False
        status = record.get("status", "pending")
        if status in {"approved", "locked"}:
            return False
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
            for record in item.values()
            if _record_is_low(record)
        )

    fields_map: dict = state.get("fields") or {}
    singleton_low = sum(
        1
        for tag, record in fields_map.items()
        if _tab_key_for_tag(tag) == tab_key and _record_is_low(record)
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
                    1 for record in item.values() if _record_is_low(record)
                )
    return singleton_low + rep_low


def build_tab_label(state: dict | None, tab_key: str) -> str:
    """Return the user-visible tab label including the count badge.

    Format: ``"<Friendly Name> (N)"`` or ``"<Friendly Name> (N · K!)"`` when
    ``K`` low-confidence fields exist. Empty tabs render the bare name with no
    count to keep the chrome quiet.
    """
    base = label_for_tab_key(tab_key)
    total = count_tab_field_total(state, tab_key)
    if total == 0:
        return base
    low = count_low_confidence_in_tab(state, tab_key)
    if low > 0:
        return f"{base} ({total} · {low}!)"
    return f"{base} ({total})"


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
        # Tracks which kind of run is in flight so the progress strip and
        # run-controls can clear themselves correctly on finish/fail.
        self._active_run_kind: str | None = None  # "extract" | "entry" | None

        # Persistent settings (QSettings) — used for geometry, recent clients,
        # and view-menu checkable states. Wrapped in a try so headless test
        # environments without an organization registry still work.
        self._qsettings: QSettings = QSettings(_QSETTINGS_ORG, _QSETTINGS_APP)

        # UX state for #8 / #9 — tracked on the window so all section views
        # share a single source of truth.
        self._find_query: str = ""
        self._low_confidence_filter: bool = False

        # Cache of QAction / QPushButton objects we need to enable/disable.
        # Values may be QAction *or* QPushButton — both have setEnabled().
        self._run_actions: dict = {}
        # Cache of recent-clients QActions so we can rebuild on aboutToShow.
        self._recent_menu: QMenu | None = None
        # The "Get started" empty-state widget; we reuse one instance.
        self._welcome_pane: WelcomePane | None = None
        # Track widgets so toggles (View → Show Audit Log, etc.) work.
        self._outer_split: QSplitter | None = None
        self._bottom_widget: QWidget | None = None
        # Sidebar + page stack (set during _build_ui)
        self._sidebar: SidebarNav | None = None
        self._pages: QStackedWidget | None = None
        self._page_index: dict[str, int] = {}
        # Upload panel (wraps PendingPdfsPane with nicer UI)
        self._upload_panel: UploadPanel | None = None
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
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        self._build_ui()
        self._build_menu_bar()
        self._restore_persisted_layout()
        self._handle_first_run_and_api_key()
        self._maybe_seed_initial_client()
        # If _maybe_seed_initial_client didn't load a client, _rebuild_tabs
        # paints the welcome empty-state tab. Loading a client also calls
        # _rebuild_tabs so this is idempotent.
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

        # Right: custom tab bar + content stack + PDF preview (center splitter)
        self._find_bar = FindBar()
        self._find_bar.query_changed.connect(self._on_find_query_changed)
        self._find_bar.closed.connect(self._on_find_bar_closed)

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
        tc_layout.addWidget(self._find_bar)
        tc_layout.addWidget(tab_bar_scroll)
        tc_layout.addWidget(self._tab_stack, 1)

        # Audit log + run controls beneath the tab area
        self._audit_log = AuditLogPane()
        self._audit_log.attach_logger("iga", level=logging.INFO)

        self._run_controls = RunControlsBar()
        self._run_controls.extract_clicked.connect(self._on_extract_clicked)
        self._run_controls.begin_entry_clicked.connect(self._on_begin_entry)
        self._run_controls.cancel_clicked.connect(self._on_cancel)
        self._run_controls.resume_clicked.connect(self._on_resume)

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

        # == History page ==================================================
        idx = self._pages.addWidget(self._build_history_page())
        self._page_index["history"] = idx

        # == Settings page =================================================
        idx = self._pages.addWidget(self._build_settings_page())
        self._page_index["settings"] = idx

        # == Help page =====================================================
        idx = self._pages.addWidget(self._build_help_page())
        self._page_index["help"] = idx

        # -- Outer container: sidebar | pages ------------------------------
        main_content = QWidget()
        main_content.setObjectName("MainContent")
        mc_layout = QHBoxLayout(main_content)
        mc_layout.setContentsMargins(0, 0, 0, 0)
        mc_layout.setSpacing(0)
        mc_layout.addWidget(self._sidebar)
        mc_layout.addWidget(self._pages, 1)

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
        """Spawn Chrome with remote-debugging enabled.

        The EPIC entry script connects to this Chrome instance via Playwright
        over CDP, so the operator's existing session (cookies, MFA state) is
        reused. Port and user-data-dir defaults match v1 conventions.
        """
        import subprocess

        port = "9222"
        userdata = r"C:\Temp\chrome-debug"
        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]
        chrome_exe = next((p for p in chrome_candidates if Path(p).exists()), None)
        if chrome_exe is None:
            QMessageBox.critical(
                self,
                "Chrome Not Found",
                "Could not locate chrome.exe. Install Chrome or update the "
                "path in main_window._on_launch_browser_clicked.",
            )
            return

        # Make sure the user-data-dir exists so Chrome doesn't fail silently
        # on first launch.
        try:
            Path(userdata).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "User Data Dir",
                f"Could not create {userdata}: {exc}",
            )
            return

        cmd = [
            chrome_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={userdata}",
        ]
        try:
            subprocess.Popen(cmd)
        except OSError as exc:
            QMessageBox.critical(self, "Launch Failed", str(exc))
            return

        self._logger.info(
            "browser.launched port=%s user_data_dir=%s exe=%s",
            port, userdata, chrome_exe,
        )
        if self._sidebar is not None:
            self._sidebar.set_engine_status(
                False, f"Browser launched (port {port})"
            )

    @staticmethod
    def _build_history_page() -> QWidget:
        w = QWidget()
        w.setObjectName("HistoryPage")
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("History")
        title.setObjectName("PlaceholderTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel(
            "Audit log and run history are shown in the\n"
            "Data Review page below the section tabs."
        )
        body.setObjectName("PlaceholderBody")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(body)
        return w

    @staticmethod
    def _build_settings_page() -> QWidget:
        w = QWidget()
        w.setObjectName("SettingsPage")
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Settings")
        title.setObjectName("PlaceholderTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel(
            "Use File → Settings (coming soon) to configure\n"
            "the Working Library path and API key."
        )
        body.setObjectName("PlaceholderBody")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(body)
        return w

    @staticmethod
    def _build_help_page() -> QWidget:
        w = QWidget()
        w.setObjectName("HelpPage")
        layout = QVBoxLayout(w)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel("Help")
        title.setObjectName("PlaceholderTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body = QLabel(
            "Open TROUBLESHOOTING.md from the Help menu\n"
            "or visit the project repository for documentation."
        )
        body.setObjectName("PlaceholderBody")
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        layout.addWidget(body)
        return w

    # -- Menu bar (UX-pass #1, #2) ------------------------------------------

    def _build_menu_bar(self) -> None:  # noqa: C901 — menu wiring is naturally long
        menubar: QMenuBar = self.menuBar()
        menubar.clear()

        # ----- File ------------------------------------------------------
        file_menu = menubar.addMenu("&File")

        act_new = QAction("New Client...", self)
        act_new.setShortcut(QKeySequence("Ctrl+N"))
        act_new.triggered.connect(self._on_create_client)
        file_menu.addAction(act_new)

        act_pick = QAction("Pick Client...", self)
        act_pick.setShortcut(QKeySequence("Ctrl+O"))
        act_pick.triggered.connect(self._on_pick_client)
        file_menu.addAction(act_pick)

        self._recent_menu = file_menu.addMenu("Recent Clients")
        self._recent_menu.aboutToShow.connect(self._rebuild_recent_clients_menu)
        # Seed an initial entry so the first show isn't empty.
        self._rebuild_recent_clients_menu()

        file_menu.addSeparator()

        act_add_pdfs = QAction("Add PDFs...", self)
        act_add_pdfs.setShortcut(QKeySequence("Ctrl+Shift+O"))
        act_add_pdfs.triggered.connect(self._on_add_pdfs)
        file_menu.addAction(act_add_pdfs)

        act_save = QAction("Save State", self)
        act_save.setShortcut(QKeySequence("Ctrl+S"))
        act_save.triggered.connect(self._on_save_state_explicit)
        file_menu.addAction(act_save)

        file_menu.addSeparator()

        act_close = QAction("Close Client", self)
        act_close.triggered.connect(self._on_close_client)
        file_menu.addAction(act_close)

        act_exit = QAction("Exit", self)
        act_exit.setShortcut(QKeySequence("Alt+F4"))
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        # ----- Edit ------------------------------------------------------
        edit_menu = menubar.addMenu("&Edit")

        act_find = QAction("Find Field...", self)
        act_find.setShortcut(QKeySequence("Ctrl+F"))
        act_find.triggered.connect(self._on_open_find_bar)
        edit_menu.addAction(act_find)

        edit_menu.addSeparator()

        act_approve_all = QAction("Approve All in Section", self)
        act_approve_all.setShortcut(QKeySequence("Ctrl+Shift+A"))
        act_approve_all.triggered.connect(
            lambda: self._on_bulk_action_active_tab("approve_all")
        )
        edit_menu.addAction(act_approve_all)

        act_reject_all = QAction("Reject All in Section", self)
        act_reject_all.triggered.connect(
            lambda: self._on_bulk_action_active_tab("reject_all")
        )
        edit_menu.addAction(act_reject_all)

        act_lock_all = QAction("Lock All Approved", self)
        act_lock_all.triggered.connect(
            lambda: self._on_bulk_action_active_tab("lock_all_approved")
        )
        edit_menu.addAction(act_lock_all)

        # ----- View ------------------------------------------------------
        view_menu = menubar.addMenu("&View")

        self._act_low_conf_filter = QAction("Show Only Low-Confidence", self)
        self._act_low_conf_filter.setShortcut(QKeySequence("Ctrl+L"))
        self._act_low_conf_filter.setCheckable(True)
        self._act_low_conf_filter.toggled.connect(self._on_low_confidence_toggled)
        view_menu.addAction(self._act_low_conf_filter)

        view_menu.addSeparator()

        self._act_show_audit = QAction("Show Audit Log", self)
        self._act_show_audit.setCheckable(True)
        self._act_show_audit.setChecked(False)
        self._act_show_audit.toggled.connect(self._on_toggle_audit_log)
        view_menu.addAction(self._act_show_audit)

        view_menu.addSeparator()

        act_reset_layout = QAction("Reset Layout", self)
        act_reset_layout.triggered.connect(self._on_reset_layout)
        view_menu.addAction(act_reset_layout)

        # ----- Run -------------------------------------------------------
        run_menu = menubar.addMenu("&Run")

        act_run_extract = QAction("Extract", self)
        act_run_extract.setShortcut(QKeySequence("Ctrl+E"))
        act_run_extract.triggered.connect(self._on_extract_clicked)
        run_menu.addAction(act_run_extract)
        self._run_actions["menu_extract"] = act_run_extract

        act_run_begin = QAction("Begin Entry", self)
        act_run_begin.setShortcut(QKeySequence("Ctrl+Return"))
        act_run_begin.triggered.connect(self._on_begin_entry)
        run_menu.addAction(act_run_begin)
        self._run_actions["menu_begin"] = act_run_begin

        act_run_cancel = QAction("Cancel Current Run", self)
        act_run_cancel.setShortcut(QKeySequence("Esc"))
        act_run_cancel.triggered.connect(self._on_cancel)
        run_menu.addAction(act_run_cancel)
        self._run_actions["menu_cancel"] = act_run_cancel

        act_run_resume = QAction("Resume Paused Entry", self)
        act_run_resume.triggered.connect(self._on_resume)
        run_menu.addAction(act_run_resume)
        self._run_actions["menu_resume"] = act_run_resume

        # ----- Help ------------------------------------------------------
        help_menu = menubar.addMenu("&Help")

        act_open_troubleshoot = QAction("Open TROUBLESHOOTING.md", self)
        act_open_troubleshoot.triggered.connect(self._on_open_troubleshooting)
        help_menu.addAction(act_open_troubleshoot)

        act_open_console = QAction("Open Anthropic Console", self)
        act_open_console.triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://console.anthropic.com/"))
        )
        help_menu.addAction(act_open_console)

        help_menu.addSeparator()

        act_about = QAction("About IGA Marketing Master", self)
        act_about.triggered.connect(self._on_about)
        help_menu.addAction(act_about)

        act_report = QAction("Report an Issue", self)
        act_report.triggered.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://github.com/anthropics/claude-code/issues")
            )
        )
        help_menu.addAction(act_report)

    # -- Menu handlers (small one-liners that didn't have a home before) ---

    def _on_save_state_explicit(self) -> None:
        """File → Save State. State auto-saves on edits; this is a nudge."""
        if self._client is None:
            self.statusBar().showMessage("No client to save.", 3_000)
            return
        self._persist_state()
        self.statusBar().showMessage("State saved.", 3_000)
        self._update_status_bar_idle()

    def _on_close_client(self) -> None:
        if self._client is None:
            return
        self._audit_log.append_event(f"Closed client: {self._client.name}")
        self._client = None
        self.setWindowTitle("IGA Marketing Master 2.0")
        self._rebuild_tabs()
        self._refresh_run_controls()
        self._update_status_bar_idle()

    def _on_open_find_bar(self) -> None:
        self._find_bar.open()

    def _on_find_bar_closed(self) -> None:
        # Clearing the line edit fires textChanged("") which already clears
        # the filter; this is here to capture the close-without-edit path.
        self._find_query = ""
        self._reapply_filters_to_visible_tabs()
        if self._tab_stack is not None:
            self._tab_stack.setFocus()

    def _on_find_query_changed(self, text: str) -> None:
        self._find_query = text
        self._reapply_filters_to_visible_tabs()

    def _on_low_confidence_toggled(self, checked: bool) -> None:
        self._low_confidence_filter = bool(checked)
        self._qsettings.setValue(_QS_VIEW_LOW_CONF_FILTER, self._low_confidence_filter)
        self._reapply_filters_to_visible_tabs()
        self._update_status_bar_idle()

    def _on_toggle_audit_log(self, checked: bool) -> None:
        if self._audit_log is not None:
            self._audit_log.setVisible(checked)
        self._qsettings.setValue(_QS_VIEW_AUDIT_VISIBLE, bool(checked))

    def _on_reset_layout(self) -> None:
        # Clear persisted geometry/state and force a sane default.
        for key in (
            _QS_GEOMETRY,
            _QS_WINDOW_STATE,
            _QS_OUTER_SPLITTER,
        ):
            self._qsettings.remove(key)
        self.resize(1400, 900)
        if self._outer_split is not None:
            self._outer_split.setSizes([1000, 0])
        self._act_show_audit.setChecked(False)
        self.statusBar().showMessage("Layout reset.", 3_000)

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

    def _rebuild_recent_clients_menu(self) -> None:
        """Repopulate the Recent Clients submenu from QSettings."""
        if self._recent_menu is None:
            return
        self._recent_menu.clear()
        recent = load_recent_clients(self._qsettings)
        if not recent:
            empty = QAction("(no recent clients)", self)
            empty.setEnabled(False)
            self._recent_menu.addAction(empty)
            return
        for path in recent:
            label = self._format_recent_label(path)
            action = QAction(label, self)
            if not path.exists():
                action.setEnabled(False)
            else:
                # Bind the path via default-arg trick so the closure
                # captures *this* path, not the loop variable.
                action.triggered.connect(lambda _checked=False, p=path: self._load_client(p))
            self._recent_menu.addAction(action)

    @staticmethod
    def _format_recent_label(path: Path) -> str:
        """Render `client_name (parent_dir_basename)`; append `(missing)` if gone."""
        name = path.name or str(path)
        parent = path.parent.name if path.parent and path.parent.name else "(root)"
        suffix = "" if path.exists() else " (missing)"
        return f"{name} ({parent}){suffix}"

    def _push_recent_client(self, path: Path) -> None:
        existing = load_recent_clients(self._qsettings)
        updated = update_recent_clients(existing, path)
        save_recent_clients(self._qsettings, updated)

    # -- Layout persistence (UX-pass #3) ------------------------------------

    def _restore_persisted_layout(self) -> None:
        """Restore geometry, splitter sizes, and view-menu states from QSettings.

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

        # View-menu checkable states (visibility + low-confidence filter).
        audit_visible = _to_bool(self._qsettings.value(_QS_VIEW_AUDIT_VISIBLE, False))
        low_conf = _to_bool(self._qsettings.value(_QS_VIEW_LOW_CONF_FILTER, False))

        if hasattr(self, "_act_show_audit"):
            self._act_show_audit.setChecked(audit_visible)
            self._audit_log.setVisible(audit_visible)
        if hasattr(self, "_act_low_conf_filter"):
            self._act_low_conf_filter.setChecked(low_conf)
            self._low_confidence_filter = low_conf

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
        super().closeEvent(event)

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
        choice = QFileDialog.getExistingDirectory(
            self,
            "Pick a client folder",
            str(self._settings.working_library),
        )
        if not choice:
            return
        self._load_client(Path(choice))

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

    def _rebuild_tabs(self) -> None:
        """Diff current tabs against target keys and apply the delta.

        The coverage tabs (DEFAULT_TAB_KEYS) are always shown so the operator
        can see the section structure even before a client is loaded. When no
        client is loaded, each tab shows an empty-state hint instead of data.
        """
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
            self._reapply_filters_to_visible_tabs()
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

        self._reapply_filters_to_visible_tabs()

    def _make_tab_button(self, key: str, label: str) -> QPushButton:
        btn = QPushButton(label)
        btn.setObjectName("TabBtn")
        btn.setProperty("active", False)
        btn.setProperty("dimmed", False)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.clicked.connect(lambda _=False, k=key: self._select_tab(k))
        return btn

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
        if record.get("status") == "pending":
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
        if record.get("status") == "pending":
            record["status"] = "approved"
        self._persist_state()
        self._audit_log.append_event(f"Edited {group}[{item_index}] {domain_tag} → {value!r}")
        self._refresh_run_controls()

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
        approved = self._count_approved_fields(self._client.state) if self._client else 0
        self._sync_run_actions(approved=approved)

    def _on_extract_clicked(self) -> None:
        """Handler for the new Extract button. See gui-fix-2 #1."""
        if self._client is None:
            QMessageBox.information(self, "No client loaded", "Pick or create a client first.")
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
        self._rebuild_tabs()
        self._refresh_run_controls()

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

    # -- Run controls (Begin Entry / Cancel / Resume) -----------------------

    def _refresh_run_controls(self) -> None:
        if self._client is None:
            self._run_controls.set_status_text("No client loaded.")
            self._run_controls.set_approved_count(0)
            self._sync_run_actions(approved=0)
            return
        approved = self._count_approved_fields(self._client.state)
        self._run_controls.set_status_text(
            f"{self._client.name} — {approved} approved field(s)."
        )
        self._run_controls.set_approved_count(approved)
        self._sync_run_actions(approved=approved)

    def _sync_run_actions(self, *, approved: int) -> None:
        """Mirror the bottom run-controls' enabled state onto menu/toolbar QActions.

        Keeps Ctrl+E / Ctrl+Enter / Esc in lockstep with the bottom buttons.
        """
        any_run_active = self._active_run_kind is not None
        queued = self._pending_pdfs_pane.count() if hasattr(self, "_pending_pdfs_pane") else 0
        can_extract = (queued > 0) and not any_run_active and self._client is not None
        can_begin = (approved > 0) and not any_run_active and self._client is not None
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
        resume_act = self._run_actions.get("menu_resume")
        if resume_act is not None:
            resume_act.setEnabled(any_run_active)

    @staticmethod
    def _count_approved_fields(state: dict) -> int:
        count = 0
        for record in (state.get("fields") or {}).values():
            if isinstance(record, dict) and record.get("status") in {"approved", "locked"}:
                count += 1
        for items in (state.get("repeatables") or {}).values():
            for item in items or []:
                if not isinstance(item, dict):
                    continue
                for record in item.values():
                    if isinstance(record, dict) and record.get("status") in {"approved", "locked"}:
                        count += 1
        return count

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

        # enter.run_entry_session signature:
        #   run_entry_session(state, field_map, browser_context, *,
        #                     on_pause_callback, on_progress_callback=None,
        #                     settings=None, client_path=None, ...)
        # The GUI keeps a plain-dict view of state for the panes; for the
        # entry walk we re-load the canonical State dataclass off disk and
        # construct a fresh FieldMap + Playwright context. Doing this on
        # the GUI thread before spawning the worker means any setup error
        # (Playwright not installed, profile in use, missing field map)
        # surfaces as a modal instead of a worker-thread exception.
        from .. import epic_session as epic_session_module
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

        try:
            browser_context = epic_session_module.launch_with_persistent_context(
                self._settings.playwright_profile,
                headed=True,
                debug=self._debug,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self,
                "Couldn't launch EPIC browser",
                f"Chromium failed to launch with the persistent profile.\n\n{exc}",
            )
            return

        client_path = self._client.path
        settings = self._settings

        def task(worker: _CallableWorker) -> object:
            worker.emit_progress("Entry session starting...")

            def progress(domain_tag: str, completed: int, total: int) -> None:
                # ``completed`` is a 0-based index of the unit *about to
                # start*; surface as 1-based for the operator.
                worker.emit_progress_full(
                    f"Entering {domain_tag} ({completed + 1}/{total})...",
                    completed + 1,
                    total,
                )

            try:
                return run_entry_session(
                    entry_state,
                    entry_field_map,
                    browser_context,
                    on_pause_callback=self._on_pause_callback,
                    on_progress_callback=progress,
                    settings=settings,
                    client_path=client_path,
                )
            finally:
                # Always tear down the browser context so the persistent
                # profile lock is released even on exceptions.
                try:
                    browser_context.close()
                except Exception:  # noqa: BLE001
                    pass
                pw = getattr(browser_context, "_iga_playwright", None)
                if pw is not None:
                    try:
                        pw.stop()
                    except Exception:  # noqa: BLE001
                        pass

        self._active_run_kind = "entry"
        self._run_controls.set_entering(True)
        self._show_progress_indeterminate("Entry session starting...")
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

    def _on_resume(self) -> None:
        # Resume is meaningful when a pause modal is in flight; the modal
        # itself drives resumption. The button is mostly a redundancy /
        # discoverability aid.
        self._audit_log.append_event("Resume requested.")
        self._run_controls.set_paused(False)

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
            self._run_controls.set_paused(True)
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
            self._run_controls.set_paused(False)

        # Thread-safety dance: Qt forbids touching widgets from a worker
        # thread, but enter.run_entry_session is called *from* a worker. We
        # marshal the modal onto the GUI thread via QTimer.singleShot(0,...)
        # and block the worker on a local QEventLoop until the operator
        # picks an answer. From the worker's perspective it just looks like
        # on_pause_callback returned.
        if QThread.currentThread() is self.thread():
            show_modal()
        else:
            from PySide6.QtCore import QEventLoop, QTimer

            loop = QEventLoop()

            def _runner() -> None:
                try:
                    show_modal()
                finally:
                    loop.quit()

            QTimer.singleShot(0, _runner)
            loop.exec()

        return result_holder["choice"]

    def _on_entry_finished(self, _result: object) -> None:
        self._run_controls.set_entering(False)
        self._run_controls.set_paused(False)
        self._active_run_kind = None
        self._hide_progress("Entry session complete.")
        self._audit_log.append_event("Entry session complete.")
        if self._client is not None:
            self._client.state = _safe_state_load(self._client.path)
            self._rebuild_tabs()
            self._refresh_run_controls()

    def _on_entry_failed(self, message: str, technical: str) -> None:
        self._run_controls.set_entering(False)
        self._run_controls.set_paused(False)
        self._active_run_kind = None
        self._hide_progress("Entry didn't finish.")
        QMessageBox.warning(self, "Entry didn't finish", message)
        _logger.error("entry failed: %s | %s", message, technical)

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
        filter_tag = (
            " · [Low-confidence filter ON]" if self._low_confidence_filter else ""
        )
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
            f"{filter_tag}{cost_tag}"
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

    # -- Filter reapply (UX-pass #8, #9) -----------------------------------

    def _reapply_filters_to_visible_tabs(self) -> None:
        """Apply the current find_query and low_confidence_only state to all tabs.

        Walks every tab's :class:`SectionTableView` (singleton tabs) and the
        nested view inside each :class:`RepeatablePane` (repeatable tabs),
        hides non-matching rows, and dims tab buttons whose visible-row count
        is zero when a find query is active.
        """
        for key, widget in self._tab_pages.items():
            if key in (None, "__welcome__"):
                continue
            visible = self._apply_filter_to_widget(widget)
            btn = self._tab_buttons.get(key)
            if btn is None:
                continue
            dimmed = bool(self._find_query and visible == 0)
            if btn.property("dimmed") != dimmed:
                btn.setProperty("dimmed", dimmed)
                btn.style().unpolish(btn)
                btn.style().polish(btn)

    def _apply_filter_to_widget(self, widget: QWidget) -> int:
        """Apply filters to all SectionTableView descendants; return total visible rows."""
        total_visible = 0
        for view in widget.findChildren(SectionTableView):
            visible = view.apply_row_visibility(
                find_query=self._find_query,
                low_confidence_only=self._low_confidence_filter,
            )
            total_visible += visible
        return total_visible

    # -- Bulk actions (UX-pass #7) -----------------------------------------

    def _on_bulk_action_active_tab(self, action: str) -> None:
        """Edit-menu entry point: dispatch to the current tab's section."""
        if self._client is None:
            return
        key = self._active_tab_key
        if not isinstance(key, str) or key == "__welcome__":
            return
        self._on_bulk_action(action, key)

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

        window = MainWindow(settings, debug=debug)
        window.show()
        return int(app.exec())

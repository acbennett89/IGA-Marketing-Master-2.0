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

from PySide6.QtCore import QObject, QSettings, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QTabWidget,
    QToolBar,
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
from .pdf_preview import PdfPreview
from .pending_pdfs_pane import PendingPdfsPane
from .repeatable_pane import RepeatablePane
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
# Tab-derivation vocabulary
# ---------------------------------------------------------------------------


TAB_LABELS: dict[str, str] = {
    "submission": "Submission",
    "account": "Account",
    "producer": "Producer",
    "policy.gl": "General Liability",
    "policy.auto": "Auto",
    "policy.property": "Property",
    "policy.workers_comp": "Workers Comp",
    "policy.umbrella": "Umbrella",
    "policy.crime": "Crime",
    "policy.cyber": "Cyber",
    "policy.inland_marine": "Inland Marine",
    "policy.professional": "Professional",
    "policy.directors_officers": "D&O",
    "policy.employment_practices": "EPL",
    "policy.pollution": "Pollution",
    "vehicle": "Vehicles",
    "driver": "Drivers",
    "location": "Locations",
    "loss_payee": "Additional Interests",
    "additional_insured": "Additional Insureds",
    "prior_carrier": "Prior Carriers",
    "loss": "Loss History",
}

# Order in which tabs appear; namespaces not listed here append after, sorted.
TAB_ORDER: tuple[str, ...] = tuple(TAB_LABELS.keys())

# Repeatable namespaces use the RepeatablePane variant; everything else is a
# plain SectionTableView over filtered state.fields.
REPEATABLE_NAMESPACES: frozenset[str] = frozenset(
    {"vehicle", "driver", "location", "loss_payee", "additional_insured", "prior_carrier", "loss"}
)


def derive_tab_keys(state: dict | None) -> list[str]:
    """Return the ordered list of tab keys present in ``state``.

    Pure function — the main window calls it whenever state changes. The
    tab key vocabulary is documented in DECISION-MAP-gui-agent.md §1.
    """
    keys: set[str] = set()
    if state:
        fields_map = state.get("fields") or {}
        for tag in fields_map.keys():
            keys.add(_tab_key_for_tag(tag))
        for group in (state.get("repeatables") or {}).keys():
            keys.add(group)
    if not keys:
        # Always-on anchor tab for an empty state.
        keys.add("submission")

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
    ``tab_key``.
    """
    if not state:
        return 0
    if tab_key in REPEATABLE_NAMESPACES:
        items = (state.get("repeatables") or {}).get(tab_key, [])
        return len(items) if isinstance(items, list) else 0
    fields_map: dict = state.get("fields") or {}
    return sum(1 for tag in fields_map.keys() if _tab_key_for_tag(tag) == tab_key)


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
    return sum(
        1
        for tag, record in fields_map.items()
        if _tab_key_for_tag(tag) == tab_key and _record_is_low(record)
    )


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

        # Cache of QAction objects we need to enable/disable from
        # _refresh_run_controls (so Ctrl+E etc. follow the button gating).
        self._run_actions: dict[str, QAction] = {}
        # Cache of recent-clients QActions so we can rebuild on aboutToShow.
        self._recent_menu: QMenu | None = None
        # The "Get started" empty-state widget; we reuse one instance.
        self._welcome_pane: WelcomePane | None = None
        # Track widgets so toggles (View → Show PDF Preview, etc.) work.
        self._center_split: QSplitter | None = None
        self._outer_split: QSplitter | None = None
        self._bottom_widget: QWidget | None = None

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

    def _build_ui(self) -> None:
        # Top toolbar — client picker + file drop / browse.
        self._toolbar = QToolBar("Workspace", self)
        self._toolbar.setMovable(False)
        self._toolbar.setObjectName("WorkspaceToolbar")
        self.addToolBar(self._toolbar)

        pick_action = QAction("Pick client...", self)
        pick_action.triggered.connect(self._on_pick_client)
        self._toolbar.addAction(pick_action)

        new_client_action = QAction("New client...", self)
        new_client_action.triggered.connect(self._on_create_client)
        self._toolbar.addAction(new_client_action)

        self._toolbar.addSeparator()

        add_pdfs_action = QAction("Add PDFs...", self)
        add_pdfs_action.triggered.connect(self._on_add_pdfs)
        self._toolbar.addAction(add_pdfs_action)

        self._toolbar.addSeparator()

        # UX-pass #10: Extract / Begin Entry on the toolbar mirror the
        # bottom run-controls bar so the operator can find them wherever
        # their eyes land.
        toolbar_extract = QAction("Extract", self)
        toolbar_extract.setToolTip("Run extraction on queued PDFs (Ctrl+E).")
        toolbar_extract.triggered.connect(self._on_extract_clicked)
        self._toolbar.addAction(toolbar_extract)
        self._run_actions["toolbar_extract"] = toolbar_extract

        toolbar_begin = QAction("Begin Entry", self)
        toolbar_begin.setToolTip("Start entering approved fields into EPIC (Ctrl+Enter).")
        toolbar_begin.triggered.connect(self._on_begin_entry)
        self._toolbar.addAction(toolbar_begin)
        self._run_actions["toolbar_begin"] = toolbar_begin

        # Central widget: horizontal splitter — tabs on left, PDF preview on right.
        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        self._tabs.setTabsClosable(False)

        # FindBar sits above the tabs. Hidden until Ctrl+F is pressed.
        self._find_bar = FindBar(self)
        self._find_bar.query_changed.connect(self._on_find_query_changed)
        self._find_bar.closed.connect(self._on_find_bar_closed)

        # Container that holds find_bar + tabs together so the splitter
        # treats them as one unit.
        tabs_container = QWidget(self)
        tabs_container_layout = QVBoxLayout(tabs_container)
        tabs_container_layout.setContentsMargins(0, 0, 0, 0)
        tabs_container_layout.setSpacing(0)
        tabs_container_layout.addWidget(self._find_bar)
        tabs_container_layout.addWidget(self._tabs, 1)

        self._pdf_preview = PdfPreview(self)

        self._center_split = QSplitter(self)
        self._center_split.setOrientation(Qt.Orientation.Horizontal)
        self._center_split.setObjectName("CenterSplitter")
        self._center_split.addWidget(tabs_container)
        self._center_split.addWidget(self._pdf_preview)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 2)

        # Bottom: pending-PDFs queue + audit log + run controls.
        self._pending_pdfs_pane = PendingPdfsPane(self)
        self._pending_pdfs_pane.paths_changed.connect(self._refresh_pending_pdfs_state)

        pending_label = QLabel("Pending PDFs (drag here or use 'Add PDFs...')", self)
        pending_label.setStyleSheet("QLabel { color: #555; padding: 2px 4px; }")

        self._audit_log = AuditLogPane(self)
        self._audit_log.attach_logger("iga", level=logging.INFO)

        self._run_controls = RunControlsBar(self)
        self._run_controls.extract_clicked.connect(self._on_extract_clicked)
        self._run_controls.begin_entry_clicked.connect(self._on_begin_entry)
        self._run_controls.cancel_clicked.connect(self._on_cancel)
        self._run_controls.resume_clicked.connect(self._on_resume)

        self._bottom_widget = QWidget(self)
        bottom_layout = QVBoxLayout(self._bottom_widget)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(2)
        bottom_layout.addWidget(pending_label)
        bottom_layout.addWidget(self._pending_pdfs_pane)
        bottom_layout.addWidget(self._audit_log, 1)
        bottom_layout.addWidget(self._run_controls)

        # Outer vertical splitter.
        self._outer_split = QSplitter(self)
        self._outer_split.setOrientation(Qt.Orientation.Vertical)
        self._outer_split.setObjectName("OuterSplitter")
        self._outer_split.addWidget(self._center_split)
        self._outer_split.addWidget(self._bottom_widget)
        self._outer_split.setStretchFactor(0, 4)
        self._outer_split.setStretchFactor(1, 1)

        self.setCentralWidget(self._outer_split)

        # Status bar: text on the left, indeterminate-by-default progress
        # bar on the right (hidden when idle). The progress strip is the
        # operator's confirmation that an extraction or entry run is alive
        # — see gui-fix-2 #2.
        self._progress_bar = QProgressBar(self)
        self._progress_bar.setMaximumWidth(220)
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._progress_bar.setTextVisible(False)
        self.statusBar().addPermanentWidget(self._progress_bar)
        self.statusBar().showMessage("Ready.")

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

        self._act_show_pdf = QAction("Show PDF Preview", self)
        self._act_show_pdf.setCheckable(True)
        self._act_show_pdf.setChecked(True)
        self._act_show_pdf.toggled.connect(self._on_toggle_pdf_preview)
        view_menu.addAction(self._act_show_pdf)

        self._act_show_audit = QAction("Show Audit Log", self)
        self._act_show_audit.setCheckable(True)
        self._act_show_audit.setChecked(True)
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
        self._tabs.setFocus()

    def _on_find_query_changed(self, text: str) -> None:
        self._find_query = text
        self._reapply_filters_to_visible_tabs()

    def _on_low_confidence_toggled(self, checked: bool) -> None:
        self._low_confidence_filter = bool(checked)
        self._qsettings.setValue(_QS_VIEW_LOW_CONF_FILTER, self._low_confidence_filter)
        self._reapply_filters_to_visible_tabs()
        self._update_status_bar_idle()

    def _on_toggle_pdf_preview(self, checked: bool) -> None:
        if self._pdf_preview is not None:
            self._pdf_preview.setVisible(checked)
        self._qsettings.setValue(_QS_VIEW_PDF_VISIBLE, bool(checked))

    def _on_toggle_audit_log(self, checked: bool) -> None:
        if self._audit_log is not None:
            self._audit_log.setVisible(checked)
        self._qsettings.setValue(_QS_VIEW_AUDIT_VISIBLE, bool(checked))

    def _on_reset_layout(self) -> None:
        # Clear persisted geometry/state and force a sane default.
        for key in (
            _QS_GEOMETRY,
            _QS_WINDOW_STATE,
            _QS_CENTER_SPLITTER,
            _QS_OUTER_SPLITTER,
        ):
            self._qsettings.remove(key)
        self.resize(1400, 900)
        if self._center_split is not None:
            self._center_split.setSizes([900, 500])
        if self._outer_split is not None:
            self._outer_split.setSizes([700, 200])
        # Re-show panes that may have been hidden via the View toggles.
        self._act_show_pdf.setChecked(True)
        self._act_show_audit.setChecked(True)
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
            center = self._qsettings.value(_QS_CENTER_SPLITTER)
            if center and self._center_split is not None:
                self._center_split.restoreState(center)
            outer = self._qsettings.value(_QS_OUTER_SPLITTER)
            if outer and self._outer_split is not None:
                self._outer_split.restoreState(outer)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("could not restore layout from QSettings: %s", exc)

        # View-menu checkable states (visibility + low-confidence filter).
        pdf_visible = _to_bool(self._qsettings.value(_QS_VIEW_PDF_VISIBLE, True))
        audit_visible = _to_bool(self._qsettings.value(_QS_VIEW_AUDIT_VISIBLE, True))
        low_conf = _to_bool(self._qsettings.value(_QS_VIEW_LOW_CONF_FILTER, False))

        if hasattr(self, "_act_show_pdf"):
            self._act_show_pdf.setChecked(pdf_visible)
            self._pdf_preview.setVisible(pdf_visible)
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
            if self._center_split is not None:
                self._qsettings.setValue(_QS_CENTER_SPLITTER, self._center_split.saveState())
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
        """Honor ``--client NAME`` if it was supplied on the CLI."""
        name = self._settings.cli_initial_client
        if not name:
            return
        candidate = self._settings.working_library / name
        if candidate.exists():
            self._load_client(candidate)

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
        """Diff current tabs against target keys and apply the delta."""
        if self._client is None:
            # UX-pass #10: empty state. Show a welcome tab instead of nothing.
            self._tabs.clear()
            if self._welcome_pane is None:
                self._welcome_pane = WelcomePane(self)
            self._welcome_pane.setProperty("tab_key", "__welcome__")
            self._tabs.addTab(self._welcome_pane, "Get started")
            return
        # Two paths: if the tab set hasn't changed, do an in-place refresh
        # (preserves the operator's current tab + scroll position). If the
        # tab set itself changed (new namespace appeared after extraction)
        # we tear down and rebuild — there are at most ~22 tabs so the
        # rebuild is cheap and avoids subtle state-mismatch bugs.
        target_keys = derive_tab_keys(self._client.state)
        current_keys = [self._tabs.widget(i).property("tab_key") for i in range(self._tabs.count())]
        if current_keys == target_keys:
            for i, key in enumerate(target_keys):
                widget = self._tabs.widget(i)
                self._refresh_tab_widget(widget, key)
                self._tabs.setTabText(i, build_tab_label(self._client.state, key))
            self._reapply_filters_to_visible_tabs()
            return

        self._tabs.clear()
        for key in target_keys:
            widget = self._build_tab_widget(key)
            widget.setProperty("tab_key", key)
            self._tabs.addTab(widget, build_tab_label(self._client.state, key))
        self._reapply_filters_to_visible_tabs()

    def _build_tab_widget(self, key: str) -> QWidget:
        if key in REPEATABLE_NAMESPACES:
            return self._build_repeatable_tab(key)
        return self._build_singleton_tab(key)

    def _refresh_tab_widget(self, widget: QWidget, key: str) -> None:
        if key in REPEATABLE_NAMESPACES:
            pane = widget.findChild(RepeatablePane)
            if pane is not None and self._client is not None:
                items = (self._client.state.get("repeatables") or {}).get(key, [])
                pane.set_items(items)
            return
        # Singleton tabs.
        view = widget.findChild(SectionTableView)
        if view is None:
            return
        model = view.model()
        if isinstance(model, SectionTableModel):
            model.set_rows(self._build_singleton_rows(key))

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
            hint = QLabel(
                "No fields in this section yet.\nDrop PDFs onto the window or click 'Add PDFs...' to extract.",
                container,
            )
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("QLabel { color: #777; padding: 24px; }")
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
        if self._client is None:
            return
        self._pdf_preview.show_source(doc_id, page, client_inputs_dir=self._client.inputs_dir)

    def _on_field_focus_changed(self, domain_tag: str) -> None:
        if self._client is None:
            return
        fields_map = self._client.state.get("fields") or {}
        record = fields_map.get(domain_tag)
        if isinstance(record, dict):
            self._show_pdf_for_record(record)

    def _show_pdf_for_record(self, record: dict) -> None:
        if self._client is None:
            return
        sources = record.get("source") or []
        if not sources:
            self._pdf_preview.show_source(None, None, client_inputs_dir=self._client.inputs_dir)
            return
        first = sources[0]
        self._pdf_preview.show_source(
            first.get("doc_id"),
            int(first.get("page") or 1),
            client_inputs_dir=self._client.inputs_dir,
        )

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
            worker.emit_progress(f"Extracting {pdf_count} PDF(s)...")
            # extract.run_extraction signature: (client_name: str,
            # pdf_paths, force_opus=False, *, settings=None, ...). It
            # accepts no progress_callback — progress is logged via the
            # `iga.extract` logger, which the AuditLogPane subscribes to.
            return run_extraction(
                self._client.name,
                pdf_paths,
                force_opus=force_opus,
                settings=self._settings,
            )

        self._active_run_kind = "extract"
        self._run_controls.set_extracting(True)
        self._show_progress_indeterminate(f"Extracting {pdf_count} PDF(s)...")
        self._spawn_worker(
            task,
            on_finished=self._on_extraction_finished,
            on_failed=self._on_extraction_failed,
        )

    def _on_extraction_finished(self, _result: object) -> None:
        self._run_controls.set_extracting(False)
        self._active_run_kind = None
        self._hide_progress("Extraction complete.")
        if self._client is None:
            return
        self._client.state = _safe_state_load(self._client.path)
        self._audit_log.append_event("Extraction complete.")
        self._rebuild_tabs()
        self._refresh_run_controls()

    def _on_extraction_failed(self, message: str, technical: str) -> None:
        self._run_controls.set_extracting(False)
        self._active_run_kind = None
        self._hide_progress("Extraction didn't finish.")
        QMessageBox.warning(
            self,
            "Extraction didn't finish",
            f"{message}\n\nYour edits are saved. Try again, or check the audit log for details.",
        )
        _logger.error("extraction failed: %s | %s", message, technical)

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

        for key in ("toolbar_extract", "menu_extract"):
            act = self._run_actions.get(key)
            if act is not None:
                act.setEnabled(can_extract)
        for key in ("toolbar_begin", "menu_begin"):
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

        worker.progress.connect(self._on_worker_progress)
        worker.finished.connect(lambda result: (self._cleanup_worker(), on_finished(result)))
        worker.failed.connect(lambda msg, tech: (self._cleanup_worker(), on_failed(msg, tech)))
        thread.started.connect(worker.run)

        self._worker_thread = thread
        self._worker = worker
        thread.start()

    def _on_worker_progress(self, message: str, current: int, total: int) -> None:
        """Receive progress events from the active worker.

        Updates the status-bar text + progress bar, and mirrors the
        message into the audit log (the prior single-arg connection's
        behavior).
        """
        self._audit_log.append_event(message)
        if total > 0:
            self._show_progress_determinate(message, current, total)
        else:
            self._show_progress_indeterminate(message)

    # -- Progress strip ----------------------------------------------------

    def _show_progress_indeterminate(self, message: str) -> None:
        """Show the progress bar in indeterminate mode + status-bar message."""
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setVisible(True)
        self.statusBar().showMessage(message)

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
        self._update_status_bar_idle()

    def _cleanup_worker(self) -> None:
        if self._worker_thread is not None:
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
            self._worker_thread = None
        self._worker = None

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
        msg = (
            f"Client: {self._client.name} · "
            f"{total_fields} fields · {total_items} items · {saved_str}"
            f"{filter_tag}"
        )
        self.statusBar().showMessage(msg)

    # -- Filter reapply (UX-pass #8, #9) -----------------------------------

    def _reapply_filters_to_visible_tabs(self) -> None:
        """Apply the current find_query and low_confidence_only state to all tabs.

        Walks every tab's :class:`SectionTableView` (singleton tabs) and the
        nested view inside each :class:`RepeatablePane` (repeatable tabs),
        hides non-matching rows, and greys out tab labels whose visible-row
        count is zero.
        """
        for i in range(self._tabs.count()):
            widget = self._tabs.widget(i)
            if widget is None:
                continue
            key = widget.property("tab_key")
            if key in (None, "__welcome__"):
                continue
            visible = self._apply_filter_to_widget(widget)
            # Adjust tab text to reflect visibility — but only when the find
            # query is active (otherwise the badges are the source of truth).
            if self._find_query and visible == 0:
                self._tabs.tabBar().setTabTextColor(i, Qt.GlobalColor.gray)
            else:
                # Default text color = black (or the OS default).
                # Resetting to QColor() restores the default brush.
                from PySide6.QtGui import QColor
                self._tabs.tabBar().setTabTextColor(i, QColor())

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
        idx = self._tabs.currentIndex()
        if idx < 0:
            return
        widget = self._tabs.widget(idx)
        if widget is None:
            return
        key = widget.property("tab_key")
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
    def run(cls, *, debug: bool = False) -> int:
        """Construct ``QApplication``, show the main window, run the loop.

        Returns the exit code from ``QApplication.exec()``. Called from
        ``cli.py``'s ``main()``.
        """
        # Build settings + logging — safe to call repeatedly.
        from ..logger import configure_logging

        settings = config_module.load_settings(
            cli_overrides={"debug": debug} if debug else None,
        )
        configure_logging(settings)

        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName(config_module.APP_NAME)

        window = MainWindow(settings, debug=debug)
        window.show()
        return int(app.exec())

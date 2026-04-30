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
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
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
    FieldRow,
    SectionTableModel,
    SectionTableView,
)

__all__ = ["IgaApp", "MainWindow", "TAB_LABELS", "TAB_ORDER"]


_logger = get_logger("gui.main_window")


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

        self.setWindowTitle("IGA Marketing Master 2.0")
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        self._build_ui()
        self._handle_first_run_and_api_key()
        self._maybe_seed_initial_client()
        self._refresh_run_controls()
        self._refresh_pending_pdfs_state()

    # -- UI construction ---------------------------------------------------

    def _build_ui(self) -> None:
        # Top toolbar — client picker + file drop / browse.
        toolbar = QToolBar("Workspace", self)
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        pick_action = QAction("Pick client...", self)
        pick_action.triggered.connect(self._on_pick_client)
        toolbar.addAction(pick_action)

        new_client_action = QAction("New client...", self)
        new_client_action.triggered.connect(self._on_create_client)
        toolbar.addAction(new_client_action)

        toolbar.addSeparator()

        add_pdfs_action = QAction("Add PDFs...", self)
        add_pdfs_action.triggered.connect(self._on_add_pdfs)
        toolbar.addAction(add_pdfs_action)

        # Central widget: horizontal splitter — tabs on left, PDF preview on right.
        self._tabs = QTabWidget(self)
        self._tabs.setDocumentMode(True)
        self._tabs.setTabsClosable(False)

        self._pdf_preview = PdfPreview(self)

        center_split = QSplitter(self)
        center_split.setOrientation(Qt.Orientation.Horizontal)
        center_split.addWidget(self._tabs)
        center_split.addWidget(self._pdf_preview)
        center_split.setStretchFactor(0, 3)
        center_split.setStretchFactor(1, 2)

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

        bottom = QWidget(self)
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(2)
        bottom_layout.addWidget(pending_label)
        bottom_layout.addWidget(self._pending_pdfs_pane)
        bottom_layout.addWidget(self._audit_log, 1)
        bottom_layout.addWidget(self._run_controls)

        # Outer vertical splitter.
        outer_split = QSplitter(self)
        outer_split.setOrientation(Qt.Orientation.Vertical)
        outer_split.addWidget(center_split)
        outer_split.addWidget(bottom)
        outer_split.setStretchFactor(0, 4)
        outer_split.setStretchFactor(1, 1)

        self.setCentralWidget(outer_split)

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
        self._rebuild_tabs()
        self._refresh_run_controls()

        # Recover-interrupted-run flow.
        pending = state.get("pending_extraction") if isinstance(state, dict) else None
        if pending:
            self._show_recover_interrupted_run(pending)

    # -- Tab derivation -----------------------------------------------------

    def _rebuild_tabs(self) -> None:
        """Diff current tabs against target keys and apply the delta."""
        if self._client is None:
            self._tabs.clear()
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
            return

        self._tabs.clear()
        for key in target_keys:
            widget = self._build_tab_widget(key)
            widget.setProperty("tab_key", key)
            self._tabs.addTab(widget, label_for_tab_key(key))

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
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        items = []
        if self._client is not None:
            items = (self._client.state.get("repeatables") or {}).get(key, [])

        pane = RepeatablePane(group=key, items=items, parent=container)
        pane.source_clicked.connect(self._on_source_clicked)
        pane.conflict_clicked.connect(self._on_conflict_clicked)
        pane.commit_requested.connect(self._on_repeatable_commit)
        pane.item_added.connect(lambda: self._on_repeatable_add(key))
        pane.item_deleted.connect(lambda idx: self._on_repeatable_delete(key, idx))
        pane.focus_changed.connect(self._on_repeatable_focus)
        layout.addWidget(pane)
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
        from datetime import datetime, timezone

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
            return
        approved = self._count_approved_fields(self._client.state)
        self._run_controls.set_status_text(
            f"{self._client.name} — {approved} approved field(s)."
        )
        self._run_controls.set_approved_count(approved)

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
        self.statusBar().showMessage(idle_message)

    def _cleanup_worker(self) -> None:
        if self._worker_thread is not None:
            self._worker_thread.quit()
            self._worker_thread.wait(2000)
            self._worker_thread = None
        self._worker = None


def _safe_user() -> str:
    """``os.getlogin()`` with a fallback that won't raise on detached terminals."""
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


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

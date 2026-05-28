"""existing_clients_dialog.py — list-based picker for existing client folders.

Replaces the folder browser previously shown by ``_on_pick_client`` with a
labelled list. Each row shows the operator-meaningful name (the typed Insured
name) up front, with the folder name + last-updated timestamp as secondary
context, so an operator who didn't pick the folder name doesn't have to know
the on-disk naming.

Surface:

    dlg = ExistingClientsDialog(parent, working_library=Path(...))
    if dlg.exec() == QDialog.DialogCode.Accepted:
        client_path = dlg.chosen_path()  # Path | None
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

_log = logging.getLogger("iga.gui.existing_clients_dialog")

# Folders we never want to show in the list. ``__draft__`` is the live
# scratch client; ``snapshots`` / ``debug`` are runtime side effects of
# parent-level clients that occasionally end up at working-library root in
# malformed setups (defensive).
_HIDDEN_FOLDERS: frozenset[str] = frozenset({"__draft__", "snapshots", "debug"})


def _read_named_insured(client_path: Path) -> str:
    """Read ``insured.named_insured`` from ``state.json`` without instantiating State.

    Avoids a full State materialization for every client in the library —
    we only need one string for display.
    """
    state_file = client_path / "state.json"
    if not state_file.is_file():
        return ""
    try:
        with state_file.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return ""
    insured = raw.get("insured") if isinstance(raw, dict) else None
    if not isinstance(insured, dict):
        return ""
    return str(insured.get("named_insured") or "").strip()


def _read_updated_at(client_path: Path) -> str:
    """Best-effort read of ``state.json``'s ``updated_at`` (formatted short)."""
    state_file = client_path / "state.json"
    if not state_file.is_file():
        return ""
    try:
        with state_file.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        ts = raw.get("updated_at") if isinstance(raw, dict) else None
        if not isinstance(ts, str) or not ts:
            return ""
        # Normalize trailing 'Z' or timezone offset for parsing.
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return ts[:19]  # fall back to raw prefix
        return dt.strftime("%Y-%m-%d %H:%M")
    except OSError:
        return ""


def _list_clients(working_library: Path) -> list[dict]:
    """Scan ``working_library`` for client folders. Returns display records."""
    if not working_library.is_dir():
        return []
    out: list[dict] = []
    for entry in sorted(working_library.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir():
            continue
        if entry.name in _HIDDEN_FOLDERS or entry.name.startswith("."):
            continue
        # Require a state.json file; skip empty / non-client folders.
        if not (entry / "state.json").is_file():
            continue
        insured_name = _read_named_insured(entry)
        out.append({
            "path": entry,
            "folder_name": entry.name,
            "insured_name": insured_name,
            "updated_at": _read_updated_at(entry),
        })
    return out


class ExistingClientsDialog(QDialog):
    """Modal client picker. Surfaces ``insured.named_insured`` per row.

    On Accept, ``chosen_path()`` returns the selected client folder.
    Double-click on a row also accepts.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        working_library: Path,
    ) -> None:
        super().__init__(parent)
        self._working_library = working_library
        self._chosen: Path | None = None

        self.setWindowTitle("Select Existing Client")
        self.setModal(True)
        self.setMinimumSize(540, 420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(10)

        title = QLabel("Select an existing client")
        title.setStyleSheet("font-size: 15px; font-weight: 700; color: #0f172a;")
        layout.addWidget(title)

        sub = QLabel(f"From: {working_library}")
        sub.setStyleSheet("color: #64748b; font-size: 11px;")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        self._list = QListWidget(self)
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            "QListWidget { border: 1px solid #cbd5e1; border-radius: 4px;"
            " background: #ffffff; }"
            "QListWidget::item { padding: 8px 10px; border-bottom: 1px solid #e2e8f0; }"
            "QListWidget::item:selected { background: #dbeafe; color: #0f172a; }"
            "QListWidget::item:alternate { background: #f8fafc; }"
        )
        self._list.itemDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._list, 1)

        # Populate.
        clients = _list_clients(working_library)
        if not clients:
            empty = QLabel(
                "No existing clients in this working library.\n\n"
                "Use \"Import New Client\" to create one, or pick a different "
                "working library in Settings."
            )
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            empty.setStyleSheet("color: #64748b; padding: 30px;")
            empty.setWordWrap(True)
            # Replace the QListWidget with the empty-state label.
            layout.removeWidget(self._list)
            self._list.hide()
            layout.addWidget(empty, 1)
        else:
            for c in clients:
                self._add_row(c)

        # Button row.
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setMinimumWidth(110)
        cancel_btn.clicked.connect(self.reject)
        open_btn = QPushButton("Open")
        open_btn.setMinimumWidth(110)
        open_btn.setDefault(True)
        open_btn.setStyleSheet(
            "QPushButton { background: #2563eb; color: white; padding: 6px 14px;"
            " border-radius: 4px; font-weight: 600; }"
            "QPushButton:hover { background: #1d4ed8; }"
            "QPushButton:disabled { background: #94a3b8; }"
        )
        open_btn.clicked.connect(self._on_open)
        if not clients:
            open_btn.setEnabled(False)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(open_btn)
        layout.addLayout(btn_row)

    def _add_row(self, client: dict) -> None:
        insured = client["insured_name"] or "(no Insured name typed)"
        folder = client["folder_name"]
        updated = client["updated_at"]
        # Two-line display: primary = insured, secondary = folder + updated.
        secondary_bits: list[str] = [f"folder: {folder}"]
        if updated:
            secondary_bits.append(f"updated: {updated}")
        text = f"{insured}\n{' · '.join(secondary_bits)}"
        item = QListWidgetItem(text, self._list)
        item.setData(Qt.ItemDataRole.UserRole, client["path"])
        if not client["insured_name"]:
            # Subtle grey for clients without a typed Insured name yet
            # (still openable — operator can add the name in the Client tab).
            item.setForeground(Qt.GlobalColor.darkGray)

    def _selected_path(self) -> Path | None:
        item = self._list.currentItem()
        if item is None:
            return None
        path = item.data(Qt.ItemDataRole.UserRole)
        return path if isinstance(path, Path) else None

    def _on_open(self) -> None:
        path = self._selected_path()
        if path is None:
            return
        self._chosen = path
        self.accept()

    def _on_double_click(self, _item: QListWidgetItem) -> None:
        self._on_open()

    def chosen_path(self) -> Path | None:
        return self._chosen

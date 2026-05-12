"""annotator.py — Field Map domain_tag annotator GUI.

Run from the project root:
    python tools/field_map_annotator/annotator.py

Walks Tier 1 fields one at a time. For each, you see the field's EPIC
metadata, a proposed domain_tag, and four buttons:
    Accept   — write the proposed tag, mark verified, advance.
    Edit     — open the tag for editing, then Accept.
    Skip     — leave the field unmapped, advance.
    Back     — return to the previous field.

Auto-saves the Field Map after every Accept and Skip.
Resume position is tracked in docs/tier1-annotation-progress.json.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Local imports — annotator package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.field_map_annotator import registry, walker  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FIELD_MAP_PATH = PROJECT_ROOT / "Library" / "Epic Field Map.json"
PROGRESS_PATH = PROJECT_ROOT / "docs" / "tier1-annotation-progress.json"

# Auto-save batch threshold (write to disk every N actions). Set to 1 = save
# every action; raise if disk I/O becomes annoying.
SAVE_EVERY = 1


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def load_field_map() -> dict:
    return json.loads(FIELD_MAP_PATH.read_text(encoding="utf-8"))


def save_field_map(fm: dict) -> None:
    tmp = FIELD_MAP_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(FIELD_MAP_PATH)


def make_session_backup() -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = FIELD_MAP_PATH.with_suffix(f".json.{ts}.bak")
    shutil.copy2(FIELD_MAP_PATH, bak)
    return bak


def load_progress() -> dict:
    if PROGRESS_PATH.is_file():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_progress(p: dict) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(p, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


_QSS = """
QMainWindow { background: #f8fafc; }
QLabel#PathLabel    { color:#475569; font-size:11px; }
QLabel#FieldLabel   { color:#0f172a; font-size:18px; font-weight:bold; }
QLabel#MetaLabel    { color:#64748b; font-size:11px; }
QLabel#FormLabel    { color:#374151; font-size:12px; font-weight:bold; }
QLabel#StatusBadge  {
    color:#1e40af; background:#dbeafe; border:1px solid #93c5fd;
    border-radius:10px; padding:2px 10px; font-size:11px;
}
QLabel#StatusBadgeVerified { color:#065f46; background:#d1fae5; border-color:#6ee7b7; }
QLabel#StatusBadgeUnmapped { color:#92400e; background:#fef3c7; border-color:#fcd34d; }
QLabel#StatusBadgeOutOfScope { color:#475569; background:#e2e8f0; border-color:#cbd5e1; }
QFrame#FieldCard {
    background:white; border:1px solid #e2e8f0; border-radius:8px;
}
QLineEdit, QPlainTextEdit {
    background:white; border:1px solid #d1d5db; border-radius:5px;
    padding:6px 8px; font-size:13px; color:#0f172a;
}
QLineEdit:focus, QPlainTextEdit:focus { border-color:#2563eb; }
QLineEdit#TagEdit  { font-family:Consolas,monospace; font-size:13px; }
QLineEdit#TagEditValid   { border-color:#86efac; background:#f0fdf4; }
QLineEdit#TagEditInvalid { border-color:#fca5a5; background:#fef2f2; }
QPushButton#PrimaryBtn {
    background:#2563eb; color:white; border:none; border-radius:6px;
    padding:8px 22px; font-size:13px; font-weight:bold; min-width:100px;
}
QPushButton#PrimaryBtn:hover    { background:#1d4ed8; }
QPushButton#PrimaryBtn:disabled { background:#93c5fd; }
QPushButton#SecondaryBtn {
    background:white; color:#374151; border:1px solid #d1d5db;
    border-radius:6px; padding:7px 18px; font-size:13px; min-width:90px;
}
QPushButton#SecondaryBtn:hover    { background:#f9fafb; border-color:#9ca3af; }
QPushButton#SecondaryBtn:disabled { color:#cbd5e1; border-color:#e5e7eb; }
QPushButton#WarnBtn {
    background:#fef3c7; color:#92400e; border:1px solid #fcd34d;
    border-radius:6px; padding:7px 18px; font-size:13px; min-width:90px;
}
QPushButton#WarnBtn:hover { background:#fde68a; }
QPushButton#DangerBtn {
    background:#fee2e2; color:#991b1b; border:1px solid #fca5a5;
    border-radius:6px; padding:7px 18px; font-size:13px; min-width:90px;
}
QPushButton#DangerBtn:hover { background:#fecaca; }
QProgressBar {
    background:#e2e8f0; border:none; border-radius:3px;
    height:6px; text-align:center;
}
QProgressBar::chunk { background:#2563eb; border-radius:3px; }
"""


class AnnotatorWindow(QMainWindow):
    def __init__(self, fm: dict, fields: list[walker.FieldRef]) -> None:
        super().__init__()
        self.setWindowTitle("Field Map Annotator — IGA Marketing Master 2.0")
        self.resize(900, 720)
        self.setStyleSheet(_QSS)

        self._fm = fm
        self._fields = fields
        self._actions_since_save = 0
        self._progress = load_progress()
        self._idx = self._initial_index()

        self._build_ui()
        self._render()

    # -- Index handling -----------------------------------------------------

    def _initial_index(self) -> int:
        """Resume at the first field that's still actionable.

        Pass over: verified, out_of_scope, and session-skipped fields.
        """
        skipped = set(self._progress.get("skipped_field_ids", []) or [])
        for i, ref in enumerate(self._fields):
            if ref.field_id() in skipped:
                continue
            status = (ref.raw or {}).get("domain_tag_status")
            if status in {"verified", "out_of_scope"}:
                continue
            return i
        return 0

    # -- UI construction ----------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        # Top progress strip
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color:#475569; font-size:12px;")
        self._progress_bar = QProgressBar()
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setRange(0, max(1, len(self._fields)))
        outer.addWidget(self._progress_label)
        outer.addWidget(self._progress_bar)

        # Field card
        card = QFrame()
        card.setObjectName("FieldCard")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(20, 18, 20, 18)
        cl.setSpacing(10)

        self._path_label = QLabel("")
        self._path_label.setObjectName("PathLabel")
        self._path_label.setWordWrap(True)
        cl.addWidget(self._path_label)

        title_row = QHBoxLayout()
        title_row.setSpacing(10)
        self._field_label = QLabel("")
        self._field_label.setObjectName("FieldLabel")
        self._field_label.setWordWrap(True)
        title_row.addWidget(self._field_label, 1)
        self._status_badge = QLabel("")
        self._status_badge.setObjectName("StatusBadge")
        self._status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_row.addWidget(self._status_badge, 0, Qt.AlignmentFlag.AlignTop)
        cl.addLayout(title_row)

        self._meta_label = QLabel("")
        self._meta_label.setObjectName("MetaLabel")
        self._meta_label.setWordWrap(True)
        self._meta_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self._meta_label)

        cl.addSpacing(6)

        # Tag edit
        cl.addWidget(self._mk_form_label("Proposed domain_tag:"))
        self._tag_edit = QLineEdit()
        self._tag_edit.setObjectName("TagEdit")
        self._tag_edit.textChanged.connect(self._on_tag_changed)
        cl.addWidget(self._tag_edit)

        self._tag_validity_label = QLabel("")
        self._tag_validity_label.setStyleSheet("color:#dc2626; font-size:11px;")
        self._tag_validity_label.setWordWrap(True)
        cl.addWidget(self._tag_validity_label)

        # Aliases
        cl.addWidget(self._mk_form_label("Aliases (comma-separated, optional):"))
        self._aliases_edit = QLineEdit()
        self._aliases_edit.setPlaceholderText("policy.gl.alt_tag, policy.gl.legacy_tag")
        cl.addWidget(self._aliases_edit)

        # Notes
        cl.addWidget(self._mk_form_label("Notes for Claude (optional):"))
        self._notes_edit = QPlainTextEdit()
        self._notes_edit.setMaximumHeight(72)
        self._notes_edit.setPlaceholderText(
            "Hint for the extractor — e.g., 'truncate to 50 chars; EPIC validates length'"
        )
        cl.addWidget(self._notes_edit)

        outer.addWidget(card, 1)

        # Bottom button bar
        btn_bar = QHBoxLayout()
        btn_bar.setSpacing(8)
        self._back_btn = QPushButton("← Back")
        self._back_btn.setObjectName("SecondaryBtn")
        self._back_btn.clicked.connect(self._on_back)
        btn_bar.addWidget(self._back_btn)
        btn_bar.addStretch(1)

        self._skip_btn = QPushButton("Skip for Now")
        self._skip_btn.setObjectName("WarnBtn")
        self._skip_btn.setToolTip("Skip this field for this session. Resume later.")
        self._skip_btn.clicked.connect(self._on_skip)
        btn_bar.addWidget(self._skip_btn)

        self._oos_btn = QPushButton("Mark N/A")
        self._oos_btn.setObjectName("DangerBtn")
        self._oos_btn.setToolTip(
            "Permanently mark this field as out of scope — never useful from "
            "PDF extraction. Persists across sessions."
        )
        self._oos_btn.clicked.connect(self._on_mark_out_of_scope)
        btn_bar.addWidget(self._oos_btn)

        self._reset_btn = QPushButton("Reset to Proposed")
        self._reset_btn.setObjectName("SecondaryBtn")
        self._reset_btn.clicked.connect(self._on_reset)
        btn_bar.addWidget(self._reset_btn)

        self._accept_btn = QPushButton("Accept →")
        self._accept_btn.setObjectName("PrimaryBtn")
        self._accept_btn.clicked.connect(self._on_accept)
        btn_bar.addWidget(self._accept_btn)
        outer.addLayout(btn_bar)

    @staticmethod
    def _mk_form_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("FormLabel")
        return lbl

    # -- Rendering ----------------------------------------------------------

    def _current(self) -> walker.FieldRef | None:
        if 0 <= self._idx < len(self._fields):
            return self._fields[self._idx]
        return None

    def _render(self) -> None:
        ref = self._current()
        n = len(self._fields)
        if ref is None:
            self._path_label.setText("")
            self._field_label.setText("All fields reviewed.")
            self._meta_label.setText("")
            self._status_badge.setText("Done")
            self._tag_edit.setText("")
            self._tag_edit.setEnabled(False)
            self._aliases_edit.setEnabled(False)
            self._notes_edit.setEnabled(False)
            self._accept_btn.setEnabled(False)
            self._skip_btn.setEnabled(False)
            self._reset_btn.setEnabled(False)
            self._progress_bar.setValue(n)
            self._progress_label.setText(self._progress_summary())
            return

        # Re-propose if not yet proposed
        if ref.proposed_tag is None:
            ref.proposed_tag = walker.propose_tag(ref) or ""

        # Path
        self._path_label.setText(ref.display_path())
        # Title
        self._field_label.setText(ref.label or "(no label)")
        # Status badge
        existing_status = (ref.raw or {}).get("domain_tag_status") or "unmapped"
        if existing_status == "verified":
            self._status_badge.setText("Verified")
            self._status_badge.setObjectName("StatusBadgeVerified")
        elif existing_status == "out_of_scope":
            self._status_badge.setText("N/A")
            self._status_badge.setObjectName("StatusBadgeOutOfScope")
        elif existing_status == "unmapped":
            self._status_badge.setText("Unmapped")
            self._status_badge.setObjectName("StatusBadgeUnmapped")
        else:
            self._status_badge.setText(existing_status.capitalize())
            self._status_badge.setObjectName("StatusBadge")
        # Reapply stylesheet so objectName change takes effect
        self._status_badge.setStyleSheet(self._status_badge.styleSheet())

        # Metadata line
        meta_bits = [
            f"name = {ref.name!r}",
            f"type = {ref.type}",
        ]
        if ref.automation_id:
            meta_bits.append(f"automation_id = {ref.automation_id!r}")
        self._meta_label.setText("   ·   ".join(meta_bits))

        # Pre-fill from existing annotation if present, else from proposal
        existing_tag = (ref.raw or {}).get("domain_tag") if ref.raw else None
        existing_aliases = (ref.raw or {}).get("aliases") if ref.raw else []
        existing_notes = (ref.raw or {}).get("notes_for_claude") if ref.raw else None

        self._tag_edit.blockSignals(True)
        if existing_tag:
            self._tag_edit.setText(existing_tag)
        else:
            self._tag_edit.setText(ref.proposed_tag or "")
        self._tag_edit.blockSignals(False)
        self._tag_edit.setEnabled(True)

        self._aliases_edit.setText(", ".join(existing_aliases or []))
        self._aliases_edit.setEnabled(True)

        self._notes_edit.setPlainText(existing_notes or "")
        self._notes_edit.setEnabled(True)

        self._accept_btn.setEnabled(True)
        self._skip_btn.setEnabled(True)
        self._reset_btn.setEnabled(True)
        self._back_btn.setEnabled(self._idx > 0)

        self._progress_bar.setValue(self._idx)
        self._progress_label.setText(self._progress_summary())

        self._on_tag_changed()  # validate

    def _progress_summary(self) -> str:
        n = len(self._fields)
        verified = sum(
            1 for f in self._fields
            if (f.raw or {}).get("domain_tag_status") == "verified"
        )
        oos = sum(
            1 for f in self._fields
            if (f.raw or {}).get("domain_tag_status") == "out_of_scope"
        )
        skipped = len(set(self._progress.get("skipped_field_ids", []) or []))
        return (
            f"Field {min(self._idx+1, n)} of {n}    ·    "
            f"{verified} verified, {oos} N/A, {skipped} skipped"
        )

    # -- Validation ---------------------------------------------------------

    def _on_tag_changed(self) -> None:
        tag = self._tag_edit.text().strip()
        if not tag:
            self._tag_edit.setObjectName("TagEdit")
            self._tag_validity_label.setText("(empty — Accept will be disabled)")
            self._accept_btn.setEnabled(False)
        else:
            ok, reason = registry.validate_tag(tag)
            if ok:
                self._tag_edit.setObjectName("TagEditValid")
                ns = registry.find_namespace(tag)
                ns_text = f" → namespace: {ns.display}" if ns else ""
                self._tag_validity_label.setText(f"valid{ns_text}")
                self._tag_validity_label.setStyleSheet("color:#15803d; font-size:11px;")
                self._accept_btn.setEnabled(True)
            else:
                self._tag_edit.setObjectName("TagEditInvalid")
                self._tag_validity_label.setText(f"invalid: {reason}")
                self._tag_validity_label.setStyleSheet("color:#dc2626; font-size:11px;")
                self._accept_btn.setEnabled(False)
        # Re-apply styles after objectName change
        self._tag_edit.setStyleSheet(self._tag_edit.styleSheet())

    # -- Action handlers ----------------------------------------------------

    def _on_accept(self) -> None:
        ref = self._current()
        if ref is None:
            return
        tag = self._tag_edit.text().strip()
        ok, reason = registry.validate_tag(tag)
        if not ok:
            QMessageBox.warning(self, "Invalid tag", reason or "tag is invalid")
            return
        aliases = [
            a.strip() for a in self._aliases_edit.text().split(",")
            if a.strip()
        ]
        bad_aliases = [a for a in aliases if not registry.validate_tag(a)[0]]
        if bad_aliases:
            QMessageBox.warning(
                self,
                "Invalid alias",
                f"These aliases failed validation:\n  " + "\n  ".join(bad_aliases),
            )
            return
        notes = self._notes_edit.toPlainText().strip() or None

        walker.write_annotation(
            self._fm, ref,
            domain_tag=tag,
            aliases=aliases,
            notes_for_claude=notes,
            status="verified",
        )
        # Remove from skip-list if previously skipped
        skipped = set(self._progress.get("skipped_field_ids", []) or [])
        if ref.field_id() in skipped:
            skipped.discard(ref.field_id())
            self._progress["skipped_field_ids"] = sorted(skipped)
        self._save_after_action()
        self._idx += 1
        self._render()

    def _on_skip(self) -> None:
        ref = self._current()
        if ref is None:
            return
        skipped = set(self._progress.get("skipped_field_ids", []) or [])
        skipped.add(ref.field_id())
        self._progress["skipped_field_ids"] = sorted(skipped)
        self._save_after_action()
        self._idx += 1
        self._render()

    def _on_mark_out_of_scope(self) -> None:
        ref = self._current()
        if ref is None:
            return
        walker.write_annotation(
            self._fm, ref,
            domain_tag=None,
            aliases=[],
            notes_for_claude=None,
            status="out_of_scope",
        )
        # Remove from the session skip list — N/A is the durable answer.
        skipped = set(self._progress.get("skipped_field_ids", []) or [])
        if ref.field_id() in skipped:
            skipped.discard(ref.field_id())
            self._progress["skipped_field_ids"] = sorted(skipped)
        self._save_after_action()
        self._idx += 1
        self._render()

    def _on_back(self) -> None:
        if self._idx > 0:
            self._idx -= 1
            self._render()

    def _on_reset(self) -> None:
        ref = self._current()
        if ref is None:
            return
        proposed = walker.propose_tag(ref) or ""
        self._tag_edit.setText(proposed)
        self._aliases_edit.setText("")
        self._notes_edit.setPlainText("")

    # -- Persistence wrapper ------------------------------------------------

    def _save_after_action(self) -> None:
        self._actions_since_save += 1
        if self._actions_since_save >= SAVE_EVERY:
            save_field_map(self._fm)
            save_progress(self._progress)
            self._actions_since_save = 0

    def closeEvent(self, event) -> None:  # noqa: N802 — Qt API
        # Final flush
        save_field_map(self._fm)
        save_progress(self._progress)
        super().closeEvent(event)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    app = QApplication(sys.argv)

    # Backup before doing anything destructive
    bak = make_session_backup()
    print(f"[annotator] session backup: {bak.name}")

    fm = load_field_map()
    fields = walker.walk(fm)
    print(f"[annotator] in-scope fields: {len(fields)}")

    if not fields:
        QMessageBox.information(
            None, "Nothing to annotate",
            "No Tier 1 fields found. Check walker.TIER1_SCREENS or the Field Map.",
        )
        return 0

    win = AnnotatorWindow(fm, fields)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

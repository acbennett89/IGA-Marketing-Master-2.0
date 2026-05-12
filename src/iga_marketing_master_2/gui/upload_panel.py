"""upload_panel.py — Upload Documents panel for the Data Review page."""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .pending_pdfs_pane import PendingPdfsPane

__all__ = ["UploadPanel"]

_QSS = """
QWidget#UploadPanelInner {
    background: white;
}
QLabel#UploadSectionHdr {
    font-size: 15px;
    font-weight: bold;
    color: #0f172a;
}
QFrame#DropZone {
    background: #f8fafc;
    border: 2px dashed #cbd5e1;
    border-radius: 10px;
}
QFrame#DropZone:hover {
    border-color: #3b82f6;
    background: #eff6ff;
}
QLabel#DropIcon {
    font-size: 28px;
    color: #3b82f6;
}
QLabel#DropMainText {
    font-size: 13px;
    font-weight: bold;
    color: #374151;
}
QLabel#DropOrText {
    font-size: 11px;
    color: #9ca3af;
}
QPushButton#BrowseBtn {
    background: #2563eb;
    color: white;
    border: none;
    border-radius: 6px;
    padding: 6px 22px;
    font-size: 12px;
    font-weight: bold;
    min-width: 110px;
}
QPushButton#BrowseBtn:hover {
    background: #1d4ed8;
}
QLabel#DropFormatNote {
    font-size: 10px;
    color: #9ca3af;
}
QLabel#QueueLabel {
    font-size: 12px;
    font-weight: bold;
    color: #374151;
}
QPushButton#ClearBtn {
    background: transparent;
    border: none;
    color: #6b7280;
    font-size: 11px;
    padding: 2px 6px;
}
QPushButton#ClearBtn:hover {
    color: #ef4444;
    text-decoration: underline;
}
QLabel#SecureNote {
    font-size: 10px;
    color: #9ca3af;
}
"""


class UploadPanel(QWidget):
    """1. Upload Documents panel — polished wrap around :class:`PendingPdfsPane`.

    Signals:
        browse_clicked(): user clicked the Browse Files button.
        paths_changed():  forwarded from :class:`PendingPdfsPane`.
    """

    browse_clicked = Signal()
    paths_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("UploadPanel")
        self.setFixedWidth(264)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._build_ui()

    # -- Build UI ----------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        inner = QWidget()
        inner.setObjectName("UploadPanelInner")
        inner.setStyleSheet(_QSS)
        layout = QVBoxLayout(inner)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(14)

        # Section header
        hdr = QLabel("1. Upload Documents")
        hdr.setObjectName("UploadSectionHdr")
        layout.addWidget(hdr)

        # Drop zone
        drop_zone = QFrame()
        drop_zone.setObjectName("DropZone")
        dz = QVBoxLayout(drop_zone)
        dz.setContentsMargins(12, 20, 12, 16)
        dz.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        dz.setSpacing(6)

        icon = QLabel("⬆")
        icon.setObjectName("DropIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz.addWidget(icon)

        drag_label = QLabel("Drag and drop PDFs here")
        drag_label.setObjectName("DropMainText")
        drag_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        drag_label.setWordWrap(True)
        dz.addWidget(drag_label)

        or_sep = QLabel("or")
        or_sep.setObjectName("DropOrText")
        or_sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz.addWidget(or_sep)

        browse_btn = QPushButton("Browse Files")
        browse_btn.setObjectName("BrowseBtn")
        browse_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_btn.clicked.connect(self.browse_clicked)
        browse_btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        dz.addWidget(browse_btn, 0, Qt.AlignmentFlag.AlignHCenter)

        fmt_note = QLabel("PDF only · Max 32 MB per file")
        fmt_note.setObjectName("DropFormatNote")
        fmt_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        dz.addWidget(fmt_note)

        layout.addWidget(drop_zone)

        # Queue header row
        queue_row = QWidget()
        qr_layout = QHBoxLayout(queue_row)
        qr_layout.setContentsMargins(0, 0, 0, 0)
        qr_layout.setSpacing(6)

        self._queue_label = QLabel("Queued Files")
        self._queue_label.setObjectName("QueueLabel")
        qr_layout.addWidget(self._queue_label, 1)

        clear_btn = QPushButton("Clear All")
        clear_btn.setObjectName("ClearBtn")
        clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_btn.setFixedHeight(22)
        clear_btn.clicked.connect(self._on_clear_all)
        qr_layout.addWidget(clear_btn)

        layout.addWidget(queue_row)

        # Pending PDFs list
        self._pdfs_pane = PendingPdfsPane(self)
        self._pdfs_pane.setMaximumHeight(220)
        self._pdfs_pane.setMinimumHeight(60)
        self._pdfs_pane.paths_changed.connect(self._on_paths_changed)
        self._pdfs_pane.paths_changed.connect(self.paths_changed)
        layout.addWidget(self._pdfs_pane)

        # Security note
        sec_note = QLabel("Documents processed locally.")
        sec_note.setObjectName("SecureNote")
        layout.addWidget(sec_note)

        layout.addStretch(1)
        outer.addWidget(inner, 1)

    # -- Internal ----------------------------------------------------------

    def _on_paths_changed(self) -> None:
        count = len(self._pdfs_pane.paths())
        if count == 0:
            self._queue_label.setText("Queued Files")
        else:
            n = "file" if count == 1 else "files"
            self._queue_label.setText(f"Queued Files  ({count} {n})")

    def _on_clear_all(self) -> None:
        self._pdfs_pane.clear_queue()

    # -- PendingPdfsPane delegation ----------------------------------------

    def paths(self) -> list[Path]:
        return self._pdfs_pane.paths()

    def add_paths(self, paths: Iterable[Path]) -> int:
        return self._pdfs_pane.add_paths(paths)

    def clear_queue(self) -> None:
        self._pdfs_pane.clear_queue()

    @property
    def pdfs_pane(self) -> PendingPdfsPane:
        return self._pdfs_pane

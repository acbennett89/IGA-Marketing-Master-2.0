"""audit_log.py — read-only ``QPlainTextEdit``-based event log pane.

Per ARCHITECTURE §8.2: the bottom audit-log pane shows per-run events
(extraction start/end, conflicts surfaced, fields approved/edited/locked,
entry-session events). Read-only.

The pane also doubles as a ``logging.Handler`` target so the rest of the
codebase can stream INFO-level messages into the GUI without coupling
to PySide6.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QWidget

__all__ = ["AuditLogPane", "QtLogHandler"]


class QtLogHandler(logging.Handler, QObject):
    """``logging.Handler`` that emits a Qt signal per record.

    Bridges the standard ``logging`` machinery into Qt's signal/slot system.
    The connected slot must run on the main thread to avoid touching widgets
    from a background worker.
    """

    record_emitted = Signal(str, int)  # (formatted_message, log_level)

    def __init__(self, level: int = logging.INFO) -> None:
        logging.Handler.__init__(self, level=level)
        QObject.__init__(self)
        self.setFormatter(logging.Formatter("%(asctime)s %(name)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)
            return
        self.record_emitted.emit(text, record.levelno)


class AuditLogPane(QPlainTextEdit):
    """Read-only log pane that auto-scrolls and color-codes by level.

    Use :meth:`append_event` to add a hand-written event line; use
    :meth:`attach_logger` to subscribe to a Python logger tree (typically
    ``"iga"``).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        # Cap visible history at 2000 lines so a long-running session doesn't
        # eat memory. Old lines roll off the top — the rotating file log is
        # still the source of truth for forensics.
        self.setMaximumBlockCount(2000)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setStyleSheet(
            "QPlainTextEdit { font-family: Consolas, 'Courier New', monospace;"
            " font-size: 11px; background: #fafafa; }"
        )
        self._handler: QtLogHandler | None = None

    # -- Public surface -----------------------------------------------------

    def append_event(self, message: str, *, level: int = logging.INFO) -> None:
        """Append a single line, prefixed with a UTC timestamp."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        prefix = self._level_prefix(level)
        self.appendPlainText(f"{ts} {prefix} {message}")
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()

    def attach_logger(self, logger_name: str = "iga", level: int = logging.INFO) -> QtLogHandler:
        """Subscribe to ``logger_name`` so log records flow into the pane."""
        if self._handler is not None:
            self.detach_logger()
        handler = QtLogHandler(level=level)
        # QueuedConnection forces the slot onto the GUI thread even when the
        # log record was emitted from a worker thread (extraction / entry).
        # Touching widgets from a worker thread is undefined behavior in Qt.
        handler.record_emitted.connect(self._on_log_record, type=Qt.ConnectionType.QueuedConnection)
        logging.getLogger(logger_name).addHandler(handler)
        self._handler = handler
        return handler

    def detach_logger(self, logger_name: str = "iga") -> None:
        """Remove the previously attached log handler. Safe if none is attached."""
        if self._handler is None:
            return
        logging.getLogger(logger_name).removeHandler(self._handler)
        self._handler.close()
        self._handler = None

    # -- Helpers ------------------------------------------------------------

    def _on_log_record(self, message: str, level: int) -> None:
        prefix = self._level_prefix(level)
        self.appendPlainText(f"{prefix} {message}")
        self.ensureCursorVisible()

    @staticmethod
    def _level_prefix(level: int) -> str:
        if level >= logging.ERROR:
            return "[ERROR]"
        if level >= logging.WARNING:
            return "[WARN ]"
        if level >= logging.INFO:
            return "[INFO ]"
        return "[DEBUG]"

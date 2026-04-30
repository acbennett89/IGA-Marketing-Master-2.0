"""pdf_preview.py — ``QPdfView``-based preview pane.

Per ARCHITECTURE §8.2 the preview uses ``PySide6.QtPdf.QPdfDocument`` +
``PySide6.QtPdfWidgets.QPdfView``. ``QWebEngineView`` is explicitly
forbidden — it would pull a Chromium dependency we don't need.

The pane is robust against missing PDFs: if the file isn't where the GUI
expects it, the panel shows a fallback message instead of raising.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from ..logger import get_logger

__all__ = ["PdfPreview"]


_logger = get_logger("gui.pdf_preview")


class PdfPreview(QWidget):
    """Read-only PDF viewer that supports cell-driven deep-links.

    Use :meth:`open_path` to set the active file (or ``None`` to clear).
    Use :meth:`jump_to_page` to navigate to a specific page (1-indexed,
    matching the ``SourceRef.page`` convention from ARCHITECTURE §5).
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._current_path: Path | None = None
        self._document: QPdfDocument | None = None
        self._build_ui()

    # -- Public API ---------------------------------------------------------

    def open_path(self, path: Path | None) -> None:
        """Load a PDF from disk; if ``path`` is None or missing, show fallback.

        Idempotent — re-opening the currently loaded path is a no-op.
        """
        if path is None:
            self._show_fallback("No PDF selected.")
            self._current_path = None
            return
        path = Path(path)
        if path == self._current_path:
            return
        if not path.exists() or not path.is_file():
            _logger.info("PDF preview: missing file %s", path)
            self._show_fallback(f"PDF not found:\n{path.name}")
            self._current_path = None
            return
        try:
            doc = QPdfDocument(self)
            status = doc.load(str(path))
            # PySide6's QPdfDocument.load returns a Status enum on success;
            # the older signature returned None and exposed status() instead.
            # Accept either to keep this resilient to PySide6 version drift.
            if status is not None and hasattr(status, "value") and getattr(QPdfDocument.Status, "Ready", None) is not None:
                if status != QPdfDocument.Status.Ready:
                    raise RuntimeError(f"QPdfDocument load status={status}")
        except Exception as exc:  # noqa: BLE001
            _logger.warning("failed to load %s: %s", path, exc)
            self._show_fallback(f"Couldn't open {path.name}.\nFile may be corrupt or locked.")
            self._current_path = None
            return
        self._document = doc
        self._pdf_view.setDocument(doc)
        self._stack.setCurrentWidget(self._pdf_view)
        self._current_path = path
        _logger.debug("PDF preview opened: %s", path)

    def jump_to_page(self, page_one_indexed: int) -> None:
        """Navigate to ``page_one_indexed`` (1-based)."""
        if self._document is None:
            return
        zero_based = max(0, page_one_indexed - 1)
        try:
            navigator = self._pdf_view.pageNavigator()
            from PySide6.QtCore import QPointF

            navigator.jump(zero_based, QPointF(0.0, 0.0), 0.0)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("pageNavigator.jump failed: %s — using setCurrentPage fallback", exc)
            try:
                self._pdf_view.setCurrentPage(zero_based)
            except Exception:  # noqa: BLE001
                _logger.warning("Both jump strategies failed for page %d", page_one_indexed)

    def show_source(self, doc_id: str | None, page: int | None, *, client_inputs_dir: Path | None) -> None:
        """Convenience: load ``client_inputs_dir/doc_id`` and jump to ``page``.

        ``doc_id`` is the basename of the source PDF per ARCHITECTURE §14 #5.
        """
        if not doc_id or client_inputs_dir is None:
            self._show_fallback("No source PDF for this field.")
            return
        path = client_inputs_dir / doc_id
        self.open_path(path)
        if page:
            self.jump_to_page(page)

    def current_path(self) -> Path | None:
        return self._current_path

    # -- Layout helpers -----------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        self._fallback = QLabel("No PDF loaded.", self)
        self._fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fallback.setWordWrap(True)
        self._fallback.setStyleSheet(
            "QLabel { color: #777; padding: 32px; background: #fafafa; }"
        )
        self._stack.addWidget(self._fallback)

        self._pdf_view = QPdfView(self)
        try:
            self._pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        except AttributeError:
            # Older Qt versions only expose SinglePage.
            pass
        self._stack.addWidget(self._pdf_view)

        self._stack.setCurrentWidget(self._fallback)

    def _show_fallback(self, text: str) -> None:
        self._fallback.setText(text)
        self._stack.setCurrentWidget(self._fallback)

"""GUI subpackage — PySide6 review/edit interface.

This package owns every operator-facing widget. Modules outside the GUI
(`extract.py`, `enter.py`, `cli.py`) interact with it through the public
re-exports below.

See:
- ARCHITECTURE.md §8 — GUI contract.
- DECISION-MAP-gui-agent.md — internal layout and rulings.
"""

from __future__ import annotations

from .audit_log import AuditLogPane
from .main_window import IgaApp, MainWindow
from .operator_modal import (
    ApiKeyPromptDialog,
    ConflictResolutionDialog,
    DomainTagConfirmationDialog,
    EpicValidationPauseDialog,
    OperatorAction,
    OperatorActionRole,
    OperatorModal,
    PauseChoice,
    RecoverInterruptedRunDialog,
    SelectorUnresolvedPauseDialog,
)
from .pdf_preview import PdfPreview
from .repeatable_pane import RepeatablePane
from .run_controls import RunControlsBar
from .section_table import (
    CONFIDENCE_HIGH_THRESHOLD,
    CONFIDENCE_LOW_THRESHOLD,
    SectionTableModel,
    SectionTableView,
    confidence_color,
)

__all__ = [
    "ApiKeyPromptDialog",
    "AuditLogPane",
    "CONFIDENCE_HIGH_THRESHOLD",
    "CONFIDENCE_LOW_THRESHOLD",
    "ConflictResolutionDialog",
    "DomainTagConfirmationDialog",
    "EpicValidationPauseDialog",
    "IgaApp",
    "MainWindow",
    "OperatorAction",
    "OperatorActionRole",
    "OperatorModal",
    "PauseChoice",
    "PdfPreview",
    "RecoverInterruptedRunDialog",
    "RepeatablePane",
    "RunControlsBar",
    "SectionTableModel",
    "SectionTableView",
    "SelectorUnresolvedPauseDialog",
    "confidence_color",
]

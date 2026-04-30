"""operator_modal.py — mandatory operator-facing dialog base class.

Every error / conflict / pause / API-key prompt UI in the application is an
``OperatorModal`` subclass or instance. The 4-part structure (headline /
what_to_do / cancel_effect / optional technical_detail) is enforced by the
constructor. The technical fold-out is collapsed by default.

Reference:
- ARCHITECTURE.md §8.3 (mandatory base class) and §8.8 (operator-facing copy
  guidelines).
- PLAN-REVIEW.md amendment #5.

The six concrete subclasses required by the architecture:

- ``DomainTagConfirmationDialog`` — JIT tag proposal (§4.5).
- ``ConflictResolutionDialog`` — surfaces ``field.conflicts[]`` candidates,
  click-to-pick.
- ``EpicValidationPauseDialog`` — opened from ``on_pause_callback`` when EPIC
  rejects a value.
- ``SelectorUnresolvedPauseDialog`` — opened when the selector chain fails
  entirely.
- ``ApiKeyPromptDialog`` — first-run prompt + re-prompt on
  ``ClaudeAuthError``.
- ``RecoverInterruptedRunDialog`` — opens when ``state.pending_extraction``
  is not None on launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..logger import get_logger

__all__ = [
    "ApiKeyPromptDialog",
    "ConflictResolutionDialog",
    "DomainTagConfirmationDialog",
    "EpicValidationPauseDialog",
    "OperatorAction",
    "OperatorActionRole",
    "OperatorModal",
    "PauseChoice",
    "RecoverInterruptedRunDialog",
    "SelectorUnresolvedPauseDialog",
]


_logger = get_logger("gui.operator_modal")


class OperatorActionRole(str, Enum):
    """Visual style + Qt role for an action button.

    Maps to ``QDialogButtonBox::ButtonRole`` for accept/reject semantics
    and to a CSS class for primary/secondary/destructive styling.
    """

    PRIMARY = "primary"
    SECONDARY = "secondary"
    DESTRUCTIVE = "destructive"


@dataclass(slots=True, kw_only=True)
class OperatorAction:
    """A single button on an :class:`OperatorModal`.

    ``return_value`` is what :meth:`OperatorModal.exec_with_choice` returns
    when this action is selected. ``role`` controls visual emphasis.
    ``is_default`` marks the Enter-key default; only one action should set
    this. ``is_cancel`` marks the Escape-key cancel; only one action should
    set this.
    """

    label: str
    return_value: Any
    role: OperatorActionRole = OperatorActionRole.SECONDARY
    is_default: bool = False
    is_cancel: bool = False


class PauseChoice(str, Enum):
    """Return values used by the pause-dialog family.

    These are the contract values ``enter.run_entry_session``'s
    ``on_pause_callback`` returns. Defined here so callers in ``enter.py``
    can branch on the same vocabulary the GUI emits.
    """

    RESUME = "resume"
    SKIP = "skip"
    CANCEL = "cancel"


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class OperatorModal(QDialog):
    """Mandatory base class for every operator-facing dialog.

    The 4-part structure is enforced by this constructor. Subclasses extend
    by inserting an additional widget into ``content_layout`` (between the
    what-to-do bullet list and the cancel-effect line) via
    :meth:`add_extra_content`.

    Forbidden in ``headline`` and ``what_to_do`` (per ARCHITECTURE §8.8):
    selector strings, exception class names, file paths longer than the
    file's basename, internal variable names. This is enforced by
    convention; the constructor logs a warning if obviously technical
    substrings are detected.
    """

    _TECHNICAL_BAD_TOKENS: tuple[str, ...] = (
        "Traceback",
        "Exception",
        "Error:",
        "input.",
        "select.",
        "div.",
        "css=",
        "xpath=",
    )

    def __init__(
        self,
        parent: QWidget | None,
        *,
        headline: str,
        what_to_do: str,
        cancel_effect: str,
        technical_detail: str | None = None,
        actions: list[OperatorAction] | None = None,
    ) -> None:
        super().__init__(parent)
        self._validate_user_facing_text(headline, "headline")
        self._validate_user_facing_text(what_to_do, "what_to_do")

        self._headline = headline
        self._what_to_do = what_to_do
        self._cancel_effect = cancel_effect
        self._technical_detail = technical_detail
        self._actions: list[OperatorAction] = list(actions) if actions else self._default_actions()
        self._chosen_value: Any = None

        self.setModal(True)
        self.setWindowTitle(self._derive_window_title(headline))
        self.setMinimumWidth(540)

        self._build_layout()

    # -- Public surface ------------------------------------------------------

    def chosen_value(self) -> Any:
        """Return value associated with the action the operator pressed.

        ``None`` if the dialog was dismissed without pressing an action
        button (e.g., Escape with no ``is_cancel`` action defined).
        """
        return self._chosen_value

    def exec_with_choice(self) -> Any:
        """Execute the dialog and return the chosen action's ``return_value``.

        Convenience wrapper around ``exec()`` for the common case where the
        caller cares only about which button was pressed.
        """
        self.exec()
        return self._chosen_value

    def add_extra_content(self, widget: QWidget) -> None:
        """Insert a subclass-specific widget into the body, before the cancel-effect line."""
        # Index: headline (0), what_to_do (1), [extras...], cancel_effect, technical fold-out, buttons.
        # We track an insertion index so multiple add_extra_content calls preserve order.
        self._content_layout.insertWidget(self._content_extras_insert_index, widget)
        self._content_extras_insert_index += 1

    # -- Internal layout -----------------------------------------------------

    def _build_layout(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 16)
        outer.setSpacing(12)

        self._content_layout = outer

        # Headline.
        headline_label = QLabel(self._headline)
        headline_font = headline_label.font()
        headline_font.setPointSize(headline_font.pointSize() + 2)
        headline_font.setBold(True)
        headline_label.setFont(headline_font)
        headline_label.setWordWrap(True)
        outer.addWidget(headline_label)

        # What-to-do — render newlines as bullets if the caller pre-formatted them.
        what_label = QLabel(self._format_what_to_do(self._what_to_do))
        what_label.setWordWrap(True)
        what_label.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(what_label)

        # Mark where subclass extras get inserted.
        self._content_extras_insert_index = outer.count()

        # Cancel-effect (small italic).
        cancel_label = QLabel(f"<i>{self._cancel_effect}</i>")
        cancel_label.setWordWrap(True)
        cancel_label.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(cancel_label)

        # Technical fold-out.
        if self._technical_detail:
            outer.addWidget(self._build_technical_foldout(self._technical_detail))

        # Buttons.
        outer.addWidget(self._build_button_box())

    def _build_technical_foldout(self, detail: str) -> QWidget:
        container = QWidget(self)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)

        toggle = QToolButton(container)
        toggle.setText("Show technical details")
        toggle.setCheckable(True)
        toggle.setChecked(False)
        toggle.setStyleSheet("QToolButton { border: none; color: #555; }")
        toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        toggle.setArrowType(Qt.ArrowType.RightArrow)
        layout.addWidget(toggle)

        body = QPlainTextEdit(container)
        body.setReadOnly(True)
        body.setPlainText(detail)
        body.setVisible(False)
        body.setMaximumHeight(180)
        body.setStyleSheet(
            "QPlainTextEdit { background: #f6f6f6; color: #333;"
            " font-family: Consolas, 'Courier New', monospace; font-size: 11px; }"
        )
        layout.addWidget(body)

        def _on_toggled(checked: bool) -> None:
            toggle.setText("Hide technical details" if checked else "Show technical details")
            toggle.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow,
            )
            body.setVisible(checked)

        toggle.toggled.connect(_on_toggled)
        return container

    def _build_button_box(self) -> QWidget:
        box = QFrame(self)
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)
        layout.addStretch(1)

        for action in self._actions:
            button = QPushButton(action.label, box)
            button.setAutoDefault(False)
            if action.role is OperatorActionRole.PRIMARY:
                button.setDefault(True)
                button.setStyleSheet(
                    "QPushButton { font-weight: bold; padding: 6px 14px; }"
                )
            elif action.role is OperatorActionRole.DESTRUCTIVE:
                button.setStyleSheet(
                    "QPushButton { color: #a40000; padding: 6px 14px; }"
                )
            else:
                button.setStyleSheet("QPushButton { padding: 6px 14px; }")
            if action.is_default:
                button.setDefault(True)
            button.clicked.connect(self._make_action_handler(action))
            layout.addWidget(button)

        return box

    def _make_action_handler(self, action: OperatorAction):
        def handler() -> None:
            self._chosen_value = action.return_value
            if action.is_cancel:
                self.reject()
            else:
                self.accept()

        return handler

    # -- Helpers / validation -----------------------------------------------

    @classmethod
    def _validate_user_facing_text(cls, text: str, field_name: str) -> None:
        """Log a warning if obviously technical content sneaks into operator-facing copy."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        # Soft tripwire: the architecture forbids exception names, selector
        # strings, etc. in headlines/what_to_do. We log a warning instead of
        # raising so a botched copy edit never crashes the GUI mid-error.
        for token in cls._TECHNICAL_BAD_TOKENS:
            if token in text:
                _logger.warning(
                    "operator-facing %s contains technical token %r — move to technical_detail",
                    field_name,
                    token,
                )
                break

    @staticmethod
    def _derive_window_title(headline: str) -> str:
        """Derive a short window title from the first sentence/line of the headline."""
        line = headline.strip().splitlines()[0]
        if "." in line:
            line = line.split(".", 1)[0].strip() + "."
        return line[:80] if len(line) > 80 else line

    @staticmethod
    def _format_what_to_do(raw: str) -> str:
        """Render newline-delimited steps as an HTML bullet list.

        If ``raw`` has no newlines, return it as a single paragraph (no list).
        """
        lines = [line.strip("- \t") for line in raw.strip().splitlines() if line.strip()]
        if len(lines) <= 1:
            return raw
        items = "".join(f"<li>{line}</li>" for line in lines)
        return f"<ul style='margin-top:0; margin-bottom:0; padding-left:18px;'>{items}</ul>"

    @staticmethod
    def _default_actions() -> list[OperatorAction]:
        return [
            OperatorAction(label="OK", return_value=True, role=OperatorActionRole.PRIMARY, is_default=True),
            OperatorAction(label="Cancel", return_value=False, role=OperatorActionRole.SECONDARY, is_cancel=True),
        ]


# ---------------------------------------------------------------------------
# Subclasses — six concrete dialog flavors per ARCHITECTURE §8.3
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class DomainTagProposal:
    """Payload handed to :class:`DomainTagConfirmationDialog`.

    Mirrors the JIT-enrichment proposal the Extractor produces (§4.5).
    """

    proposed_tag: str
    field_label: str
    field_name_in_epic: str
    sample_value: str | None = None
    rationale: str | None = None  # Claude's "why this tag" if available.


class DomainTagConfirmationDialog(OperatorModal):
    """JIT domain_tag confirmation (ARCHITECTURE §3.5 / §4.5).

    Three actions: Accept / Edit / Reject. On Edit, the dialog reveals an
    inline editor and the result is returned in the dialog's
    :attr:`edited_tag` attribute.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        proposal: DomainTagProposal,
        technical_detail: str | None = None,
    ) -> None:
        self._proposal = proposal
        self.edited_tag: str | None = None

        headline = f"Confirm a new tag for {proposal.field_label!r}."
        what_to_do = (
            f"Claude proposed the tag {proposal.proposed_tag!r} for the EPIC field "
            f"{proposal.field_name_in_epic!r}.\n"
            "Click Accept if the tag is correct.\n"
            "Click Edit to fix it (e.g., wrong namespace).\n"
            "Click Reject to drop the proposal — the field stays untagged."
        )
        cancel_effect = "If you cancel, the tag is rejected and the field stays untagged."

        actions = [
            OperatorAction(
                label="Accept",
                return_value="accept",
                role=OperatorActionRole.PRIMARY,
                is_default=True,
            ),
            OperatorAction(
                label="Edit",
                return_value="edit",
                role=OperatorActionRole.SECONDARY,
            ),
            OperatorAction(
                label="Reject",
                return_value="reject",
                role=OperatorActionRole.DESTRUCTIVE,
                is_cancel=True,
            ),
        ]

        super().__init__(
            parent,
            headline=headline,
            what_to_do=what_to_do,
            cancel_effect=cancel_effect,
            technical_detail=technical_detail,
            actions=actions,
        )

        # Inline editor for the tag (revealed when Edit is chosen).
        self._tag_editor = QLineEdit(self)
        self._tag_editor.setText(proposal.proposed_tag)
        self._tag_editor.setVisible(False)
        self._tag_editor.setPlaceholderText("namespace.segment")
        self._tag_editor.editingFinished.connect(self._on_editor_finished)
        self.add_extra_content(self._tag_editor)

        if proposal.sample_value:
            sample_label = QLabel(
                f"Sample value: <code>{proposal.sample_value}</code>",
                self,
            )
            sample_label.setTextFormat(Qt.TextFormat.RichText)
            self.add_extra_content(sample_label)

    @property
    def proposal(self) -> DomainTagProposal:
        return self._proposal

    def _on_editor_finished(self) -> None:
        candidate = self._tag_editor.text().strip()
        self.edited_tag = candidate or None

    def exec_with_choice(self) -> str:
        choice = super().exec_with_choice()
        if choice == "edit":
            # Reveal editor and re-run modal until accept or reject.
            self._tag_editor.setVisible(True)
            self._chosen_value = None
            choice = super().exec_with_choice()
        return choice or "reject"


class ConflictResolutionDialog(OperatorModal):
    """Surfaces ``field.conflicts[]`` candidates; click-to-pick.

    The selected candidate becomes the new canonical value; previous canonical
    moves into the conflicts list. The dialog returns the index of the chosen
    candidate via :attr:`chosen_index`, or ``None`` to keep current.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        field_label: str,
        current_value: object,
        current_confidence: float,
        candidates: list[dict],  # ConflictCandidate-shaped dicts
        technical_detail: str | None = None,
    ) -> None:
        self._candidates = candidates
        self.chosen_index: int | None = None

        headline = f"Multiple values found for {field_label!r}."
        what_to_do = (
            "Pick the value you want EPIC to receive.\n"
            "Click a row to select it. The value not chosen stays available in history."
        )
        cancel_effect = "If you cancel, the current value is kept and conflicts remain unresolved."

        actions = [
            OperatorAction(
                label="Use selected",
                return_value="use_selected",
                role=OperatorActionRole.PRIMARY,
                is_default=True,
            ),
            OperatorAction(
                label="Keep current",
                return_value="keep_current",
                role=OperatorActionRole.SECONDARY,
                is_cancel=True,
            ),
        ]
        super().__init__(
            parent,
            headline=headline,
            what_to_do=what_to_do,
            cancel_effect=cancel_effect,
            technical_detail=technical_detail,
            actions=actions,
        )

        # Candidate list.
        list_widget = QListWidget(self)
        list_widget.addItem(
            QListWidgetItem(self._format_row(current_value, current_confidence, source=None, label="(current)"))
        )
        for cand in candidates:
            label = self._format_row(
                cand.get("value"),
                float(cand.get("confidence", 0.0)),
                source=cand.get("source"),
                model_used=cand.get("model_used"),
            )
            list_widget.addItem(QListWidgetItem(label))
        list_widget.setCurrentRow(0)
        list_widget.itemDoubleClicked.connect(lambda _: self.accept())
        self._list_widget = list_widget
        self.add_extra_content(list_widget)

    @staticmethod
    def _format_row(
        value: object,
        confidence: float,
        *,
        source: dict | None = None,
        model_used: str | None = None,
        label: str = "",
    ) -> str:
        confidence_pct = int(round(confidence * 100))
        parts = [f"{value!r}", f"{confidence_pct}% confidence"]
        if source:
            doc = source.get("doc_id", "?")
            page = source.get("page", "?")
            parts.append(f"{doc} p{page}")
        if model_used:
            parts.append(model_used)
        if label:
            parts.append(label)
        return "  |  ".join(parts)

    def exec_with_choice(self) -> dict | None:
        choice = super().exec_with_choice()
        if choice != "use_selected":
            return None
        # Index 0 is the current value; subtract 1 to map onto candidates.
        idx = self._list_widget.currentRow()
        if idx <= 0:
            return None
        self.chosen_index = idx - 1
        return self._candidates[self.chosen_index]


class EpicValidationPauseDialog(OperatorModal):
    """Pause modal raised when EPIC rejects a value (validation error).

    Returns one of :class:`PauseChoice` values via ``exec_with_choice``.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        field_label: str,
        attempted_value: object,
        screen_label: str,
        technical_detail: str | None = None,
    ) -> None:
        headline = f"EPIC didn't accept {attempted_value!r} for {field_label}."
        what_to_do = (
            f"Open the {field_label} field on the {screen_label} screen in EPIC.\n"
            "Pick or type a value EPIC will accept.\n"
            "Then click Resume."
        )
        cancel_effect = (
            "Your edits are saved. The entry run won't continue. "
            "You can resume later from this state."
        )
        actions = [
            OperatorAction(
                label="Resume",
                return_value=PauseChoice.RESUME,
                role=OperatorActionRole.PRIMARY,
                is_default=True,
            ),
            OperatorAction(
                label="Skip this field",
                return_value=PauseChoice.SKIP,
                role=OperatorActionRole.SECONDARY,
            ),
            OperatorAction(
                label="Cancel run",
                return_value=PauseChoice.CANCEL,
                role=OperatorActionRole.DESTRUCTIVE,
                is_cancel=True,
            ),
        ]
        super().__init__(
            parent,
            headline=headline,
            what_to_do=what_to_do,
            cancel_effect=cancel_effect,
            technical_detail=technical_detail,
            actions=actions,
        )


class SelectorUnresolvedPauseDialog(OperatorModal):
    """Pause modal raised when the selector chain fails entirely (selector drift).

    Same return vocabulary as :class:`EpicValidationPauseDialog`.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        field_label: str,
        screen_label: str,
        technical_detail: str | None = None,
    ) -> None:
        headline = f"Couldn't find the {field_label} field on the {screen_label} screen."
        what_to_do = (
            f"Make sure the {screen_label} screen is open in EPIC and the "
            f"{field_label} field is visible.\n"
            "If EPIC has moved or renamed the field, fix it manually in EPIC.\n"
            "Then click Resume — we'll re-read the screen and continue."
        )
        cancel_effect = (
            "Your edits are saved. The entry run won't continue. "
            "You can resume later from this state."
        )
        actions = [
            OperatorAction(
                label="Resume",
                return_value=PauseChoice.RESUME,
                role=OperatorActionRole.PRIMARY,
                is_default=True,
            ),
            OperatorAction(
                label="Skip this field",
                return_value=PauseChoice.SKIP,
                role=OperatorActionRole.SECONDARY,
            ),
            OperatorAction(
                label="Cancel run",
                return_value=PauseChoice.CANCEL,
                role=OperatorActionRole.DESTRUCTIVE,
                is_cancel=True,
            ),
        ]
        super().__init__(
            parent,
            headline=headline,
            what_to_do=what_to_do,
            cancel_effect=cancel_effect,
            technical_detail=technical_detail,
            actions=actions,
        )


class ApiKeyPromptDialog(OperatorModal):
    """First-run / re-prompt for the Anthropic API key.

    The key the operator types is exposed via :meth:`api_key` after
    ``exec_with_choice`` returns ``"save"``. The dialog never persists the
    key itself — the caller is responsible for invoking
    :func:`secret_store.set_anthropic_api_key`.
    """

    def __init__(
        self,
        parent: QWidget | None,
        *,
        is_reprompt: bool = False,
        technical_detail: str | None = None,
    ) -> None:
        if is_reprompt:
            headline = "Your Anthropic API key didn't work."
            what_to_do = (
                "Paste a valid Anthropic API key below and click Save.\n"
                "You can get a key from console.anthropic.com."
            )
        else:
            headline = "Add your Anthropic API key."
            what_to_do = (
                "Paste your Anthropic API key below and click Save.\n"
                "The key is stored in Windows Credential Manager — you only do this once."
            )
        cancel_effect = (
            "If you cancel, you can still review extracted data, "
            "but you can't run new extractions until a key is set."
        )

        actions = [
            OperatorAction(
                label="Save",
                return_value="save",
                role=OperatorActionRole.PRIMARY,
                is_default=True,
            ),
            OperatorAction(
                label="Cancel",
                return_value="cancel",
                role=OperatorActionRole.SECONDARY,
                is_cancel=True,
            ),
        ]
        super().__init__(
            parent,
            headline=headline,
            what_to_do=what_to_do,
            cancel_effect=cancel_effect,
            technical_detail=technical_detail,
            actions=actions,
        )

        self._key_editor = QLineEdit(self)
        self._key_editor.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_editor.setPlaceholderText("sk-ant-...")
        self.add_extra_content(self._key_editor)

    def api_key(self) -> str:
        return self._key_editor.text().strip()


class RecoverInterruptedRunDialog(OperatorModal):
    """Surfaces a ``state.pending_extraction`` block on launch (§5.5)."""

    def __init__(
        self,
        parent: QWidget | None,
        *,
        completed_count: int,
        total_count: int,
        remaining_basenames: list[str],
        started_at: str,
        technical_detail: str | None = None,
    ) -> None:
        headline = (
            f"Last run was interrupted after {completed_count} of {total_count} PDFs."
        )
        remaining_str = "\n".join(f"- {name}" for name in remaining_basenames[:8])
        if len(remaining_basenames) > 8:
            remaining_str += f"\n- (+{len(remaining_basenames) - 8} more)"
        what_to_do = (
            "Click Resume to continue from where the run left off.\n"
            "Click Discard to throw away the in-progress run and start fresh.\n"
            f"Remaining PDFs:\n{remaining_str}\n"
            f"Run started {started_at}."
        )
        cancel_effect = "If you cancel, nothing changes; the run stays paused."
        actions = [
            OperatorAction(
                label="Resume",
                return_value="resume",
                role=OperatorActionRole.PRIMARY,
                is_default=True,
            ),
            OperatorAction(
                label="Discard",
                return_value="discard",
                role=OperatorActionRole.DESTRUCTIVE,
            ),
            OperatorAction(
                label="Decide later",
                return_value="cancel",
                role=OperatorActionRole.SECONDARY,
                is_cancel=True,
            ),
        ]
        super().__init__(
            parent,
            headline=headline,
            what_to_do=what_to_do,
            cancel_effect=cancel_effect,
            technical_detail=technical_detail,
            actions=actions,
        )

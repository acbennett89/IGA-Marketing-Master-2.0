"""runtime.py — per-run context shared between the GUI and step files.

The entry walker spawns a worker thread that calls the step files
(``step_general_liability.run``, ``step_inland_marine.run``, ...). Each
step needs to:

* Periodically check whether the user clicked Cancel (hard-abort).
* Halt for human review on every validation error EPIC surfaces, showing
  the operator a Proceed / Cancel dialog with a screenshot + the script's
  intent.
* Write screenshots + structured error info to a per-run artifacts folder
  the user can email at the end of the run.

Passing all that through each step's ``run()`` signature would touch
every step file and every call site. Instead we publish the context on
this module and the step files read it via :func:`get_runtime`. The GUI
calls :func:`set_runtime` before the entry walk starts and
:func:`clear_runtime` when it finishes.

The runtime lives in module state, not thread-local, because the GUI's
worker spawns a single thread per run and step files all execute on that
thread. If we ever go multi-threaded inside a run, swap module state for
``threading.local`` here and nothing else changes.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional


ValidationHaltChoice = Literal["proceed", "cancel"]
"""Return value of the validation-halt callback."""


class EntryCancelled(Exception):
    """Raised when the user clicks Cancel on the busy dialog or a halt prompt.

    Step files don't catch this — it propagates back up to the worker so the
    run terminates cleanly without trying to finish the current row.
    """


@dataclass(slots=True, kw_only=True)
class ValidationFinding:
    """One validation issue detected by ``validation_check.check``."""

    error_text: str
    """The exact text EPIC displayed (e.g. ``Required information is missing.``)."""

    surface: Literal["portal_modal", "message_box", "inline_marker", "unknown"]
    """Where on the page the error was found."""

    screen_code: str
    """EPIC screen code from the footer (e.g. ``CHM-EFADDCOV``); ``""`` if absent."""

    expecting: str
    """What the script was about to do at this checkpoint (operator-facing)."""

    screenshot_path: Optional[Path]
    """Path of the captured screenshot, or ``None`` if capture failed."""

    sequence: int
    """1-based order this finding was raised in the run."""


@dataclass(slots=True, kw_only=True)
class EntryRuntime:
    """Per-run shared context."""

    run_id: str
    artifacts_dir: Path
    """Run-specific folder under ``<Working Library>/<Client>/run_artifacts/``.

    ``validation_check`` writes screenshots + JSON here, the email sender
    zips it, and the GUI deletes it after a successful send."""

    cancel_event: threading.Event
    """Set by the GUI Cancel button; step files raise :class:`EntryCancelled`
    next time they call :func:`check_cancel`."""

    on_validation_halt: Callable[[ValidationFinding], ValidationHaltChoice]
    """GUI callback that pops the modal halt dialog and returns the user's
    choice. The step file does *not* dismiss EPIC's modal first — the user
    decides whether to fix-and-proceed (modal stays up so they can read it)
    or cancel (run aborts)."""

    on_status: Callable[[str], None] = field(default=lambda _msg: None)
    """Lightweight status reporter for the busy dialog text line."""

    on_inline_dup_prompt: Callable[[str, str], ValidationHaltChoice] = field(
        default=lambda _name, _panel_text: "cancel"
    )
    """GUI callback for EPIC's *inline* duplicate panel (the one that
    appears mid-form on the Add Account screen, before Save).

    Signature: ``(target_account_name, panel_text) -> "proceed" | "cancel"``.

    Differs from :attr:`on_validation_halt` in that the operator handles the
    EPIC dismiss themselves (in the browser) — our popup is purely a "ready
    to continue?" gate. The step file does **not** click Dismiss before
    calling this; the prompt instructs the operator to do so and then press
    OK in our dialog. Default returns ``"cancel"`` so a no-GUI test context
    safely backs out instead of creating duplicates."""

    findings: list[ValidationFinding] = field(default_factory=list)
    """Accumulated findings, in order. The email sender includes a summary."""


# Module-level slot. None means no entry run is active.
_runtime: EntryRuntime | None = None
_runtime_lock = threading.Lock()


def set_runtime(runtime: EntryRuntime) -> None:
    """Publish the runtime context. Called by the GUI before step files run."""
    global _runtime
    with _runtime_lock:
        _runtime = runtime


def clear_runtime() -> None:
    """Drop the runtime when the run ends. Subsequent ``get_runtime`` returns None."""
    global _runtime
    with _runtime_lock:
        _runtime = None


def get_runtime() -> EntryRuntime | None:
    """Return the active runtime, or ``None`` if no entry run is in progress.

    Step files call this; when ``None`` they fall back to old non-interactive
    behavior so the existing standalone ``test_*.py`` scripts keep working.
    """
    return _runtime


def check_cancel() -> None:
    """Raise :class:`EntryCancelled` if the user clicked Cancel.

    Safe to call from anywhere in a step file. No-op when no runtime is set.
    """
    rt = _runtime
    if rt is not None and rt.cancel_event.is_set():
        raise EntryCancelled("user cancelled run")

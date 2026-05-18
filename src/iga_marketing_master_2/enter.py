"""enter.py - field-by-field EPIC data entry driver.

Reads the approved ``state.json`` and walks the operator from their current
EPIC screen field-by-field through every ``status="approved"`` value.

Selector resolution chain (per field, ARCHITECTURE §7.1): handled by
:func:`epic_session.resolve_locator` -- ``data-automation-id`` -> ``name``
-> ``get_by_label`` fallback.

Pre-flight selector smoke check (Amendment #6, ARCHITECTURE §7.3): at the
start of every entry session, walks only the screens we are about to touch
(derived from approved fields' ``screen_code`` set). Surfaces a single
non-blocking yellow banner if any selector looks stale, but never blocks.

Pause-for-human protocol (ARCHITECTURE §7.4): on selector-unresolved or
EPIC validation rejection, build a :class:`PendingPause`, save it durably,
call ``on_pause_callback``, await its return. On ``"resume"``, re-read the
DOM via ``locator.input_value()`` and treat that value as canonical.

``--debug`` discipline (ARCHITECTURE §10): when enabled, Playwright tracing
is started in :mod:`epic_session.launch_with_persistent_context` and stopped
here at session end; trace.zip plus per-action screenshots land under
``<client>/debug/playwright/<run_id>/``. Auto-prune to last 5 run_ids.

This module imports ONLY: ``field_map``, ``state`` (when state.py lands -
the contract today is duck-typed), ``config``, ``logger``, and
``epic_session``. **It does NOT import ``gui``** -- the GUI calls into
:func:`run_entry_session` and supplies an ``on_pause_callback``.
"""

from __future__ import annotations

import shutil
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import epic_session
from .config import Settings
from .logger import get_logger

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Locator, Page

    from .field_map import FieldEntry, FieldMap


__all__ = [
    "DEBUG_RUN_RETENTION",
    "EntryError",
    "EntryResult",
    "PauseInfo",
    "PauseResolution",
    "run_entry_session",
]


_LOG = get_logger("enter")


# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #

#: Mirror of ARCHITECTURE Appendix A constant: keep last N run_ids per client
#: under ``<client>/debug/playwright/``.
DEBUG_RUN_RETENTION: int = 5

#: Read-back debounce after a fill before validating, milliseconds.
_VALIDATE_DEBOUNCE_MS: int = 250


# --------------------------------------------------------------------------- #
# Public types
# --------------------------------------------------------------------------- #


PauseResolution = Literal["resume", "skip", "abort"]
"""Return contract for the GUI's ``on_pause_callback``."""


class EntryError(Exception):
    """Base for any error raised by :func:`run_entry_session`."""


@dataclass(slots=True, kw_only=True)
class PauseInfo:
    """Operator-readable pause description handed to the GUI callback.

    Mirrors ARCHITECTURE §5.1's ``PendingPause`` shape but is the in-memory
    object the callback sees. The state-side ``PendingPause`` (when state.py
    lands) is constructed from this via ``state.set_pending_pause``.
    """

    run_id: str
    paused_at: str  # ISO-8601 UTC
    domain_tag: str
    repeatable_group: str | None
    repeatable_index: int | None
    screen_code: str | None
    reason_code: Literal[
        "validation_rejected",
        "selector_unresolved",
        "user_requested",
        "exception",
    ]
    reason_message: str  # operator-readable, no jargon
    technical_detail: str  # selector chain, exception text, etc.
    field_label: str
    field_name: str


@dataclass(slots=True, kw_only=True)
class EntryResult:
    """Returned by :func:`run_entry_session` on session end."""

    run_id: str
    fields_entered: int
    fields_skipped: int
    fields_paused: int
    outcome: Literal["completed", "paused", "aborted"]
    pause_reason: PauseInfo | None = None
    smoke_stale_count: int = 0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Internal types
# --------------------------------------------------------------------------- #


@dataclass(slots=True, kw_only=True)
class _Unit:
    """One field-to-enter, normalized for the entry walk.

    Repeatable fields are flattened into one ``_Unit`` per (group, index, tag)
    so iteration is a single linear pass.
    """

    domain_tag: str
    value: Any
    repeatable_group: str | None
    repeatable_index: int | None
    record: Any
    """The underlying ``FieldRecord``-shaped object so we can mutate
    ``status`` / ``history`` after a successful enter."""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def run_entry_session(
    state: Any,
    field_map: "FieldMap",
    browser_context: "BrowserContext",
    *,
    on_pause_callback: Callable[[PauseInfo], PauseResolution],
    on_progress_callback: Callable[[str, int, int], None] | None = None,
    settings: Settings | None = None,
    client_path: Path | None = None,
    save_state_callback: Callable[[Any], None] | None = None,
    screen_container: "Locator | None" = None,
    include_namespaces: list[str] | None = None,
) -> EntryResult:
    """Walk approved fields and enter them into EPIC.

    The contract follows ARCHITECTURE §7.2 verbatim. The state-agent has not
    yet landed at the time of this writing, so the ``state`` argument is
    typed as ``Any`` and accessed via duck-typed reads:

    - ``state.fields`` -- mapping of ``domain_tag -> FieldRecord``.
    - ``state.repeatables`` -- mapping of ``group -> list[RepeatableItem]``.
    - ``state.set_pending_pause(state, pause)`` -- module-level function on
      ``state.py``; resolved at runtime via ``getattr``.
    - ``save_state_callback`` -- the caller may inject a saver to keep
      ``enter`` decoupled from ``state.save_atomic``'s exact import path.

    :param state: The canonical State object (per ARCHITECTURE §5.1).
    :param field_map: Loaded :class:`FieldMap`.
    :param browser_context: Live Playwright BrowserContext; the first page
        is used as the active page.
    :param on_pause_callback: GUI-supplied closure invoked when the entry
        loop must pause for human intervention. Returns one of
        ``"resume" | "skip" | "abort"``.
    :param on_progress_callback: Optional ``(domain_tag, completed, total)``
        progress reporter.
    :param settings: Runtime :class:`Settings`. ``debug`` enables tracing
        teardown into ``client_path/debug/playwright/<run_id>/``.
    :param client_path: Required when ``settings.debug`` is True so the
        trace file has a home; ignored otherwise.
    :param save_state_callback: Optional state-saver; defaults to a no-op.
    :param screen_container: Optional Playwright Locator scoping the active
        screen container (advanced; usually None - the page is the scope).
    """
    if settings is None:
        settings = Settings()  # type: ignore[call-arg]
    save = save_state_callback or _noop_save
    run_id = uuid.uuid4().hex

    page = _first_page(browser_context)
    debug_dir = _resolve_debug_dir(settings, client_path, run_id)

    _LOG.info(
        "enter.run_started",
        extra={
            "run_id": run_id,
            "debug": settings.debug,
            "debug_dir": str(debug_dir) if debug_dir else None,
        },
    )

    units = list(_collect_approved_units(state, include_namespaces=include_namespaces))
    total = len(units)

    if total == 0:
        _LOG.info("enter.no_approved_fields", extra={"run_id": run_id})
        result = EntryResult(
            run_id=run_id,
            fields_entered=0,
            fields_skipped=0,
            fields_paused=0,
            outcome="completed",
        )
        _stop_tracing(browser_context, debug_dir, settings)
        _append_run_history(state, run_id, result)
        save(state)
        return result

    # Pre-flight smoke (non-blocking) ------------------------------------
    # We run a fast pass over only the screens this run will actually touch
    # to surface selector drift before the operator commits. Non-blocking on
    # purpose: a stale selector might still resolve via the label fallback,
    # and refusing to start because of yellow-flag drift is too aggressive.
    tags = [u.domain_tag for u in units]
    smoke_report = epic_session.preflight_selector_smoke(
        page, tags, field_map, screen_container=screen_container
    )

    warnings: list[str] = []
    if smoke_report.has_stale:
        warning = (
            f"{smoke_report.stale_count} selector(s) look stale - drift may "
            "slow this run."
        )
        warnings.append(warning)
        _LOG.warning(
            "enter.preflight_stale",
            extra={
                "run_id": run_id,
                "stale_count": smoke_report.stale_count,
                "unresolved_count": smoke_report.unresolved_count,
            },
        )

    # Main walk ----------------------------------------------------------
    fields_entered = 0
    fields_skipped = 0
    fields_paused = 0
    outcome: Literal["completed", "paused", "aborted"] = "completed"
    pause_reason: PauseInfo | None = None
    aborted = False

    for index, unit in enumerate(units):
        if on_progress_callback is not None:
            try:
                on_progress_callback(unit.domain_tag, index, total)
            except Exception:  # noqa: BLE001 - progress is advisory
                pass

        # Look up the FieldEntry for selectors / type / enum_values.
        from . import field_map as _fm

        entry = _fm.lookup_by_domain_tag(field_map, unit.domain_tag)
        if entry is None:
            _LOG.warning(
                "enter.skip_unknown_domain_tag",
                extra={"run_id": run_id, "domain_tag": unit.domain_tag},
            )
            fields_skipped += 1
            continue

        try:
            locator, strategy = epic_session.resolve_locator(
                page, entry, screen_container=screen_container
            )
        except epic_session.SelectorUnresolvedError as exc:
            pause = _build_pause(
                run_id=run_id,
                unit=unit,
                entry=entry,
                reason_code="selector_unresolved",
                reason_message=(
                    f"EPIC didn't show the {entry.label!r} field where we "
                    "expected it. Make sure you're on the right screen, then "
                    "click Resume."
                ),
                technical_detail=str(exc),
            )
            resolution = _handle_pause(
                state=state,
                pause=pause,
                on_pause_callback=on_pause_callback,
                save=save,
                page=page,
                locator=None,
                unit=unit,
                entry=entry,
            )
            if resolution == "abort":
                aborted = True
                outcome = "aborted"
                pause_reason = pause
                fields_paused += 1
                break
            if resolution == "skip":
                fields_skipped += 1
                fields_paused += 1
                continue
            # resume: we don't have a locator, so the human did the work in
            # EPIC. We can't read DOM without a locator, so trust that the
            # human did the entry, mark approved (un-entered) and continue.
            fields_paused += 1
            continue

        # Capture the pre-fill screenshot for debug runs.
        _maybe_screenshot(page, debug_dir, settings, index, unit, "before")

        # Fill the field.
        try:
            _fill_field(locator, entry, unit.value)
        except Exception as exc:  # noqa: BLE001 - any fill failure paused
            pause = _build_pause(
                run_id=run_id,
                unit=unit,
                entry=entry,
                reason_code="exception",
                reason_message=(
                    f"Couldn't enter the {entry.label!r} field. Try filling "
                    "it manually in EPIC, then click Resume."
                ),
                technical_detail=f"{type(exc).__name__}: {exc} (strategy={strategy})",
            )
            resolution = _handle_pause(
                state=state,
                pause=pause,
                on_pause_callback=on_pause_callback,
                save=save,
                page=page,
                locator=locator,
                unit=unit,
                entry=entry,
            )
            if resolution == "abort":
                aborted = True
                outcome = "aborted"
                pause_reason = pause
                fields_paused += 1
                break
            if resolution == "skip":
                fields_skipped += 1
                fields_paused += 1
                continue
            fields_entered += 1
            fields_paused += 1
            continue

        _debounce(_VALIDATE_DEBOUNCE_MS)

        # Validate read-back.
        validation = _validate_after_fill(
            page, locator, entry, intended=unit.value, screen_container=screen_container
        )
        if validation.ok:
            _mark_entered(state, unit, entry, run_id, strategy)
            save(state)
            _maybe_screenshot(page, debug_dir, settings, index, unit, "after")
            fields_entered += 1
            continue

        # Validation failed -> pause.
        pause = _build_pause(
            run_id=run_id,
            unit=unit,
            entry=entry,
            reason_code="validation_rejected",
            reason_message=(
                f"EPIC didn't accept {unit.value!r} for {entry.label!r}. "
                "Open the field in EPIC, fix it, then click Resume."
            ),
            technical_detail=(
                f"strategy={strategy} read_back={validation.read_back!r} "
                f"error_text={validation.error_text!r}"
            ),
        )
        resolution = _handle_pause(
            state=state,
            pause=pause,
            on_pause_callback=on_pause_callback,
            save=save,
            page=page,
            locator=locator,
            unit=unit,
            entry=entry,
        )
        if resolution == "abort":
            aborted = True
            outcome = "aborted"
            pause_reason = pause
            fields_paused += 1
            break
        if resolution == "skip":
            fields_skipped += 1
            fields_paused += 1
            continue
        # resume - DOM was re-read inside _handle_pause and committed.
        fields_entered += 1
        fields_paused += 1

    # Session teardown ---------------------------------------------------
    if not aborted:
        outcome = "completed"

    # Clear any leftover pending_pause that the GUI may have observed.
    _set_pending_pause(state, None)

    _stop_tracing(browser_context, debug_dir, settings)

    if debug_dir is not None and client_path is not None:
        _prune_debug_runs(client_path, retention=DEBUG_RUN_RETENTION)

    result = EntryResult(
        run_id=run_id,
        fields_entered=fields_entered,
        fields_skipped=fields_skipped,
        fields_paused=fields_paused,
        outcome=outcome,
        pause_reason=pause_reason,
        smoke_stale_count=smoke_report.stale_count,
        warnings=warnings,
    )
    _append_run_history(state, run_id, result)
    save(state)

    _LOG.info(
        "enter.run_complete",
        extra={
            "run_id": run_id,
            "fields_entered": fields_entered,
            "fields_skipped": fields_skipped,
            "fields_paused": fields_paused,
            "outcome": outcome,
            "smoke_stale_count": smoke_report.stale_count,
        },
    )
    return result


# --------------------------------------------------------------------------- #
# Enterable-unit collection
# --------------------------------------------------------------------------- #


def _is_enterable(record: Any) -> bool:
    """Decide whether a FieldRecord (or dict-shaped equivalent) is ready
    for EPIC entry.

    Matches the GUI's Begin Entry gate (see ``MainWindow._count_enterable_fields``):
    any record with a non-empty value that hasn't already been entered.
    Skips records whose ``status == "entered"`` to avoid double-typing on
    resume, and records with empty/None values (nothing to type). The
    legacy "approved" gate is gone — the operator never asked for a
    manual review step.
    """
    if _record_status(record) == "entered":
        return False
    v = _record_value(record)
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    return True


def _matches_namespace(tag: str, namespaces: list[str] | None) -> bool:
    """True when ``tag`` lives under any of the allowed namespaces.

    ``namespaces=None`` means "no filter — every tag passes". A tag
    matches a namespace prefix when the tag equals it OR starts with
    ``prefix + "."``. So ``"policy.gl"`` allows ``policy.gl.hazard.*``
    and ``policy.gl.aggregate_limit`` but NOT ``policy.glOTHER.x``.
    """
    if not namespaces:
        return True
    for prefix in namespaces:
        if tag == prefix or tag.startswith(prefix + "."):
            return True
    return False


def _collect_approved_units(
    state: Any,
    *,
    include_namespaces: list[str] | None = None,
) -> Iterable[_Unit]:
    """Yield singleton + repeatable enterable fields in deterministic order.

    Name retained for backward compatibility with callers; semantics are
    now "enterable" (any with a value) rather than "status-approved".

    :param include_namespaces: Optional list of canonical-prefix filters
        (e.g. ``["policy.gl", "policy.property"]``). When provided, only
        tags / repeatable-groups whose dotted path starts with one of
        the prefixes are yielded. Used by the debug-mode coverage picker
        so iteration on a single LOB stays tight.
    """

    fields_map = getattr(state, "fields", None) or {}
    for tag in sorted(fields_map.keys()):
        if not _matches_namespace(tag, include_namespaces):
            continue
        record = fields_map[tag]
        if not _is_enterable(record):
            continue
        yield _Unit(
            domain_tag=tag,
            value=_record_value(record),
            repeatable_group=None,
            repeatable_index=None,
            record=record,
        )

    repeatables = getattr(state, "repeatables", None) or {}
    for group in sorted(repeatables.keys()):
        if not _matches_namespace(group, include_namespaces):
            continue
        items = repeatables[group] or []
        for idx, item in enumerate(items):
            # An item is dict-like: domain_tag -> FieldRecord
            try:
                tag_iter = sorted(item.keys())
            except AttributeError:
                continue
            for tag in tag_iter:
                record = item[tag]
                if not _is_enterable(record):
                    continue
                yield _Unit(
                    domain_tag=tag,
                    value=_record_value(record),
                    repeatable_group=group,
                    repeatable_index=idx,
                    record=record,
                )


# Alias the new name for callers that want clearer naming going forward.
_collect_enterable_units = _collect_approved_units


def _record_status(record: Any) -> str:
    """Read ``status`` from a record, falling back to dict-style access."""
    status = getattr(record, "status", None)
    if status is None and isinstance(record, dict):
        status = record.get("status")
    return status or "pending"


def _record_value(record: Any) -> Any:
    value = getattr(record, "value", None)
    if value is None and isinstance(record, dict):
        return record.get("value")
    return value


# --------------------------------------------------------------------------- #
# Fill + validate
# --------------------------------------------------------------------------- #


@dataclass(slots=True, kw_only=True)
class _ValidationResult:
    ok: bool
    read_back: Any
    error_text: str | None


def _fill_field(locator: "Locator", entry: "FieldEntry", value: Any) -> None:
    """Type-aware fill against ``locator``.

    Handles the common EPIC field types per ARCHITECTURE §4.2:
    text, textarea, date, select/combo, checkbox, currency.
    """
    field_type = (entry.type or "").lower()
    hint = (entry.hint or "").lower() if entry.hint else ""

    if field_type == "checkbox":
        if _truthy(value):
            locator.check()
        else:
            locator.uncheck()
        return

    if field_type == "select" or hint == "combo":
        # Validate against enum_values when known.
        enum_values = entry.enum_values
        target = "" if value is None else str(value)
        if enum_values and target and target not in enum_values:
            raise EntryError(
                f"Value {target!r} not in enum_values for {entry.label!r}: "
                f"{enum_values}"
            )
        locator.select_option(target)
        return

    if field_type == "date" or hint == "date":
        locator.fill(_format_date(value))
        return

    # Currency heuristic - hint is "integer" or label contains "Amount" /
    # "Premium" etc. Normalize numeric strings to "1234.56".
    if hint in ("integer", "currency") or _looks_currency(entry.label):
        locator.fill(_format_currency(value))
        return

    # Default: text / textarea / hint=text.
    locator.fill("" if value is None else str(value))


def _validate_after_fill(
    page: "Page",
    locator: "Locator",
    entry: "FieldEntry",
    *,
    intended: Any,
    screen_container: "Locator | None",
) -> _ValidationResult:
    """Read back the DOM and look for EPIC validation errors."""
    field_type = (entry.type or "").lower()

    if field_type == "checkbox":
        try:
            checked = bool(locator.is_checked())
        except Exception as exc:  # noqa: BLE001
            return _ValidationResult(ok=False, read_back=None, error_text=str(exc))
        ok = checked == _truthy(intended)
        return _ValidationResult(
            ok=ok,
            read_back=checked,
            error_text=None if ok else "checkbox state mismatch",
        )

    try:
        read_back = locator.input_value()
    except Exception as exc:  # noqa: BLE001
        return _ValidationResult(ok=False, read_back=None, error_text=str(exc))

    intended_str = "" if intended is None else str(intended)
    # Loose equality - select widgets often round-trip as the value, not the
    # display label.
    if str(read_back) != intended_str and str(read_back) != intended_str.strip():
        # Currency/date may be reformatted by EPIC; compare normalized.
        if field_type == "date" or (entry.hint or "").lower() == "date":
            ok = _normalize_date(read_back) == _normalize_date(intended_str)
        else:
            ok = _loose_eq(read_back, intended_str)
    else:
        ok = True

    error_text = _find_inline_error(page, screen_container)
    if error_text:
        return _ValidationResult(ok=False, read_back=read_back, error_text=error_text)

    return _ValidationResult(
        ok=ok,
        read_back=read_back,
        error_text=None if ok else "read-back mismatch",
    )


def _find_inline_error(
    page: "Page", screen_container: "Locator | None"
) -> str | None:
    """Look for an EPIC inline error indicator near the field container."""
    root: Any = screen_container if screen_container is not None else page
    try:
        err_loc = root.locator(":scope .error, :scope [role='alert']").first
        count = err_loc.count() if hasattr(err_loc, "count") else 0
        if count and count > 0:
            text = err_loc.text_content()
            return (text or "").strip() or "EPIC validation error"
    except Exception:  # noqa: BLE001 - probe is best-effort
        pass
    return None


# --------------------------------------------------------------------------- #
# Pause-for-human
# --------------------------------------------------------------------------- #


def _build_pause(
    *,
    run_id: str,
    unit: _Unit,
    entry: "FieldEntry",
    reason_code: Literal[
        "validation_rejected",
        "selector_unresolved",
        "user_requested",
        "exception",
    ],
    reason_message: str,
    technical_detail: str,
) -> PauseInfo:
    return PauseInfo(
        run_id=run_id,
        paused_at=_utc_now(),
        domain_tag=unit.domain_tag,
        repeatable_group=unit.repeatable_group,
        repeatable_index=unit.repeatable_index,
        screen_code=entry.screen_code,
        reason_code=reason_code,
        reason_message=reason_message,
        technical_detail=technical_detail,
        field_label=entry.label,
        field_name=entry.name,
    )


def _handle_pause(
    *,
    state: Any,
    pause: PauseInfo,
    on_pause_callback: Callable[[PauseInfo], PauseResolution],
    save: Callable[[Any], None],
    page: "Page",
    locator: "Locator | None",
    unit: _Unit,
    entry: "FieldEntry",
) -> PauseResolution:
    """Persist the pause, invoke the callback, and on resume re-read DOM.

    Returns the callback's choice. On ``"resume"``, the canonical record is
    updated to the DOM value (per ARCHITECTURE §7.4); ``state`` is saved
    again before returning.
    """
    # 1. Persist the pause to disk *before* opening the modal. If the
    #    operator's machine crashes or they force-close the window mid-
    #    pause, the next launch will spot pending_pause and offer to
    #    resume from this exact field instead of restarting the whole run.
    _set_pending_pause(state, pause)
    save(state)

    _LOG.info(
        "enter.pause_opened",
        extra={
            "run_id": pause.run_id,
            "domain_tag": pause.domain_tag,
            "reason_code": pause.reason_code,
        },
    )

    try:
        resolution = on_pause_callback(pause)
    except Exception as exc:  # noqa: BLE001 - callback failure -> abort
        _LOG.error(
            "enter.pause_callback_error",
            extra={"run_id": pause.run_id, "error": str(exc)},
        )
        resolution = "abort"

    if resolution not in ("resume", "skip", "abort"):
        _LOG.warning(
            "enter.pause_invalid_resolution",
            extra={"run_id": pause.run_id, "got": str(resolution)},
        )
        resolution = "abort"

    _LOG.info(
        "enter.pause_resolved",
        extra={
            "run_id": pause.run_id,
            "domain_tag": pause.domain_tag,
            "resolution": resolution,
        },
    )

    if resolution == "resume":
        # DOM-as-truth on resume: whatever the operator typed into EPIC
        # while we were paused becomes the new canonical value. Trying to
        # diff "what we sent" vs "what's actually there" is a debugging
        # nightmare — the human just edited the live system, trust them.
        new_value: Any = None
        if locator is not None:
            new_value = _safe_read_dom(locator, entry)
        if new_value is None:
            # Operator clicked Resume but left the field empty — flip back
            # to pending so the GUI flags it for another look later.
            _record_status_set(unit.record, "pending")
        else:
            _commit_dom_value(
                record=unit.record,
                new_value=new_value,
                run_id=pause.run_id,
                actor="user",
                action="resumed_after_pause",
            )

    # Always clear the durable pause when control returns to the loop. The
    # caller (run_entry_session) will react to the resolution.
    _set_pending_pause(state, None)
    save(state)
    return resolution


def _safe_read_dom(locator: "Locator", entry: "FieldEntry") -> Any:
    field_type = (entry.type or "").lower()
    try:
        if field_type == "checkbox":
            return bool(locator.is_checked())
        value = locator.input_value()
    except Exception as exc:  # noqa: BLE001
        _LOG.warning(
            "enter.dom_read_failed",
            extra={"field_name": entry.name, "error": str(exc)},
        )
        return None
    if isinstance(value, str) and value == "":
        return None
    return value


def _commit_dom_value(
    *,
    record: Any,
    new_value: Any,
    run_id: str,
    actor: str,
    action: str,
) -> None:
    """Write ``new_value`` onto ``record`` and append a history entry.

    Uses dict-style or attribute-style access so it works against either a
    pydantic model or a hand-rolled dataclass once state.py lands.
    """
    prior = {
        "value": _record_value(record),
        "status": _record_status(record),
        "confidence": _record_confidence(record),
    }
    _record_value_set(record, new_value)
    _record_status_set(record, "entered")
    _record_confidence_set(record, 1.0)
    _record_append_history(
        record,
        run_id=run_id,
        actor=actor,
        action=action,
        prior=prior,
        new={
            "value": new_value,
            "status": "entered",
            "confidence": 1.0,
        },
    )


# --------------------------------------------------------------------------- #
# Mark-entered helpers
# --------------------------------------------------------------------------- #


def _mark_entered(
    state: Any,
    unit: _Unit,
    entry: "FieldEntry",
    run_id: str,
    strategy: str,
) -> None:
    record = unit.record
    prior = {
        "value": _record_value(record),
        "status": _record_status(record),
        "confidence": _record_confidence(record),
    }
    _record_status_set(record, "entered")
    _record_append_history(
        record,
        run_id=run_id,
        actor="entry",
        action="enter",
        prior=prior,
        new={
            "value": prior["value"],
            "status": "entered",
            "confidence": prior["confidence"],
        },
        strategy=strategy,
    )
    _LOG.debug(
        "enter.field_entered",
        extra={
            "run_id": run_id,
            "domain_tag": unit.domain_tag,
            "strategy": strategy,
            "screen_code": entry.screen_code,
        },
    )


# --------------------------------------------------------------------------- #
# Duck-typed state mutation helpers
# --------------------------------------------------------------------------- #


def _record_confidence(record: Any) -> float:
    val = getattr(record, "confidence", None)
    if val is None and isinstance(record, dict):
        val = record.get("confidence")
    return float(val) if val is not None else 0.0


def _record_value_set(record: Any, value: Any) -> None:
    if isinstance(record, dict):
        record["value"] = value
        return
    try:
        setattr(record, "value", value)
    except (AttributeError, TypeError):
        # frozen dataclass - cannot mutate; surface a warning.
        _LOG.warning("enter.cannot_set_value_on_record", extra={"type": type(record).__name__})


def _record_status_set(record: Any, status: str) -> None:
    if isinstance(record, dict):
        record["status"] = status
        return
    try:
        setattr(record, "status", status)
    except (AttributeError, TypeError):
        _LOG.warning("enter.cannot_set_status_on_record", extra={"type": type(record).__name__})


def _record_confidence_set(record: Any, confidence: float) -> None:
    if isinstance(record, dict):
        record["confidence"] = confidence
        return
    try:
        setattr(record, "confidence", confidence)
    except (AttributeError, TypeError):
        pass


def _record_append_history(
    record: Any,
    *,
    run_id: str,
    actor: str,
    action: str,
    prior: dict[str, Any] | None,
    new: dict[str, Any],
    strategy: str | None = None,
) -> None:
    entry: dict[str, Any] = {
        "run_id": run_id,
        "ts": _utc_now(),
        "actor": actor,
        "action": action,
        "prior": prior,
        "new": new,
    }
    if strategy is not None:
        entry["strategy"] = strategy

    history = None
    if isinstance(record, dict):
        history = record.setdefault("history", [])
    else:
        history = getattr(record, "history", None)
        if history is None:
            try:
                setattr(record, "history", [])
                history = getattr(record, "history")
            except (AttributeError, TypeError):
                history = None
    if history is not None:
        try:
            history.append(entry)
        except AttributeError:
            pass


def _set_pending_pause(state: Any, pause: PauseInfo | None) -> None:
    """Write ``pause`` onto state, preferring the state.set_pending_pause API.

    state.py is not yet implemented at the time this module ships. When it
    lands, ``state.set_pending_pause(state, pause)`` is the canonical setter.
    Until then, fall back to attribute write so test doubles work.
    """
    try:
        from . import state as state_mod  # type: ignore[attr-defined]
    except ImportError:
        state_mod = None  # type: ignore[assignment]

    if state_mod is not None and hasattr(state_mod, "set_pending_pause"):
        try:
            state_mod.set_pending_pause(state, pause)
            return
        except Exception as exc:  # noqa: BLE001 - fall through to attribute set
            _LOG.warning(
                "enter.set_pending_pause_failed",
                extra={"error": str(exc)},
            )
    try:
        setattr(state, "pending_pause", pause)
    except (AttributeError, TypeError):
        if isinstance(state, dict):
            state["pending_pause"] = pause


def _append_run_history(state: Any, run_id: str, result: EntryResult) -> None:
    """Append an ``entry`` run_history entry. Best-effort against duck-typed state."""
    history_entry = {
        "run_id": run_id,
        "ts": _utc_now(),
        "kind": "entry",
        "inputs": [],
        "outcome": result.outcome,
        "fields_entered": result.fields_entered,
        "fields_skipped": result.fields_skipped,
        "fields_paused": result.fields_paused,
    }

    try:
        from . import state as state_mod  # type: ignore[attr-defined]
    except ImportError:
        state_mod = None  # type: ignore[assignment]

    if state_mod is not None and hasattr(state_mod, "append_run_history"):
        try:
            state_mod.append_run_history(state, history_entry)
            return
        except Exception:  # noqa: BLE001
            pass

    history = getattr(state, "run_history", None)
    if history is None and isinstance(state, dict):
        history = state.setdefault("run_history", [])
    if history is None:
        try:
            setattr(state, "run_history", [history_entry])
            return
        except (AttributeError, TypeError):
            return
    try:
        history.append(history_entry)
    except AttributeError:
        pass


# --------------------------------------------------------------------------- #
# Tracing / debug
# --------------------------------------------------------------------------- #


def _resolve_debug_dir(
    settings: Settings, client_path: Path | None, run_id: str
) -> Path | None:
    if not settings.debug or client_path is None:
        return None
    debug_dir = client_path / "debug" / "playwright" / run_id
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _LOG.warning(
            "enter.debug_dir_create_failed",
            extra={"path": str(debug_dir), "error": str(exc)},
        )
        return None
    return debug_dir


def _stop_tracing(
    browser_context: "BrowserContext",
    debug_dir: Path | None,
    settings: Settings,
) -> None:
    if not settings.debug or debug_dir is None:
        return
    target = debug_dir / "trace.zip"
    try:
        browser_context.tracing.stop(path=str(target))
        _LOG.info("enter.tracing_saved", extra={"path": str(target)})
    except Exception as exc:  # noqa: BLE001 - tracing is best-effort
        _LOG.warning("enter.tracing_stop_failed", extra={"error": str(exc)})


def _maybe_screenshot(
    page: "Page",
    debug_dir: Path | None,
    settings: Settings,
    index: int,
    unit: _Unit,
    phase: Literal["before", "after"],
) -> None:
    if not settings.debug or debug_dir is None:
        return
    safe_tag = unit.domain_tag.replace(".", "_").replace("/", "_")
    target = debug_dir / f"{index:04d}-{safe_tag}-{phase}.png"
    try:
        page.screenshot(path=str(target))
    except Exception as exc:  # noqa: BLE001 - screenshots are best-effort
        _LOG.debug(
            "enter.screenshot_failed",
            extra={"path": str(target), "error": str(exc)},
        )


def _prune_debug_runs(client_path: Path, *, retention: int) -> None:
    """Keep the newest ``retention`` ``<run_id>`` directories under
    ``<client>/debug/playwright/``; delete older ones."""
    parent = client_path / "debug" / "playwright"
    if not parent.exists():
        return
    try:
        run_dirs = [p for p in parent.iterdir() if p.is_dir()]
    except OSError:
        return
    run_dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for victim in run_dirs[retention:]:
        try:
            shutil.rmtree(victim, ignore_errors=True)
            _LOG.info("enter.debug_pruned", extra={"path": str(victim)})
        except OSError as exc:
            _LOG.warning(
                "enter.debug_prune_failed",
                extra={"path": str(victim), "error": str(exc)},
            )


# --------------------------------------------------------------------------- #
# Misc helpers
# --------------------------------------------------------------------------- #


def _first_page(browser_context: "BrowserContext") -> "Page":
    pages = list(getattr(browser_context, "pages", []) or [])
    if pages:
        return pages[0]
    # Fall back to new_page if no page is open yet.
    new_page = getattr(browser_context, "new_page", None)
    if callable(new_page):
        return new_page()
    raise EntryError(
        "BrowserContext has no pages and no new_page() factory; "
        "operator must navigate to a starting screen before Begin Entry."
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "checked", "x")
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _looks_currency(label: str | None) -> bool:
    if not label:
        return False
    needles = ("amount", "premium", "limit", "value", "cost", "deductible")
    lowered = label.lower()
    return any(n in lowered for n in needles)


def _format_currency(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:.2f}".rstrip("0").rstrip(".") or "0"
    s = str(value).strip().replace(",", "").replace("$", "")
    return s


def _format_date(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _normalize_date(value: Any) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s.replace("-", "/").replace(" ", "")


def _loose_eq(a: Any, b: Any) -> bool:
    if a is None and b is None:
        return True
    sa = "" if a is None else str(a).strip()
    sb = "" if b is None else str(b).strip()
    if sa == sb:
        return True
    # Numeric tolerance.
    try:
        return float(sa) == float(sb)
    except (TypeError, ValueError):
        return False


def _debounce(ms: int) -> None:
    if ms <= 0:
        return
    time.sleep(ms / 1000.0)


def _noop_save(_state: Any) -> None:
    return None

"""epic_session.py - Playwright persistent context lifecycle for EPIC.

Owns the EPIC browser session primitives:

- Pinned ``playwright >= 1.55`` (Amendment #16; RESEARCH.md Finding 4).
- ``launch_persistent_context`` only - never CDP attach.
- Absolute paths required for ``user_data_dir`` (Playwright #34700).
- :func:`cleanup_user_data_dir_lock` - runs before every launch; removes stale
  ``SingletonLock`` / ``LOCK`` / ``lockfile`` files when no live PID owns them.
  Raises :class:`PlaywrightProfileInUseError` when a live PID owns the dir.
- :func:`resolve_locator` - selector resolution chain
  ``data-automation-id`` -> ``name`` -> ``get_by_label`` fallback. Returns
  ``(Locator, strategy_used)`` so callers can record drift without re-parsing
  logs (ARCHITECTURE §13.8).
- :func:`preflight_selector_smoke` - non-blocking touched-screens-only smoke
  test (Amendment #6, ARCHITECTURE §7.3). Target budget 2-5s; warns at 30s.
- ``--debug`` enables Playwright tracing
  (``screenshots=True, snapshots=True, sources=True``) - the trace.zip is
  stopped + saved by :mod:`enter` at session end.

ARCHITECTURE.md sections: 4 (FieldEntry), 7 (entry driver contract), 10
(--debug discipline), 13.8/13.12 (rulings).
"""

# Module-level requirements (mirrored in requirements.txt; reproduced here per
# build instruction): playwright >= 1.55, psutil for stale-PID checks.

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .logger import get_logger

if TYPE_CHECKING:
    # Real Playwright types are only imported for static checking. The
    # runtime imports happen lazily inside :func:`launch_with_persistent_context`
    # so unit tests can mock the API without paying the import cost.
    from playwright.sync_api import (
        BrowserContext,
        Locator,
        Page,
    )

    from .field_map import FieldEntry, FieldMap


__all__ = [
    "EpicSessionError",
    "PlaywrightProfileInUseError",
    "SelectorUnresolvedError",
    "SmokeReport",
    "SmokeReportEntry",
    "STRATEGY_AUTOMATION_ID",
    "STRATEGY_LABEL_FALLBACK",
    "STRATEGY_NAME",
    "cleanup_user_data_dir_lock",
    "launch_with_persistent_context",
    "preflight_selector_smoke",
    "resolve_locator",
]


_LOG = get_logger("epic_session")


# --------------------------------------------------------------------------- #
# Public constants
# --------------------------------------------------------------------------- #

#: Strategy identifiers returned by :func:`resolve_locator`.
STRATEGY_AUTOMATION_ID: str = "automation_id"
STRATEGY_NAME: str = "name"
STRATEGY_LABEL_FALLBACK: str = "label_fallback"

#: Filenames searched by :func:`cleanup_user_data_dir_lock`. Order matters
#: only for log readability.
LOCK_FILES: tuple[str, ...] = (
    "SingletonLock",
    "SingletonCookie",
    "SingletonSocket",
    "LOCK",
    "lockfile",
    "parent.lock",
)

#: Soft-target time budget for the pre-flight smoke (ARCHITECTURE §7.3).
PREFLIGHT_TARGET_SECONDS: float = 5.0
PREFLIGHT_WARN_SECONDS: float = 30.0


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class EpicSessionError(Exception):
    """Base class for any error raised by this module."""


class PlaywrightProfileInUseError(EpicSessionError):
    """Raised when ``user_data_dir`` is locked by a live process."""


class SelectorUnresolvedError(EpicSessionError):
    """Raised by :func:`resolve_locator` when none of the three strategies match.

    ``attempts`` carries the per-strategy diagnostic so the caller can build
    a ``PendingPause.technical_detail`` block.
    """

    def __init__(self, *, field_label: str, field_name: str, attempts: dict[str, str]) -> None:
        self.field_label = field_label
        self.field_name = field_name
        self.attempts = attempts
        super().__init__(
            f"Could not resolve locator for field {field_label!r} "
            f"(name={field_name!r}); attempts={attempts}"
        )


# --------------------------------------------------------------------------- #
# Smoke report types
# --------------------------------------------------------------------------- #


@dataclass(slots=True, kw_only=True)
class SmokeReportEntry:
    """One field's pre-flight smoke result."""

    screen_code: str
    domain_tag: str | None
    field_name: str
    resolved: bool
    strategy: str | None  # one of STRATEGY_* or None when unresolved
    stale: bool  # True for label_fallback or unresolved
    elapsed_ms: float
    error: str | None = None


@dataclass(slots=True, kw_only=True)
class SmokeReport:
    """Aggregate result of :func:`preflight_selector_smoke`."""

    elapsed_ms: float
    entries: list[SmokeReportEntry] = field(default_factory=list)

    @property
    def stale_count(self) -> int:
        return sum(1 for e in self.entries if e.stale)

    @property
    def unresolved_count(self) -> int:
        return sum(1 for e in self.entries if not e.resolved)

    @property
    def has_stale(self) -> bool:
        return self.stale_count > 0


# --------------------------------------------------------------------------- #
# Lock cleanup
# --------------------------------------------------------------------------- #


def _read_pid_from_lock(path: Path) -> int | None:
    """Best-effort PID extraction from a Chromium lock file.

    Chromium writes ``<pid>-<hostname>`` into ``SingletonLock``. Other lock
    files may be empty or hold a bare integer. Anything we cannot parse maps
    to ``None`` (treated as stale by the caller).
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not text:
        return None
    # Chromium pattern "1234-host.local"
    head, _, _ = text.partition("-")
    head = head.strip()
    if head.isdigit():
        try:
            return int(head)
        except ValueError:
            return None
    return None


def _is_pid_alive(pid: int) -> bool:
    """Return True if ``pid`` belongs to a currently-running process.

    Uses ``psutil`` if available; falls back to ``os.kill(pid, 0)`` on POSIX.
    On Windows without psutil, returns ``False`` (treat lock as stale) - this
    is the safer default given Path B's single-user assumption.
    """
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        psutil = None  # type: ignore[assignment]

    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except (OSError, ValueError):
            return False

    if os.name == "nt":
        # Without psutil on Windows we have no reliable way to probe a PID
        # (signal-0 doesn't work on Windows), so we treat the lock as stale
        # and let Playwright surface its own "already in use" error on launch
        # if we guessed wrong. Better than refusing to launch over a ghost.
        _LOG.warning(
            "epic_session.pid_check_unavailable",
            extra={"pid": pid, "reason": "psutil missing on Windows"},
        )
        return False

    # POSIX fallback: signal 0 doesn't deliver, just probes existence.
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we don't own it; treat as alive to be safe.
        return True
    except OSError:
        return False


def cleanup_user_data_dir_lock(user_data_dir: Path) -> None:
    """Remove stale Chromium lock files in ``user_data_dir``.

    Runs before every :func:`launch_with_persistent_context` (ARCHITECTURE
    §7.1, Amendment #16). Each known lock file (see :data:`LOCK_FILES`) is
    inspected: if its embedded PID is dead (or unparseable), the file is
    deleted; if a live PID owns it, :class:`PlaywrightProfileInUseError` is
    raised.

    Idempotent and safe to call when the directory does not yet exist.
    """
    if not user_data_dir.is_absolute():
        raise ValueError(
            f"user_data_dir must be absolute (Playwright #34700); got {user_data_dir!r}"
        )

    if not user_data_dir.exists():
        _LOG.debug(
            "epic_session.cleanup_lock.skip_missing_dir",
            extra={"user_data_dir": str(user_data_dir)},
        )
        return

    cleaned: list[str] = []
    for filename in LOCK_FILES:
        target = user_data_dir / filename
        if not target.exists() and not target.is_symlink():
            continue
        pid = _read_pid_from_lock(target) if target.is_file() else None
        # If a live PID still owns this lock, a real Chromium instance is
        # using the profile. Deleting the lock would corrupt the profile, so
        # we refuse and tell the operator to close the browser first.
        if pid is not None and _is_pid_alive(pid):
            raise PlaywrightProfileInUseError(
                f"Playwright profile {user_data_dir} is owned by live PID {pid} "
                f"(lock file: {filename}). Close the running browser session "
                "or kill the process before retrying."
            )
        # Stale or unparseable - delete.
        try:
            target.unlink(missing_ok=True)
            cleaned.append(filename)
            _LOG.info(
                "epic_session.cleanup_lock.removed",
                extra={
                    "user_data_dir": str(user_data_dir),
                    "lock_file": filename,
                    "pid_in_file": pid,
                },
            )
        except OSError as exc:
            _LOG.warning(
                "epic_session.cleanup_lock.unlink_failed",
                extra={
                    "user_data_dir": str(user_data_dir),
                    "lock_file": filename,
                    "error": str(exc),
                },
            )

    if not cleaned:
        _LOG.debug(
            "epic_session.cleanup_lock.no_action",
            extra={"user_data_dir": str(user_data_dir)},
        )


# --------------------------------------------------------------------------- #
# Launch
# --------------------------------------------------------------------------- #


def launch_with_persistent_context(
    user_data_dir: Path,
    *,
    headed: bool = True,
    debug: bool = False,
    cdp_port: int | None = None,
) -> "BrowserContext":
    """Launch Chromium with a persistent profile and return the context.

    Calls :func:`cleanup_user_data_dir_lock` first (per ARCHITECTURE §7.1).
    ``user_data_dir`` must be absolute (Playwright #34700). When ``debug`` is
    True, tracing is started immediately so :mod:`enter` can stop+save it
    on session end.

    :param cdp_port: When set, passes
        ``--remote-debugging-port=<cdp_port>`` to Chromium so an external
        CDP client (e.g. Playwright MCP) can attach to the running
        browser. Chromium binds the port to localhost only — it is not
        exposed to the network. Used in --debug runs for co-iterating
        automation against the same browser the operator is driving.

    Raises:
        ValueError: ``user_data_dir`` is not absolute.
        PlaywrightProfileInUseError: a live PID owns the profile.
        EpicSessionError: any other launch failure (wraps the underlying
            Playwright exception).
    """
    if not user_data_dir.is_absolute():
        raise ValueError(
            f"user_data_dir must be absolute (Playwright #34700); got {user_data_dir!r}"
        )

    cleanup_user_data_dir_lock(user_data_dir)
    user_data_dir.mkdir(parents=True, exist_ok=True)

    # Lazy import - keeps unit tests free of Playwright at import time.
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - import error guard
        raise EpicSessionError(
            "playwright>=1.55 is required; install via "
            "`pip install playwright` then `playwright install chromium`"
        ) from exc

    chromium_args: list[str] = []
    if cdp_port is not None:
        chromium_args.append(f"--remote-debugging-port={int(cdp_port)}")

    pw = sync_playwright().start()
    try:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=not headed,
            args=chromium_args or None,
        )
    except Exception as exc:  # noqa: BLE001 - wrap any launch failure
        # Surface Playwright's "user data dir already in use" as the typed
        # error so the GUI can present a clean OperatorModal.
        msg = str(exc)
        if "already in use" in msg.lower() or "singleton" in msg.lower():
            try:
                pw.stop()
            except Exception:  # noqa: BLE001 - best-effort cleanup
                pass
            raise PlaywrightProfileInUseError(
                f"Chromium reports {user_data_dir} is in use after lock cleanup; "
                f"try restarting Windows. Underlying error: {msg}"
            ) from exc
        try:
            pw.stop()
        except Exception:  # noqa: BLE001 - best-effort cleanup
            pass
        raise EpicSessionError(f"Failed to launch persistent context: {msg}") from exc

    # Stash the playwright handle on the context so `enter` can call
    # `pw.stop()` on session end. Use a private attribute name.
    setattr(context, "_iga_playwright", pw)

    if debug:
        try:
            context.tracing.start(
                screenshots=True,
                snapshots=True,
                sources=True,
            )
            _LOG.info("epic_session.tracing.started")
        except Exception as exc:  # noqa: BLE001 - tracing is best-effort
            _LOG.warning(
                "epic_session.tracing.start_failed",
                extra={"error": str(exc)},
            )

    _LOG.info(
        "epic_session.launched",
        extra={
            "user_data_dir": str(user_data_dir),
            "headed": headed,
            "debug": debug,
            "cdp_port": cdp_port,
        },
    )
    return context


# --------------------------------------------------------------------------- #
# Selector resolution
# --------------------------------------------------------------------------- #


def _locator_count(locator: Any) -> int:
    """Best-effort count probe that tolerates tests using a stub Locator."""
    try:
        count = locator.count()
        return int(count) if count is not None else 0
    except Exception:  # noqa: BLE001 - tests may stub
        return 0


def resolve_locator(
    page: "Page",
    field_entry: "FieldEntry",
    *,
    screen_container: "Locator | None" = None,
) -> tuple["Locator", str]:
    """Resolve ``field_entry`` to a Playwright ``Locator``.

    Strategy order (ARCHITECTURE §7.1):

    1. ``data-automation-id`` from the Field Map's ``automation_id`` (if
       present on the entry's raw dict).
    2. ``name`` attribute (always present per Field Map schema §4.2).
    3. ``page.get_by_label(label, exact=True)`` fallback. Logs at WARNING
       so the orchestrator can later wire field_map drift recording.

    Each strategy is tried against ``screen_container`` if given, else the
    bare ``page`` (sufficient for fields that are unique on the page).

    :returns: ``(Locator, strategy_used)`` where ``strategy_used`` is one
        of ``"automation_id"`` / ``"name"`` / ``"label_fallback"``.
    :raises SelectorUnresolvedError: when none of the three resolve.
    """
    root: Any = screen_container if screen_container is not None else page
    attempts: dict[str, str] = {}

    automation_id = field_entry.raw.get("automation_id") if hasattr(field_entry, "raw") else None
    name = field_entry.name
    label = field_entry.label

    # Strategy chain ordered from most stable to most fragile. We log a
    # warning when we fall back to label_fallback because hitting that path
    # means EPIC's automation_id or name attribute drifted — drift this
    # session means broken selectors next session.

    # 1. data-automation-id
    if isinstance(automation_id, str) and automation_id:
        try:
            loc = root.locator(f"[data-automation-id={_css_quote(automation_id)}]")
            count = _locator_count(loc)
            attempts[STRATEGY_AUTOMATION_ID] = f"count={count}"
            if count >= 1:
                _LOG.debug(
                    "epic_session.resolve_locator.hit",
                    extra={
                        "strategy": STRATEGY_AUTOMATION_ID,
                        "automation_id": automation_id,
                        "field_name": name,
                    },
                )
                return loc, STRATEGY_AUTOMATION_ID
        except Exception as exc:  # noqa: BLE001
            attempts[STRATEGY_AUTOMATION_ID] = f"error: {exc}"
    else:
        attempts[STRATEGY_AUTOMATION_ID] = "absent"

    # 2. name attribute
    if isinstance(name, str) and name:
        try:
            loc = root.locator(f"[name={_css_quote(name)}]")
            count = _locator_count(loc)
            attempts[STRATEGY_NAME] = f"count={count}"
            if count >= 1:
                _LOG.debug(
                    "epic_session.resolve_locator.hit",
                    extra={"strategy": STRATEGY_NAME, "field_name": name},
                )
                return loc, STRATEGY_NAME
        except Exception as exc:  # noqa: BLE001
            attempts[STRATEGY_NAME] = f"error: {exc}"
    else:
        attempts[STRATEGY_NAME] = "absent"

    # 3. label fallback
    if isinstance(label, str) and label:
        try:
            loc = root.get_by_label(label, exact=True)
            count = _locator_count(loc)
            attempts[STRATEGY_LABEL_FALLBACK] = f"count={count}"
            if count >= 1:
                _LOG.warning(
                    "epic_session.resolve_locator.label_fallback",
                    extra={
                        "label": label,
                        "field_name": name,
                        "automation_id": automation_id,
                        "domain_tag": field_entry.domain_tag,
                        "screen_code": field_entry.screen_code,
                    },
                )
                return loc, STRATEGY_LABEL_FALLBACK
        except Exception as exc:  # noqa: BLE001
            attempts[STRATEGY_LABEL_FALLBACK] = f"error: {exc}"
    else:
        attempts[STRATEGY_LABEL_FALLBACK] = "absent"

    raise SelectorUnresolvedError(
        field_label=label or "<unlabeled>",
        field_name=name or "<unnamed>",
        attempts=attempts,
    )


def _css_quote(value: str) -> str:
    """Quote a value for embedding into a CSS attribute selector.

    Returns a double-quoted string with embedded ``"`` and ``\\`` escaped.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# --------------------------------------------------------------------------- #
# Pre-flight smoke
# --------------------------------------------------------------------------- #


def preflight_selector_smoke(
    page: "Page",
    tags_to_enter: Iterable[str],
    field_map: "FieldMap",
    *,
    screen_container: "Locator | None" = None,
) -> SmokeReport:
    """Walk the touched screens' fields and verify selectors resolve.

    Non-blocking (per Amendment #6 / ARCHITECTURE §7.3):

    - Iterates ``screens_touched_by_domain_tags(tags_to_enter)`` only.
    - For each leaf field whose ``domain_tag`` is in the input set, attempts
      :func:`resolve_locator` against the current page (or ``screen_container``
      if given).
    - Records each result in a :class:`SmokeReport`. ``label_fallback`` and
      unresolved are both classified as ``stale``.
    - The caller (``enter.py``) surfaces a yellow banner if ``has_stale``;
      this function never raises.
    - Time budget: 2-5s typical. If elapsed >30s, logs a warning.
    """
    # Local imports avoid an `epic_session -> field_map` import cycle for
    # type checking (we only need the runtime object).
    from . import field_map as _field_map_mod

    started = time.perf_counter()
    tags_list = list(tags_to_enter)
    screens = _field_map_mod.screens_touched_by_domain_tags(field_map, tags_list)

    report = SmokeReport(elapsed_ms=0.0)
    tag_set = set(tags_list)

    for screen_code in sorted(screens):
        try:
            entries = _field_map_mod.fields_for_screen(field_map, screen_code)
        except Exception as exc:  # noqa: BLE001
            _LOG.warning(
                "epic_session.preflight.fields_for_screen_failed",
                extra={"screen_code": screen_code, "error": str(exc)},
            )
            continue

        for entry in entries:
            tag = entry.domain_tag
            if tag is None or tag not in tag_set:
                continue
            t0 = time.perf_counter()
            try:
                _, strategy = resolve_locator(
                    page,
                    entry,
                    screen_container=screen_container,
                )
                stale = strategy == STRATEGY_LABEL_FALLBACK
                report.entries.append(
                    SmokeReportEntry(
                        screen_code=screen_code,
                        domain_tag=tag,
                        field_name=entry.name,
                        resolved=True,
                        strategy=strategy,
                        stale=stale,
                        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                    )
                )
            except SelectorUnresolvedError as exc:
                report.entries.append(
                    SmokeReportEntry(
                        screen_code=screen_code,
                        domain_tag=tag,
                        field_name=entry.name,
                        resolved=False,
                        strategy=None,
                        stale=True,
                        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                        error=f"unresolved (attempts={exc.attempts})",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - smoke is non-blocking
                report.entries.append(
                    SmokeReportEntry(
                        screen_code=screen_code,
                        domain_tag=tag,
                        field_name=entry.name,
                        resolved=False,
                        strategy=None,
                        stale=True,
                        elapsed_ms=(time.perf_counter() - t0) * 1000.0,
                        error=f"exception: {exc}",
                    )
                )

    elapsed = time.perf_counter() - started
    report.elapsed_ms = elapsed * 1000.0

    if elapsed > PREFLIGHT_WARN_SECONDS:
        _LOG.warning(
            "epic_session.preflight.slow",
            extra={
                "elapsed_seconds": round(elapsed, 2),
                "checked": len(report.entries),
                "stale": report.stale_count,
            },
        )

    _LOG.info(
        "epic_session.preflight.complete",
        extra={
            "elapsed_ms": round(report.elapsed_ms, 1),
            "screens": sorted(screens),
            "checked": len(report.entries),
            "stale": report.stale_count,
            "unresolved": report.unresolved_count,
        },
    )
    return report

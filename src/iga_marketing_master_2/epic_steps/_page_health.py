"""Page-health helpers for EPIC navigation steps.

The repeated failure mode in step files is: an EPIC validation error or
internal Program Alert fires, the script doesn't notice, and subsequent
actions land in a corrupted page state — which sometimes leaves EPIC
unrecoverable. This module gives step files three primitives that make
"detect before move on" the default:

  :func:`assert_clean`           — probe the page once, return a
                                    :class:`PageHealth` describing what
                                    (if anything) is wrong.
  :func:`wait_for_loading_clear` — block until any spinners / "Retrieving"
                                    indicators are gone.
  :func:`safe_action`            — context manager that wraps a single
                                    action: ``assert_clean`` before,
                                    wait for loading, run the action,
                                    wait again, ``assert_clean`` after.
                                    Hard errors → cancel run. Soft errors
                                    → operator halt via runtime callback.

Adoption philosophy: start with one section (IM Coverage/Deductible),
prove it works, then propagate.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterator

from . import runtime as _runtime
from .runtime import EntryCancelled

_log = logging.getLogger("iga.epic_steps.page_health")


# ── Public types ─────────────────────────────────────────────────────────────


class HealthLevel(Enum):
    """How bad is the current page state."""

    CLEAN       = "clean"
    """Nothing flagged. Safe to proceed."""

    LOADING     = "loading"
    """EPIC is mid-render (spinner / Retrieving / busy). Caller should wait."""

    SOFT_ERROR  = "soft_error"
    """EPIC surfaced a recoverable problem (validation modal, field error,
    row-add silently rejected). Operator decides whether to proceed."""

    HARD_ERROR  = "hard_error"
    """EPIC is in a corrupt state (Program Alert / Issue Code XXX). The
    only safe action is to cancel the run — proceeding past one of these
    has caused EPIC to lock up in past runs."""


@dataclass(slots=True)
class PageHealth:
    """Snapshot of page health at a single moment in time."""

    level: HealthLevel
    summary: str = ""
    """Operator-facing one-line description of what's wrong."""

    detail: str = ""
    """Full text captured from EPIC (modal body, banner text, etc.)."""

    detected_selector: str = ""
    """Which selector triggered the detection — for debugging."""

    @property
    def is_clean(self) -> bool:
        return self.level is HealthLevel.CLEAN

    @property
    def is_loading(self) -> bool:
        return self.level is HealthLevel.LOADING

    @property
    def is_soft_error(self) -> bool:
        return self.level is HealthLevel.SOFT_ERROR

    @property
    def is_hard_error(self) -> bool:
        return self.level is HealthLevel.HARD_ERROR


class EPICHardError(Exception):
    """Raised when EPIC enters a corrupt state we can't safely proceed past.

    Caught by the worker as a run-terminating error — operator sees the
    detail and EPIC must be restarted or recovered manually.
    """

    def __init__(self, health: PageHealth) -> None:
        super().__init__(health.summary or "EPIC entered a hard error state")
        self.health = health


# ── Detection ────────────────────────────────────────────────────────────────

# Selectors are listed in priority order. First match wins.
# Each entry: (selector, level, summary, extract_detail).

_HARD_ERROR_PROBES: list[tuple[str, str]] = [
    # EPIC's "Program Alert" red-headered dialog — the worst kind. Surfaces
    # an Issue Code like POLVIEW-3DD-52J-DZX-... that has historically
    # required restarting EPIC.
    (
        # Probe via text — class names vary across EPIC builds.
        """() => {
            const text = (document.body && document.body.innerText) || '';
            if (!/Program Alert\\b/i.test(text)) return null;
            // Grab a tight window of text around the alert + the Issue Code.
            const m = text.match(/Program Alert[\\s\\S]{0,400}?(Issue Code:[^\\n]+|$)/i);
            return m ? m[0].trim().slice(0, 600) : 'Program Alert detected';
        }""",
        "EPIC Program Alert — internal error",
    ),
]

_SOFT_ERROR_PROBES: list[tuple[str, str]] = [
    # Standard EPIC validation modal.
    (
        """() => {
            const sels = ['[role="dialog"]', '[role="alertdialog"]', 'modal-screen'];
            for (const sel of sels) {
                const els = Array.from(document.querySelectorAll(sel))
                    .filter(el => el.offsetParent !== null);
                for (const el of els) {
                    const txt = (el.innerText || '').trim();
                    if (/required information is missing|please correct|invalid/i.test(txt)) {
                        return txt.slice(0, 600);
                    }
                }
            }
            return null;
        }""",
        "EPIC validation modal is open",
    ),
    # Inline banner — e.g. "The page has unsaved changes" or per-field
    # error summaries at the top of a frame.
    (
        """() => {
            const sels = ['[role="alert"]', '[class*="ErrorBanner"]',
                          '[class*="error-banner"]', '[class*="ValidationMessage"]'];
            for (const sel of sels) {
                const els = Array.from(document.querySelectorAll(sel))
                    .filter(el => el.offsetParent !== null);
                for (const el of els) {
                    const txt = (el.innerText || '').trim();
                    if (txt && txt.length < 400) return txt;
                }
            }
            return null;
        }""",
        "EPIC inline error banner is showing",
    ),
]

_LOADING_PROBE = """() => {
    // Aria-busy on any visible element
    const busy = document.querySelector('[aria-busy="true"]');
    if (busy && busy.offsetParent !== null) return 'aria-busy=true';
    // Common spinner classes
    const spinner = document.querySelector(
        '[class*="Spinner"]:not([style*="display: none"]), [class*="LoadingIndicator"], .loading-overlay'
    );
    if (spinner && spinner.offsetParent !== null) return 'spinner visible';
    // EPIC's "Retrieving..." indicator (Home dashboard only, but cheap to check)
    const txt = (document.body && document.body.innerText) || '';
    if (/\\bRetrieving\\b/i.test(txt)) return 'Retrieving text';
    return null;
}"""


def assert_clean(page: Any) -> PageHealth:
    """One-shot probe of page health. Does NOT wait.

    Order: hard errors → soft errors → loading → clean. First match wins
    so a Program Alert reported as a soft error never happens.
    """
    # Hard errors first — these are non-recoverable.
    for js, summary in _HARD_ERROR_PROBES:
        try:
            detail = page.evaluate(js)
        except Exception:  # noqa: BLE001
            detail = None
        if detail:
            return PageHealth(
                level=HealthLevel.HARD_ERROR,
                summary=summary,
                detail=str(detail),
            )

    # Soft errors next.
    for js, summary in _SOFT_ERROR_PROBES:
        try:
            detail = page.evaluate(js)
        except Exception:  # noqa: BLE001
            detail = None
        if detail:
            return PageHealth(
                level=HealthLevel.SOFT_ERROR,
                summary=summary,
                detail=str(detail),
            )

    # Loading.
    try:
        loading_detail = page.evaluate(_LOADING_PROBE)
    except Exception:  # noqa: BLE001
        loading_detail = None
    if loading_detail:
        return PageHealth(
            level=HealthLevel.LOADING,
            summary="EPIC is still loading",
            detail=str(loading_detail),
        )

    return PageHealth(level=HealthLevel.CLEAN)


def wait_for_loading_clear(page: Any, *, timeout_ms: int = 10_000) -> bool:
    """Poll until ``assert_clean`` reports non-LOADING.

    Returns True when loading is gone (or was never present), False on
    timeout. Does NOT raise on hard/soft errors — those are the caller's
    problem; ``safe_action`` handles them.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        h = assert_clean(page)
        if not h.is_loading:
            return True
        page.wait_for_timeout(200)
    return False


# ── Operator halt for soft errors ────────────────────────────────────────────


def _halt_for_operator(
    page: Any,
    health: PageHealth,
    *,
    context: str,
) -> str:
    """Surface a soft error to the operator via :func:`runtime.on_validation_halt`.

    Returns ``"proceed"`` (operator says they fixed it / want to skip the
    failing action) or ``"cancel"`` (abort the run).

    When no runtime is registered (test-script context), defaults to
    raising EntryCancelled so we don't silently swallow the error.
    """
    rt = _runtime.get_runtime()
    if rt is None:
        # Test context — fail loudly rather than guess.
        raise EntryCancelled(
            f"[{context}] EPIC soft error and no runtime to halt: "
            f"{health.summary} :: {health.detail[:200]}"
        )

    screenshot_path = None
    try:
        screenshot_path = rt.artifacts_dir / f"page_health_{int(time.time())}.png"
        page.screenshot(path=str(screenshot_path), full_page=False)
    except Exception:  # noqa: BLE001
        screenshot_path = None

    finding = _runtime.ValidationFinding(
        error_text=health.detail or health.summary,
        surface="portal_modal",
        screen_code="",
        expecting=context,
        screenshot_path=screenshot_path,
        sequence=len(rt.findings) + 1,
    )
    rt.findings.append(finding)
    try:
        return rt.on_validation_halt(finding)
    except EntryCancelled:
        raise
    except Exception as exc:  # noqa: BLE001
        _log.error("on_validation_halt callback failed: %s", exc)
        return "cancel"


# ── Action wrapper ───────────────────────────────────────────────────────────


@contextmanager
def safe_action(
    page: Any,
    *,
    context: str,
    pre_assert: bool = True,
    loading_timeout_ms: int = 10_000,
    post_assert_delay_ms: int = 200,
) -> Iterator[None]:
    """Wrap a single page action with health checks on both sides.

    Usage::

        with safe_action(page, context="IM Coverage: click rbtnScheduled"):
            page.locator('input[value="rbtnScheduled"]').click()

    Before the action:
      - Wait for any loading to clear (max *loading_timeout_ms*).
      - ``assert_clean`` — if soft/hard error, halt or cancel.

    After the action:
      - ``wait_for_timeout(post_assert_delay_ms)`` to let any new DOM
        churn settle.
      - Wait for loading to clear.
      - ``assert_clean`` — if soft/hard error, halt or cancel.

    Raises:
      :class:`EPICHardError` on a Program Alert (uncatchable by the step;
        bubbles to the worker which terminates the run).
      :class:`EntryCancelled` if the operator picks Cancel on a soft halt.
    """
    if pre_assert:
        wait_for_loading_clear(page, timeout_ms=loading_timeout_ms)
        pre = assert_clean(page)
        if pre.is_hard_error:
            _log.error("[%s] PRE: hard error — %s", context, pre.summary)
            raise EPICHardError(pre)
        if pre.is_soft_error:
            _log.warning("[%s] PRE: soft error — %s", context, pre.summary)
            choice = _halt_for_operator(page, pre, context=f"{context} (pre-check)")
            if choice != "proceed":
                raise EntryCancelled(
                    f"[{context}] operator cancelled on pre-check soft error"
                )

    yield

    if post_assert_delay_ms > 0:
        try:
            page.wait_for_timeout(post_assert_delay_ms)
        except Exception:  # noqa: BLE001
            pass
    wait_for_loading_clear(page, timeout_ms=loading_timeout_ms)
    post = assert_clean(page)
    if post.is_hard_error:
        _log.error("[%s] POST: hard error — %s", context, post.summary)
        raise EPICHardError(post)
    if post.is_soft_error:
        _log.warning("[%s] POST: soft error — %s", context, post.summary)
        choice = _halt_for_operator(page, post, context=f"{context} (post-check)")
        if choice != "proceed":
            raise EntryCancelled(
                f"[{context}] operator cancelled on post-check soft error"
            )


# ── Post-condition helpers ───────────────────────────────────────────────────


def wait_for(
    page: Any,
    js_condition: str,
    *,
    timeout_ms: int = 5_000,
    poll_ms: int = 150,
    label: str = "",
) -> bool:
    """Poll a JS predicate until truthy or timeout.

    *js_condition* is a JS expression returning truthy when the
    post-condition holds, e.g. ``"document.querySelector('foo') !== null"``.
    Returns True on satisfied, False on timeout. No exceptions raised
    on timeout — caller decides whether to escalate.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            ok = bool(page.evaluate(f"() => Boolean({js_condition})"))
        except Exception:  # noqa: BLE001
            ok = False
        if ok:
            return True
        page.wait_for_timeout(poll_ms)
    if label:
        _log.warning("wait_for(%s) timed out after %d ms", label, timeout_ms)
    return False


def assert_screen_code(
    page: Any, expected: str, *, timeout_ms: int = 5_000,
) -> None:
    """Block until EPIC's footer reports *expected* (case-insensitive).

    Raises :class:`EPICHardError` on timeout — we should never proceed
    when on the wrong screen. The footer screen code is the canonical
    source of truth for "which EPIC screen am I on right now".
    """
    want = expected.upper()
    js = (
        f"() => {{ "
        f"  const txt = (document.body && document.body.innerText) || ''; "
        f"  return new RegExp('\\\\b' + {want!r} + '\\\\b', 'i').test(txt); "
        f"}}"
    )
    if wait_for(page, js, timeout_ms=timeout_ms, label=f"screen_code={expected}"):
        return
    raise EPICHardError(PageHealth(
        level=HealthLevel.HARD_ERROR,
        summary=f"Expected screen {expected!r} but it never appeared",
        detail=f"timeout={timeout_ms}ms",
    ))


def verify_input_value(
    page: Any, selector: str, expected: str,
    *, normalize: Callable[[str], str] | None = None, timeout_ms: int = 2_000,
) -> bool:
    """Read an input's actual value and confirm it matches *expected*.

    Polls briefly because EPIC sometimes echoes a typed value back after
    a debounce + reformat (e.g. ``5000`` → ``5,000``). Pass *normalize*
    to compare under a custom mapping (e.g. strip commas).

    Returns True on match. False on timeout — caller decides whether to
    halt or retry.
    """
    norm = normalize or (lambda s: s)
    want = norm(expected)
    deadline = time.time() + timeout_ms / 1000.0
    last_seen = ""
    while time.time() < deadline:
        try:
            got = page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    return el ? (el.value ?? '') : '';
                }""",
                selector,
            )
            last_seen = str(got or "")
        except Exception:  # noqa: BLE001
            pass
        if norm(last_seen) == want:
            return True
        page.wait_for_timeout(150)
    _log.warning(
        "verify_input_value(%s): expected %r but saw %r after %d ms",
        selector, expected, last_seen, timeout_ms,
    )
    return False


def wait_for_grid_add_ready(
    page: Any, vlvw_name: str, *, timeout_ms: int = 15_000,
) -> bool:
    """Poll until a React grid's Add button is mounted, visible, and enabled.

    On EPIC's IM/BAUT screens the SPA transition is silent — no spinner, no
    aria-busy. The reliable signal that a section finished mounting is the
    grid's own Add button: ``[data-test="<vlvw_name>_add"]`` showing up,
    being visible, and not disabled. Confirmed live 2026-05-26 via DOM
    dumps: the button literally does not exist in the DOM until the
    section is ready to take input.

    Returns True on ready, False on timeout. No exceptions on timeout —
    caller decides whether to escalate.
    """
    js = f"""() => {{
        const el = document.querySelector('[data-test="{vlvw_name}_add"]');
        if (!el) return false;
        if (el.offsetParent === null) return false;
        if (el.disabled === true) return false;
        if (el.getAttribute('aria-disabled') === 'true') return false;
        return true;
    }}"""
    return wait_for(
        page, js, timeout_ms=timeout_ms, poll_ms=150,
        label=f"{vlvw_name}_add ready",
    )


def verify_radio_checked(
    page: Any, value_attr: str, *, timeout_ms: int = 2_000,
) -> bool:
    """Poll until ``input[type=radio][value=value_attr]`` is ``.checked``.

    Returns True on confirmed-checked, False on timeout.
    """
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            checked = bool(page.evaluate(
                """(v) => {
                    const el = document.querySelector(
                        `input[type="radio"][value="${v}"]`
                    );
                    return !!(el && el.checked);
                }""",
                value_attr,
            ))
        except Exception:  # noqa: BLE001
            checked = False
        if checked:
            return True
        page.wait_for_timeout(120)
    return False

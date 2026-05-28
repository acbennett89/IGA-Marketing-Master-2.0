"""step_navigate_to_entry_start.py — pre-flight nav before policy entry.

Public entry point :func:`run` is called by the GUI just before the entry
walker fires. It detects where EPIC is right now and, if needed, navigates
to **Marketed Policies for the correct client**, then reports back what it
landed on so the caller can decide what to do next (open an MMS, create one,
or prompt the operator).

Outcome contract
----------------

The caller receives a :class:`NavOutcome` with one of these statuses:

* ``MMS_OPEN`` — the right account is loaded and an MMS detail screen is
  showing. Caller typically pops a confirm dialog and proceeds with entry.
* ``ON_MARKETED_POLICIES`` — the right account is loaded and the Marketed
  Policies list view is showing. Caller typically prompts the operator to
  open the desired MMS (or runs the MMS-create flow if checked).
* ``LOGIN_REQUIRED`` — EPIC isn't past login. Hard halt.
* ``FAILED`` — something else went wrong. ``error`` carries detail.

Decision tree (matches Andrew's spec 2026-05-22):

    probe →
      no account loaded OR wrong account:
        → step_account_lookup_nav → step_account_search → step_account_select_row
        → step_account_sidebar_nav("Policies") → step_view_picker("Marketed")
        → return ON_MARKETED_POLICIES

      right account, MMS detail open (screen code starts MK*):
        → return MMS_OPEN

      right account, on POLVIEW but view != "Marketed":
        → step_view_picker("Marketed")
        → return ON_MARKETED_POLICIES

      right account, not on Policies / Marketed:
        → step_account_sidebar_nav("Policies") → step_view_picker("Marketed")
        → return ON_MARKETED_POLICIES

Cancel + validation halts: each underlying nav step already calls
``checkpoint()``, so user-initiated Cancel and EPIC validation errors bubble
up via ``EntryCancelled`` automatically. No extra handling needed here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from iga_marketing_master_2.epic_steps import (
    step_account_lookup_nav,
    step_account_search,
    step_account_select_row,
    step_account_sidebar_nav,
    step_view_picker,
)
from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.navigate_to_entry_start")


class NavStatus(Enum):
    MMS_OPEN              = "mms_open"
    ON_MARKETED_POLICIES  = "on_marketed_policies"
    LOGIN_REQUIRED        = "login_required"
    FAILED                = "failed"


@dataclass(slots=True)
class NavOutcome:
    """Result returned by :func:`run`."""

    status: NavStatus
    loaded_lookup_code: str = ""
    """Lookup code EPIC currently shows in the account-info header."""
    loaded_account_name: str = ""
    """Account name from the document title (best-effort)."""
    screen_code: str = ""
    """Footer screen code at the moment of probe (e.g. ``POLVIEW``,
    ``MKMMSDET``). Best-effort — empty when no recognizable code was visible."""
    view_label: str = ""
    """Current view picker value when on POLVIEW (e.g. ``Marketed``). Empty
    when not applicable."""
    error: str = ""
    """Operator-facing reason when status is ``FAILED``."""


# ── Probe ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _Probe:
    logged_in: bool
    account_loaded: bool
    loaded_lookup_code: str
    loaded_account_name: str
    screen_code: str
    view_label: str
    selected_sidebar: str


_PROBE_SETTLE_TIMEOUT_MS = 5_000
_PROBE_POLL_INTERVAL_MS  = 250


def _raw_probe(page: Any) -> dict:
    """Single-shot read of the probe state — no waiting, no retries.

    Returns the raw dict the JS evaluator produced. Used both by the
    public :func:`_probe` (which retries) and by debug logging that wants
    to see *exactly* what the page looks like at one moment.
    """
    return page.evaluate(
        """() => {
            const out = {
                logged_in: !!document.querySelector('div.main-button[title="Locate (F2)"]'),
                account_loaded: false,
                loaded_lookup_code: '',
                loaded_account_name: '',
                screen_code: '',
                view_label: '',
                selected_sidebar: '',
                _diag: {
                    title: (document.title || '').trim(),
                    has_account_info_el: !!document.querySelector('account-info'),
                    has_locate_btn:      !!document.querySelector('div.main-button[title="Locate (F2)"]'),
                    has_sidebar_any:     !!document.querySelector('a.sidebar-button.level-1'),
                    has_selected_sidebar:!!document.querySelector('a.sidebar-button.level-1.selected'),
                    has_view_picker:     !!document.querySelector('span.context-menu-title'),
                },
            };
            const accInfo = document.querySelector('account-info');
            if (accInfo) {
                out.account_loaded = true;
                const lc = accInfo.querySelector('.lookup-code');
                if (lc) out.loaded_lookup_code = (lc.innerText || '').trim();
            }
            // Title is "<LOOKUP> - <Account Name>" when an account is loaded.
            // Use this as the canonical source of both fields — the
            // ``account-info`` element renders its ``.lookup-code`` child a
            // bit later than the title flips, so trusting the element alone
            // gives us false-empty reads (see 2026-05-22 race).
            const title = (document.title || '').trim();
            const m = title.match(/^([^ ]+)\\s*-\\s*(.+)$/);
            if (m) {
                if (m[1] && !out.loaded_lookup_code) {
                    out.loaded_lookup_code = m[1].trim();
                }
                if (m[2]) out.loaded_account_name = m[2].trim();
            }
            // Screen code: look in body text for the known footer pattern.
            const bodyText = (document.body && document.body.innerText) || '';
            const codes = bodyText.match(/\\b(MK[A-Z0-9]{3,12}|CHM-[A-Z0-9]{3,12}|POLVIEW|HOME|LOCATE)\\b/g);
            if (codes && codes.length) {
                // Prefer non-generic codes (MK* / CHM-* over HOME).
                const specific = codes.find(c => !/HOME|LOCATE/.test(c));
                out.screen_code = specific || codes[0];
            }
            const viewEl = document.querySelector('span.context-menu-title');
            if (viewEl) out.view_label = (viewEl.innerText || '').trim();
            const sel = document.querySelector('a.sidebar-button.level-1.selected');
            if (sel) out.selected_sidebar = (sel.innerText || '').trim();
            return out;
        }"""
    )


def _probe(page: Any) -> _Probe:
    """Read state from the current page, retrying briefly when it looks empty.

    EPIC's screens render in two phases: the document title flips quickly,
    but the body (account-info element, sidebar, view picker, footer screen
    code) lags by a few hundred ms — especially right after a new-account
    save when the React tree is rebuilding from scratch. A single probe
    immediately after that title flip will see ``account_loaded=False``,
    empty screen code, empty sidebar — and the caller will mistakenly try
    to re-locate the account.

    We poll up to ``_PROBE_SETTLE_TIMEOUT_MS`` for at least *some* body
    signal (account_loaded OR a non-empty screen_code OR a selected
    sidebar) before returning. Diagnostic counters on every probe attempt
    are logged at DEBUG so we can see how long the body took to mount.
    """
    import time as _time

    deadline = _time.time() + _PROBE_SETTLE_TIMEOUT_MS / 1000.0
    raw = _raw_probe(page)
    attempt = 1
    _log.debug(
        "probe attempt 1 — title=%r dom=%s",
        raw.get("_diag", {}).get("title"),
        raw.get("_diag", {}),
    )

    while _time.time() < deadline:
        # Strong signals — settle on these. ``account_loaded`` (element
        # exists) alone is *not* enough: the element renders before its
        # ``.lookup-code`` child has text. Insist on something concrete
        # so the dispatcher doesn't read empty and assume "wrong account".
        if (
            raw.get("loaded_lookup_code")
            or raw.get("loaded_account_name")
            or raw.get("screen_code")
            or raw.get("selected_sidebar")
        ):
            break
        page.wait_for_timeout(_PROBE_POLL_INTERVAL_MS)
        attempt += 1
        raw = _raw_probe(page)
        _log.debug(
            "probe attempt %d — title=%r dom=%s",
            attempt,
            raw.get("_diag", {}).get("title"),
            raw.get("_diag", {}),
        )
    else:
        _log.warning(
            "probe never observed a body signal within %d ms — "
            "returning whatever we last read (likely empty)",
            _PROBE_SETTLE_TIMEOUT_MS,
        )

    if attempt > 1:
        _log.info(
            "probe settled after %d attempt(s) (~%d ms)",
            attempt, (attempt - 1) * _PROBE_POLL_INTERVAL_MS,
        )

    return _Probe(
        logged_in=bool(raw.get("logged_in")),
        account_loaded=bool(raw.get("account_loaded")),
        loaded_lookup_code=str(raw.get("loaded_lookup_code") or ""),
        loaded_account_name=str(raw.get("loaded_account_name") or ""),
        screen_code=str(raw.get("screen_code") or ""),
        view_label=str(raw.get("view_label") or ""),
        selected_sidebar=str(raw.get("selected_sidebar") or ""),
    )


def _is_mms_detail(screen_code: str) -> bool:
    """MMS detail screens all begin with MK (MKMMSDET, MKADMSTR, etc.)."""
    return bool(screen_code) and screen_code.upper().startswith("MK")


_POLICIES_FRAME_SETTLE_MS  = 10_000
_MARKETED_GRID_SETTLE_MS   = 10_000
_POST_NAV_PAD_MS           = 1_500  # final settle pad after each nav step


def _wait_for_policies_frame(page: Any) -> bool:
    """Wait for the Policies frame's view-picker trigger to render.

    Sidebar navigation reports success the moment the sidebar button
    flips to ``.selected`` — but EPIC keeps rendering the destination
    frame for another second or two after that. The frame also embeds
    additional ``.frame-title-text.context-menu-link`` dropdowns (e.g.
    an account-type scope filter with options ``Client/All/Line``) that
    render *before* the view picker does — opening the view picker too
    soon picks one of those by mistake (saw it in the 2026-05-22 log).

    We poll up to ``_POLICIES_FRAME_SETTLE_MS`` for the *specific*
    trigger whose ``.title`` text equals "Policies" — that's the
    canonical view picker. After the trigger appears we burn an extra
    ``_POST_NAV_PAD_MS`` so the rest of the frame finishes mounting
    before the caller acts on it (Program Alert ``POLVIEW-...`` seen
    2026-05-22 when we acted too fast after this returned True).

    Returns True when that trigger appears, False on timeout (caller
    proceeds anyway).
    """
    import time as _time

    deadline = _time.time() + _POLICIES_FRAME_SETTLE_MS / 1000.0
    while _time.time() < deadline:
        try:
            ready = page.evaluate(
                """() => {
                    const fra = document.querySelector(
                        '[data-automation-id="fraPolicies"]'
                    );
                    if (!fra || fra.offsetParent === null) return false;
                    // Trigger whose .title is literally "Policies" — that's
                    // the view picker; everything else inside fraPolicies
                    // (scope filter, etc.) has different .title text.
                    const triggers = Array.from(fra.querySelectorAll(
                        'span.frame-title-text.context-menu-link'
                    )).filter(t => t.offsetParent !== null);
                    return triggers.some(t => {
                        const titleEl = t.querySelector('.title');
                        const txt = (titleEl?.innerText || '').trim().toLowerCase();
                        return txt === 'policies';
                    });
                }"""
            )
            if ready:
                page.wait_for_timeout(_POST_NAV_PAD_MS)
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)
    _log.warning(
        "Policies frame view-picker trigger did not appear within %d ms "
        "— proceeding anyway",
        _POLICIES_FRAME_SETTLE_MS,
    )
    return False


def _wait_for_marketed_grid(page: Any) -> bool:
    """Wait for the Marketed-Policies content grid to render after a view
    switch. The view-picker step returns as soon as the title text flips
    to "Marketed", but EPIC keeps rebuilding the grid for another second
    or two — clicking into it during that window has triggered a hard
    EPIC ``POLVIEW-...`` Program Alert.

    Polls for ``fraMasterMarketingSubmissions`` (the Marketed-Policies
    grid container, also the pre-condition of ``step_mms_create``) to
    be visible, then burns ``_POST_NAV_PAD_MS`` of slack.
    """
    import time as _time

    deadline = _time.time() + _MARKETED_GRID_SETTLE_MS / 1000.0
    while _time.time() < deadline:
        try:
            ready = page.evaluate(
                """() => {
                    const grid = document.querySelector(
                        '[data-automation-id="fraMasterMarketingSubmissions"]'
                    );
                    return !!(grid && grid.offsetParent !== null);
                }"""
            )
            if ready:
                page.wait_for_timeout(_POST_NAV_PAD_MS)
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)
    _log.warning(
        "Marketed-policies grid did not render within %d ms — "
        "proceeding anyway", _MARKETED_GRID_SETTLE_MS,
    )
    return False


# ── Public API ──────────────────────────────────────────────────────────────


def run(
    page: Any,
    *,
    lookup_code: str,
    expected_account_name: str = "",
) -> NavOutcome:
    """Drive EPIC to a known-good starting point for entry.

    :param page: live Playwright Page bound to the EPIC tab.
    :param lookup_code: target client's EPIC lookup code (from
        ``state.insured.lookup_code``).
    :param expected_account_name: optional sanity-check hint; only used in
        debug logging when the lookup code mismatches but a name was provided.
    """
    code = (lookup_code or "").strip()
    if not code:
        return NavOutcome(
            status=NavStatus.FAILED,
            error=("No EPIC lookup code provided. Set the client's lookup_code "
                   "in the Client section before running entry."),
        )

    # Initial guard — checkpoint() catches cancel + any EPIC modal already up.
    checkpoint(page, "Entry pre-flight: probe current EPIC location")

    probe = _probe(page)
    _log.info(
        "pre-flight probe: logged_in=%s account=%r screen=%r view=%r sidebar=%r",
        probe.logged_in, probe.loaded_lookup_code, probe.screen_code,
        probe.view_label, probe.selected_sidebar,
    )

    if not probe.logged_in:
        return NavOutcome(
            status=NavStatus.LOGIN_REQUIRED,
            error="EPIC is not past login. Sign in and try again.",
            screen_code=probe.screen_code,
        )

    target_account_loaded = (
        probe.account_loaded
        and probe.loaded_lookup_code.casefold() == code.casefold()
    )

    # ── Path A: no account loaded OR wrong account ───────────────────────────
    if not target_account_loaded:
        if probe.account_loaded:
            _log.info(
                "Wrong account loaded (%r) — switching to %r",
                probe.loaded_lookup_code, code,
            )
        else:
            _log.info("No account loaded — opening %r", code)
        try:
            if not step_account_lookup_nav.run(page):
                return NavOutcome(
                    status=NavStatus.FAILED,
                    error="Could not open the Account Locate screen.",
                )
            if not step_account_search.run(page, lookup_code=code):
                return NavOutcome(
                    status=NavStatus.FAILED,
                    error=f"Account search for {code!r} failed.",
                )
            sel = step_account_select_row.run(page, lookup_code=code)
            sel_outcome = getattr(sel, "outcome", None)
            sel_name = getattr(sel_outcome, "name", str(sel_outcome))
            if sel_name != "SELECTED":
                return NavOutcome(
                    status=NavStatus.FAILED,
                    error=(f"Account {code!r} not found in search results "
                           f"({sel_name}). "
                           f"{getattr(sel, 'error', '') or ''}").strip(),
                )
            if not step_account_sidebar_nav.run(page, section="Policies"):
                return NavOutcome(
                    status=NavStatus.FAILED,
                    error="Could not navigate to the Policies sidebar section.",
                )
            _wait_for_policies_frame(page)
            if not step_view_picker.run(
                page,
                view="Marketed",
                frame_id="fraPolicies",
                screen_name="Policies",
            ):
                return NavOutcome(
                    status=NavStatus.FAILED,
                    error='Could not switch the Policies view to "Marketed".',
                )
            _wait_for_marketed_grid(page)
        except EntryCancelled:
            raise
        # Re-probe so the caller sees the new screen + landed account info.
        after = _probe(page)
        return NavOutcome(
            status=NavStatus.ON_MARKETED_POLICIES,
            loaded_lookup_code=after.loaded_lookup_code,
            loaded_account_name=after.loaded_account_name,
            screen_code=after.screen_code,
            view_label=after.view_label,
        )

    # ── Right account loaded — what screen? ──────────────────────────────────

    if _is_mms_detail(probe.screen_code):
        return NavOutcome(
            status=NavStatus.MMS_OPEN,
            loaded_lookup_code=probe.loaded_lookup_code,
            loaded_account_name=probe.loaded_account_name,
            screen_code=probe.screen_code,
            view_label=probe.view_label,
        )

    # On POLVIEW but the wrong view (e.g. Current/Renewed) — switch.
    if (probe.screen_code.upper() == "POLVIEW"
            and probe.view_label.casefold() != "marketed"):
        _log.info(
            "On POLVIEW but view is %r — switching to Marketed",
            probe.view_label,
        )
        try:
            _wait_for_policies_frame(page)
            if not step_view_picker.run(
                page,
                view="Marketed",
                frame_id="fraPolicies",
                screen_name="Policies",
            ):
                return NavOutcome(
                    status=NavStatus.FAILED,
                    error='Could not switch the Policies view to "Marketed".',
                    loaded_lookup_code=probe.loaded_lookup_code,
                    loaded_account_name=probe.loaded_account_name,
                    screen_code=probe.screen_code,
                )
            _wait_for_marketed_grid(page)
        except EntryCancelled:
            raise
        after = _probe(page)
        return NavOutcome(
            status=NavStatus.ON_MARKETED_POLICIES,
            loaded_lookup_code=after.loaded_lookup_code,
            loaded_account_name=after.loaded_account_name,
            screen_code=after.screen_code,
            view_label=after.view_label,
        )

    # Right account but not on Policies / Marketed — drive there.
    _log.info(
        "Right account but not on Marketed Policies (sidebar=%r screen=%r) "
        "— navigating",
        probe.selected_sidebar, probe.screen_code,
    )
    try:
        if not step_account_sidebar_nav.run(page, section="Policies"):
            return NavOutcome(
                status=NavStatus.FAILED,
                error="Could not navigate to the Policies sidebar section.",
                loaded_lookup_code=probe.loaded_lookup_code,
                loaded_account_name=probe.loaded_account_name,
                screen_code=probe.screen_code,
            )
        _wait_for_policies_frame(page)
        if not step_view_picker.run(
            page,
            view="Marketed",
            frame_id="fraPolicies",
            screen_name="Policies",
        ):
            return NavOutcome(
                status=NavStatus.FAILED,
                error='Could not switch the Policies view to "Marketed".',
                loaded_lookup_code=probe.loaded_lookup_code,
                loaded_account_name=probe.loaded_account_name,
                screen_code=probe.screen_code,
            )
        _wait_for_marketed_grid(page)
    except EntryCancelled:
        raise
    after = _probe(page)
    return NavOutcome(
        status=NavStatus.ON_MARKETED_POLICIES,
        loaded_lookup_code=after.loaded_lookup_code,
        loaded_account_name=after.loaded_account_name,
        screen_code=after.screen_code,
        view_label=after.view_label,
    )


def probe_state(page: Any) -> NavOutcome:
    """Read-only probe — return current EPIC state without navigating.

    Useful for re-checking after the operator manually opens an MMS following
    an ``ON_MARKETED_POLICIES`` prompt.
    """
    probe = _probe(page)
    if not probe.logged_in:
        return NavOutcome(
            status=NavStatus.LOGIN_REQUIRED,
            screen_code=probe.screen_code,
        )
    if _is_mms_detail(probe.screen_code):
        status = NavStatus.MMS_OPEN
    elif probe.screen_code.upper() == "POLVIEW" and probe.view_label.casefold() == "marketed":
        status = NavStatus.ON_MARKETED_POLICIES
    else:
        status = NavStatus.FAILED
    return NavOutcome(
        status=status,
        loaded_lookup_code=probe.loaded_lookup_code,
        loaded_account_name=probe.loaded_account_name,
        screen_code=probe.screen_code,
        view_label=probe.view_label,
    )

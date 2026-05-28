"""Shared low-level helpers for React grid sections (vlvw* pattern).

Extracted from step_inland_marine.py on 2026-05-26 so the same lenient
field-fill + grid-grow + address-tail logic can be reused across LOBs
(Property, Business Auto, GL, Umbrella) when each gets its own AI flow
rewritten to match the IM pattern hardened during the 2026-05-26 e2e.

Imports kept minimal — only Playwright + runtime + ``_page_health`` —
so any epic_step module can import without creating a cycle.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from playwright.sync_api import Page

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps._page_health import (
    safe_action,
    verify_input_value,
)

_log = logging.getLogger("iga.epic_steps.grid_helpers")

AI_REASON_MAX_CHARS = 30
AC_DESC_MAX_CHARS   = 30


def keyboard_fill(page: Page, field_id: str, value: str) -> None:
    """Fill a React `__textField`-style input with real keystrokes so EPIC marks it dirty.

    Ctrl+A → Delete → type (delay=25) → Tab → 300 ms settle. Blank values
    are skipped. Catches and logs any exception so the caller can still
    run the verify path (which raises EntryCancelled on actual failure).
    """
    if not value:
        _log.debug("kb_fill #%s: skipped (blank)", field_id)
        return
    try:
        inp = page.locator(f'#{field_id}')
        if not inp.count():
            _log.warning("kb_fill #%s: element not found", field_id)
            return
        inp.first.click(timeout=2_000)
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
        page.keyboard.type(value, delay=25)
        page.keyboard.press("Tab")
        page.wait_for_timeout(300)
        _log.debug("kb_fill #%s = %r OK", field_id, value)
    except Exception as exc:  # noqa: BLE001
        _log.warning("kb_fill #%s = %r FAILED: %s", field_id, value, exc)


def fill_field_verified(
    page: Page,
    field_id: str,
    value: str,
    row_idx: int,
    label: str,
    *,
    normalize: Optional[Callable[[str], str]] = None,
    section: str = "Row",
    lenient: bool = False,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Per-field write + verify for any vlvw grid row.

    Generalised from ``step_inland_marine._fill_im_field``.  ``section``
    is a short tag used in log lines (e.g. "Sched", "Unsched", "AI").

    Verification mode:
      * ``lenient=False`` (default): strict — verify the field's value
        matches *value* (after optional *normalize*). Mismatch raises
        :class:`EntryCancelled`.
      * ``lenient=True``: only require the field is non-empty. Used
        when EPIC normalizes input (USPS street canonicalization,
        truncation, etc.). The actual stored value is logged.

    Blank values are skipped (DEBUG log line).
    """
    log = logger or _log
    if not value:
        log.debug("%s #%d: %s blank — skipping", section, row_idx, label)
        return
    log.debug("%s #%d: filling %s (#%s) = %r", section, row_idx, label, field_id, value)
    with safe_action(page, context=f"{section} #{row_idx}: fill {label}={value!r}"):
        keyboard_fill(page, field_id, value)

    if lenient:
        try:
            actual = page.evaluate(
                "(sel) => { const e = document.querySelector(sel); "
                "return e ? (e.value ?? '') : ''; }",
                f"#{field_id}",
            )
        except Exception:  # noqa: BLE001
            actual = ""
        if not str(actual or "").strip():
            log.error(
                "%s #%d: %s left field blank (wanted %r)",
                section, row_idx, label, value,
            )
            raise EntryCancelled(
                f"{section} #{row_idx}: {label}={value!r} did not save"
            )
        if str(actual).strip() != value.strip():
            log.info(
                "%s #%d: %s — EPIC normalized %r → %r (lenient, accepted)",
                section, row_idx, label, value, actual,
            )
        else:
            log.debug("%s #%d: %s verified OK", section, row_idx, label)
        return

    if not verify_input_value(
        page, f"#{field_id}", value,
        normalize=normalize, timeout_ms=2_000,
    ):
        log.error(
            "%s #%d: %s value did not stick (wanted %r)",
            section, row_idx, label, value,
        )
        raise EntryCancelled(
            f"{section} #{row_idx}: {label}={value!r} did not save"
        )
    log.debug("%s #%d: %s verified OK", section, row_idx, label)


def grid_row_count(page: Page, vlvw_name: str) -> int:
    """Return a vlvw grid's data-row count (max of DOM rows + footer-label count).

    Footer regex is scoped to ``[data-test="<vlvw_name>-footer"]`` so a
    sibling grid's footer can't bleed in when the section transitions.
    """
    try:
        dom_count = int(page.evaluate(
            f"""() => document.querySelectorAll('[data-test^="{vlvw_name}-focusable-row-"]').length"""
        ))
    except Exception:  # noqa: BLE001
        dom_count = 0
    try:
        label_count = int(page.evaluate(
            f"""() => {{
                const footer = document.querySelector('[data-test="{vlvw_name}-footer"]');
                if (!footer) return -1;
                const txt = footer.textContent || '';
                const m = txt.match(/(\\d+)\\s+Items?\\b/);
                return m ? parseInt(m[1], 10) : -1;
            }}"""
        ))
    except Exception:  # noqa: BLE001
        label_count = -1
    return max(dom_count, label_count if label_count >= 0 else 0)


def wait_grid_grew(
    page: Page,
    vlvw_name: str,
    rows_before: int,
    *,
    timeout_ms: int = 5_000,
    poll_ms: int = 150,
) -> bool:
    """Poll until the grid's row count exceeds *rows_before*."""
    import time as _t
    deadline = _t.time() + timeout_ms / 1000.0
    while _t.time() < deadline:
        if grid_row_count(page, vlvw_name) > rows_before:
            return True
        page.wait_for_timeout(poll_ms)
    return False


def address_tail_after_normalization(original: str, epic_kept: str) -> str:
    """Compute the suite/unit suffix EPIC stripped from a streetLine input.

    EPIC's adePrimary-streetLine widget runs USPS canonicalization on blur
    ("Place" → "Pl"), and drops trailing tokens it doesn't recognize as a
    USPS suffix. Tokenize both strings on whitespace; the first
    ``len(epic_kept_tokens)`` tokens of *original* are assumed to be what
    EPIC's kept value represents; everything after is the tail.

    Returns the tail joined with single spaces, or "" when there is no
    extra tail (EPIC kept the full street, or only abbreviated in place).
    """
    o_tokens = (original or "").split()
    e_tokens = (epic_kept or "").split()
    if len(o_tokens) <= len(e_tokens):
        return ""
    return " ".join(o_tokens[len(e_tokens):]).strip()


def read_input_value(page: Page, selector: str) -> str:
    """One-shot read of an input's current value (returns "" on failure)."""
    try:
        v = page.evaluate(
            "(sel) => { const e = document.querySelector(sel); "
            "return e ? (e.value ?? '') : ''; }",
            selector,
        )
        return str(v or "")
    except Exception:  # noqa: BLE001
        return ""


def fill_validated_address_then_manual(
    page: Page,
    *,
    street: str,
    city: str,
    state_code: str,
    zip_code: str,
    full_address: str,
    row_idx: int,
    section: str = "AI",
    street_id: str = "adePrimary-streetLine",
    city_id: str = "adePrimary-city",
    zip_id: str = "adePrimary-zipCode",
    address2_id: str = "adePrimary-address2",
    state_combo_id: str = "state",
    fill_validated_fn=None,
    fill_combo_fn=None,
    logger: Optional[logging.Logger] = None,
) -> None:
    """Validated-lookup-first, manual-lenient-fallback address fill.

    The validated path (SmartyStreets-style suggestion dropdown) is
    tried first. On miss — common for "Ste 100" / "PO Box" suffixes
    USPS rejects — we fall back to per-field lenient fills on the
    streetLine / city / zip fields plus the state combo, and push
    any EPIC-dropped tail onto address2 so the info isn't lost.

    *fill_validated_fn* is the LOB's existing ``_fill_validated_address``
    (signature: ``(page, street, full_address) -> bool``).  When
    omitted, only the manual path runs.

    *fill_combo_fn* is the LOB's React combo helper (signature:
    ``(page, combo_id, value) -> None``).  Omit to skip the state combo.
    """
    log = logger or _log

    validated_ok = False
    if fill_validated_fn is not None:
        try:
            validated_ok = bool(fill_validated_fn(
                page, street or full_address, full_address,
            ))
        except Exception as exc:  # noqa: BLE001
            log.debug("%s #%d: validated address path raised: %s", section, row_idx, exc)

    if validated_ok:
        log.debug("%s #%d: address filled via validated lookup", section, row_idx)
        return

    log.info(
        "%s #%d: validated lookup returned no rows — manual address fill",
        section, row_idx,
    )

    if street:
        fill_field_verified(
            page, street_id, street, row_idx, "addr.street",
            section=section, lenient=True, logger=log,
        )
        epic_street = read_input_value(page, f"#{street_id}")
        tail = address_tail_after_normalization(street, epic_street)
        if tail:
            log.info(
                "%s #%d: EPIC dropped %r from streetLine — pushing to address2",
                section, row_idx, tail,
            )
            fill_field_verified(
                page, address2_id, tail, row_idx, "addr.line2",
                section=section, lenient=True, logger=log,
            )

    if city:
        fill_field_verified(
            page, city_id, city, row_idx, "addr.city",
            section=section, lenient=True, logger=log,
        )
    if zip_code:
        fill_field_verified(
            page, zip_id, zip_code, row_idx, "addr.zip",
            section=section, logger=log,
        )
    if state_code and fill_combo_fn is not None:
        try:
            with safe_action(
                page, context=f"{section} #{row_idx}: combo state={state_code}",
            ):
                fill_combo_fn(page, state_combo_id, state_code)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "%s #%d: state combo %r failed: %s",
                section, row_idx, state_code, exc,
            )


def abbreviate_if_too_long(
    raw: str,
    *,
    max_chars: int,
    row_idx: int,
    label: str = "description",
    section: str = "Row",
    logger: Optional[logging.Logger] = None,
) -> str:
    """Pass-through if *raw* fits, otherwise call Claude to abbreviate.

    Mirrors the IM Unscheduled description-short pattern. The Claude
    response is in-process cached by ``claude_client.abbreviate_for_epic``.
    Returns the input verbatim on Claude failure so the caller can
    still send something (EPIC will then truncate to its maxlength).
    """
    log = logger or _log
    raw = (raw or "").strip()
    if not raw or len(raw) <= max_chars:
        return raw
    try:
        from iga_marketing_master_2.claude_client import abbreviate_for_epic
        short = abbreviate_for_epic(raw, max_chars=max_chars)
        log.info(
            "%s #%d: %s abbreviated (%d → %d chars): %r → %r",
            section, row_idx, label, len(raw), len(short), raw, short,
        )
        return short
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "%s #%d: %s abbreviation via Claude failed (%s) — sending raw, EPIC will truncate",
            section, row_idx, label, exc,
        )
        return raw

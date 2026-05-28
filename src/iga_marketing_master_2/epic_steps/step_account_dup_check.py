"""step_account_dup_check.py — scan Account Locate results for a duplicate.

Pre-condition
-------------
The browser is on the EPIC Account Locate screen and a search has been
submitted via :mod:`step_account_search` (typically with
``search_by="Account/Business Name"``). The results grid may still be
populating — the step waits up to *settle_timeout_ms* for rows to render.

What this step does
-------------------
1.  Waits briefly for the results grid to settle.
2.  Reads every visible row out of ``vlvwResults`` and parses the Lookup Code
    and Account Name from each (columns 1 and 2).
3.  Normalises the target name (case-fold + whitespace collapse) and compares
    against every row's normalised account name.
4.  Returns a :class:`DuplicateCheckResult`:

    * ``NO_MATCH``      — zero result rows. Safe to create.
    * ``EXACT``         — at least one row's normalised name == target.
                          Caller should block creation and prompt operator.
    * ``FUZZY_MATCHES`` — at least one row present but none matches exactly.
                          Caller surfaces the rows to the operator for review.
    * ``ERROR``         — pre-condition failed or DOM was unexpectedly shaped.

The step is intentionally headless — it does **not** open a popup, click a row,
or block creation on its own. The caller (GUI) decides what to do with the
outcome and the row list.

Selector notes (confirmed via live CDP inspection 2026-05-22)
-------------------------------------------------------------
- Results grid:   ``[data-automation-id="fraResults"]``
- Result rows:    ``div[data-automation-id^="vlvwResults body-row"]``
- Row text layout (cells joined by ``\\n``)::

      GORBILL-01\\nBill Goran Widgets LLC\\nInsured\\nActive\\n6640 Carothers Pkwy\\n...

  Index 0 = Lookup Code, 1 = Account Name, 2 = Client Type, 3 = Account Status.

Name normalisation
------------------
Two names are considered "exactly the same" when their case-folded,
whitespace-collapsed forms are equal. Examples that DO match::

    "Acme Corp"     == "ACME CORP"
    "Acme  Corp "   == "Acme Corp"

Examples that do NOT match (and fall into FUZZY_MATCHES instead)::

    "Acme Corp"      != "Acme Corp LLC"
    "Acme Corp"      != "Acme Corporation"
    "Acme Corp Inc." != "Acme Corp Inc"   # trailing punctuation matters

The conservative normalisation is deliberate — operator review on near-misses
is the right default. Add suffix-stripping later (and bump the version) if
false-negatives become a pain.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum

from playwright.sync_api import Page

from iga_marketing_master_2.epic_steps.runtime import EntryCancelled
from iga_marketing_master_2.epic_steps.validation_check import checkpoint

_log = logging.getLogger("iga.epic_steps.account_dup_check")

_SCREEN_SEL  = '[data-automation-id="AccountLocate"]'
_ROW_SEL     = 'div[data-automation-id^="vlvwResults body-row"]'

_DEFAULT_SETTLE_MS = 8_000


class DupCheckOutcome(Enum):
    """Outcome of a duplicate-name search."""

    NO_MATCH      = "no_match"
    """Search returned zero rows — name appears unused."""

    EXACT         = "exact"
    """At least one row's normalised name equals the normalised target."""

    FUZZY_MATCHES = "fuzzy_matches"
    """Rows were returned but none matches exactly — operator should review."""

    ERROR         = "error"
    """Pre-condition failed or the results grid could not be read."""


@dataclass(slots=True)
class AccountResult:
    """One row from the Account Locate results grid."""

    lookup_code:  str
    account_name: str
    client_type:  str = ""
    status:       str = ""
    address:      str = ""

    def display(self) -> str:
        """Single-line summary suitable for an operator-facing popup."""
        parts = [self.lookup_code, self.account_name]
        if self.client_type:
            parts.append(self.client_type)
        if self.status:
            parts.append(self.status)
        return " — ".join(p for p in parts if p)


@dataclass(slots=True)
class DuplicateCheckResult:
    """Structured outcome of the duplicate check.

    *matches* contains every row scraped from the results grid, in display
    order. On ``EXACT`` the first match is the exact one; on ``FUZZY_MATCHES``
    all rows are near-misses by the search filter alone.
    """

    outcome: DupCheckOutcome
    target_name: str
    matches: list[AccountResult] = field(default_factory=list)
    error: str | None = None

    @property
    def exact_match(self) -> AccountResult | None:
        """Return the first exact-match row, or None when there isn't one."""
        if self.outcome is not DupCheckOutcome.EXACT:
            return None
        target_norm = _normalise(self.target_name)
        for m in self.matches:
            if _normalise(m.account_name) == target_norm:
                return m
        return None


_WHITESPACE_RE = re.compile(r"\s+")


def _normalise(name: str) -> str:
    """Case-fold + collapse whitespace. See module docstring for rationale."""
    return _WHITESPACE_RE.sub(" ", (name or "").strip()).casefold()


def _parse_row(page: Page, row_locator) -> AccountResult:
    """Read one row's columns into an :class:`AccountResult`.

    Failures (hidden row, DOM mismatch) produce a result with empty fields,
    which the caller naturally filters out.
    """
    try:
        text = row_locator.evaluate("el => (el.innerText || '').trim()")
    except Exception:  # noqa: BLE001
        return AccountResult(lookup_code="", account_name="")
    if not text:
        return AccountResult(lookup_code="", account_name="")

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    def get(i: int) -> str:
        return lines[i] if i < len(lines) else ""

    # Address is multi-line (Address 1, Address 2?, City, State, ZIP) — join
    # the trailing columns into one string for display purposes.
    return AccountResult(
        lookup_code  = get(0),
        account_name = get(1),
        client_type  = get(2),
        status       = get(3),
        address      = " ".join(lines[4:]) if len(lines) > 4 else "",
    )


def run(
    page: Page,
    *,
    target_name: str,
    settle_timeout_ms: int = _DEFAULT_SETTLE_MS,
) -> DuplicateCheckResult:
    """Scan the current Account Locate results for *target_name*.

    The caller is expected to have already submitted a search whose criteria
    will surface candidate duplicates — typically a name-substring search via
    ``step_account_search.run(search_by="Account/Business Name", ...)``.

    *settle_timeout_ms* — how long to wait for at least one row to appear
    before declaring NO_MATCH. Eight seconds is the same default the
    select-row step uses.
    """
    name = (target_name or "").strip()
    if not name:
        return DuplicateCheckResult(
            outcome=DupCheckOutcome.ERROR,
            target_name="",
            error="target_name is empty",
        )

    checkpoint(page, f"Account: dup-check for {name!r}")

    # 0. Pre-condition: Account Locate screen must be open. Same probe as
    # step_account_search — title + marker, since the wrapper element has 0 px
    # dimensions and ``is_visible()`` lies about it.
    try:
        title_ok  = (page.title() or "").strip().lower() == "account locate"
        marker_ok = page.locator(_SCREEN_SEL).count() > 0
        if not (title_ok or marker_ok):
            return DuplicateCheckResult(
                outcome=DupCheckOutcome.ERROR,
                target_name=name,
                error=(
                    "Account Locate screen is not open. "
                    "Run step_account_lookup_nav + step_account_search first."
                ),
            )
    except Exception as exc:  # noqa: BLE001
        return DuplicateCheckResult(
            outcome=DupCheckOutcome.ERROR,
            target_name=name,
            error=f"Could not verify Account Locate screen: {exc}",
        )

    # 1. Wait for the results grid to settle — poll until at least one row
    # exists or *settle_timeout_ms* elapses. A 0-row outcome past the timeout
    # is NO_MATCH (a legitimate result, not an error).
    rows_loc = page.locator(_ROW_SEL)
    deadline = time.time() + settle_timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if rows_loc.count() > 0:
                break
        except EntryCancelled:
            raise
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(150)

    # 2. Enumerate and parse rows.
    try:
        n_rows = rows_loc.count()
    except Exception as exc:  # noqa: BLE001
        return DuplicateCheckResult(
            outcome=DupCheckOutcome.ERROR,
            target_name=name,
            error=f"Could not count result rows: {exc}",
        )

    if n_rows == 0:
        _log.info("Dup-check NO_MATCH for %r (zero rows)", name)
        return DuplicateCheckResult(
            outcome=DupCheckOutcome.NO_MATCH,
            target_name=name,
        )

    matches: list[AccountResult] = []
    for i in range(n_rows):
        row = _parse_row(page, rows_loc.nth(i))
        if row.lookup_code or row.account_name:
            matches.append(row)

    # 3. Classify.
    target_norm = _normalise(name)
    exact = any(_normalise(m.account_name) == target_norm for m in matches)

    if exact:
        _log.info(
            "Dup-check EXACT for %r — %d total row(s) in grid",
            name, len(matches),
        )
        return DuplicateCheckResult(
            outcome=DupCheckOutcome.EXACT,
            target_name=name,
            matches=matches,
        )

    if matches:
        _log.info(
            "Dup-check FUZZY_MATCHES for %r — %d row(s), none exact",
            name, len(matches),
        )
        return DuplicateCheckResult(
            outcome=DupCheckOutcome.FUZZY_MATCHES,
            target_name=name,
            matches=matches,
        )

    # Rows were counted but none parsed — treat as ERROR rather than
    # silently allowing creation.
    return DuplicateCheckResult(
        outcome=DupCheckOutcome.ERROR,
        target_name=name,
        error=f"{n_rows} result row(s) present but none could be parsed",
    )

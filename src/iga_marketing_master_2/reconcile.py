"""reconcile.py — Client tab ↔ Named Insureds reconciliation.

Pure logic, no Qt imports. The GUI hooks in at the end of every extraction run
to compare the Client-tab Insured singleton against the matching row in the
``account.named_insured`` repeatable and decide per-field what to do:

- both sides agree → noop
- one side blank, other has value → silent merge (fill the blank)
- both sides disagree (first-time) → ``conflict`` → operator picks in popup
- canon field disagrees with extraction → ``canon_extraction_diff`` → review popup

Match key is normalized name only. FEIN is intentionally NOT a fallback because
operators frequently leave the Client-tab FEIN blank — the Client tab gates
extraction, so reconciliation cannot require an extraction-only field.

See ``C:\\Users\\Andrew\\.claude\\plans\\eager-shimmying-hummingbird.md``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .state import (
    FieldRecord,
    SourceRef,
    State,
    normalize_entity_name as _state_normalize_entity_name,
)

# Sentinel source label written on auto-created NI rows (B10 case) so the
# GUI's hover tooltip reads "from Client tab" instead of "?". The tag is
# also a clear marker for any future audit of operator-typed (vs extracted)
# NI rows.
_CLIENT_TAB_SOURCE_DOC = "Client tab"

# (Insured attribute on state.insured, account.named_insured.* leaf tag).
# Edit-in-place pairs; order drives the order of rows in the reconciliation
# dialog. Add new pairs here when a sync field is added on either side.
INSURED_NI_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("named_insured",  "account.named_insured.name"),
    ("business_type",  "account.named_insured.business_type"),
    ("fein",           "account.named_insured.fein"),
    ("street_address", "account.named_insured.address.line_1"),
    ("city",           "account.named_insured.address.city"),
    ("state",          "account.named_insured.address.state"),
    ("zip_code",       "account.named_insured.address.zip"),
)

NAMED_INSURED_GROUP = "account.named_insured"
NAMED_INSURED_NAME_TAG = "account.named_insured.name"

# Field-label for the dialog header. Operator-visible.
INSURED_FIELD_LABELS: dict[str, str] = {
    "named_insured":  "Named Insured",
    "business_type":  "Business Type",
    "fein":           "FEIN / Tax ID",
    "street_address": "Street Address",
    "city":           "City",
    "state":          "State",
    "zip_code":       "ZIP Code",
}

# Match per-field outcomes. The dialog only shows "conflict" and
# "canon_extraction_diff" rows; "silent_merge_*" are auto-applied (with an
# informational list in the dialog header) and "noop" is dropped.
_KIND_NOOP                  = "noop"
_KIND_SILENT_TO_INSURED     = "silent_merge_to_insured"
_KIND_SILENT_TO_NI          = "silent_merge_to_ni"
_KIND_CONFLICT              = "conflict"
_KIND_CANON_EXTRACTION_DIFF = "canon_extraction_diff"


@dataclass(slots=True)
class ReconcileDelta:
    """One field's reconciliation outcome.

    ``field`` is the Insured attribute name; ``insured_value`` and ``ni_value``
    are the raw string values from each side (already stripped). ``kind`` is
    one of the ``_KIND_*`` constants above.
    """

    field: str
    insured_value: str
    ni_value: str
    kind: str


# --- name normalization ---------------------------------------------------

# Business-entity name normalization is defined once in state.py and re-exported
# here so callers can keep importing ``reconcile.normalize_entity_name``. Sharing
# the implementation with the extraction merge layer is what guarantees a
# "Acme, LLC" extraction row will land on the same NI row as the Client tab's
# "Acme LLC" — otherwise the merge appends a ghost row that this module can't see.
normalize_entity_name = _state_normalize_entity_name


# --- match selection ------------------------------------------------------

def _ni_row_name(item: dict[str, FieldRecord]) -> str:
    """Best-effort name read for a named_insured row."""
    rec = item.get(NAMED_INSURED_NAME_TAG)
    if rec is None:
        return ""
    v = rec.value
    return str(v) if v is not None else ""


def find_matching_ni_row(state: State) -> int | None:
    """Return the index of the named_insured row matching the Client tab.

    Match key: ``normalize_entity_name(state.insured.named_insured)`` equals
    the normalized name on the row. Returns ``None`` when the Client tab name
    is blank, the repeatable is empty, or no row matches. When multiple rows
    share a normalized name, the first match wins.
    """
    target = normalize_entity_name(state.insured.named_insured)
    if not target:
        return None
    rows = state.repeatables.get(NAMED_INSURED_GROUP) or []
    for idx, item in enumerate(rows):
        if normalize_entity_name(_ni_row_name(item)) == target:
            return idx
    return None


# --- delta computation ----------------------------------------------------

def _insured_value(state: State, attr: str) -> str:
    v = getattr(state.insured, attr, "") or ""
    return str(v).strip()


def _ni_field_value(item: dict[str, FieldRecord], tag: str) -> str:
    rec = item.get(tag)
    if rec is None:
        return ""
    v = rec.value
    return str(v).strip() if v is not None else ""


def _ni_disagreeing_candidate(
    item: dict[str, FieldRecord], tag: str, *, vs_value: str
) -> str | None:
    """First ``FieldRecord.conflicts[]`` candidate whose value disagrees with ``vs_value``.

    Used to detect the canon_extraction_diff case where extraction proposed a
    different value but lost the confidence comparison and was demoted to a
    conflict candidate. Returns ``None`` when no such disagreeing candidate
    exists. ``vs_value`` is already stripped; comparison is case-insensitive.
    """
    rec = item.get(tag)
    if rec is None or not rec.conflicts:
        return None
    target = vs_value.strip().lower()
    for c in rec.conflicts:
        v = c.value
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() != target:
            return s
    return None


def _values_equal(a: str, b: str, *, attr: str) -> bool:
    """Per-field equality. Name uses normalized comparison; others are
    case-insensitive whitespace-collapsed string equality."""
    if attr == "named_insured":
        return normalize_entity_name(a) == normalize_entity_name(b)
    return a.strip().lower() == b.strip().lower()


def compute_deltas(state: State) -> list[ReconcileDelta]:
    """Compute one ReconcileDelta per (Insured attr, NI leaf) pair.

    Returns an empty list when there's no matched NI row — the caller
    should detect that via ``find_matching_ni_row`` and skip reconciliation
    entirely. (We return ``[]`` here so callers can use a single code path.)
    """
    matched_idx = find_matching_ni_row(state)
    if matched_idx is None:
        return []
    ni_row = state.repeatables[NAMED_INSURED_GROUP][matched_idx]
    canon = set(state.insured.canon_fields or [])

    out: list[ReconcileDelta] = []
    for attr, ni_tag in INSURED_NI_FIELD_MAP:
        iv = _insured_value(state, attr)
        nv = _ni_field_value(ni_row, ni_tag)

        if not iv and not nv:
            kind = _KIND_NOOP
        elif iv and not nv:
            kind = _KIND_SILENT_TO_NI
        elif nv and not iv:
            kind = _KIND_SILENT_TO_INSURED
        elif _values_equal(iv, nv, attr=attr):
            kind = _KIND_NOOP
            # Even when surface values agree, a canon field with a lurking
            # disagreeing candidate in conflicts[] (extraction lost the
            # confidence comparison) should still surface for review.
            if attr in canon:
                lost = _ni_disagreeing_candidate(ni_row, ni_tag, vs_value=iv)
                if lost is not None:
                    out.append(ReconcileDelta(
                        field=attr,
                        insured_value=iv,
                        ni_value=lost,
                        kind=_KIND_CANON_EXTRACTION_DIFF,
                    ))
                    continue
        elif attr in canon:
            kind = _KIND_CANON_EXTRACTION_DIFF
        else:
            kind = _KIND_CONFLICT
        out.append(ReconcileDelta(field=attr, insured_value=iv, ni_value=nv, kind=kind))
    return out


# --- mutation -------------------------------------------------------------

def _set_insured(state: State, attr: str, value: str) -> None:
    setattr(state.insured, attr, value)


def _set_ni_field(state: State, attr: str, ni_tag: str, value: str, *, pinned: bool) -> None:
    """Write ``value`` to the matched NI row's field; create a FieldRecord if needed."""
    matched_idx = find_matching_ni_row(state)
    if matched_idx is None:
        return
    row = state.repeatables[NAMED_INSURED_GROUP][matched_idx]
    rec = row.get(ni_tag)
    if rec is None:
        # Create a minimal FieldRecord. Confidence 1.0 because this is
        # operator-authoritative (or matches operator-typed Client tab).
        rec = FieldRecord(value=value, confidence=1.0, status="confirmed", pinned=pinned)
        row[ni_tag] = rec
    else:
        rec.value = value
        if pinned:
            rec.pinned = True


def apply_silent_merges(state: State, deltas: list[ReconcileDelta]) -> None:
    """Apply gap-fills for ``silent_merge_*`` deltas. Does NOT mark canon."""
    for d in deltas:
        if d.kind == _KIND_SILENT_TO_INSURED:
            _set_insured(state, d.field, d.ni_value)
        elif d.kind == _KIND_SILENT_TO_NI:
            ni_tag = dict(INSURED_NI_FIELD_MAP)[d.field]
            _set_ni_field(state, d.field, ni_tag, d.insured_value, pinned=False)


def ensure_client_insured_in_ni(state: State) -> bool:
    """Auto-create a primary named_insured row from the Client tab when no
    matching row exists.

    Used after extraction to guarantee that the operator-typed insured
    is always represented in ``state.repeatables["account.named_insured"]``
    — even when extraction didn't surface a row matching the typed name.
    The created row carries:

    - **Low confidence (0.5)** on every field — these values were operator-typed,
      not document-extracted, so they should sit below an extraction-confirmed
      record on confidence comparisons.
    - ``needs_review=True`` so the GUI's Named Insureds table flags the row
      (highlighted / badge) as "needs review — no extraction backing".
    - A ``SourceRef`` whose ``doc_id`` is "Client tab" so the per-field hover
      tooltip reads "from Client tab" instead of "from ?".

    Returns True if a row was created, False if nothing changed (Client tab
    blank, OR a matching row already exists).
    """
    insured_name = (state.insured.named_insured or "").strip()
    if not insured_name:
        return False
    if find_matching_ni_row(state) is not None:
        return False
    # Collect every Client-tab field that should populate the new row.
    pairs: list[tuple[str, str]] = [(NAMED_INSURED_NAME_TAG, insured_name)]
    for attr, ni_tag in INSURED_NI_FIELD_MAP:
        if attr == "named_insured":
            continue
        val = _insured_value(state, attr)
        if val:
            pairs.append((ni_tag, val))
    row: dict[str, FieldRecord] = {}
    client_tab_source = SourceRef(
        doc_id=_CLIENT_TAB_SOURCE_DOC,
        page=0,
        quote="(operator-typed on the Client tab)",
    )
    for tag, val in pairs:
        row[tag] = FieldRecord(
            value=val,
            confidence=0.5,
            status="pending",
            source=[client_tab_source],
            needs_review=True,
        )
    state.repeatables.setdefault(NAMED_INSURED_GROUP, []).append(row)
    return True


def apply_resolutions(state: State, resolutions: dict[str, str]) -> None:
    """Apply operator-chosen values from the reconciliation popup.

    Writes ``value`` to BOTH sides (Client tab + matched NI row), marks the
    Insured attribute as canon, and pins the NI row's FieldRecord.
    ``resolutions`` is a mapping ``{insured_attr: chosen_value}``.
    """
    field_map = dict(INSURED_NI_FIELD_MAP)
    canon = list(state.insured.canon_fields or [])
    for attr, value in resolutions.items():
        if attr not in field_map:
            continue
        _set_insured(state, attr, value)
        _set_ni_field(state, attr, field_map[attr], value, pinned=True)
        if attr not in canon:
            canon.append(attr)
    state.insured.canon_fields = canon

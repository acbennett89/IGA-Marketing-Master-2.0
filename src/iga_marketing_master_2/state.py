"""state.py — per-client `state.json` read/write.

Owns the canonical extracted state per client. Schema and I/O contract are
specified in ARCHITECTURE.md §5. This module implements:

- `State` dataclass tree (top-level + `FieldRecord`, `RepeatableItem`,
  `ConflictCandidate`, `SourceRef`, `HistoryEntry`, `RunHistoryEntry`,
  `PendingPause`, `PendingExtraction`).
- Atomic write protocol: write tmp -> fsync -> rotate prior to .bak ->
  os.replace. On read of a corrupt state.json, fall back to .bak; if both
  corrupt, fall back to the most recent successful snapshot. If all three
  fail, raise `StateCorruptError`.
- Daily snapshot decision: first successful save per day per client copies
  state.json into `<client_dir>/snapshots/state-YYYY-MM-DD.json`. Snapshots
  older than `SNAPSHOT_RETENTION_DAYS` are pruned on each save.
- `merge_extraction` merges Claude's `ExtractedField` records into
  `state.fields` and `state.repeatables`, dedupes repeatables by per-group
  natural keys (see DECISION-MAP-state-agent.md §2), and surfaces conflicts
  on `FieldRecord.conflicts[]`.
- `pending_pause` and `pending_extraction` blocks support resume-on-crash.
- `schema_version: 1` honored on read; future versions trigger a migration
  hook (no-op for v1; future schemas populate it).

Path B: single-user, no lock files, no compare-and-swap.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TypeAlias
from uuid import uuid4

__all__ = [
    # Constants
    "STATE_SCHEMA_VERSION",
    "SNAPSHOT_RETENTION_DAYS",
    "STATE_FILENAME",
    "STATE_BAK_FILENAME",
    "SNAPSHOTS_DIRNAME",
    "SNAPSHOT_FILENAME_PREFIX",
    # Exceptions
    "StateError",
    "StateCorruptError",
    "StateSchemaVersionError",
    "StateMergeError",
    # Types
    "PrimitiveValue",
    "FieldStatus",
    "Actor",
    "Action",
    "RunKind",
    "Outcome",
    "ModelUsed",
    "PauseReasonCode",
    "ConflictDetection",
    "SourceRef",
    "ConflictCandidate",
    "HistoryEntry",
    "FieldRecord",
    "RepeatableItem",
    "PendingPause",
    "PendingExtraction",
    "RunHistoryEntry",
    "State",
    "ExtractedField",
    "MergeReport",
    # API
    "load",
    "save_atomic",
    "merge_extraction",
    "detect_conflicts",
    "write_history_entry",
    "take_daily_snapshot",
    "get_pending_pause",
    "set_pending_pause",
    "get_pending_extraction",
    "set_pending_extraction",
    "get_pending_domain_tag_proposals",
    "set_pending_domain_tag_proposals",
    "append_run_history",
    "natural_key_for",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_SCHEMA_VERSION: int = 1
SNAPSHOT_RETENTION_DAYS: int = 30

STATE_FILENAME: str = "state.json"
STATE_BAK_FILENAME: str = "state.json.bak"
SNAPSHOTS_DIRNAME: str = "snapshots"
SNAPSHOT_FILENAME_PREFIX: str = "state-"
SNAPSHOT_FILENAME_SUFFIX: str = ".json"

_TMP_PREFIX: str = "state.json.tmp."
_SNAPSHOT_DATE_RE: re.Pattern[str] = re.compile(
    r"^state-(\d{4})-(\d{2})-(\d{2})\.json$"
)

logger = logging.getLogger("iga.state")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

PrimitiveValue: TypeAlias = str | int | float | bool | None
FieldStatus: TypeAlias = Literal["pending", "approved", "locked", "entered"]
Actor: TypeAlias = Literal["extractor", "gui", "entry"]
Action: TypeAlias = Literal[
    "create",
    "update",
    "approve",
    "lock",
    "unlock",
    "enter",
    "resolve_conflict",
]
RunKind: TypeAlias = Literal["extraction", "entry", "manual_edit"]
Outcome: TypeAlias = Literal["completed", "partial", "aborted", "paused"]
ModelUsed: TypeAlias = Literal["sonnet-4-6", "opus-4-7", "mixed"]
PauseReasonCode: TypeAlias = Literal[
    "validation_rejected",
    "selector_unresolved",
    "user_requested",
    "exception",
]
ConflictDetection: TypeAlias = Literal[
    "no_existing", "match", "conflict", "lower_confidence_dropped"
]


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StateError(Exception):
    """Base for all state.py errors."""


class StateCorruptError(StateError):
    """state.json (and .bak, and all snapshots) could not be parsed.

    The GUI is expected to catch this and surface it to the operator via an
    `OperatorModal` ("Your client's state file is unreadable; pick a snapshot
    to restore from, or contact support").
    """


class StateSchemaVersionError(StateError):
    """state.json declares a schema_version this build does not understand.

    Raised when the on-disk `schema_version` is higher than
    `STATE_SCHEMA_VERSION`. (Lower versions trigger migration; this is the
    "downgrade detected" guard.)
    """


class StateMergeError(StateError):
    """An invariant failed during merge_extraction (e.g., contradictory
    repeatable_group / domain_tag namespace)."""


# ---------------------------------------------------------------------------
# Dataclasses (state schema)
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class SourceRef:
    """One literal source quote from a PDF that contributed to a value."""

    doc_id: str
    page: int
    quote: str


@dataclass(slots=True, kw_only=True)
class ConflictCandidate:
    """An alternative value seen for the same domain_tag from another source."""

    value: PrimitiveValue
    confidence: float
    source: SourceRef
    observed_at: str
    model_used: str | None = None


@dataclass(slots=True, kw_only=True)
class HistoryEntry:
    """An append-only audit row on a single FieldRecord."""

    run_id: str
    ts: str
    user: str
    actor: Actor
    action: Action
    prior: dict[str, Any] | None
    new: dict[str, Any]


@dataclass(slots=True, kw_only=True)
class FieldRecord:
    """One canonical field value plus its full audit trail."""

    value: PrimitiveValue
    confidence: float
    status: FieldStatus = "pending"
    source: list[SourceRef] = field(default_factory=list)
    conflicts: list[ConflictCandidate] = field(default_factory=list)
    history: list[HistoryEntry] = field(default_factory=list)
    needs_review: bool = False
    model_used: str | None = None


# RepeatableItem is a dict[domain_tag -> FieldRecord]. Aliased for clarity.
RepeatableItem: TypeAlias = dict[str, FieldRecord]


@dataclass(slots=True, kw_only=True)
class PendingPause:
    """Set while an entry session is paused awaiting human action."""

    run_id: str
    paused_at: str
    domain_tag: str
    screen_code: str
    reason_code: PauseReasonCode
    reason_message: str
    technical_detail: str
    repeatable_group: str | None = None
    repeatable_index: int | None = None


@dataclass(slots=True, kw_only=True)
class PendingExtraction:
    """Set while an extraction run is in flight; cleared on clean completion."""

    run_id: str
    started_at: str
    pdf_paths: list[str]
    completed_pdf_basenames: list[str] = field(default_factory=list)
    notes: str | None = None


@dataclass(slots=True, kw_only=True)
class RunHistoryEntry:
    """One top-level run-history entry per extraction or entry run."""

    run_id: str
    ts: str
    user: str
    kind: RunKind
    inputs: list[str] = field(default_factory=list)
    outcome: Outcome = "completed"
    model_used: str | None = None
    forced_opus: bool | None = None
    notes: str | None = None


@dataclass(slots=True, kw_only=True)
class State:
    """The full per-client canonical state."""

    client: str
    created_at: str
    updated_at: str
    schema_version: int = STATE_SCHEMA_VERSION
    run_history: list[RunHistoryEntry] = field(default_factory=list)
    fields: dict[str, FieldRecord] = field(default_factory=dict)
    repeatables: dict[str, list[RepeatableItem]] = field(default_factory=dict)
    pending_pause: PendingPause | None = None
    pending_extraction: PendingExtraction | None = None
    pending_domain_tag_proposals: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extractor interop types (mirrored from claude_client.py contract §6.1)
#
# We define a minimal, structural ExtractedField here so state.py does not
# import claude_client (avoids the forbidden cross-module edge — claude_client
# may not import state, but state shouldn't import claude_client either, since
# the dependency direction in §11 is extract -> {state, claude_client}, not
# state -> claude_client). Callers (extract.py) pass either the real
# `claude_client.ExtractedField` or a duck-typed equivalent — both work as
# long as the attributes match.
# ---------------------------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class ExtractedField:
    """Structural mirror of `claude_client.ExtractedField`.

    state.merge_extraction accepts either this type or an instance of
    `claude_client.ExtractedField` — only the attribute surface is consulted.
    """

    domain_tag: str
    value: PrimitiveValue
    source_doc: str
    source_page: int
    source_quote: str
    confidence: float
    model_used: str
    needs_review: bool = False
    repeatable_group: str | None = None
    repeatable_index: int | None = None


@dataclass(slots=True, kw_only=True)
class MergeReport:
    """Summary of one `merge_extraction` call. Caller logs / surfaces to GUI."""

    fields_created: int = 0
    fields_updated: int = 0
    conflicts_added: int = 0
    repeatable_items_added: int = 0
    repeatable_items_merged: int = 0
    appended_without_key: int = 0
    domain_tag_proposals: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 UTC timestamp with offset, no microseconds."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _today() -> date:
    """The local-system today (snapshots are dated by local-system day)."""
    return date.today()


# ---------------------------------------------------------------------------
# Natural-key helpers (Open Item #1)
# ---------------------------------------------------------------------------


def _norm(v: PrimitiveValue) -> str:
    """Normalize a value for natural-key compares.

    - Cast to str.
    - strip + casefold.
    - Collapse internal whitespace runs to a single space.
    - None / missing -> "" (caller treats "" as "no key value").
    """
    if v is None:
        return ""
    s = str(v).strip().casefold()
    return re.sub(r"\s+", " ", s)


def _norm_vin(v: PrimitiveValue) -> str:
    """VIN-specific: upper-case, length-17 sanity check."""
    if v is None:
        return ""
    s = str(v).strip().upper()
    s = re.sub(r"\s+", "", s)
    if len(s) != 17:
        return ""
    return s


def _value_of(item: RepeatableItem, tag: str) -> PrimitiveValue:
    """Read a tag's primitive value out of a RepeatableItem (or None)."""
    rec = item.get(tag)
    return rec.value if rec is not None else None


def _vehicle_key(item: RepeatableItem) -> str | None:
    # VIN is globally unique by design — strongest possible natural key.
    vin = _norm_vin(_value_of(item, "vehicle.vin"))
    return vin or None


def _driver_key(item: RepeatableItem) -> str | None:
    # Prefer license# + state (uniquely identifies a person across documents);
    # fall back to name + DOB when the license isn't on the doc. Returning
    # None tells the merge layer "no key — append rather than try to merge".
    lic = _norm(_value_of(item, "driver.license_number"))
    state = _norm(_value_of(item, "driver.license_state"))
    if lic and state:
        return f"lic:{lic}|{state}"
    name = _norm(_value_of(item, "driver.name"))
    dob = _norm(_value_of(item, "driver.date_of_birth"))
    if name and dob:
        return f"nm:{name}|{dob}"
    return None


def _location_key(item: RepeatableItem) -> str | None:
    line1 = _norm(_value_of(item, "location.address.line1"))
    city = _norm(_value_of(item, "location.address.city"))
    st = _norm(_value_of(item, "location.address.state"))
    zp = _norm(_value_of(item, "location.address.zip"))
    if line1 and city and st and zp:
        return f"{line1}|{city}|{st}|{zp}"
    return None


def _name_addr_key(prefix: str, item: RepeatableItem) -> str | None:
    name = _norm(_value_of(item, f"{prefix}.name"))
    line1 = _norm(_value_of(item, f"{prefix}.address.line1"))
    if name and line1:
        return f"{name}|{line1}"
    if name:
        return f"{name}|"
    return None


def _loss_payee_key(item: RepeatableItem) -> str | None:
    return _name_addr_key("loss_payee", item)


def _additional_insured_key(item: RepeatableItem) -> str | None:
    return _name_addr_key("additional_insured", item)


def _prior_carrier_key(item: RepeatableItem) -> str | None:
    pol = _norm(_value_of(item, "prior_carrier.policy_number"))
    if pol:
        return f"pol:{pol}"
    name = _norm(_value_of(item, "prior_carrier.name"))
    return f"nm:{name}" if name else None


def _loss_key(item: RepeatableItem) -> str | None:
    dol = _norm(_value_of(item, "loss.date_of_loss"))
    paid = _norm(_value_of(item, "loss.amount_paid"))
    desc = _norm(_value_of(item, "loss.description"))[:64]
    if dol and (paid or desc):
        return f"{dol}|{paid}|{desc}"
    return None


def _policy_auto_vehicle_key(item: RepeatableItem) -> str | None:
    vin = _norm_vin(_value_of(item, "policy.auto.vehicle.vin"))
    if vin:
        return vin
    yr = _norm(_value_of(item, "policy.auto.vehicle.year"))
    mk = _norm(_value_of(item, "policy.auto.vehicle.make"))
    mo = _norm(_value_of(item, "policy.auto.vehicle.model"))
    if yr and mk and mo:
        return f"{yr}|{mk}|{mo}"
    return None


def _policy_auto_driver_key(item: RepeatableItem) -> str | None:
    lic = _norm(_value_of(item, "policy.auto.driver.drivers_license_number"))
    st = _norm(_value_of(item, "policy.auto.driver.state"))
    if lic and st:
        return f"lic:{lic}|{st}"
    name = _norm(_value_of(item, "policy.auto.driver.name"))
    dob = _norm(_value_of(item, "policy.auto.driver.birth"))
    if name and dob:
        return f"nm:{name}|{dob}"
    if name:
        return f"nm:{name}|"
    return None


def _location_key_actual(item: RepeatableItem) -> str | None:
    # Try the canonical address tag, then fallbacks used in the section form.
    addr = (
        _norm(_value_of(item, "location.building_description"))
        or _norm(_value_of(item, "location.address"))
        or _norm(_value_of(item, "location.description"))
    )
    bldg = (
        _norm(_value_of(item, "location.building_number"))
        or _norm(_value_of(item, "location.bldg_number"))
    )
    return f"{addr}|{bldg}" if addr else None


def _account_named_insured_key(item: RepeatableItem) -> str | None:
    name = (
        _norm(_value_of(item, "account.named_insured.name"))
        or _norm(_value_of(item, "account.named_insured.fni_name"))
    )
    return f"{name}|" if name else None


def _gl_hazard_key(item: RepeatableItem) -> str | None:
    code = _norm(_value_of(item, "policy.gl.hazard.class_code"))
    loc = _norm(_value_of(item, "policy.gl.hazard.location_number"))
    bldg = _norm(_value_of(item, "policy.gl.hazard.building_number"))
    if code and loc and bldg:
        return f"{code}|{loc}|{bldg}"
    if code and loc:
        return f"{code}|{loc}|"
    return None


def _property_subject_key(item: RepeatableItem) -> str | None:
    loc = _norm(_value_of(item, "policy.property.subject.location_number"))
    bldg = _norm(_value_of(item, "policy.property.subject.building_number"))
    subj = _norm(_value_of(item, "policy.property.subject.subject"))
    if loc and bldg and subj:
        return f"{loc}|{bldg}|{subj}"
    return None


def _wc_class_code_key(item: RepeatableItem) -> str | None:
    code = _norm(_value_of(item, "policy.workers_comp.class_code.class_code"))
    st = _norm(_value_of(item, "policy.workers_comp.class_code.state"))
    if code and st:
        return f"{code}|{st}"
    if code:
        return f"{code}|"
    return None


def _im_scheduled_item_key(item: RepeatableItem) -> str | None:
    serial = _norm(_value_of(item, "policy.inland_marine.scheduled_item.serial_number"))
    # Exclude placeholder serial numbers that carry no identifying information.
    if serial and serial not in ("xxxx", "n/a", "none", "unknown"):
        return f"sn:{serial}"
    desc = _norm(_value_of(item, "policy.inland_marine.scheduled_item.description"))
    amt = _norm_amount(_value_of(item, "policy.inland_marine.scheduled_item.amt_insurance"))
    if desc and amt:
        return f"desc+amt:{desc}|{amt}"
    return f"desc:{desc}" if desc else None


def _norm_amount(v: PrimitiveValue) -> str:
    """Normalize a currency/numeric amount for comparison.

    Strips leading ``$``, removes thousands commas, strips trailing ``.00``,
    then applies standard _norm (casefold + whitespace collapse).  This lets
    ``"$150,000"`` and ``"150000"`` compare equal across documents that format
    amounts differently.
    """
    s = str(v).strip() if v is not None else ""
    s = s.lstrip("$").replace(",", "")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return _norm(s)


def _im_scheduled_item_extended_match(
    candidate: RepeatableItem,
    items: list[RepeatableItem],
) -> int | None:
    """Secondary match for IM scheduled items when the primary natural key
    did not find a hit.

    Rationale: dec pages and coverage summaries list the same equipment with
    different serial-number availability.  A document that has the serial
    number generates key ``sn:xxx``; a summary doc without the serial generates
    ``desc+amt:…``.  These will never collide on string equality, so the same
    item gets appended twice.  This function bridges that gap by looking for
    an existing item whose (description, amount) pair matches the candidate
    even if the key prefixes differ.

    Amount comparison uses ``_norm_amount`` so that ``"$150,000"`` and
    ``"150000"`` are treated as equal.
    """
    amt = _norm_amount(_value_of(candidate, "policy.inland_marine.scheduled_item.amt_insurance"))
    desc = _norm(_value_of(candidate, "policy.inland_marine.scheduled_item.description"))
    if not amt or not desc:
        return None
    for i, existing in enumerate(items):
        ex_amt = _norm_amount(_value_of(existing, "policy.inland_marine.scheduled_item.amt_insurance"))
        ex_desc = _norm(_value_of(existing, "policy.inland_marine.scheduled_item.description"))
        if ex_amt != amt:
            continue
        # Accept if one description is a substring of the other (covers
        # "Hydraulic Excavator" vs "2022 KOMATSU HYDRAULIC EXCAVATOR" style).
        if ex_desc == desc or desc in ex_desc or ex_desc in desc:
            return i
    return None


def _im_unscheduled_item_key(item: RepeatableItem) -> str | None:
    desc = _norm(_value_of(item, "policy.inland_marine.unscheduled_item.description"))
    return f"desc:{desc}" if desc else None


def _umbrella_underlying_other_key(item: RepeatableItem) -> str | None:
    carrier = _norm(_value_of(item, "policy.umbrella.underlying.other.carrier"))
    pol = _norm(_value_of(item, "policy.umbrella.underlying.other.pol_num"))
    if carrier and pol:
        return f"{carrier}|{pol}"
    if pol:
        return f"|{pol}"
    return None


_NATURAL_KEY_FUNCS: dict[str, Any] = {
    # Short-name cross-LOB groups (legacy / may appear in older data)
    "loss_payee": _loss_payee_key,
    "additional_insured": _additional_insured_key,
    "prior_carrier": _prior_carrier_key,
    "loss": _loss_key,
    # Actual group names used by the application
    "location": _location_key_actual,
    "account.named_insured": _account_named_insured_key,
    "policy.auto.vehicle": _policy_auto_vehicle_key,
    "policy.auto.driver": _policy_auto_driver_key,
    "policy.gl.hazard": _gl_hazard_key,
    "policy.property.subject": _property_subject_key,
    "policy.workers_comp.class_code": _wc_class_code_key,
    "policy.inland_marine.scheduled_item": _im_scheduled_item_key,
    "policy.inland_marine.unscheduled_item": _im_unscheduled_item_key,
    "policy.umbrella.underlying.other": _umbrella_underlying_other_key,
}


def natural_key_for(group: str, item: RepeatableItem) -> str | None:
    """Public hook: return the natural-key string for a repeatable item, or
    None if this item lacks the values needed to compute one.

    Documented in DECISION-MAP-state-agent.md §2. Unknown groups return None
    (caller appends without merge).
    """
    fn = _NATURAL_KEY_FUNCS.get(group)
    if fn is None:
        return None
    return fn(item)


# ---------------------------------------------------------------------------
# Serialization (dataclass <-> dict)
# ---------------------------------------------------------------------------


def _dc_to_dict(obj: Any) -> Any:
    """Recursively convert a dataclass tree to JSON-safe dicts/lists.

    `dataclasses.asdict` would do most of this, but we want None-suppression
    on optional fields where the contract says "absent" rather than "null"
    is the preferred form (e.g., model_used). We keep nulls for visibility
    in v1 — the JSON is meant to be human-diffable. Just use asdict + a
    type-coercion pass for any awkward types.
    """
    return dataclasses.asdict(obj)


def _state_to_dict(state: State) -> dict[str, Any]:
    """Serialize a State to a JSON-ready dict.

    Top-level keys are emitted in canonical order; nested dicts (fields,
    repeatables) are sorted by key for deterministic diffs.
    """
    fields_dict: dict[str, dict[str, Any]] = {
        tag: _dc_to_dict(rec) for tag, rec in sorted(state.fields.items())
    }
    repeatables_dict: dict[str, list[dict[str, dict[str, Any]]]] = {}
    for group in sorted(state.repeatables.keys()):
        items_out: list[dict[str, dict[str, Any]]] = []
        for item in state.repeatables[group]:
            items_out.append(
                {tag: _dc_to_dict(rec) for tag, rec in sorted(item.items())}
            )
        repeatables_dict[group] = items_out

    return {
        "schema_version": state.schema_version,
        "client": state.client,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "run_history": [_dc_to_dict(r) for r in state.run_history],
        "fields": fields_dict,
        "repeatables": repeatables_dict,
        "pending_pause": (
            _dc_to_dict(state.pending_pause)
            if state.pending_pause is not None
            else None
        ),
        "pending_extraction": (
            _dc_to_dict(state.pending_extraction)
            if state.pending_extraction is not None
            else None
        ),
        "pending_domain_tag_proposals": [
            dict(p) for p in state.pending_domain_tag_proposals
        ],
    }


def _source_ref_from_dict(d: dict[str, Any]) -> SourceRef:
    return SourceRef(doc_id=d["doc_id"], page=int(d["page"]), quote=d["quote"])


def _conflict_from_dict(d: dict[str, Any]) -> ConflictCandidate:
    return ConflictCandidate(
        value=d.get("value"),
        confidence=float(d.get("confidence", 0.0)),
        source=_source_ref_from_dict(d["source"]),
        observed_at=d["observed_at"],
        model_used=d.get("model_used"),
    )


def _history_from_dict(d: dict[str, Any]) -> HistoryEntry:
    return HistoryEntry(
        run_id=d["run_id"],
        ts=d["ts"],
        user=d["user"],
        actor=d["actor"],
        action=d["action"],
        prior=d.get("prior"),
        new=d["new"],
    )


def _field_record_from_dict(d: dict[str, Any]) -> FieldRecord:
    return FieldRecord(
        value=d.get("value"),
        confidence=float(d.get("confidence", 0.0)),
        status=d.get("status", "pending"),
        source=[_source_ref_from_dict(s) for s in d.get("source", [])],
        conflicts=[_conflict_from_dict(c) for c in d.get("conflicts", [])],
        history=[_history_from_dict(h) for h in d.get("history", [])],
        needs_review=bool(d.get("needs_review", False)),
        model_used=d.get("model_used"),
    )


def _repeatable_from_dict(
    d: dict[str, dict[str, Any]],
) -> RepeatableItem:
    return {tag: _field_record_from_dict(rec) for tag, rec in d.items()}


def _pending_pause_from_dict(d: dict[str, Any] | None) -> PendingPause | None:
    if d is None:
        return None
    return PendingPause(
        run_id=d["run_id"],
        paused_at=d["paused_at"],
        domain_tag=d["domain_tag"],
        screen_code=d["screen_code"],
        reason_code=d["reason_code"],
        reason_message=d["reason_message"],
        technical_detail=d["technical_detail"],
        repeatable_group=d.get("repeatable_group"),
        repeatable_index=d.get("repeatable_index"),
    )


def _pending_extraction_from_dict(
    d: dict[str, Any] | None,
) -> PendingExtraction | None:
    if d is None:
        return None
    return PendingExtraction(
        run_id=d["run_id"],
        started_at=d["started_at"],
        pdf_paths=list(d.get("pdf_paths", [])),
        completed_pdf_basenames=list(d.get("completed_pdf_basenames", [])),
        notes=d.get("notes"),
    )


def _run_history_from_dict(d: dict[str, Any]) -> RunHistoryEntry:
    return RunHistoryEntry(
        run_id=d["run_id"],
        ts=d["ts"],
        user=d["user"],
        kind=d["kind"],
        inputs=list(d.get("inputs", [])),
        outcome=d.get("outcome", "completed"),
        model_used=d.get("model_used"),
        forced_opus=d.get("forced_opus"),
        notes=d.get("notes"),
    )


def _state_from_dict(raw: dict[str, Any], *, client_fallback: str) -> State:
    """Deserialize a parsed JSON dict into a State object.

    Performs schema-version handling: if `schema_version` is missing (legacy
    pre-versioned files), assume 1. If it's higher than this build supports,
    raise StateSchemaVersionError. If it's lower, route through migration.
    """
    raw_version = raw.get("schema_version", 1)
    if not isinstance(raw_version, int):
        raise StateCorruptError(
            f"schema_version must be int; got {raw_version!r}"
        )
    if raw_version > STATE_SCHEMA_VERSION:
        raise StateSchemaVersionError(
            f"state.json schema_version={raw_version} is newer than this "
            f"build supports (max {STATE_SCHEMA_VERSION}). Refusing to "
            f"open — running an older build against a newer state file would "
            f"silently drop fields."
        )
    if raw_version < STATE_SCHEMA_VERSION:
        raw = _migrate_state(raw, from_version=raw_version)

    return State(
        schema_version=STATE_SCHEMA_VERSION,
        client=raw.get("client", client_fallback),
        created_at=raw.get("created_at", _now_iso()),
        updated_at=raw.get("updated_at", _now_iso()),
        run_history=[
            _run_history_from_dict(r) for r in raw.get("run_history", [])
        ],
        fields={
            tag: _field_record_from_dict(rec)
            for tag, rec in raw.get("fields", {}).items()
        },
        repeatables={
            group: [_repeatable_from_dict(item) for item in items]
            for group, items in raw.get("repeatables", {}).items()
        },
        pending_pause=_pending_pause_from_dict(raw.get("pending_pause")),
        pending_extraction=_pending_extraction_from_dict(
            raw.get("pending_extraction")
        ),
        pending_domain_tag_proposals=[
            dict(p) for p in raw.get("pending_domain_tag_proposals", [])
        ],
    )


def _migrate_state(raw: dict[str, Any], *, from_version: int) -> dict[str, Any]:
    """Migration hook for older schema versions.

    v1 has no predecessors; this function exists so future schema bumps
    have a single place to plug in (e.g., `if from_version == 1: raw = ...`).
    Until v2 lands, any unsupported `from_version` raises.
    """
    if from_version == STATE_SCHEMA_VERSION:
        return raw
    raise StateSchemaVersionError(
        f"No migration path from schema_version={from_version} to "
        f"{STATE_SCHEMA_VERSION}. (Migration hook present but empty for v1.)"
    )


# ---------------------------------------------------------------------------
# Atomic write protocol
# ---------------------------------------------------------------------------


def _cleanup_orphan_tmps(client_path: Path) -> None:
    """Remove any stale `state.json.tmp.*` files left by a crashed prior write.

    Best-effort; failures here are logged and swallowed.
    """
    try:
        for entry in client_path.iterdir():
            if entry.name.startswith(_TMP_PREFIX):
                try:
                    entry.unlink()
                    logger.debug(
                        "cleaned orphan tmp %s", entry.name
                    )
                except OSError as exc:
                    logger.warning(
                        "failed to clean orphan tmp %s: %s",
                        entry.name,
                        exc,
                    )
    except FileNotFoundError:
        return


def _serialize_state(state: State) -> bytes:
    """Canonical JSON: indent=2, top-level keys ordered by `_state_to_dict`'s
    insertion order, nested dicts sorted, UTF-8, no BOM, trailing newline."""
    payload = _state_to_dict(state)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    return (text + "\n").encode("utf-8")


def save_atomic(state: State, client_path: Path) -> None:
    """Atomic write of `state` to `<client_path>/state.json`.

    Protocol (ARCHITECTURE.md §5.4):
      1. Stamp updated_at = now.
      2. Serialize bytes.
      3. Write to a unique tmp file in the same directory.
      4. fsync the tmp file.
      5. If state.json exists, copy it to state.json.bak (overwrite).
      6. os.replace(tmp, state.json) — atomic on same volume.
      7. Take daily snapshot if today's is missing.
      8. Prune snapshots older than SNAPSHOT_RETENTION_DAYS.

    On any pre-replace OSError, the tmp is cleaned and the original
    state.json + .bak are left untouched.
    """
    client_path.mkdir(parents=True, exist_ok=True)
    _cleanup_orphan_tmps(client_path)

    state.updated_at = _now_iso()
    data = _serialize_state(state)

    state_path = client_path / STATE_FILENAME
    bak_path = client_path / STATE_BAK_FILENAME

    # The order here matters for crash recovery:
    #   1. Write the new payload to a sibling .tmp + fsync (so the bytes are
    #      durably on disk even if the OS crashes immediately after).
    #   2. Copy the prior good state.json to .bak (rollback target).
    #   3. os.replace(tmp, state.json) — atomic on a single Windows volume.
    # If any step before #3 fails, the original state.json + .bak are intact.

    # --- 1. Write tmp + fsync ---
    fd, tmp_name = tempfile.mkstemp(
        prefix=_TMP_PREFIX, dir=str(client_path)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

        # --- 2. Rotate prior to .bak (only after tmp is durable) ---
        if state_path.exists():
            try:
                shutil.copy2(state_path, bak_path)
            except OSError as exc:
                # If we can't write .bak we abort the whole save: better to
                # leave the user with their old (intact) state than to swap
                # in the new one with no rollback target.
                logger.error(
                    "save_atomic: failed to rotate .bak: %s; aborting save",
                    exc,
                )
                tmp_path.unlink(missing_ok=True)
                raise

        # --- 3. Atomic replace ---
        os.replace(tmp_path, state_path)
    except BaseException:
        # Anything from fdopen/write/fsync onward; ensure tmp is gone so we
        # don't accumulate orphan tmp files on each failed save.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise

    # --- 4. Daily snapshot (best-effort; failure here doesn't fail save) ---
    snapshot_taken: Path | None = None
    try:
        snapshot_taken = take_daily_snapshot(client_path)
    except OSError as exc:
        logger.warning(
            "save_atomic: snapshot step failed: %s (state.json itself saved)",
            exc,
        )

    # --- 5. Prune snapshots ---
    pruned = 0
    try:
        pruned = _prune_snapshots(client_path)
    except OSError as exc:
        logger.warning(
            "save_atomic: snapshot prune failed: %s",
            exc,
        )

    logger.info(
        "save_atomic ok client=%s bytes=%d snapshot_taken=%s pruned=%d",
        state.client,
        len(data),
        snapshot_taken.name if snapshot_taken else "no",
        pruned,
    )


def _read_json_file(path: Path) -> dict[str, Any]:
    """Read+parse a JSON file. Raises StateCorruptError on parse failure.

    FileNotFoundError is allowed to bubble — callers want to distinguish
    "missing" from "corrupt" themselves.
    """
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateCorruptError(
            f"failed to parse {path.name}: {exc.msg} at line {exc.lineno}"
        ) from exc
    if not isinstance(raw, dict):
        raise StateCorruptError(
            f"{path.name} root must be an object, got {type(raw).__name__}"
        )
    return raw


def load(client_path: Path) -> State:
    """Read `<client_path>/state.json`, with .bak and snapshot fallback.

    Returns a fresh State if the file does not exist (first run for this
    client). On corruption, walks the recovery chain:
      state.json -> state.json.bak -> snapshots/state-YYYY-MM-DD.json (newest first).

    Raises StateCorruptError only when none of those paths yield a parseable
    file. Raises StateSchemaVersionError if the file is newer than this
    build supports.
    """
    client_path = Path(client_path)
    client_name = client_path.name
    state_path = client_path / STATE_FILENAME
    bak_path = client_path / STATE_BAK_FILENAME

    # --- Fresh state if nothing exists at all ---
    if not state_path.exists() and not bak_path.exists():
        snapshots = _list_snapshots(client_path)
        if not snapshots:
            now = _now_iso()
            return State(
                client=client_name,
                created_at=now,
                updated_at=now,
            )

    # Recovery chain: state.json → .bak → newest snapshot. We let
    # StateSchemaVersionError bubble immediately (a newer build wrote this
    # file; refusing to open is the safer behavior) but treat parse errors
    # as recoverable.

    # --- Try state.json ---
    if state_path.exists():
        try:
            raw = _read_json_file(state_path)
            return _state_from_dict(raw, client_fallback=client_name)
        except StateSchemaVersionError:
            raise
        except StateCorruptError as exc:
            logger.warning(
                "load: %s corrupt, attempting .bak fallback: %s",
                state_path.name,
                exc,
            )

    # --- Fallback to .bak ---
    if bak_path.exists():
        try:
            raw = _read_json_file(bak_path)
            state = _state_from_dict(raw, client_fallback=client_name)
            logger.warning(
                "load: recovered from %s (state.json was missing or corrupt)",
                bak_path.name,
            )
            return state
        except StateSchemaVersionError:
            raise
        except StateCorruptError as exc:
            logger.warning(
                "load: %s also corrupt, attempting snapshot fallback: %s",
                bak_path.name,
                exc,
            )

    # --- Fallback to snapshots, newest first ---
    snapshots = _list_snapshots(client_path)
    for snap in snapshots:
        try:
            raw = _read_json_file(snap)
            state = _state_from_dict(raw, client_fallback=client_name)
            logger.warning(
                "load: recovered from snapshot %s "
                "(state.json + .bak both unreadable)",
                snap.name,
            )
            return state
        except StateSchemaVersionError:
            raise
        except StateCorruptError as exc:
            logger.warning(
                "load: snapshot %s corrupt: %s; trying older",
                snap.name,
                exc,
            )

    raise StateCorruptError(
        f"state.json, state.json.bak, and all snapshots in "
        f"{client_path}/snapshots/ are unreadable. Operator intervention "
        f"required (restore from backup or start fresh)."
    )


# ---------------------------------------------------------------------------
# Daily snapshots
# ---------------------------------------------------------------------------


def _snapshot_filename_for(d: date) -> str:
    return f"{SNAPSHOT_FILENAME_PREFIX}{d.isoformat()}{SNAPSHOT_FILENAME_SUFFIX}"


def _list_snapshots(client_path: Path) -> list[Path]:
    """Return snapshot files in the client's snapshots/ dir, newest first.

    Newest-first by the date encoded in the filename, not mtime — robust to
    file-system timestamp jitter.
    """
    snap_dir = client_path / SNAPSHOTS_DIRNAME
    if not snap_dir.is_dir():
        return []
    found: list[tuple[date, Path]] = []
    for entry in snap_dir.iterdir():
        if not entry.is_file():
            continue
        m = _SNAPSHOT_DATE_RE.match(entry.name)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        found.append((d, entry))
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [p for _, p in found]


def take_daily_snapshot(client_path: Path) -> Path | None:
    """Copy current state.json to snapshots/state-YYYY-MM-DD.json if today's
    snapshot does not yet exist.

    Returns the snapshot path on creation, None if today's already exists or
    state.json is missing (first-run; nothing to snapshot).

    Auto-prune of old snapshots is intentionally NOT done here — callers
    (save_atomic) prune separately after this call so prune failures cannot
    swallow snapshot-creation success.
    """
    client_path = Path(client_path)
    state_path = client_path / STATE_FILENAME
    if not state_path.exists():
        return None

    snap_dir = client_path / SNAPSHOTS_DIRNAME
    snap_dir.mkdir(parents=True, exist_ok=True)

    today_path = snap_dir / _snapshot_filename_for(_today())
    if today_path.exists():
        return None

    shutil.copy2(state_path, today_path)
    logger.info("snapshot created %s", today_path.name)
    return today_path


def _prune_snapshots(client_path: Path) -> int:
    """Delete snapshot files dated more than SNAPSHOT_RETENTION_DAYS ago.

    Returns the count of files deleted.
    """
    snap_dir = client_path / SNAPSHOTS_DIRNAME
    if not snap_dir.is_dir():
        return 0

    cutoff = _today() - timedelta(days=SNAPSHOT_RETENTION_DAYS)
    deleted = 0
    for entry in snap_dir.iterdir():
        if not entry.is_file():
            continue
        m = _SNAPSHOT_DATE_RE.match(entry.name)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if d < cutoff:
            try:
                entry.unlink()
                deleted += 1
                logger.debug("pruned old snapshot %s", entry.name)
            except OSError as exc:
                logger.warning(
                    "snapshot prune: could not delete %s: %s",
                    entry.name,
                    exc,
                )
    return deleted


# ---------------------------------------------------------------------------
# Conflict detection + history
# ---------------------------------------------------------------------------


def _values_match(a: PrimitiveValue, b: PrimitiveValue) -> bool:
    """Equality for conflict detection.

    Strings are compared after strip+casefold. Numerics compared with == on
    the float casts (handles 1 vs 1.0). None == None. Mixed types are
    treated as inequality.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b and isinstance(a, bool) == isinstance(b, bool)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().casefold() == b.strip().casefold()
    return False


def detect_conflicts(
    state: State,
    domain_tag: str,
    candidate: ExtractedField,
) -> ConflictDetection:
    """Compare a candidate against the existing canonical value at
    `state.fields[domain_tag]`."""
    existing = state.fields.get(domain_tag)
    if existing is None:
        return "no_existing"
    if _values_match(existing.value, candidate.value):
        return "match"
    # Different values — is the candidate higher confidence?
    if candidate.confidence > existing.confidence:
        return "conflict"
    return "lower_confidence_dropped"


def write_history_entry(
    state: State,
    *,
    domain_tag: str,
    run_id: str,
    user: str,
    actor: Actor,
    action: Action,
    prior: dict[str, Any] | None,
    new: dict[str, Any],
) -> None:
    """Append a HistoryEntry to `state.fields[domain_tag].history[]`.

    Singleton-only convenience. Repeatables write history per `RepeatableItem`
    member by direct list append on the relevant FieldRecord — same shape.
    """
    rec = state.fields.get(domain_tag)
    if rec is None:
        raise StateMergeError(
            f"write_history_entry: no field {domain_tag!r} in state"
        )
    rec.history.append(
        HistoryEntry(
            run_id=run_id,
            ts=_now_iso(),
            user=user,
            actor=actor,
            action=action,
            prior=prior,
            new=new,
        )
    )


def _record_snapshot(rec: FieldRecord) -> dict[str, Any]:
    """Capture the {value, status, confidence} triple for a HistoryEntry."""
    return {
        "value": rec.value,
        "status": rec.status,
        "confidence": rec.confidence,
    }


# ---------------------------------------------------------------------------
# Merge extraction
# ---------------------------------------------------------------------------


def _make_source_ref(r: ExtractedField) -> SourceRef:
    return SourceRef(
        doc_id=r.source_doc,
        page=int(r.source_page),
        quote=r.source_quote,
    )


def _make_conflict_candidate(r: ExtractedField) -> ConflictCandidate:
    return ConflictCandidate(
        value=r.value,
        confidence=float(r.confidence),
        source=_make_source_ref(r),
        observed_at=_now_iso(),
        model_used=r.model_used,
    )


def _create_field_record(r: ExtractedField, run_id: str) -> FieldRecord:
    rec = FieldRecord(
        value=r.value,
        confidence=float(r.confidence),
        status="pending",
        source=[_make_source_ref(r)],
        conflicts=[],
        history=[],
        needs_review=bool(r.needs_review),
        model_used=r.model_used,
    )
    rec.history.append(
        HistoryEntry(
            run_id=run_id,
            ts=_now_iso(),
            user="",  # caller writes RunHistoryEntry separately with os.getlogin
            actor="extractor",
            action="create",
            prior=None,
            new=_record_snapshot(rec),
        )
    )
    return rec


def _merge_singleton(
    state: State,
    r: ExtractedField,
    run_id: str,
    report: MergeReport,
) -> None:
    """Merge a non-repeatable field into state.fields."""
    tag = r.domain_tag
    detection = detect_conflicts(state, tag, r)

    if detection == "no_existing":
        state.fields[tag] = _create_field_record(r, run_id)
        report.fields_created += 1
        return

    rec = state.fields[tag]
    prior_snap = _record_snapshot(rec)
    new_source = _make_source_ref(r)

    if detection == "match":
        # Same value from a new doc — append source if it's not a duplicate.
        if not any(
            s.doc_id == new_source.doc_id
            and s.page == new_source.page
            and s.quote == new_source.quote
            for s in rec.source
        ):
            rec.source.append(new_source)
        # If the new candidate has higher confidence, bump.
        if r.confidence > rec.confidence:
            rec.confidence = float(r.confidence)
            rec.history.append(
                HistoryEntry(
                    run_id=run_id,
                    ts=_now_iso(),
                    user="",
                    actor="extractor",
                    action="update",
                    prior=prior_snap,
                    new=_record_snapshot(rec),
                )
            )
            report.fields_updated += 1
        return

    if detection == "conflict":
        # New value wins on confidence — promote it to canonical and demote
        # the old value to a conflict candidate so the operator can flip
        # back via the GUI's conflict modal if Claude was wrong.
        old_candidate = ConflictCandidate(
            value=rec.value,
            confidence=rec.confidence,
            source=(rec.source[0] if rec.source else new_source),
            observed_at=_now_iso(),
            model_used=rec.model_used,
        )
        rec.conflicts.append(old_candidate)
        rec.value = r.value
        rec.confidence = float(r.confidence)
        rec.model_used = r.model_used
        rec.needs_review = bool(r.needs_review or rec.needs_review)
        rec.source = [new_source]
        rec.history.append(
            HistoryEntry(
                run_id=run_id,
                ts=_now_iso(),
                user="",
                actor="extractor",
                action="update",
                prior=prior_snap,
                new=_record_snapshot(rec),
            )
        )
        report.fields_updated += 1
        report.conflicts_added += 1
        return

    if detection == "lower_confidence_dropped":
        # New value loses on confidence, but it's still a real disagreement —
        # capture as a conflict candidate and flag for review so the GUI
        # highlights the cell regardless of which value "won" on confidence.
        rec.conflicts.append(_make_conflict_candidate(r))
        rec.needs_review = True
        report.conflicts_added += 1
        return


def _merge_repeatable_item(
    item: RepeatableItem,
    r: ExtractedField,
    run_id: str,
    report: MergeReport,
) -> None:
    """Apply one ExtractedField to one RepeatableItem (same row)."""
    tag = r.domain_tag
    if tag not in item:
        item[tag] = _create_field_record(r, run_id)
        report.fields_created += 1
        return

    rec = item[tag]
    prior_snap = _record_snapshot(rec)
    new_source = _make_source_ref(r)

    if _values_match(rec.value, r.value):
        if not any(
            s.doc_id == new_source.doc_id
            and s.page == new_source.page
            and s.quote == new_source.quote
            for s in rec.source
        ):
            rec.source.append(new_source)
        if r.confidence > rec.confidence:
            rec.confidence = float(r.confidence)
            rec.history.append(
                HistoryEntry(
                    run_id=run_id,
                    ts=_now_iso(),
                    user="",
                    actor="extractor",
                    action="update",
                    prior=prior_snap,
                    new=_record_snapshot(rec),
                )
            )
            report.fields_updated += 1
        return

    if r.confidence > rec.confidence:
        old_candidate = ConflictCandidate(
            value=rec.value,
            confidence=rec.confidence,
            source=(rec.source[0] if rec.source else new_source),
            observed_at=_now_iso(),
            model_used=rec.model_used,
        )
        rec.conflicts.append(old_candidate)
        rec.value = r.value
        rec.confidence = float(r.confidence)
        rec.model_used = r.model_used
        rec.needs_review = bool(r.needs_review or rec.needs_review)
        rec.source = [new_source]
        rec.history.append(
            HistoryEntry(
                run_id=run_id,
                ts=_now_iso(),
                user="",
                actor="extractor",
                action="update",
                prior=prior_snap,
                new=_record_snapshot(rec),
            )
        )
        report.fields_updated += 1
        report.conflicts_added += 1
    else:
        rec.conflicts.append(_make_conflict_candidate(r))
        report.conflicts_added += 1


def _merge_repeatable(
    state: State,
    record_group: list[ExtractedField],
    run_id: str,
    report: MergeReport,
) -> None:
    """Merge a per-(group, repeatable_index) batch of ExtractedFields into
    state.repeatables[group].

    Why this exists: across multiple PDFs (e.g., ACORD apps + carrier specs)
    Claude reports the same vehicle/driver/etc. with different per-doc index
    numbers. We can't trust those indices to align — instead we compute a
    natural key (VIN, license#, address) and merge by that. If no key is
    derivable, we append rather than risk a wrong merge.

    Strategy:
      1. Build a tentative item dict from the batch (the per-row payload).
      2. Compute the natural key.
      3. If an existing item in state.repeatables[group] matches that key,
         merge each ExtractedField into the matching item.
      4. Otherwise append as a new item; if the key was None, increment
         appended_without_key.
    """
    if not record_group:
        return
    group = record_group[0].repeatable_group
    if group is None:
        raise StateMergeError("_merge_repeatable: empty repeatable_group")
    items = state.repeatables.setdefault(group, [])

    # Build candidate item for natural-key computation.
    candidate_item: RepeatableItem = {}
    for r in record_group:
        if r.domain_tag in candidate_item:
            # Same tag twice in one row from one batch: keep higher-confidence.
            existing = candidate_item[r.domain_tag]
            if r.confidence > existing.confidence:
                candidate_item[r.domain_tag] = _create_field_record(r, run_id)
        else:
            candidate_item[r.domain_tag] = _create_field_record(r, run_id)

    new_key = natural_key_for(group, candidate_item)
    target_index: int | None = None
    if new_key is not None:
        matches: list[int] = []
        for i, existing in enumerate(items):
            existing_key = natural_key_for(group, existing)
            if existing_key is not None and existing_key == new_key:
                matches.append(i)
        if matches:
            if len(matches) > 1:
                logger.warning(
                    "merge: ambiguous natural key for group=%s key=%s; "
                    "merging into first match (idx=%d)",
                    group,
                    new_key,
                    matches[0],
                )
            target_index = matches[0]

    # Extended match: when the primary key didn't resolve, try a
    # content-based secondary match for groups that support it (currently
    # only IM scheduled items, where serial-number availability varies across
    # docs).
    if target_index is None and group == "policy.inland_marine.scheduled_item":
        target_index = _im_scheduled_item_extended_match(candidate_item, items)

    if target_index is None:
        # Append new item.
        items.append(candidate_item)
        report.repeatable_items_added += 1
        if new_key is None:
            report.appended_without_key += 1
            report.notes.append(
                f"repeatable.{group}: appended row without natural key "
                f"(values keyed off Claude's row index only)"
            )
        return

    # Merge into the existing item.
    existing_item = items[target_index]
    for r in record_group:
        _merge_repeatable_item(existing_item, r, run_id, report)
    report.repeatable_items_merged += 1


def _validate_repeatable_namespace(records: list[ExtractedField]) -> None:
    """Sanity check: a record marked repeatable_group=X must have a
    domain_tag whose namespace prefix matches X.

    `repeatable_group` may be 1-3 segments per the locked registry:
      - 1-segment cross-LOB groups: 'location', 'prior_carrier', 'loss'
      - 2-segment account group:    'account.named_insured'
      - 3-segment LOB-nested:       'policy.gl.hazard',
                                    'policy.auto.vehicle',
                                    'policy.<lob>.additional_interest', etc.

    The domain_tag must start with `repeatable_group + "."` exactly.
    """
    for r in records:
        if r.repeatable_group is None:
            continue
        expected_prefix = r.repeatable_group + "."
        if not r.domain_tag.startswith(expected_prefix):
            raise StateMergeError(
                f"repeatable_group={r.repeatable_group!r} does not match "
                f"namespace of domain_tag={r.domain_tag!r} "
                f"(expected the tag to start with {expected_prefix!r}). "
                f"See ARCHITECTURE.md §3 rule 5."
            )


def merge_extraction(
    state: State,
    claude_records: list[ExtractedField],
    run_id: str,
    model_used: str,
) -> MergeReport:
    """Merge a batch of ExtractedField records into state.

    - Singleton fields go to `state.fields[domain_tag]`.
    - Repeatable fields are grouped by `(repeatable_group, repeatable_index)`
      from one source-doc batch and merged using the per-group natural key.
    - Conflicts are surfaced on `FieldRecord.conflicts[]`.
    - The MergeReport summarizes the call. Caller writes a `RunHistoryEntry`
      separately (we don't synthesize one here because run-level metadata —
      user, model_used, inputs, outcome — lives at the call site).
    - This function MUTATES `state` and does NOT save. Caller must call
      `save_atomic(state, client_path)` afterward.
    """
    _ = model_used  # currently informational; reserved for future per-batch tagging
    _validate_repeatable_namespace(claude_records)
    report = MergeReport()

    # Split into singletons vs repeatables.
    singletons: list[ExtractedField] = []
    repeatable_buckets: dict[tuple[str, str], list[ExtractedField]] = {}
    for r in claude_records:
        if r.repeatable_group is None:
            singletons.append(r)
        else:
            # Bucket by (group, source_doc + ":" + index) — per source-doc the
            # index is meaningful. Across docs we re-merge by natural key inside
            # _merge_repeatable.
            key = (
                r.repeatable_group,
                f"{r.source_doc}:{r.repeatable_index}",
            )
            repeatable_buckets.setdefault(key, []).append(r)

    for r in singletons:
        _merge_singleton(state, r, run_id, report)

    for bucket in repeatable_buckets.values():
        _merge_repeatable(state, bucket, run_id, report)

    logger.info(
        "merge_extraction run_id=%s created=%d updated=%d conflicts=%d "
        "items_added=%d items_merged=%d appended_without_key=%d",
        run_id,
        report.fields_created,
        report.fields_updated,
        report.conflicts_added,
        report.repeatable_items_added,
        report.repeatable_items_merged,
        report.appended_without_key,
    )
    return report


# ---------------------------------------------------------------------------
# Pending-pause / pending-extraction / run-history helpers
# ---------------------------------------------------------------------------


def get_pending_pause(state: State) -> PendingPause | None:
    return state.pending_pause


def set_pending_pause(state: State, pause: PendingPause | None) -> None:
    """Set or clear the pending_pause block. Caller is responsible for save."""
    state.pending_pause = pause


def get_pending_extraction(state: State) -> PendingExtraction | None:
    return state.pending_extraction


def set_pending_extraction(
    state: State,
    pending: PendingExtraction | None,
) -> None:
    """Set or clear the pending_extraction block. Caller is responsible for save."""
    state.pending_extraction = pending


def get_pending_domain_tag_proposals(state: State) -> list[dict[str, Any]]:
    """Return the current list of pending JIT domain_tag proposals (may be empty)."""
    return state.pending_domain_tag_proposals


def set_pending_domain_tag_proposals(
    state: State,
    proposals: list[dict[str, Any]],
) -> None:
    """Persist JIT domain_tag proposals on the state object for crash-safe resume.

    Each proposal is a dict with at minimum: domain_tag (or proposed_tag), value
    (or sample_value), source_doc, source_page, source_quote, confidence,
    model_used. The GUI's DomainTagConfirmationDialog consumes this list and,
    on confirmation, calls field_map.update_field() to write back to the Field
    Map.

    Mutates `state` in place; caller drives `save_atomic()`. Pass an empty list
    to clear.
    """
    state.pending_domain_tag_proposals = list(proposals)


def append_run_history(state: State, entry: RunHistoryEntry) -> None:
    """Append a top-level run-history entry. Caller is responsible for save."""
    state.run_history.append(entry)


# ---------------------------------------------------------------------------
# Convenience builder for tests + callers
# ---------------------------------------------------------------------------


def new_state(client: str) -> State:
    """Create an empty State for a new client (test + caller convenience)."""
    now = _now_iso()
    return State(client=client, created_at=now, updated_at=now)


def new_run_id() -> str:
    """uuid4 as a hex string. Caller convenience; not part of the schema."""
    return str(uuid4())

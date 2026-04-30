"""extract.py — multi-PDF extraction orchestration into state.json.

Public surface:
    run_extraction(client_name, pdf_paths, force_opus=False, *, settings=None,
                   resume_run_id=None) -> ExtractionResult
    resume_extraction_clear(client_path) -> None
    DomainTagProposal (dataclass)
    DocSummary (dataclass)
    CacheStats (dataclass)
    ExtractionResult (dataclass)
    ExtractionInputError, DuplicatePdfBasenameError,
    PendingExtractionDetectedError (exceptions)

Pipeline (see DECISION-MAP-extraction-agent.md for diagrams):
1. Validate inputs (non-empty list; files exist; no duplicate basenames).
2. Load state and field_map; check for stale `pending_extraction` block.
3. Set a fresh `pending_extraction` and atomically save (durable resume point).
4. For each PDF:
    a. Call `claude_client.extract_from_pdf`.
    b. Split returned records into known-tag (in current Field Map enum) and
       unknown-tag proposals (queued for GUI confirmation).
    c. Drop malformed tags with a logged warning.
    d. Merge known records into state via `state.merge_extraction`.
    e. Update `pending_extraction.completed_pdf_basenames` and save.
5. On clean completion: clear `pending_extraction`, append a RunHistoryEntry,
   persist any DomainTagProposals into `state.pending_domain_tag_proposals`,
   final atomic save.
6. On Claude error: same as 5 but with outcome="aborted".
7. Aggregate cache stats and log at INFO.

The extraction agent NEVER:
    - calls `field_map.update_field` or `field_map.save_atomic`
      (JIT confirmation is the GUI's job).
    - imports `gui`, `enter`, or `epic_session`.
    - picks a winner between conflicting values
      (operator decides in GUI).

See ARCHITECTURE.md §§ 4, 5, 6, 12, 14 #5 and DECISION-MAP-extraction-agent.md.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import claude_client, config, field_map, state

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from collections.abc import Sequence

__all__ = [
    "CacheStats",
    "DocSummary",
    "DomainTagProposal",
    "DuplicatePdfBasenameError",
    "ExtractionError",
    "ExtractionInputError",
    "ExtractionResult",
    "PendingExtractionDetectedError",
    "resume_extraction_clear",
    "run_extraction",
]

_logger = logging.getLogger("iga.extract")

# domain_tag grammar regex (ARCHITECTURE.md §3.1):
#   namespace.segment[.segment...]; lowercase a-z, digits, underscore;
#   at least one dot.
_DOMAIN_TAG_PATTERN: re.Pattern[str] = re.compile(
    r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$"
)


def _prompt_assets_dir() -> Path:
    """Return the assets/prompts directory bundled with the source tree.

    Resolves relative to this module's location so the lookup works whether
    the package is installed editable or copied into a build artifact.
    """
    here = Path(__file__).resolve()
    # extract.py -> iga_marketing_master_2 -> src -> repo root
    return here.parent.parent.parent / "assets" / "prompts"


def _load_prompt_asset(name: str, fallback: str) -> str:
    """Read a packaged prompt asset, falling back to a short string on error.

    The fallback exists so the module remains importable even if the assets
    folder is missing — the runtime cache_breakpoint_undersized warning
    will surface the real problem instead of an ImportError.
    """
    target = _prompt_assets_dir() / name
    try:
        return target.read_text(encoding="utf-8")
    except OSError as exc:
        _logger.warning(
            "extract.prompt_asset_missing path=%s error=%s using_fallback=true",
            target,
            exc,
        )
        return fallback


_DEFAULT_GLOSSARY: str = _load_prompt_asset(
    "glossary.txt",
    fallback=(
        "Insurance terms glossary. Used by Claude to disambiguate ACORD-form "
        "conventions, common LOB shorthand, and EPIC's vocabulary."
    ),
)
_DEFAULT_SYSTEM_PROMPT: str = _load_prompt_asset(
    "system_prompt.txt",
    fallback=(
        "You are an expert insurance-data extraction assistant. For every field "
        "you can identify in the attached PDF, call the record_extracted_field "
        "tool exactly once with a stable domain_tag, the literal source quote, "
        "the page number, and your confidence (0.0-1.0). If a field is missing "
        "or ambiguous, set needs_review=true and explain in source_quote."
    ),
)


# Exceptions ----------------------------------------------------------------


class ExtractionError(Exception):
    """Base exception for extraction-agent errors."""


class ExtractionInputError(ExtractionError):
    """Raised when run_extraction is called with invalid inputs."""


class DuplicatePdfBasenameError(ExtractionError):
    """Raised when two PDF paths in a single run share the same basename.

    See ARCHITECTURE.md §14 #5 — the extraction-agent uses basename as
    `doc_id`. Within-run collisions are blocked at validation time so the
    operator can rename or skip one of the files; we do not silently
    disambiguate.
    """

    def __init__(self, basename: str, paths: list[Path]) -> None:
        self.basename = basename
        self.paths = list(paths)
        joined = "\n  ".join(str(p) for p in paths)
        super().__init__(
            f"Two or more input PDFs share the basename {basename!r}. "
            f"Rename or skip one before re-running:\n  {joined}"
        )


class PendingExtractionDetectedError(ExtractionError):
    """Raised when run_extraction detects a stale `pending_extraction` block.

    The GUI catches this and prompts the operator to resume or discard.
    The pending block is exposed via `.pending` on the exception.
    """

    def __init__(self, pending: Any) -> None:
        self.pending = pending
        run_id = getattr(pending, "run_id", "<unknown>")
        started_at = getattr(pending, "started_at", "<unknown>")
        super().__init__(
            f"A previous extraction (run_id={run_id}, started_at={started_at}) "
            "was interrupted. Call resume_extraction_clear() to discard, or "
            "re-run run_extraction() with the remaining PDFs."
        )


# Public dataclasses --------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class DomainTagProposal:
    """A `domain_tag` proposed by Claude that is not in the current Field Map.

    Queued for human confirmation in the GUI. The GUI may accept (writes
    via `field_map.update_field`), edit (corrects the tag, then writes),
    or reject (drops the proposal). The extraction-agent never writes to
    the Field Map directly.

    ``reformatted_from`` is set when the proposal was salvaged from a
    grammatically-malformed Claude tag (e.g., flat snake_case
    ``named_insured`` rebuilt as ``account.named_insured``). The GUI shows
    this so the operator knows the proposal needs review even if the
    rewritten tag looks fine.
    """

    proposed_tag: str
    sample_value: str | int | float | bool | None
    source_doc: str
    source_page: int
    source_quote: str
    confidence: float
    run_id: str
    repeatable_group: str | None = None
    repeatable_index: int | None = None
    reformatted_from: str | None = None


@dataclass(slots=True, kw_only=True)
class CacheStats:
    """Aggregated prompt-cache + token usage for an extraction run."""

    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0


@dataclass(slots=True, kw_only=True)
class DocSummary:
    """Per-document summary surfaced in `ExtractionResult.per_doc`."""

    doc_id: str
    pdf_path: str
    records_returned: int = 0
    known_records: int = 0
    unknown_proposals: int = 0
    malformed_tags_dropped: int = 0
    fields_created: int = 0
    fields_updated: int = 0
    conflicts_added: int = 0
    repeatable_items_added: int = 0
    model_used: str | None = None
    error: str | None = None


@dataclass(slots=True, kw_only=True)
class ExtractionResult:
    """Returned by `run_extraction`. Passed to the GUI for review handoff."""

    run_id: str
    client_name: str
    outcome: str  # "completed" | "partial" | "aborted"
    started_at: str
    finished_at: str
    pdf_count: int
    fields_extracted: int = 0
    conflicts_surfaced: int = 0
    unknown_tags_queued: int = 0
    malformed_tags_dropped: int = 0
    repeatable_items_added: int = 0
    per_doc: list[DocSummary] = field(default_factory=list)
    pending_proposals: list[DomainTagProposal] = field(default_factory=list)
    cache_stats: CacheStats = field(default_factory=CacheStats)
    error_message: str | None = None


# Internal helpers ----------------------------------------------------------


def _now_iso() -> str:
    """ISO-8601 UTC timestamp used for state metadata."""
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return str(uuid.uuid4())


def _is_well_formed_tag(tag: str) -> bool:
    """Match the §3.1 grammar: lowercase namespace.segment[.segment...]."""
    if not isinstance(tag, str):
        return False
    return bool(_DOMAIN_TAG_PATTERN.match(tag))


# Heuristic mapping from common flat snake_case tags Claude tends to emit
# in bootstrap (no enum, no grammar) to dotted-grammar candidates the
# operator is likely to confirm. The mapping is deliberately small and
# conservative: it only handles the high-frequency flat tokens we saw in
# the operator's first real run; anything not in the map gets a generic
# ``proposed.<original>`` namespace so the operator can rename in the GUI.
_FLAT_TAG_NAMESPACE_HINTS: dict[str, str] = {
    "named_insured": "account",
    "named_insured_address": "account",
    "dba": "account",
    "fein": "account",
    "ein": "account",
    "mailing_address": "account",
    "agency_name": "agency",
    "agency_address": "agency",
    "agency_code": "agency",
    "agency_phone": "agency",
    "customer_number": "agency",
    "insurer_name": "policy",
    "insurer": "policy",
    "carrier": "policy",
    "carrier_name": "policy",
    "policy_number": "policy",
    "policy_form": "policy",
    "policy_effective_date": "policy",
    "policy_effective_time": "policy",
    "policy_expiration_date": "policy",
    "policy_expiration_time": "policy",
    "total_premium": "policy",
    "policy_premium": "policy",
    "endorsement_description": "policy.endorsement",
    "endorsement_form_number": "policy.endorsement",
    "endorsement_premium": "policy.endorsement",
    "vin": "vehicle",
    "make": "vehicle",
    "model": "vehicle",
    "year": "vehicle",
}


def _reformat_malformed_tag(tag: str) -> str | None:
    """Best-effort rewrite of a malformed Claude tag into dotted grammar.

    Returns the rewritten tag (always grammar-valid) or None if the input
    is unsalvageable (empty, non-string, or after cleaning still has no
    a-z characters). The result is always a JIT proposal — the operator
    confirms or renames in the GUI.
    """
    if not isinstance(tag, str):
        return None
    cleaned = tag.strip().lower()
    if not cleaned:
        return None
    # Replace whitespace, hyphens, and other separators with underscores;
    # drop anything that's not [a-z0-9_].
    cleaned = re.sub(r"[\s\-]+", "_", cleaned)
    cleaned = re.sub(r"[^a-z0-9_.]", "", cleaned)
    cleaned = cleaned.strip(".")
    if not cleaned:
        return None
    # Already dotted? Just guarantee shape.
    if "." in cleaned:
        # Collapse repeated dots and leading-digit segments.
        parts = [p for p in cleaned.split(".") if p]
        if not parts:
            return None
        # First segment must start with a letter.
        if not parts[0] or not parts[0][0].isalpha():
            parts[0] = "proposed_" + parts[0]
        candidate = ".".join(parts)
        if _DOMAIN_TAG_PATTERN.match(candidate):
            return candidate
        return f"proposed.{candidate.replace('.', '_')}"
    # Flat tag: try the namespace hint table first.
    namespace = _FLAT_TAG_NAMESPACE_HINTS.get(cleaned)
    if namespace is not None:
        candidate = f"{namespace}.{cleaned}"
        if _DOMAIN_TAG_PATTERN.match(candidate):
            return candidate
    # Fallback: tuck under a "proposed" namespace so the operator can rename.
    candidate = f"proposed.{cleaned}"
    if _DOMAIN_TAG_PATTERN.match(candidate):
        return candidate
    return None


def _check_duplicate_basenames(pdf_paths: list[Path]) -> None:
    """Raise DuplicatePdfBasenameError on within-run basename collisions."""
    seen: dict[str, list[Path]] = {}
    for p in pdf_paths:
        seen.setdefault(p.name, []).append(p)
    for basename, paths in seen.items():
        if len(paths) > 1:
            _logger.error(
                "extract.duplicate_basename basename=%s count=%d",
                basename,
                len(paths),
            )
            raise DuplicatePdfBasenameError(basename, paths)


def _load_settings_or_default(
    settings: config.Settings | None,
) -> config.Settings:
    if settings is not None:
        return settings
    return config.load_settings()


def _resolve_client_path(
    client_name: str,
    settings: config.Settings,
) -> Path:
    cleaned = client_name.strip()
    if not cleaned:
        raise ExtractionInputError("client_name must be a non-empty string")
    return settings.working_library / cleaned


def _split_records_by_known_tag(
    records: list[Any],
    known_tags: set[str],
    *,
    run_id: str,
    summary: DocSummary,
    recover_malformed: bool = True,
) -> tuple[list[Any], list[DomainTagProposal]]:
    """Partition records into (known, proposals).

    Three classifications per ARCHITECTURE.md §3.1 + the post-mortem on
    the bootstrap-state extraction:

    1. Valid + tag in current Field Map enum  -> known, merged into state.
    2. Valid grammar + not in current enum    -> JIT proposal.
    3. Invalid grammar                         -> JIT proposal with the
       reformatted candidate, ``reformatted_from`` set so the GUI can
       prompt the operator to confirm / rename. The original raw tag is
       still logged via ``extract.malformed_tag_reformatted``.

    Setting ``recover_malformed=False`` falls back to the legacy "drop
    malformed" behavior — used by tests that want to verify the dropped
    counter still ticks.
    """
    known: list[Any] = []
    proposals: list[DomainTagProposal] = []
    # Empty Field Map (early v1) means we accept every well-formed tag and
    # send every one to the JIT confirmation queue. Once the Field Map is
    # populated, only tags that match the canonical set bypass review.
    enforce_enum = bool(known_tags)
    for r in records:
        tag = getattr(r, "domain_tag", None)
        reformatted_from: str | None = None
        if not isinstance(tag, str) or not _is_well_formed_tag(tag):
            if not recover_malformed:
                _logger.warning(
                    "extract.malformed_tag_dropped run_id=%s doc=%s raw_tag=%r",
                    run_id,
                    summary.doc_id,
                    tag,
                )
                summary.malformed_tags_dropped += 1
                continue
            # Recoverable: rewrite to dotted grammar and queue for human
            # confirmation. We still bump the malformed counter so
            # diagnostics can show the bootstrap-state recovery rate.
            rewritten = _reformat_malformed_tag(tag) if isinstance(tag, str) else None
            if rewritten is None:
                _logger.warning(
                    "extract.malformed_tag_dropped run_id=%s doc=%s raw_tag=%r reason=unsalvageable",
                    run_id,
                    summary.doc_id,
                    tag,
                )
                summary.malformed_tags_dropped += 1
                continue
            _logger.warning(
                "extract.malformed_tag_reformatted run_id=%s doc=%s raw_tag=%r rewritten=%s",
                run_id,
                summary.doc_id,
                tag,
                rewritten,
            )
            summary.malformed_tags_dropped += 1
            reformatted_from = tag if isinstance(tag, str) else repr(tag)
            tag = rewritten
        if not enforce_enum or tag in known_tags:
            if reformatted_from is None:
                known.append(r)
                continue
            # A reformatted tag that happens to land on a known enum
            # value still goes through proposal review — the operator
            # should see the rewrite even if it's "right", because Claude
            # broke the grammar contract and we don't trust the value
            # blindly.
        proposals.append(
            DomainTagProposal(
                proposed_tag=tag,
                sample_value=getattr(r, "value", None),
                source_doc=getattr(r, "source_doc", summary.doc_id),
                source_page=int(getattr(r, "source_page", 1) or 1),
                source_quote=getattr(r, "source_quote", ""),
                confidence=float(getattr(r, "confidence", 0.0) or 0.0),
                run_id=run_id,
                repeatable_group=getattr(r, "repeatable_group", None),
                repeatable_index=getattr(r, "repeatable_index", None),
                reformatted_from=reformatted_from,
            )
        )
        _logger.info(
            "extract.proposal_queued run_id=%s doc=%s proposed_tag=%s sample_value=%r reformatted_from=%r",
            run_id,
            summary.doc_id,
            tag,
            getattr(r, "value", None),
            reformatted_from,
        )
    return known, proposals


def _aggregate_model_used(records: list[Any]) -> str | None:
    """Inspect ExtractedField.model_used across records.

    Returns "sonnet-4-6", "opus-4-7", "mixed", or None if no records.
    """
    seen: set[str] = set()
    for r in records:
        m = getattr(r, "model_used", None)
        if isinstance(m, str) and m:
            seen.add(m)
    if not seen:
        return None
    if len(seen) == 1:
        return next(iter(seen))
    return "mixed"


def _build_pending_extraction(
    *,
    run_id: str,
    started_at: str,
    pdf_paths: list[Path],
    completed: list[str],
) -> Any:
    """Construct state.PendingExtraction.

    Indirected through the `state` module so tests can substitute a mock
    PendingExtraction class.
    """
    return state.PendingExtraction(
        run_id=run_id,
        started_at=started_at,
        pdf_paths=[str(p) for p in pdf_paths],
        completed_pdf_basenames=list(completed),
    )


def _build_run_history_entry(
    *,
    run_id: str,
    user: str,
    inputs: list[str],
    outcome: str,
    model_used: str | None,
    forced_opus: bool,
    notes: str | None,
) -> Any:
    return state.RunHistoryEntry(
        run_id=run_id,
        ts=_now_iso(),
        user=user,
        kind="extraction",
        inputs=list(inputs),
        model_used=model_used,
        forced_opus=forced_opus,
        outcome=outcome,
        notes=notes,
    )


def _current_user() -> str:
    """Best-effort `os.getlogin()` with fallbacks for headless / CI."""
    import os
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"


def _record_to_source(record: Any, doc_id: str) -> dict[str, Any]:
    """Diagnostic-only — used in INFO logs to identify a record."""
    return {
        "doc_id": doc_id,
        "page": getattr(record, "source_page", None),
        "quote": getattr(record, "source_quote", "")[:80],
    }


def _accumulate_cache_stats(
    aggregate: CacheStats,
    per_call: Any,
) -> None:
    """Fold a per-call usage object (or dict) into the aggregate.

    Accepts either a dict-like or an attribute-bearing object so tests can
    substitute either shape.
    """
    if per_call is None:
        return

    def _get(name: str) -> int:
        if isinstance(per_call, dict):
            v = per_call.get(name, 0)
        else:
            v = getattr(per_call, name, 0)
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    aggregate.cache_creation_input_tokens += _get("cache_creation_input_tokens")
    aggregate.cache_read_input_tokens += _get("cache_read_input_tokens")
    aggregate.input_tokens += _get("input_tokens")
    aggregate.output_tokens += _get("output_tokens")
    aggregate.api_calls += 1


def _persist_pending_proposals(
    state_obj: Any,
    client_path: Path,
    proposals: list[DomainTagProposal],
) -> None:
    """Write proposals into `state.pending_domain_tag_proposals` if state.py
    exposes a setter; otherwise no-op (the GUI uses the in-memory return).
    """
    setter = getattr(state, "set_pending_domain_tag_proposals", None)
    if setter is None or not proposals:
        return
    try:
        setter(state_obj, [_proposal_to_dict(p) for p in proposals])
        state.save_atomic(state_obj, client_path)
    except (OSError, AttributeError, TypeError) as exc:
        _logger.warning(
            "extract.persist_proposals_failed err=%s count=%d",
            exc,
            len(proposals),
        )


def _proposal_to_dict(p: DomainTagProposal) -> dict[str, Any]:
    """JSON-friendly view of a proposal, for storage in state.json."""
    return {
        "proposed_tag": p.proposed_tag,
        "sample_value": p.sample_value,
        "source_doc": p.source_doc,
        "source_page": p.source_page,
        "source_quote": p.source_quote,
        "confidence": p.confidence,
        "run_id": p.run_id,
        "repeatable_group": p.repeatable_group,
        "repeatable_index": p.repeatable_index,
        "reformatted_from": p.reformatted_from,
    }


# Public API ----------------------------------------------------------------


def resume_extraction_clear(client_path: Path) -> None:
    """Discard a stale `pending_extraction` block on the given client.

    Called by the GUI when the operator chooses "Discard" on the recovery
    prompt. Loads state, clears the block, and atomically saves.
    """
    _logger.info("extract.resume_clear client_path=%s", client_path)
    state_obj = state.load(client_path)
    state.set_pending_extraction(state_obj, None)
    state.save_atomic(state_obj, client_path)


def run_extraction(
    client_name: str,
    pdf_paths: "Sequence[Path]",
    force_opus: bool = False,
    *,
    settings: config.Settings | None = None,
    resume_run_id: str | None = None,
    glossary: str | None = None,
    system_prompt: str | None = None,
) -> ExtractionResult:
    """Orchestrate extraction across `pdf_paths` for `client_name`.

    Args:
        client_name: Canonical client folder name under the Working Library.
        pdf_paths: One or more absolute Paths to source PDFs.
        force_opus: If True, claude_client uses Opus 4.7 on the first call
            (no Sonnet → Opus auto-escalation).
        settings: Optional override for `config.Settings`. If None, loaded
            via `config.load_settings()`.
        resume_run_id: If provided, continues a previously interrupted run.
            The caller must already have cleared any non-matching pending
            block via `resume_extraction_clear`.
        glossary: Optional override for the glossary block. Default uses
            an internal placeholder; production callers will pass the
            project glossary string.
        system_prompt: Optional override for the system prompt. Default
            uses an internal placeholder.

    Returns:
        ExtractionResult — counts, per-doc summary, total cache stats,
        run_id, pending_proposals.

    Raises:
        ExtractionInputError: invalid inputs.
        DuplicatePdfBasenameError: two PDFs share the same basename.
        PendingExtractionDetectedError: a stale `pending_extraction` block
            is present and `resume_run_id` does not match it.
    """
    pdf_list: list[Path] = [Path(p) for p in pdf_paths]
    if not pdf_list:
        raise ExtractionInputError(
            "pdf_paths must contain at least one PDF Path"
        )
    missing = [p for p in pdf_list if not p.is_file()]
    if missing:
        raise ExtractionInputError(
            "the following PDF paths do not exist or are not files:\n  "
            + "\n  ".join(str(p) for p in missing)
        )
    _check_duplicate_basenames(pdf_list)

    settings = _load_settings_or_default(settings)
    client_path = _resolve_client_path(client_name, settings)
    client_path.mkdir(parents=True, exist_ok=True)

    fm = field_map.load()
    enum_tags = set(field_map.generate_domain_tag_enum(fm))

    state_obj = state.load(client_path)

    pending_existing = state.get_pending_extraction(state_obj)
    if pending_existing is not None:
        existing_id = getattr(pending_existing, "run_id", None)
        if resume_run_id is None or resume_run_id != existing_id:
            _logger.warning(
                "extract.pending_detected run_id=%s started_at=%s",
                existing_id,
                getattr(pending_existing, "started_at", None),
            )
            raise PendingExtractionDetectedError(pending_existing)
        run_id = existing_id
        started_at = (
            getattr(pending_existing, "started_at", None) or _now_iso()
        )
        completed = list(
            getattr(pending_existing, "completed_pdf_basenames", []) or []
        )
        _logger.info(
            "extract.resume run_id=%s already_completed=%d total=%d",
            run_id,
            len(completed),
            len(pdf_list),
        )
    else:
        run_id = _new_run_id()
        started_at = _now_iso()
        completed = []

    _logger.info(
        "extract.run_start client=%s run_id=%s docs=%d force_opus=%s",
        client_name,
        run_id,
        len(pdf_list),
        force_opus,
    )

    # Durable pending block before first API call. If the process crashes
    # mid-run (or the operator force-closes the window), `pending_extraction`
    # is what tells us "this client has work in flight" on next launch — the
    # GUI's RecoverInterruptedRunDialog reads it and offers Resume/Discard.
    pending = _build_pending_extraction(
        run_id=run_id,
        started_at=started_at,
        pdf_paths=pdf_list,
        completed=completed,
    )
    state.set_pending_extraction(state_obj, pending)
    state.save_atomic(state_obj, client_path)

    aggregate_cache = CacheStats()
    per_doc: list[DocSummary] = []
    all_proposals: list[DomainTagProposal] = []
    total_records = 0
    total_conflicts = 0
    total_repeatables = 0
    total_malformed = 0
    run_failed = False
    error_message: str | None = None
    last_completed_model: str | None = None
    completed_set = set(completed)
    debug_root = client_path / "debug" / "claude" / run_id

    glossary_text = glossary if glossary is not None else _DEFAULT_GLOSSARY
    system_prompt_text = (
        system_prompt if system_prompt is not None else _DEFAULT_SYSTEM_PROMPT
    )

    for pdf_path in pdf_list:
        doc_id = pdf_path.name
        if doc_id in completed_set:
            _logger.info(
                "extract.skip_already_done run_id=%s doc=%s", run_id, doc_id
            )
            continue
        summary = DocSummary(doc_id=doc_id, pdf_path=str(pdf_path))
        _logger.info(
            "extract.doc_start run_id=%s doc=%s", run_id, doc_id
        )
        try:
            records = claude_client.extract_from_pdf(
                pdf_path,
                fm,
                glossary_text,
                system_prompt_text,
                force_opus=force_opus,
                run_id=run_id,
                debug_dir=debug_root,
            )
        except claude_client.ClaudeError as exc:
            run_failed = True
            error_message = f"{type(exc).__name__}: {exc}"
            summary.error = error_message
            per_doc.append(summary)
            _logger.error(
                "extract.claude_error run_id=%s doc=%s err=%s",
                run_id,
                doc_id,
                error_message,
            )
            break

        # Aggregate cache stats: claude_client may attach a `cache_usage`
        # attribute to the returned list (some implementations) or to each
        # record. We accept either; missing => zero. Read BEFORE coercing
        # to a plain list so attribute-bearing list subclasses survive.
        usage = getattr(records, "cache_usage", None)
        records = list(records or [])
        if usage is None and records:
            usage = getattr(records[0], "_call_usage", None)
        _accumulate_cache_stats(aggregate_cache, usage)
        summary.records_returned = len(records)

        # Partition tags.
        known, proposals = _split_records_by_known_tag(
            records,
            enum_tags,
            run_id=run_id,
            summary=summary,
        )
        summary.known_records = len(known)
        summary.unknown_proposals = len(proposals)
        all_proposals.extend(proposals)
        total_malformed += summary.malformed_tags_dropped

        summary.model_used = _aggregate_model_used(records)
        if summary.model_used is not None:
            last_completed_model = (
                summary.model_used
                if last_completed_model is None
                or last_completed_model == summary.model_used
                else "mixed"
            )

        # Merge known records into state.
        report = state.merge_extraction(
            state_obj,
            known,
            run_id,
            summary.model_used or "sonnet-4-6",
        )
        summary.fields_created = int(getattr(report, "fields_created", 0) or 0)
        summary.fields_updated = int(getattr(report, "fields_updated", 0) or 0)
        summary.conflicts_added = int(getattr(report, "conflicts_added", 0) or 0)
        summary.repeatable_items_added = int(
            getattr(report, "repeatable_items_added", 0) or 0
        )

        total_records += summary.known_records
        total_conflicts += summary.conflicts_added
        total_repeatables += summary.repeatable_items_added

        _logger.info(
            "extract.doc_merged run_id=%s doc=%s "
            "fields_created=%d fields_updated=%d conflicts=%d repeatables=%d "
            "known=%d unknown=%d malformed_dropped=%d",
            run_id,
            doc_id,
            summary.fields_created,
            summary.fields_updated,
            summary.conflicts_added,
            summary.repeatable_items_added,
            summary.known_records,
            summary.unknown_proposals,
            summary.malformed_tags_dropped,
        )

        # Per-doc commit: each PDF that finishes successfully advances the
        # `completed_pdf_basenames` list and gets saved to disk. A crash now
        # only loses work for whatever doc was actively being extracted.
        completed_set.add(doc_id)
        pending = _build_pending_extraction(
            run_id=run_id,
            started_at=started_at,
            pdf_paths=pdf_list,
            completed=sorted(completed_set),
        )
        state.set_pending_extraction(state_obj, pending)
        state.save_atomic(state_obj, client_path)
        per_doc.append(summary)

    # Finalize ----------------------------------------------------------
    finished_at = _now_iso()
    if run_failed:
        outcome = "aborted"
    elif len(completed_set) < len(pdf_list):
        outcome = "partial"
    else:
        outcome = "completed"

    history_entry = _build_run_history_entry(
        run_id=run_id,
        user=_current_user(),
        inputs=[p.name for p in pdf_list],
        outcome=outcome,
        model_used=last_completed_model,
        forced_opus=force_opus,
        notes=error_message,
    )
    state.append_run_history(state_obj, history_entry)
    state.set_pending_extraction(state_obj, None)
    state.save_atomic(state_obj, client_path)

    if all_proposals:
        _persist_pending_proposals(state_obj, client_path, all_proposals)

    _logger.info(
        "extract.run_end client=%s run_id=%s outcome=%s "
        "total_records=%d conflicts=%d proposals=%d malformed=%d "
        "cache_creation=%d cache_read=%d input=%d output=%d api_calls=%d",
        client_name,
        run_id,
        outcome,
        total_records,
        total_conflicts,
        len(all_proposals),
        total_malformed,
        aggregate_cache.cache_creation_input_tokens,
        aggregate_cache.cache_read_input_tokens,
        aggregate_cache.input_tokens,
        aggregate_cache.output_tokens,
        aggregate_cache.api_calls,
    )

    return ExtractionResult(
        run_id=run_id,
        client_name=client_name,
        outcome=outcome,
        started_at=started_at,
        finished_at=finished_at,
        pdf_count=len(pdf_list),
        fields_extracted=total_records,
        conflicts_surfaced=total_conflicts,
        unknown_tags_queued=len(all_proposals),
        malformed_tags_dropped=total_malformed,
        repeatable_items_added=total_repeatables,
        per_doc=per_doc,
        pending_proposals=all_proposals,
        cache_stats=aggregate_cache,
        error_message=error_message,
    )

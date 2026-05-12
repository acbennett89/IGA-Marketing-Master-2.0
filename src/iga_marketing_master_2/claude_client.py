"""claude_client.py - Anthropic SDK wrapper for IGA Marketing Master 2.0.

Owns all Claude API interaction:

- Default model: Sonnet 4.6. Auto-escalation to Opus 4.7 per-field on
  ``confidence < CONFIDENCE_LOW_THRESHOLD`` OR ``needs_review`` OR
  required-field-missing.
- JSON-mode output (NOT tool use). The system prompt embeds the literal
  expected JSON schema; Claude returns one JSON object containing every
  extracted field plus every repeatable item. The pipeline parses that
  object with ``json.loads`` and emits one ``ExtractedField`` per entry.
- Two prompt-cache breakpoints: stable system+glossary+examples, then
  Field Map.
- PDF document blocks with pagecount/size preflight; auto-split at 80-page
  boundaries with 1-page overlap.
- Exponential-backoff retry on rate limit / transient outage; surfaces
  operator-readable errors to the GUI.
- Cache verification (Amendment #17): logs ``cache_creation_input_tokens``
  and ``cache_read_input_tokens`` from every response. Warns if both are
  zero on a request that should have cached. Field Map churn is documented
  as a known cause of cache misses (Amendment #4).

SDK pin: ``anthropic>=0.42`` (post-Sonnet-4.6 SDK).
Verified against ``anthropic==0.97.0``: ``Message.usage.cache_creation_input_tokens``
and ``Message.usage.cache_read_input_tokens`` exist as the documented
attribute names. See DECISION-MAP-claude-client-agent.md for details.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import anthropic
import httpx
import pypdf

from . import field_map as field_map_mod  # FieldMap protocol satisfied by field_map module
from . import secret_store

__all__ = [
    "CONFIDENCE_LOW_THRESHOLD",
    "MAX_PAGES_PER_CALL",
    "MAX_BYTES_PER_CALL",
    "PAGE_OVERLAP",
    "DEFAULT_SONNET_MODEL",
    "DEFAULT_OPUS_MODEL",
    "RETRY_BACKOFF_SECONDS",
    "MAX_TOKENS_PER_CALL",
    "ExtractedField",
    "ClaudeError",
    "ClaudeAuthError",
    "ClaudeRateLimitError",
    "ClaudeServerError",
    "ClaudePDFTooLargeError",
    "ClaudeCacheMissError",
    "ClaudeParseError",
    "FieldMap",
    "extract_from_pdf",
    "reextract_low_confidence_fields",
]

# ----- Module-level constants (per ARCHITECTURE.md Appendix A) ----------------

CONFIDENCE_LOW_THRESHOLD: float = 0.7
ESCALATION_ENABLED: bool = False  # Opus auto-escalation; disable to run Sonnet-only
MAX_PAGES_PER_CALL: int = 80
MAX_BYTES_PER_CALL: int = 32 * 1024 * 1024  # Anthropic 32 MB ceiling
PAGE_OVERLAP: int = 1
DEFAULT_SONNET_MODEL: str = "claude-sonnet-4-6"
DEFAULT_OPUS_MODEL: str = "claude-opus-4-7"
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (1, 2, 4, 8)
MAX_TOKENS_PER_CALL: int = 32_000

# Sonnet 4.6 cache breakpoint minimum (RESEARCH.md Finding 5)
SONNET_CACHE_MIN_TOKENS: int = 2_048

# Heuristic for breakpoint sizing warnings (chars per token)
_CHARS_PER_TOKEN_HEURISTIC: int = 4

# Repeatable groups recognized in the JSON ``repeatables`` block. Any
# group key Claude emits will be honored; this list documents the
# canonical set the system prompt also lists. Group names are NOT enforced
# at parse time so a JIT-proposal flow can still surface novel groups.
_KNOWN_REPEATABLE_GROUPS: tuple[str, ...] = (
    "vehicle",
    "driver",
    "location",
    "loss_payee",
    "additional_insured",
    "prior_carrier",
    "loss",
)

logger = logging.getLogger("iga.claude")


# ----- Exception hierarchy (per ARCHITECTURE.md §6.8) -------------------------


class ClaudeError(Exception):
    """Base exception for the Claude integration."""


class ClaudeAuthError(ClaudeError):
    """401/403 from Anthropic. Surface to operator for re-prompt."""


class ClaudeRateLimitError(ClaudeError):
    """429 from Anthropic. Caller may retry with backoff."""


class ClaudeServerError(ClaudeError):
    """5xx from Anthropic. Caller may retry."""


class ClaudePDFTooLargeError(ClaudeError):
    """Caught and resolved internally via split. Should not escape extract_from_pdf."""


class ClaudeCacheMissError(ClaudeError):
    """NEVER raised; cache miss is a warning, not an error.

    Reserved for symmetry with ARCHITECTURE.md §6.8.
    """


class ClaudeParseError(ClaudeError):
    """Claude returned text that could not be parsed as the expected JSON object.

    Raised by :func:`_parse_json_object` when the response text isn't valid
    JSON, isn't a JSON object, or is missing entirely. The extraction-agent
    surfaces this to the operator as a per-doc error in the
    ``ExtractionResult``; the run as a whole continues so other PDFs still
    get processed.
    """


# ----- Public dataclass -------------------------------------------------------


@dataclass(slots=True, kw_only=True)
class ExtractedField:
    """One extracted insurance field, returned by the Extractor."""

    domain_tag: str
    value: str | int | float | bool | None
    source_doc: str
    source_page: int
    source_quote: str
    confidence: float
    needs_review: bool = False
    repeatable_group: str | None = None
    repeatable_index: int | None = None
    model_used: str = DEFAULT_SONNET_MODEL


@dataclass(slots=True, kw_only=True)
class _CallUsage:
    """Per-call (or summed-across-chunks) Anthropic usage block.

    Attached to the ``list`` returned by :func:`extract_from_pdf` /
    :func:`reextract_low_confidence_fields` so ``extract.py`` can
    aggregate cache + token usage across documents without re-reading
    the SDK Message objects (which are already discarded by the time
    extract sees the records).

    Bug 7 fix: previously these numbers were only logged at INFO and
    never propagated, so ``extract.run_end`` reported all zeros even
    on a successful run with real billing.
    """

    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    api_calls: int = 0

    def add(self, other: "_CallUsage") -> None:
        """In-place sum of ``other`` into ``self``."""
        self.cache_creation_input_tokens += other.cache_creation_input_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.api_calls += other.api_calls


class _RecordsList(list):  # noqa: SLOT000 - list subclass needs __dict__ for cache_usage
    """``list`` subclass that carries a ``cache_usage`` attribute.

    The extraction-agent reads this via ``getattr(records, "cache_usage", None)``
    (see ``extract.py``) — keeping the wire shape as a plain list keeps the
    public contract stable while letting us hang per-call usage off the
    return value.
    """

    cache_usage: _CallUsage | None = None


# ---------------------------------------------------------------------------
# Cost calculation
# ---------------------------------------------------------------------------
# Anthropic API pricing per 1M tokens, USD. Documented rates as of 2026-05.
# Cache-write is 1.25× input; cache-read is 0.10× input.
_PRICING_PER_M: dict[str, dict[str, float]] = {
    "sonnet": {
        "input":       3.00,
        "cache_write": 3.75,
        "cache_read":  0.30,
        "output":     15.00,
    },
    "opus": {
        "input":      15.00,
        "cache_write": 18.75,
        "cache_read":  1.50,
        "output":     75.00,
    },
}


def compute_cost_usd(
    *,
    input_tokens: int,
    cache_creation_input_tokens: int,
    cache_read_input_tokens: int,
    output_tokens: int,
    model: str | None,
) -> float:
    """Return the USD cost of an Anthropic API call.

    Selects Sonnet or Opus rates based on the model string ('opus' substring
    match → Opus rates; everything else, including None and 'mixed', falls
    back to Sonnet rates so we under-estimate rather than over-bill).
    """
    m = (model or "").lower()
    rates = _PRICING_PER_M["opus"] if "opus" in m else _PRICING_PER_M["sonnet"]
    return (
        int(input_tokens or 0)                  / 1_000_000 * rates["input"]
        + int(cache_creation_input_tokens or 0) / 1_000_000 * rates["cache_write"]
        + int(cache_read_input_tokens or 0)     / 1_000_000 * rates["cache_read"]
        + int(output_tokens or 0)               / 1_000_000 * rates["output"]
    )


def _usage_from_response(response: Any) -> _CallUsage:
    """Build a :class:`_CallUsage` from an Anthropic ``Message.usage`` block.

    All fields default to zero on missing/None values so a partial SDK
    response (or a test stub) doesn't blow up the aggregation.
    """
    usage = getattr(response, "usage", None)
    return _CallUsage(
        cache_creation_input_tokens=int(
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        ),
        cache_read_input_tokens=int(
            getattr(usage, "cache_read_input_tokens", 0) or 0
        ),
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        api_calls=1,
    )


# ----- FieldMap protocol ------------------------------------------------------


@runtime_checkable
class FieldMap(Protocol):
    """Structural protocol for FieldMap.

    The real implementation lives in ``field_map.py``; this Protocol
    defines the surface ``claude_client`` requires so the module can be
    loaded and tested independently.

    Any duck-typed stand-in (test fakes, future alternative backends) must
    expose ``generate_domain_tag_enum`` and ``lookup_by_domain_tag``. The
    optional ``raw`` mapping (top-level JSON dict keyed by screen label) is
    consulted by :func:`_serialize_field_map_for_prompt` when present so
    Claude sees the live screen/field universe; absent it, only the enum
    falls through.
    """

    def generate_domain_tag_enum(self) -> list[str]: ...

    def lookup_by_domain_tag(self, domain_tag: str) -> Any | None: ...


# A few-shot/example payload type alias
SystemPromptPart = Mapping[str, Any]


# ----- Field Map enum helper --------------------------------------------------


def _safe_generate_domain_tag_enum(field_map: FieldMap) -> list[str]:
    """Call ``field_map.generate_domain_tag_enum()`` defensively.

    The Field Map module is owned by another agent and may not be loaded
    in unit tests. Falls back to an empty enum on any failure.
    """
    try:
        result = field_map.generate_domain_tag_enum()
        if result is None:
            return []
        return [str(t) for t in result]
    except Exception:  # pragma: no cover - defensive
        logger.warning(
            "field_map.generate_domain_tag_enum failed; using free-text domain_tag",
            exc_info=True,
        )
        return []


# ----- Field Map prompt block ------------------------------------------------


_GRAMMAR_REMINDER: str = (
    "When recording an extracted field, set `domain_tag` to a dot-separated "
    "lowercase identifier matching this map (e.g., `submission.name`, "
    "`account.named_insured`, `policy.gl.aggregate_limit`, `vehicle.vin`). "
    "If you encounter a concept not in this map, propose a new tag using "
    "the same grammar — the human will confirm or rename it."
)


def _serialize_field_map_for_prompt(field_map: FieldMap) -> str:
    """Render the Field Map as a stable text block for the second cache breakpoint.

    Strategy:
      1. Header + grammar reminder.
      2. Sorted ``domain_tag`` enum (canonical tags currently in the map).
      3. A walk of the live ``raw`` JSON: every screen, with every leaf
         field's label, name, type, hint, enum_values, notes_for_claude.
      4. Aggregated ``notes_for_claude`` block (legacy view).

    The block is deterministic across runs (sorted screen labels, sorted
    field keys) so prompt caching keys remain stable. If the FieldMap
    duck-typed object doesn't expose ``raw`` (test stubs), only sections
    1, 2, and 4 are emitted.
    """
    enum_values = _safe_generate_domain_tag_enum(field_map)

    parts: list[str] = []
    parts.append("=== FIELD MAP — grammar reminder ===")
    parts.append(_GRAMMAR_REMINDER)
    parts.append("")
    parts.append("=== FIELD MAP — domain_tag enum (sorted) ===")
    if enum_values:
        parts.extend(enum_values)
    else:
        parts.append("(empty — free-text domain_tag accepted; JIT enrichment will follow)")

    # Walk the on-disk Field Map for the screen/field universe. This is the
    # single most important block for first-call extraction quality:
    # without it, Claude has no idea what fields EPIC actually contains.
    raw = getattr(field_map, "raw", None)
    if isinstance(raw, dict) and raw:
        parts.append("")
        parts.append("=== FIELD MAP — screens and fields (EPIC universe) ===")
        for screen_label in sorted(raw.keys()):
            screen_obj = raw[screen_label]
            if not isinstance(screen_obj, dict):
                continue
            screen_lines = _render_screen(screen_label, screen_obj)
            if screen_lines:
                parts.extend(screen_lines)
                parts.append("")

    notes_block = _collect_notes_for_claude(field_map)
    if notes_block:
        parts.append("=== FIELD MAP — notes_for_claude ===")
        parts.append(notes_block)

    return "\n".join(parts)


def _render_screen(screen_label: str, screen_obj: Mapping[str, Any]) -> list[str]:
    """Render a single screen (incl. its tabs and sub_tabs) as text lines."""
    screen_code = screen_obj.get("screen_code")
    header = (
        f"## Screen: {screen_label}"
        + (f" (screen_code={screen_code})" if isinstance(screen_code, str) else "")
    )
    lines: list[str] = [header]

    field_lines = _render_field_list(screen_obj.get("fields") or [])
    if field_lines:
        lines.extend(field_lines)

    tabs = screen_obj.get("tabs") or []
    if isinstance(tabs, list):
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            tab_label = tab.get("label") or "<unnamed-tab>"
            tab_field_lines = _render_field_list(tab.get("fields") or [])
            if tab_field_lines:
                lines.append(f"  Tab: {tab_label}")
                lines.extend(_indent_lines(tab_field_lines, 2))
            sub_tabs = tab.get("sub_tabs") or []
            if isinstance(sub_tabs, list):
                for sub_tab in sub_tabs:
                    if not isinstance(sub_tab, dict):
                        continue
                    sub_label = sub_tab.get("label") or "<unnamed-sub_tab>"
                    sub_field_lines = _render_field_list(sub_tab.get("fields") or [])
                    if sub_field_lines:
                        lines.append(f"    Sub-tab: {sub_label}")
                        lines.extend(_indent_lines(sub_field_lines, 4))

    sub_tabs = screen_obj.get("sub_tabs") or []
    if isinstance(sub_tabs, list):
        for sub_tab in sub_tabs:
            if not isinstance(sub_tab, dict):
                continue
            sub_label = sub_tab.get("label") or "<unnamed-sub_tab>"
            sub_field_lines = _render_field_list(sub_tab.get("fields") or [])
            if sub_field_lines:
                lines.append(f"  Sub-tab: {sub_label}")
                lines.extend(_indent_lines(sub_field_lines, 2))

    # Skip a screen entirely when we have nothing useful to say about it.
    if len(lines) <= 1:
        return []
    return lines


def _render_field_list(raw_fields: Any) -> list[str]:
    """Render one container's ``fields[]`` list as deterministic text lines."""
    if not isinstance(raw_fields, list):
        return []
    rendered: list[str] = []
    for raw_field in raw_fields:
        if not isinstance(raw_field, dict):
            continue
        line = _render_field(raw_field)
        if line is not None:
            rendered.append(line)
    rendered.sort()
    return rendered


def _render_field(raw_field: Mapping[str, Any]) -> str | None:
    """Render one leaf field's descriptive content as a single line.

    Returns None when the field has no useful descriptive content (no
    label, no hint, no domain_tag, no notes). The Field Map JSON does
    contain a few placeholder entries; we drop them silently so Claude's
    context window isn't burned on noise.
    """
    label = raw_field.get("label")
    name = raw_field.get("name")
    field_type = raw_field.get("type")
    hint = raw_field.get("hint")
    domain_tag = raw_field.get("domain_tag")
    notes = raw_field.get("notes_for_claude")
    enum_values = raw_field.get("enum_values")

    if not any([label, name, hint, domain_tag, notes]):
        return None

    pieces: list[str] = []
    if isinstance(label, str) and label:
        pieces.append(f"label={label!r}")
    if isinstance(name, str) and name:
        pieces.append(f"name={name!r}")
    if isinstance(field_type, str) and field_type:
        pieces.append(f"type={field_type}")
    if isinstance(domain_tag, str) and domain_tag:
        pieces.append(f"domain_tag={domain_tag}")
    if isinstance(hint, str) and hint:
        pieces.append(f"hint={hint!r}")
    if isinstance(enum_values, list) and enum_values:
        # Cap at 12 to keep individual lines from blowing up the prompt.
        sample = [str(v) for v in enum_values[:12]]
        suffix = "" if len(enum_values) <= 12 else f" (+{len(enum_values) - 12} more)"
        pieces.append(f"enum=[{', '.join(sample)}]{suffix}")
    if isinstance(notes, str) and notes:
        pieces.append(f"notes={notes!r}")
    return "- " + "; ".join(pieces)


def _indent_lines(lines: list[str], spaces: int) -> list[str]:
    pad = " " * spaces
    return [f"{pad}{line}" for line in lines]


def _collect_notes_for_claude(field_map: FieldMap) -> str:
    """Pull ``notes_for_claude`` from the Field Map if available.

    Returns an empty string when the Field Map doesn't expose a helper or
    a module-level ``iter_notes_for_claude(field_map)`` function isn't
    present. This keeps caching deterministic and avoids accidental
    coupling to a Field Map implementation that hasn't shipped yet.
    """
    notes: Iterable[tuple[str, str]] | None = None
    instance_helper = getattr(field_map, "iter_notes_for_claude", None)
    if callable(instance_helper):
        try:
            notes = list(instance_helper())
        except Exception:  # pragma: no cover - defensive
            notes = None
    if notes is None:
        module_helper = getattr(field_map_mod, "iter_notes_for_claude", None)
        if callable(module_helper):
            try:
                notes = list(module_helper(field_map))
            except Exception:  # pragma: no cover - defensive
                notes = None
    if not notes:
        return ""
    rendered: list[str] = []
    for tag, note in notes:
        if not note:
            continue
        rendered.append(f"- {tag}: {note}")
    return "\n".join(rendered)


# ----- PDF preflight + split (per ARCHITECTURE.md §6.6) -----------------------


@dataclass(slots=True, kw_only=True)
class _PdfChunk:
    """One slice of a (possibly split) PDF, ready for an API call."""

    pdf_bytes: bytes
    start_page: int  # 1-indexed inclusive
    end_page: int  # 1-indexed inclusive
    basename: str
    source_basename: str  # original PDF basename (used as source_doc)
    on_disk_path: Path | None = None  # set when written under debug/split/


def _read_pdf_chunks(
    pdf_path: Path,
    *,
    debug_dir: Path | None,
    run_id: str,
) -> list[_PdfChunk]:
    """Open ``pdf_path`` and return one or more chunks ready for API calls.

    A single-call PDF (<=80 pages AND <=32 MB) yields a one-element list
    with the raw bytes. Otherwise the PDF is split at 80-page boundaries
    with a 1-page overlap.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    raw = pdf_path.read_bytes()
    size_bytes = len(raw)
    reader = pypdf.PdfReader(io.BytesIO(raw))
    page_count = len(reader.pages)

    # Anthropic enforces both a page-count cap (80) and a byte-size cap
    # (32 MB) on a single request. If we're under both limits we send the
    # raw PDF unchanged; otherwise we split into 80-page slices with a
    # 1-page overlap so a field that straddles a page break is seen by both
    # halves.
    needs_split = page_count > MAX_PAGES_PER_CALL or size_bytes > MAX_BYTES_PER_CALL
    if not needs_split:
        return [
            _PdfChunk(
                pdf_bytes=raw,
                start_page=1,
                end_page=page_count,
                basename=pdf_path.name,
                source_basename=pdf_path.name,
            )
        ]

    chunk_specs = _compute_split_ranges(page_count)
    logger.info(
        "claude.split_pdf doc=%s page_count=%d size_bytes=%d chunks=%s",
        pdf_path.name,
        page_count,
        size_bytes,
        chunk_specs,
    )

    chunks: list[_PdfChunk] = []
    out_dir: Path | None = None
    if debug_dir is not None:
        # Per ARCHITECTURE.md §6.6: write split chunks under
        # <client>/debug/split/<run_id>/. The caller passes the per-doc
        # claude debug dir; we hop up to ../split/<run_id>/.
        out_dir = debug_dir.parent.parent / "split" / run_id
        out_dir.mkdir(parents=True, exist_ok=True)

    for idx, (start, end) in enumerate(chunk_specs, start=1):
        writer = pypdf.PdfWriter()
        # pypdf pages are 0-indexed; ranges are 1-indexed inclusive.
        for page_idx in range(start - 1, end):
            writer.add_page(reader.pages[page_idx])
        buf = io.BytesIO()
        writer.write(buf)
        buf.seek(0)
        chunk_bytes = buf.read()
        chunk_basename = f"{pdf_path.stem}.chunk{idx:02d}.p{start}-{end}.pdf"
        on_disk: Path | None = None
        if out_dir is not None:
            on_disk = out_dir / chunk_basename
            on_disk.write_bytes(chunk_bytes)
        chunks.append(
            _PdfChunk(
                pdf_bytes=chunk_bytes,
                start_page=start,
                end_page=end,
                basename=chunk_basename,
                source_basename=pdf_path.name,
                on_disk_path=on_disk,
            )
        )

    return chunks


def _compute_split_ranges(page_count: int) -> list[tuple[int, int]]:
    """Return list of (start_page, end_page) 1-indexed inclusive ranges.

    Each chunk is at most ``MAX_PAGES_PER_CALL`` pages. Adjacent chunks
    overlap by ``PAGE_OVERLAP`` pages: chunk N's last page == chunk N+1's
    first page.
    """
    if page_count <= MAX_PAGES_PER_CALL:
        return [(1, page_count)]

    ranges: list[tuple[int, int]] = []
    start = 1
    while start <= page_count:
        end = min(start + MAX_PAGES_PER_CALL - 1, page_count)
        ranges.append((start, end))
        if end == page_count:
            break
        # Next chunk starts at the overlap boundary.
        start = end - PAGE_OVERLAP + 1
    return ranges


# ----- Message construction --------------------------------------------------


def _build_request(
    *,
    pdf_chunk: _PdfChunk,
    field_map: FieldMap,
    glossary: str,
    system_prompt: str,
    model: str,
    user_instruction: str,
) -> dict[str, Any]:
    """Build the kwargs dict passed to ``client.messages.create``.

    Two cache_control breakpoints per ARCHITECTURE.md §6.3:
      BP1 = system block (system_prompt + glossary + examples)
      BP2 = first user text block (Field Map + notes_for_claude)

    NO ``tools`` parameter — output is a single JSON object the model
    writes as plain text. The system prompt embeds the literal expected
    schema (see ``assets/prompts/system_prompt.txt``).
    """
    system_text = _build_system_text(system_prompt=system_prompt, glossary=glossary)
    field_map_text = _serialize_field_map_for_prompt(field_map)

    pdf_b64 = base64.standard_b64encode(pdf_chunk.pdf_bytes).decode("ascii")

    # User instruction tagged with chunk page range when this is a split chunk.
    # A chunk is a "split chunk" when its basename differs from the source
    # basename (set by _read_pdf_chunks for any chunk it actually produced
    # via PdfWriter, never for the single-call passthrough).
    full_instruction = user_instruction
    if pdf_chunk.basename != pdf_chunk.source_basename:
        full_instruction = (
            f"{user_instruction}\n\nNote: this is pages {pdf_chunk.start_page}-{pdf_chunk.end_page} "
            f"of '{pdf_chunk.source_basename}'. Treat the PDF you are seeing as a "
            f"slice of that larger document and report `source_page` as the page "
            f"number within this slice."
        )

    # Two prompt-cache breakpoints. Anthropic only caches identical prefixes,
    # so we pin the stable system prompt + glossary as breakpoint 1 and the
    # Field Map block as breakpoint 2. After the first call in a session
    # both blocks come back as cache_read tokens (cheap), and only the PDF
    # bytes + user instruction are billed at full rate.
    request: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS_PER_CALL,
        "system": [
            {
                "type": "text",
                "text": system_text,
                "cache_control": {"type": "ephemeral"},  # Breakpoint 1
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": field_map_text,
                        "cache_control": {"type": "ephemeral"},  # Breakpoint 2
                    },
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": pdf_b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": full_instruction,
                    },
                ],
            }
        ],
    }

    _warn_if_breakpoint_below_min(system_text, label="system_prompt+glossary")
    _warn_if_breakpoint_below_min(field_map_text, label="field_map")

    return request


def _build_system_text(*, system_prompt: str, glossary: str) -> str:
    """Concatenate the stable system text block.

    Order is fixed for cache-key stability: system_prompt, then glossary.
    Few-shot examples may already be embedded in ``system_prompt`` by the
    Extractor; we don't enforce a specific layout here.
    """
    parts = [system_prompt.rstrip(), "", "=== GLOSSARY ===", glossary.rstrip()]
    return "\n".join(parts)


def _warn_if_breakpoint_below_min(text: str, *, label: str) -> None:
    """Heuristic cache-min check.

    Sonnet 4.6 needs cached prefixes >= 2,048 tokens. We can't tokenize
    locally, so we use a chars/token heuristic to flag obviously-tiny
    breakpoints. The actual token count is verified post-hoc via
    response.usage and logged as a cache_miss warning if both creation
    and read are zero.
    """
    approx_tokens = max(1, len(text) // _CHARS_PER_TOKEN_HEURISTIC)
    if approx_tokens < SONNET_CACHE_MIN_TOKENS:
        logger.warning(
            "claude.cache_breakpoint_undersized label=%s approx_tokens=%d threshold=%d",
            label,
            approx_tokens,
            SONNET_CACHE_MIN_TOKENS,
        )


# ----- API call with retry + cache verification + debug ----------------------


def _resolve_client(api_key: str | None) -> anthropic.Anthropic:
    """Build an Anthropic client. Caller-supplied key overrides secret store."""
    key = api_key or secret_store.get_anthropic_api_key()
    if not key:
        raise ClaudeAuthError(
            "No Anthropic API key found. Set ANTHROPIC_API_KEY env var or "
            "store via secret_store.set_anthropic_api_key()."
        )
    # Stash a fingerprint (last 4 chars) on the client so the round-trip
    # logger can record which key actually went out without exposing it.
    client = anthropic.Anthropic(api_key=key)
    try:
        # Defensive: client may be a MagicMock under tests; setattr is safe.
        client._iga_key_fingerprint = key[-4:] if len(key) >= 4 else "????"
    except Exception:  # pragma: no cover - defensive
        pass
    # SDK reads ANTHROPIC_API_KEY from env if api_key is None; we pass
    # explicitly so we honor the secret_store value.
    return client


def _api_key_fingerprint(client: Any) -> str:
    """Return the last-4 fingerprint stashed on the client, or '????'."""
    fp = getattr(client, "_iga_key_fingerprint", None)
    return fp if isinstance(fp, str) else "????"


def _classify_anthropic_error(exc: BaseException) -> ClaudeError:
    """Map an Anthropic SDK exception to our typed hierarchy."""
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ClaudeAuthError(str(exc))
    if isinstance(exc, anthropic.RateLimitError):
        return ClaudeRateLimitError(str(exc))
    # 5xx
    if isinstance(exc, anthropic.InternalServerError):
        return ClaudeServerError(str(exc))
    if isinstance(exc, anthropic.APIStatusError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int) and status >= 500:
            return ClaudeServerError(str(exc))
        if isinstance(status, int) and status in (401, 403):
            return ClaudeAuthError(str(exc))
        return ClaudeError(str(exc))
    if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
        return ClaudeServerError(str(exc))
    return ClaudeError(str(exc))


def _create_message_with_retry(
    client: anthropic.Anthropic,
    *,
    request: dict[str, Any],
    backoffs: Sequence[int] = RETRY_BACKOFF_SECONDS,
) -> Any:
    """Call ``client.messages.create`` with exponential backoff on transient errors.

    Retries on ``ClaudeRateLimitError`` (429) and ``ClaudeServerError`` (5xx /
    connection / timeout). Surfaces ``ClaudeAuthError`` and other 4xx
    immediately. ``time.sleep`` is read from the module on each call so
    tests can monkeypatch it.
    """
    last_classified: ClaudeError | None = None
    model_alias = request.get("model", "<unknown>")
    fingerprint = _api_key_fingerprint(client)
    # Total attempts = 1 + len(backoffs). With (1,2,4,8) → 4 retries, 5 attempts.
    for attempt in range(len(backoffs) + 1):
        # Outbound diagnostic log per ARCHITECTURE.md §6.5 + Amendment #17:
        # the operator (and a future fix-pass) should be able to confirm a
        # real Anthropic round-trip happened by reading the local app log.
        logger.info(
            "claude.call_outbound model=%s api_key_fingerprint=%s attempt=%d",
            model_alias,
            fingerprint,
            attempt + 1,
        )
        try:
            # Streaming is required by Anthropic for max_tokens > ~21K and
            # also avoids client-side timeouts on long responses. We just
            # want the final assembled Message — same shape as create().
            with client.messages.stream(**request) as stream:
                response = stream.get_final_message()
        except anthropic.AnthropicError as raw:
            classified = _classify_anthropic_error(raw)
            last_classified = classified
            # Only retry the transient classes: rate limits and 5xx/network.
            # Auth errors mean the API key is bad — retrying just delays the
            # operator-visible failure.
            if not isinstance(classified, ClaudeRateLimitError | ClaudeServerError):
                # Auth or other 4xx — surface immediately.
                raise classified from raw
            if attempt >= len(backoffs):
                raise classified from raw
            delay = backoffs[attempt]
            logger.warning(
                "claude.retry attempt=%d/%d delay=%ds error=%s",
                attempt + 1,
                len(backoffs) + 1,
                delay,
                classified.__class__.__name__,
            )
            time.sleep(delay)
            continue
        except httpx.HTTPError as raw:
            # Transport-level errors during streaming sometimes escape the
            # Anthropic SDK wrapper unwrapped — most often
            # httpx.RemoteProtocolError ("peer closed connection without
            # sending complete message body") on long responses. Treat these
            # as transient and retry just like 5xx/rate-limit errors.
            classified = ClaudeServerError(
                f"{type(raw).__name__}: {raw}"
            )
            last_classified = classified
            if attempt >= len(backoffs):
                raise classified from raw
            delay = backoffs[attempt]
            logger.warning(
                "claude.retry attempt=%d/%d delay=%ds error=%s detail=%s",
                attempt + 1,
                len(backoffs) + 1,
                delay,
                type(raw).__name__,
                str(raw)[:160],
            )
            time.sleep(delay)
            continue
        # Inbound diagnostic log: pairs with claude.call_outbound to verify
        # an actual Anthropic round-trip. Anthropic stamps `id` on the
        # response; if it's missing or oddly formatted, the call wasn't real.
        logger.info(
            "claude.call_returned model=%s response_id=%s stop_reason=%s",
            getattr(response, "model", model_alias),
            getattr(response, "id", "<missing>"),
            getattr(response, "stop_reason", "<missing>"),
        )
        return response
    # Defensive: loop should always either return or raise.
    raise last_classified or ClaudeError("messages.create exhausted retries")


# ----- Response parsing ------------------------------------------------------


_MARKDOWN_FENCE_OPEN_RE = re.compile(r"^\s*```(?:json)?\s*", re.IGNORECASE)
_MARKDOWN_FENCE_CLOSE_RE = re.compile(r"\s*```\s*$")


def _extract_text_from_response(response: Any) -> str:
    """Concatenate all text blocks in ``response.content``.

    Tool-use blocks (which we don't expect anymore) are ignored. Returns
    empty string if no text blocks are present so the caller can decide
    how to surface that as a parse error.
    """
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts).strip()


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Strip optional markdown fences and parse ``raw`` into a JSON dict.

    Mirrors v1's ``_parse_json_object`` helper. Accepts a single-element
    list as a courtesy (Claude occasionally wraps the object in a list when
    it gets confused by the "schema as example" pattern). Raises
    :class:`ClaudeParseError` on any failure so the caller can surface a
    typed error rather than a JSONDecodeError stack trace.
    """
    if not raw or not raw.strip():
        raise ClaudeParseError(
            "Claude returned an empty response — no JSON object to parse."
        )
    text = raw.strip()
    text = _MARKDOWN_FENCE_OPEN_RE.sub("", text, count=1)
    text = _MARKDOWN_FENCE_CLOSE_RE.sub("", text, count=1)
    text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        snippet = text[:200].replace("\n", " ")
        raise ClaudeParseError(
            f"Claude response was not valid JSON: {exc.msg} at line {exc.lineno} "
            f"col {exc.colno}. First 200 chars: {snippet!r}"
        ) from exc
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise ClaudeParseError(
            f"Expected JSON object, got {type(data).__name__}."
        )
    return data


def _records_from_parsed_json(
    parsed: Mapping[str, Any],
    *,
    pdf_chunk: _PdfChunk,
    model: str,
) -> list[ExtractedField]:
    """Walk a parsed JSON object and emit ``ExtractedField`` records.

    The expected shape is::

        {
          "fields": {
            "<domain_tag>": {
              "value": ..., "confidence": ..., "source_page": ...,
              "source_quote": ..., "needs_review": ...
            },
            ...
          },
          "repeatables": {
            "<group>": [
              { "<group>.<sub>": ..., ..., "_confidence": ..., ... },
              ...
            ],
            ...
          }
        }

    Any malformed entry is logged at WARNING and skipped — a single bad
    row should not nuke a 30-field response.
    """
    records: list[ExtractedField] = []

    fields = parsed.get("fields")
    if isinstance(fields, Mapping):
        for domain_tag, payload in fields.items():
            if not isinstance(domain_tag, str):
                logger.warning(
                    "claude.json_field_invalid_key key=%r (not a string)", domain_tag
                )
                continue
            if not isinstance(payload, Mapping):
                logger.warning(
                    "claude.json_field_invalid_payload tag=%s payload=%r",
                    domain_tag,
                    payload,
                )
                continue
            try:
                record = _record_from_field_entry(
                    domain_tag,
                    payload,
                    source_basename=pdf_chunk.source_basename,
                    model=model,
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "claude.json_field_invalid tag=%s error=%s payload=%r",
                    domain_tag,
                    exc,
                    payload,
                )
                continue
            records.append(record)

    repeatables = parsed.get("repeatables")
    if isinstance(repeatables, Mapping):
        for group_name, items in repeatables.items():
            if not isinstance(group_name, str):
                logger.warning(
                    "claude.json_repeatable_invalid_group group=%r (not a string)",
                    group_name,
                )
                continue
            if not isinstance(items, list):
                logger.warning(
                    "claude.json_repeatable_invalid_items group=%s items=%r",
                    group_name,
                    items,
                )
                continue
            for index, item in enumerate(items):
                if not isinstance(item, Mapping):
                    logger.warning(
                        "claude.json_repeatable_invalid_item group=%s index=%d item=%r",
                        group_name,
                        index,
                        item,
                    )
                    continue
                records.extend(
                    _records_from_repeatable_item(
                        group_name=group_name,
                        index=index,
                        item=item,
                        source_basename=pdf_chunk.source_basename,
                        model=model,
                    )
                )

    return records


def _record_from_field_entry(
    domain_tag: str,
    payload: Mapping[str, Any],
    *,
    source_basename: str,
    model: str,
) -> ExtractedField:
    """Build an ExtractedField for a single ``fields[<tag>]`` entry."""
    if "value" not in payload:
        raise KeyError("missing 'value'")
    if "confidence" not in payload:
        raise KeyError("missing 'confidence'")
    if "source_page" not in payload:
        raise KeyError("missing 'source_page'")
    return ExtractedField(
        domain_tag=str(domain_tag),
        value=payload.get("value"),
        source_doc=source_basename,
        source_page=int(payload["source_page"]),
        source_quote=str(payload.get("source_quote", "")),
        confidence=float(payload["confidence"]),
        needs_review=bool(payload.get("needs_review", False)),
        repeatable_group=None,
        repeatable_index=None,
        model_used=model,
    )


def _records_from_repeatable_item(
    *,
    group_name: str,
    index: int,
    item: Mapping[str, Any],
    source_basename: str,
    model: str,
) -> list[ExtractedField]:
    """Emit one ExtractedField per (sub_field) inside a repeatable item.

    Metadata keys (``_confidence``, ``_source_page``, ``_source_quote``,
    ``_needs_review``) are pulled off the item and applied uniformly to
    every emitted record. A sub-field with its own confidence/quote on a
    nested dict is also supported (see ``_split_record_or_use_metadata``).
    """
    # Pull item-level metadata.
    item_confidence_raw = item.get("_confidence")
    item_page_raw = item.get("_source_page")
    item_quote = str(item.get("_source_quote", ""))
    item_needs_review = bool(item.get("_needs_review", False))

    try:
        item_confidence = (
            float(item_confidence_raw) if item_confidence_raw is not None else None
        )
    except (TypeError, ValueError):
        logger.warning(
            "claude.json_repeatable_invalid_metadata group=%s index=%d "
            "_confidence=%r — skipping item",
            group_name,
            index,
            item_confidence_raw,
        )
        return []

    try:
        item_page = int(item_page_raw) if item_page_raw is not None else None
    except (TypeError, ValueError):
        logger.warning(
            "claude.json_repeatable_invalid_metadata group=%s index=%d "
            "_source_page=%r — skipping item",
            group_name,
            index,
            item_page_raw,
        )
        return []

    records: list[ExtractedField] = []
    for sub_key, sub_value in item.items():
        if not isinstance(sub_key, str):
            continue
        if sub_key.startswith("_"):
            # Item-level metadata, already consumed.
            continue
        # The convention is `<group>.<sub>` — but we accept bare `<sub>`
        # too and rewrite it to `<group>.<sub>` so prompts that drift from
        # the schema still produce usable records.
        if "." in sub_key:
            domain_tag = sub_key
        else:
            domain_tag = f"{group_name}.{sub_key}"

        # If a sub-field is itself a {value, confidence, ...} dict, honor
        # it; otherwise treat the value as the literal value and inherit
        # item-level metadata.
        if isinstance(sub_value, Mapping) and "value" in sub_value:
            value = sub_value.get("value")
            confidence_raw = sub_value.get("confidence", item_confidence)
            page_raw = sub_value.get("source_page", item_page)
            quote = str(sub_value.get("source_quote", item_quote))
            needs_review = bool(sub_value.get("needs_review", item_needs_review))
        else:
            value = sub_value
            confidence_raw = item_confidence
            page_raw = item_page
            quote = item_quote
            needs_review = item_needs_review

        if confidence_raw is None or page_raw is None:
            logger.warning(
                "claude.json_repeatable_field_missing_metadata group=%s index=%d "
                "tag=%s confidence=%r page=%r",
                group_name,
                index,
                domain_tag,
                confidence_raw,
                page_raw,
            )
            continue

        try:
            confidence = float(confidence_raw)
            page = int(page_raw)
        except (TypeError, ValueError):
            logger.warning(
                "claude.json_repeatable_field_invalid_metadata group=%s index=%d "
                "tag=%s confidence=%r page=%r",
                group_name,
                index,
                domain_tag,
                confidence_raw,
                page_raw,
            )
            continue

        records.append(
            ExtractedField(
                domain_tag=domain_tag,
                value=value,
                source_doc=source_basename,
                source_page=page,
                source_quote=quote,
                confidence=confidence,
                needs_review=needs_review,
                repeatable_group=group_name,
                repeatable_index=index,
                model_used=model,
            )
        )
    return records


# ----- Cache verification + debug artifact saving ----------------------------


def _log_cache_usage(
    response: Any,
    *,
    run_id: str,
    model: str,
    doc_basename: str,
    expected_cached: bool,
    cache_miss_reason_hint: str | None = None,
) -> None:
    """Log cache + token usage; warn on apparent cache miss.

    Per ARCHITECTURE.md §6.5 / RESEARCH.md Finding 5 / Amendment #4 + #17.
    """
    usage = getattr(response, "usage", None)
    cache_creation = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    logger.info(
        "claude.call run_id=%s model=%s doc=%s input=%d output=%d "
        "cache_creation=%d cache_read=%d",
        run_id,
        model,
        doc_basename,
        input_tokens,
        output_tokens,
        cache_creation,
        cache_read,
    )
    if expected_cached and cache_creation == 0 and cache_read == 0:
        reason = cache_miss_reason_hint or "unknown"
        logger.warning(
            "claude.cache_miss run_id=%s model=%s doc=%s reason=%s",
            run_id,
            model,
            doc_basename,
            reason,
        )


def _save_debug_artifacts(
    *,
    debug_dir: Path,
    doc_id: str,
    call_n: int,
    request: Mapping[str, Any],
    response: Any,
) -> None:
    """Write request and response JSON to the per-run debug directory.

    PDF base64 payloads are elided to a sha256 hash so the artifacts stay
    small. The original PDF stays at the source path.
    """
    debug_dir.mkdir(parents=True, exist_ok=True)
    safe_doc_id = doc_id.replace("/", "_").replace("\\", "_")
    req_path = debug_dir / f"{safe_doc_id}-call_{call_n}.req.json"
    resp_path = debug_dir / f"{safe_doc_id}-call_{call_n}.resp.json"

    req_serializable = _elide_pdf_base64(request)
    try:
        req_path.write_text(
            json.dumps(req_serializable, indent=2, default=str), encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - debug best-effort
        logger.warning("claude.debug_write_failed path=%s error=%s", req_path, exc)
        return

    resp_payload = _response_to_dict(response)
    try:
        resp_path.write_text(
            json.dumps(resp_payload, indent=2, default=str), encoding="utf-8"
        )
    except OSError as exc:  # pragma: no cover - debug best-effort
        logger.warning("claude.debug_write_failed path=%s error=%s", resp_path, exc)


def _elide_pdf_base64(request: Mapping[str, Any]) -> dict[str, Any]:
    """Replace PDF base64 with a hash placeholder for debug dumps."""
    cloned: dict[str, Any] = json.loads(json.dumps(request, default=str))
    for message in cloned.get("messages", []):
        for block in message.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "document":
                source = block.get("source") or {}
                data = source.get("data")
                if isinstance(data, str) and len(data) > 64:
                    digest = hashlib.sha256(data.encode("ascii")).hexdigest()
                    source["data"] = f"<base64 elided sha256={digest} bytes_b64={len(data)}>"
                    block["source"] = source
    return cloned


def _response_to_dict(response: Any) -> dict[str, Any]:
    """Best-effort coercion of a Message response to a serializable dict."""
    for attr in ("model_dump", "to_dict"):
        fn = getattr(response, attr, None)
        if callable(fn):
            try:
                return fn()
            except TypeError:
                try:
                    return fn(mode="python")
                except Exception:  # pragma: no cover
                    pass
    # Fallback: pull commonly-needed attributes by hand.
    return {
        "id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "stop_reason": getattr(response, "stop_reason", None),
        "content": [
            {
                "type": getattr(b, "type", None),
                "text": getattr(b, "text", None),
            }
            for b in getattr(response, "content", []) or []
        ],
        "usage": {
            "input_tokens": getattr(getattr(response, "usage", None), "input_tokens", None),
            "output_tokens": getattr(getattr(response, "usage", None), "output_tokens", None),
            "cache_creation_input_tokens": getattr(
                getattr(response, "usage", None), "cache_creation_input_tokens", None
            ),
            "cache_read_input_tokens": getattr(
                getattr(response, "usage", None), "cache_read_input_tokens", None
            ),
        },
    }


# ----- Per-chunk extraction --------------------------------------------------


@dataclass(slots=True, kw_only=True)
class _CallContext:
    """Runtime context shared across calls inside a single extract_from_pdf."""

    run_id: str
    debug_dir: Path | None
    client: anthropic.Anthropic
    field_map: FieldMap
    glossary: str
    system_prompt: str
    call_counter: dict[str, int] = field(default_factory=dict)
    expected_cached: bool = False  # flips to True after the first successful call

    def next_call_number(self, doc_id: str) -> int:
        n = self.call_counter.get(doc_id, 0) + 1
        self.call_counter[doc_id] = n
        return n


def _extract_one_chunk(
    chunk: _PdfChunk,
    *,
    ctx: _CallContext,
    model: str,
    user_instruction: str,
    cache_miss_reason_hint: str | None = None,
) -> tuple[list[ExtractedField], _CallUsage]:
    """Run extraction on a single chunk via a single JSON-mode API call.

    The Anthropic call has NO ``tools`` parameter; the system prompt
    embeds the literal expected JSON schema and Claude returns one big
    JSON object as text content. We parse it with :func:`_parse_json_object`
    and emit ExtractedField records via :func:`_records_from_parsed_json`.

    A parse error is surfaced as :class:`ClaudeParseError` so the caller
    can attach it to a per-doc error. Other typed Claude errors (auth,
    rate limit, server) propagate unchanged.
    """
    request = _build_request(
        pdf_chunk=chunk,
        field_map=ctx.field_map,
        glossary=ctx.glossary,
        system_prompt=ctx.system_prompt,
        model=model,
        user_instruction=user_instruction,
    )

    try:
        response = _create_message_with_retry(ctx.client, request=request)
    except ClaudeError:
        # Try to dump the request even on failure for debug forensics.
        if ctx.debug_dir is not None:
            try:
                call_n = ctx.next_call_number(chunk.basename)
                ctx.debug_dir.mkdir(parents=True, exist_ok=True)
                req_path = (
                    ctx.debug_dir / f"{chunk.basename}-call_{call_n}.req.json"
                )
                req_path.write_text(
                    json.dumps(_elide_pdf_base64(request), indent=2, default=str),
                    encoding="utf-8",
                )
            except OSError:  # pragma: no cover - best effort
                pass
        raise

    _log_cache_usage(
        response,
        run_id=ctx.run_id,
        model=model,
        doc_basename=chunk.basename,
        expected_cached=ctx.expected_cached,
        cache_miss_reason_hint=cache_miss_reason_hint,
    )

    if ctx.debug_dir is not None:
        call_n = ctx.next_call_number(chunk.basename)
        _save_debug_artifacts(
            debug_dir=ctx.debug_dir,
            doc_id=chunk.basename,
            call_n=call_n,
            request=request,
            response=response,
        )

    # After the first successful call the system+field_map prefix is
    # in the cache; subsequent calls are expected to hit it.
    ctx.expected_cached = True

    text = _extract_text_from_response(response)
    if not text:
        logger.warning(
            "claude.empty_response run_id=%s doc=%s — no text content",
            ctx.run_id,
            chunk.basename,
        )
        raise ClaudeParseError(
            f"Claude returned no text content for {chunk.basename}."
        )

    parsed = _parse_json_object(text)
    records = _records_from_parsed_json(parsed, pdf_chunk=chunk, model=model)

    logger.info(
        "claude.json_parsed run_id=%s doc=%s records=%d stop_reason=%s",
        ctx.run_id,
        chunk.basename,
        len(records),
        getattr(response, "stop_reason", "<unknown>"),
    )

    return records, _usage_from_response(response)


# ----- Merge across chunks ---------------------------------------------------


def _record_dedup_key(rec: ExtractedField) -> tuple[str, str | None, int | None]:
    return (rec.domain_tag, rec.repeatable_group, rec.repeatable_index)


def _merge_chunk_records(chunks: Iterable[Sequence[ExtractedField]]) -> list[ExtractedField]:
    """Union records across chunks; dedup by (domain_tag, group, index).

    On dedup conflicts, prefer the higher-confidence record (per
    ARCHITECTURE.md §6.6). The lower-confidence record is dropped here;
    extract.py is responsible for funneling true cross-document conflicts
    into ``state.fields[...].conflicts[]`` via ``merge_extraction``.
    """
    by_key: dict[tuple[str, str | None, int | None], ExtractedField] = {}
    for batch in chunks:
        for rec in batch:
            key = _record_dedup_key(rec)
            existing = by_key.get(key)
            if existing is None or rec.confidence > existing.confidence:
                by_key[key] = rec
    # Stable order: by domain_tag then index.
    return sorted(
        by_key.values(),
        key=lambda r: (r.domain_tag, r.repeatable_group or "", r.repeatable_index or 0),
    )


# ----- Public entry points ---------------------------------------------------


def extract_from_pdf(
    pdf_path: Path,
    field_map: FieldMap,
    glossary: str,
    system_prompt: str,
    *,
    force_opus: bool = False,
    run_id: str,
    debug_dir: Path | None = None,
    api_key: str | None = None,
) -> list[ExtractedField]:
    """Extract every field Claude can find from ``pdf_path``.

    Workflow:

    1. Preflight + (if needed) split at 80-page boundaries with 1-page overlap.
    2. First pass against Sonnet 4.6 (or Opus if ``force_opus``) per chunk.
       Each call returns a single JSON object that we parse into one
       ExtractedField per ``fields`` entry plus one ExtractedField per
       sub-field of every ``repeatables[group][index]``.
    3. Auto-escalation: any field with ``confidence < 0.7`` OR
       ``needs_review`` OR required-and-empty is re-prompted against
       Opus 4.7. The Opus result replaces the Sonnet record and is tagged
       ``model_used="claude-opus-4-7"`` for GUI badging.
    4. Cache + token usage is logged INFO on every call; cache miss is
       logged WARNING (never raised).

    ``debug_dir`` is the per-doc claude debug directory the caller wants
    artifacts under (typically ``<client>/debug/claude/<run_id>/``). When
    omitted, no artifacts are written.
    """
    if not pdf_path.is_absolute():
        pdf_path = pdf_path.resolve()

    client = _resolve_client(api_key)
    chunks = _read_pdf_chunks(pdf_path, debug_dir=debug_dir, run_id=run_id)

    ctx = _CallContext(
        run_id=run_id,
        debug_dir=debug_dir,
        client=client,
        field_map=field_map,
        glossary=glossary,
        system_prompt=system_prompt,
    )

    initial_model = DEFAULT_OPUS_MODEL if force_opus else DEFAULT_SONNET_MODEL
    user_instruction = (
        "Read the attached PDF and extract every relevant insurance field. "
        "Return ONE JSON object matching the schema in the system prompt: "
        "non-repeatable fields under `fields` keyed by canonical domain_tag; "
        "repeatable items (vehicle/driver/location/loss_payee/additional_insured/"
        "prior_carrier/loss) grouped under `repeatables`. Use the canonical "
        "domain_tag from the FIELD MAP block above. For each field include "
        "`value`, `confidence`, `source_page` (1-indexed), a verbatim "
        "`source_quote`, and `needs_review`. Return ONLY the JSON object — "
        "no prose, no markdown fences."
    )

    chunk_records: list[list[ExtractedField]] = []
    aggregate_usage = _CallUsage()
    for idx, chunk in enumerate(chunks):
        # First call of the run can't possibly hit the cache; subsequent calls can.
        chunk_recs, chunk_usage = _extract_one_chunk(
            chunk,
            ctx=ctx,
            model=initial_model,
            user_instruction=user_instruction,
            cache_miss_reason_hint=(
                "first_call_in_session" if idx == 0 else None
            ),
        )
        chunk_records.append(chunk_recs)
        aggregate_usage.add(chunk_usage)

    first_pass = _merge_chunk_records(chunk_records)

    if force_opus:
        # Already Opus; tag and return.
        return _attach_usage(
            [_with_model(rec, DEFAULT_OPUS_MODEL) for rec in first_pass],
            aggregate_usage,
        )

    if not ESCALATION_ENABLED:
        return _attach_usage(first_pass, aggregate_usage)

    # Two-pass escalation: Sonnet is fast and cheap, but Opus catches the
    # fields Sonnet flagged as low-confidence / missing-required. We only
    # rerun Opus on those specific fields — full re-extraction would burn
    # tokens with no payoff for fields Sonnet was already sure about.
    needs_escalation = _select_for_escalation(first_pass, field_map=field_map)
    if not needs_escalation:
        return _attach_usage(first_pass, aggregate_usage)

    logger.info(
        "claude.escalation run_id=%s doc=%s candidates=%d",
        run_id,
        pdf_path.name,
        len(needs_escalation),
    )

    # Second pass: Opus on un-resolved fields. We pass the original PDF
    # path (un-split — Opus accepts the same 32 MB / 80 pp limits, so we
    # split again if needed inside reextract_low_confidence_fields).
    opus_records, opus_usage = _reextract_against_opus(
        target_fields=needs_escalation,
        pdf_path=pdf_path,
        ctx=ctx,
    )
    aggregate_usage.add(opus_usage)

    merged = _merge_first_pass_with_opus(first_pass, opus_records)
    return _attach_usage(_drop_orphan_singletons(merged), aggregate_usage)


def reextract_low_confidence_fields(
    fields: list[ExtractedField],
    pdf_path: Path,
    field_map: FieldMap,
    system_prompt: str,
    *,
    run_id: str,
    debug_dir: Path | None = None,
    glossary: str = "",
    api_key: str | None = None,
) -> list[ExtractedField]:
    """Per-field re-prompt against Opus 4.7.

    Used by extract.py for fields the operator/extractor wants Claude to
    reconsider after the initial pass. Returns replacement records tagged
    ``model_used="claude-opus-4-7"``.
    """
    if not fields:
        return []

    if not pdf_path.is_absolute():
        pdf_path = pdf_path.resolve()

    client = _resolve_client(api_key)
    chunks = _read_pdf_chunks(pdf_path, debug_dir=debug_dir, run_id=run_id)

    ctx = _CallContext(
        run_id=run_id,
        debug_dir=debug_dir,
        client=client,
        field_map=field_map,
        glossary=glossary,
        system_prompt=system_prompt,
    )

    opus_records, opus_usage = _reextract_against_opus(
        target_fields=fields,
        pdf_path=pdf_path,
        ctx=ctx,
        prebuilt_chunks=chunks,
    )
    return _attach_usage(opus_records, opus_usage)


def _reextract_against_opus(
    *,
    target_fields: Sequence[ExtractedField],
    pdf_path: Path,
    ctx: _CallContext,
    prebuilt_chunks: Sequence[_PdfChunk] | None = None,
) -> tuple[list[ExtractedField], _CallUsage]:
    """Run Opus 4.7 on the document, asking only about the supplied fields.

    Returns ``(records, usage)`` so the caller can fold the Opus call's
    cache + token usage into the per-doc aggregate (Bug 7 fix).
    """
    chunks = (
        list(prebuilt_chunks)
        if prebuilt_chunks is not None
        else _read_pdf_chunks(pdf_path, debug_dir=ctx.debug_dir, run_id=ctx.run_id)
    )

    target_tags = sorted({f.domain_tag for f in target_fields})
    instruction = _opus_reextract_instruction(target_fields)

    chunk_records: list[list[ExtractedField]] = []
    aggregate_usage = _CallUsage()
    for chunk in chunks:
        recs, usage = _extract_one_chunk(
            chunk,
            ctx=ctx,
            model=DEFAULT_OPUS_MODEL,
            user_instruction=instruction,
            cache_miss_reason_hint=None,
        )
        aggregate_usage.add(usage)
        # Only keep records that match a requested tag.
        chunk_records.append([r for r in recs if r.domain_tag in set(target_tags)])

    merged = _merge_chunk_records(chunk_records)
    return [_with_model(r, DEFAULT_OPUS_MODEL) for r in merged], aggregate_usage


def _opus_reextract_instruction(target_fields: Sequence[ExtractedField]) -> str:
    """Build the Opus re-prompt body for a specific set of low-confidence fields."""
    bullets: list[str] = []
    for f in target_fields:
        prior_value = "(missing)" if f.value in (None, "") else repr(f.value)
        prior_quote = f.source_quote.strip() or "(no quote)"
        bullet = (
            f"- {f.domain_tag} (prior value={prior_value}, prior confidence={f.confidence:.2f}, "
            f"prior page={f.source_page}, prior quote={prior_quote!r})"
        )
        bullets.append(bullet)
    body = "\n".join(bullets)
    return (
        "These specific fields had low confidence on the first pass — please "
        "look at the attached PDF again and be precise. Return one JSON object "
        "matching the schema in the system prompt; the `fields` block must "
        "contain an entry for each tag listed below with your best `value` "
        "and a `confidence` between 0 and 1. If you genuinely cannot find a "
        "value, return `value: null` with `needs_review: true` and explain in "
        "the `source_quote`. Return ONLY the JSON object.\n\n"
        f"Fields to re-examine:\n{body}"
    )


# ----- Escalation gate -------------------------------------------------------


def _select_for_escalation(
    records: Sequence[ExtractedField],
    *,
    field_map: FieldMap,
) -> list[ExtractedField]:
    """Apply the ARCHITECTURE.md §6.4 escalation rules."""
    selected: list[ExtractedField] = []
    seen_keys: set[tuple[str, str | None, int | None]] = set()
    for rec in records:
        if _should_escalate(rec, field_map=field_map):
            key = _record_dedup_key(rec)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            selected.append(rec)
    return selected


def _should_escalate(rec: ExtractedField, *, field_map: FieldMap) -> bool:
    if rec.confidence < CONFIDENCE_LOW_THRESHOLD:
        return True
    if rec.needs_review:
        return True
    if _is_required_missing(rec, field_map=field_map):
        return True
    return False


def _is_required_missing(rec: ExtractedField, *, field_map: FieldMap) -> bool:
    if rec.value not in (None, ""):
        return False
    entry = _safe_lookup_by_domain_tag(field_map, rec.domain_tag)
    if entry is None:
        return False
    return bool(getattr(entry, "is_required", False))


def _safe_lookup_by_domain_tag(field_map: FieldMap, domain_tag: str) -> Any | None:
    try:
        return field_map.lookup_by_domain_tag(domain_tag)
    except Exception:  # pragma: no cover - defensive
        return None


def _merge_first_pass_with_opus(
    sonnet: Sequence[ExtractedField],
    opus: Sequence[ExtractedField],
) -> list[ExtractedField]:
    """Replace Sonnet records with Opus records on matching dedup keys.

    Sonnet records that weren't escalated keep their ``model_used`` value.
    Opus records are tagged ``model_used="claude-opus-4-7"`` (already done
    upstream). Records present only in Opus output (e.g., a field Sonnet
    missed entirely but Opus found) are appended.
    """
    by_key: dict[tuple[str, str | None, int | None], ExtractedField] = {
        _record_dedup_key(r): r for r in sonnet
    }
    for r in opus:
        key = _record_dedup_key(r)
        existing = by_key.get(key)
        if existing is None or r.confidence > existing.confidence:
            by_key[key] = r
    return sorted(
        by_key.values(),
        key=lambda r: (r.domain_tag, r.repeatable_group or "", r.repeatable_index or 0),
    )


def _drop_orphan_singletons(records: list[ExtractedField]) -> list[ExtractedField]:
    """Remove singleton records whose domain_tag also appears as a repeatable.

    When Opus re-extracts escalated fields it sometimes loses the
    repeatable_group context, emitting vehicle/driver/etc. tags as bare
    singletons. Those would duplicate the repeatable data already present
    from the Sonnet pass. Drop any record where repeatable_group is None
    but the same domain_tag is carried by at least one repeatable record.
    """
    repeatable_tags: set[str] = {r.domain_tag for r in records if r.repeatable_group is not None}
    return [r for r in records if not (r.repeatable_group is None and r.domain_tag in repeatable_tags)]


def _with_model(rec: ExtractedField, model: str) -> ExtractedField:
    """Return a copy of ``rec`` with ``model_used`` set to ``model``."""
    return dataclasses.replace(rec, model_used=model)


def _attach_usage(
    records: Sequence[ExtractedField],
    usage: _CallUsage,
) -> _RecordsList:
    """Wrap ``records`` in a :class:`_RecordsList` with ``cache_usage`` set.

    The extraction-agent reads this attribute via
    ``getattr(records, "cache_usage", None)`` (see ``extract.py``
    line ~820). Returning a list subclass keeps the public contract
    (a list of ExtractedField) backward-compatible.
    """
    out = _RecordsList(records)
    out.cache_usage = usage
    return out

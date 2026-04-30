# STATUS — claude-client-agent

**Status:** ✅ DONE
**Date:** 2026-04-30
**Owner:** claude-client-agent
**Scope:** `src/iga_marketing_master_2/claude_client.py` + `tests/test_claude_client.py` only.

---

## Files written

| File | Purpose |
|---|---|
| `src/iga_marketing_master_2/claude_client.py` | Anthropic SDK wrapper. Sole owner of all Claude API interaction. |
| `tests/test_claude_client.py` | 30 tests, all passing. SDK fully mocked. |
| `DECISION-MAP-claude-client-agent.md` | Mermaid flow diagrams + SDK verification notes. |
| `STATUS_claude-client-agent.md` | This file. |

No other files modified.

---

## Public surface (matches ARCHITECTURE.md §6.1)

```python
@dataclass(slots=True, kw_only=True)
class ExtractedField: ...

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
) -> list[ExtractedField]: ...

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
) -> list[ExtractedField]: ...

def build_record_field_tool_schema(field_map: FieldMap) -> dict[str, Any]: ...
```

`api_key` is an additive optional parameter; defaults to `secret_store.get_anthropic_api_key()`. Architecture-required signatures are preserved.

---

## Constants (Architecture Appendix A)

```python
CONFIDENCE_LOW_THRESHOLD   = 0.7
MAX_PAGES_PER_CALL         = 80
MAX_BYTES_PER_CALL         = 32 * 1024 * 1024   # 32 MB
PAGE_OVERLAP               = 1
DEFAULT_SONNET_MODEL       = "claude-sonnet-4-6"
DEFAULT_OPUS_MODEL         = "claude-opus-4-7"
RETRY_BACKOFF_SECONDS      = (1, 2, 4, 8)
MAX_TOKENS_PER_CALL        = 16_384
SONNET_CACHE_MIN_TOKENS    = 2_048
```

---

## Exception hierarchy (Architecture §6.8)

- `ClaudeError` (base)
  - `ClaudeAuthError` — 401/403, surfaced immediately
  - `ClaudeRateLimitError` — 429, retried with `(1, 2, 4, 8)` backoff
  - `ClaudeServerError` — 5xx / connection / timeout, retried
  - `ClaudePDFTooLargeError` — caught & resolved internally via split
  - `ClaudeCacheMissError` — never raised; reserved for symmetry. Cache miss is a WARNING.

Mapping from Anthropic SDK exceptions:
- `AuthenticationError`, `PermissionDeniedError` → `ClaudeAuthError`
- `RateLimitError` → `ClaudeRateLimitError`
- `InternalServerError`, 5xx `APIStatusError`, `APIConnectionError`, `APITimeoutError` → `ClaudeServerError`
- All others → `ClaudeError`

---

## Open item from ARCHITECTURE.md §14 — resolved

**#2 — Anthropic SDK exact pin + response shape verification:**
- Pinned `anthropic>=0.42` (post-Sonnet-4.6 SDK) in module-level requirements comment.
- Verified locally against `anthropic==0.97.0`:
  - `Message.usage.cache_creation_input_tokens` exists.
  - `Message.usage.cache_read_input_tokens` exists.
- Field names match ARCHITECTURE.md §6.5 exactly. **No rename required.**
- Documented in `DECISION-MAP-claude-client-agent.md`.

---

## Architecture compliance checklist

- [x] Two cache breakpoints: BP1 = system block, BP2 = first user text block. Both use explicit `cache_control: {"type": "ephemeral"}` (not auto top-level mode).
- [x] Tool use with single tool `record_extracted_field`. Schema regenerated per call from `field_map.generate_domain_tag_enum()`. Enum omitted gracefully when Field Map has no populated tags.
- [x] PDF document blocks via base64 source.
- [x] Pagecount + size preflight via `pypdf.PdfReader`.
- [x] 80-page split with 1-page overlap when oversized (`> 80 pages` OR `> 32 MB`).
- [x] Per-field auto-escalation: `confidence < 0.7` OR `needs_review=True` OR `is_required and value in (None, "")`.
- [x] Opus re-prompt records tagged `model_used="claude-opus-4-7"` for GUI badging.
- [x] `force_opus=True` mode runs Opus on first pass; tags every record.
- [x] Cache hit verification: `usage.cache_creation_input_tokens` + `usage.cache_read_input_tokens` logged INFO on every call. WARNING `claude.cache_miss` when both are zero on a call where caching was expected (i.e., not the first call in a session).
- [x] Retry with exponential backoff (1s, 2s, 4s, 8s = 4 retries / 5 attempts max) on `RateLimitError` and `5xx`. Auth/4xx surfaced immediately.
- [x] `--debug` artifacts: when `debug_dir` is set, writes `<doc_id>-call_<n>.req.json` and `.resp.json`. PDF base64 elided to sha256 hash to keep artifacts small.
- [x] Split chunks written under `<client>/debug/split/<run_id>/` when `debug_dir` is provided.
- [x] Sonnet 4.6 cache 2,048-token minimum (Amendment #17): heuristic warn at request build time; verified post-hoc via cache_miss log.
- [x] Dependency graph: imports `field_map`, `secret_store` only. **No** imports from `gui`, `enter`, `epic_session`, `extract`, `state`.
- [x] Type hints on every public symbol; Python 3.13 syntax (`X | None`, `list[T]`).
- [x] No real API calls in tests; SDK fully mocked.
- [x] No hard-coded API keys.

---

## Tests (30 total, all passing)

```
test_tool_schema_uses_field_map_enum
test_tool_schema_omits_enum_when_field_map_empty
test_cache_breakpoints_placed_in_request
test_cache_breakpoints_clear_2048_token_minimum
test_split_ranges_for_200_page_pdf                      [80+80+40, 1-page overlap]
test_split_ranges_for_exact_80_page_pdf
test_split_ranges_for_81_page_pdf
test_extract_splits_200_page_pdf_into_three_calls
test_split_calls_use_original_basename_in_extracted_records
test_split_writes_chunks_to_debug_split_dir
test_low_confidence_triggers_opus_reextract             [conf=0.5 → Opus]
test_needs_review_triggers_escalation
test_required_missing_triggers_escalation
test_no_escalation_when_all_records_confident
test_force_opus_skips_sonnet_first_pass
test_cache_miss_warning_logged_when_both_cache_tokens_zero
test_no_cache_miss_warning_on_first_call
test_cache_usage_logged_at_info
test_retry_on_rate_limit_then_succeeds
test_retry_on_5xx_then_succeeds
test_auth_error_not_retried
test_rate_limit_exhausts_retries_then_raises
test_debug_artifacts_written_when_debug_dir_set
test_no_debug_artifacts_when_debug_dir_none
test_reextract_low_confidence_fields_runs_opus
test_reextract_with_empty_targets_skips_api
test_reextract_filters_to_requested_tags
test_extract_from_pdf_raises_auth_error_without_key
test_invalid_tool_input_is_skipped_not_raised
test_unknown_tool_name_warned_and_skipped
```

`pytest tests/test_claude_client.py` → **30 passed in 0.94s**.

---

## Notes for downstream agents

- **extract-agent:** When you call `extract_from_pdf`, pass `debug_dir=<client>/debug/claude/<run_id>/` only when `Settings.debug == True`. The 5-run-id auto-prune lives in your module per ARCHITECTURE.md §6.7 / §10. We honor the directory path you give us; we don't manage retention.
- **field-map-agent:** Two protocol methods are required: `generate_domain_tag_enum() -> list[str]` and `lookup_by_domain_tag(domain_tag) -> entry | None` (entry must expose `.is_required: bool`). An optional `iter_notes_for_claude()` method (yielding `(tag, note)` tuples) is honored if present and added to the cached Field Map prompt block.
- **gui-agent:** Records produced by Opus re-prompt have `model_used="claude-opus-4-7"`. Use that to badge fields in the per-section table.
- **JIT-aware caching:** When the Field Map gets a new `domain_tag` between calls (per §4.5), the next call's BP2 (Field Map block) cache hash changes and you'll see `cache_creation_input_tokens > 0, cache_read_input_tokens == 0`. That's expected. Mass JIT churn can be correlated with cost via the INFO logs.

---

## Out of scope (explicitly)

- The Field Map JIT enrichment flow itself — that lives in `field_map.py` and `extract.py`.
- Per-client log routing, `runs.log` writes — that's `logger.py` / `extract.py`.
- The 5-run-id auto-prune of debug directories — caller's responsibility.
- The `prompt_for_anthropic_api_key_via_gui` function — that's `secret_store.py` / GUI agent.
- Merging records into `state.json` — that's `state.merge_extraction`.

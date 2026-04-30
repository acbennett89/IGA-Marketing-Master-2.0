"""claude_client.py — Anthropic SDK wrapper.

Owns all Claude API interaction:
- Default model: Sonnet 4.6. Auto-escalation to Opus 4.7 per-field on
  `confidence < 0.7` OR `needs_review` OR required-field-missing.
- Tool use (single `record_extracted_field` tool with `domain_tag` enum
  regenerated per call from the live Field Map). Not JSON mode.
- Two prompt-cache breakpoints: stable system+glossary+examples, then
  Field Map.
- PDF document blocks with pagecount/size preflight (handled in `extract.py`).
- Exponential-backoff retry on rate limit / outage; surfaces operator-readable
  errors to the GUI.

Cache verification (Amendment #17): logs `cache_creation_input_tokens` and
`cache_read_input_tokens` from every response. Warns if both are zero on a
request that should have cached. Field Map churn is documented as a known
cause of cache misses (Amendment #4).
"""

# TODO: implementation pending

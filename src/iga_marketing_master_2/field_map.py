"""field_map.py — load, query, and JIT-enrich the EPIC Field Map.

Reads `Library/Epic Field Map.json` (the authoritative ~1,631-entry field
universe). Provides lookups by `domain_tag`, `screen_code`, etc., and
generates the `domain_tag` enum used in the Claude tool-use schema.

JIT enrichment writes new metadata back to the JSON file via plain atomic
replace (no `version` field, no compare-and-swap — single-user only per
Path B / Amendment #11).

Schema fields managed: is_required, is_repeatable, repeatable_group,
parent_field, enum_values, validation_pattern, depends_on, default_value,
domain_tag, aliases, last_verified_at, epic_build_version, notes_for_claude.
"""

# TODO: implementation pending

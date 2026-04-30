"""extract.py — multi-PDF extraction loop into state.json.

Pipeline:
1. Pre-flight per PDF: `pypdf` page count + file size check against Anthropic
   limits.
2. If a PDF exceeds limits: split at page boundaries into 80-page chunks with
   1-page overlap (Amendment #3). Run a separate Claude extraction call per
   chunk; merge results by `domain_tag` using the same conflict-detection
   logic used for multi-doc merging.
3. For each (chunk or whole-PDF) call: hand off to `claude_client.py`,
   collect tool-use outputs, merge into the in-memory state.
4. JIT enrichment: when Claude proposes a new `domain_tag`, queue it for
   GUI confirmation; on confirm, write back to Field Map.
5. Persist final merged state via `state.py` (atomic write + daily snapshot).

Logs split decisions to `runs.log` and `debug/` so operators can see what
happened.
"""

# TODO: implementation pending

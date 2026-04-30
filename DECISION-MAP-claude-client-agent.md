# DECISION-MAP — claude-client-agent

**Scope:** `src/iga_marketing_master_2/claude_client.py`
**Owner:** claude-client-agent
**Status:** Implementing
**SDK pin:** `anthropic>=0.42` (confirmed against installed 0.97.0 — `usage.cache_creation_input_tokens` and `usage.cache_read_input_tokens` exist as documented; no rename needed)

---

## Top-level flow: `extract_from_pdf`

```mermaid
flowchart TD
    A[extract_from_pdf called] --> B[Preflight: pypdf.PdfReader]
    B --> C{page_count > 80<br/>OR size > 32 MB?}
    C -- No --> D[Single-call path]
    C -- Yes --> E[Split into 80-page chunks<br/>with 1-page overlap]
    E --> F[For each chunk:<br/>write tmp PDF under<br/>debug/split/run_id/]
    F --> G[Run extract on chunk<br/>same path as single-call]
    D --> H[Build tool schema<br/>field_map.generate_domain_tag_enum]
    G --> H
    H --> I[Build messages with<br/>2 cache_control breakpoints]
    I --> J{force_opus?}
    J -- Yes --> K[Call Opus 4.7 directly]
    J -- No --> L[Call Sonnet 4.6]
    L --> M[Parse tool_use blocks<br/>into ExtractedField records]
    M --> N[Log usage:<br/>cache_creation/cache_read]
    N --> O{Both cache<br/>tokens == 0?}
    O -- Yes --> P[WARN: claude.cache_miss]
    O -- No --> Q[INFO: claude.call]
    P --> R[Identify escalation<br/>candidates]
    Q --> R
    R --> S{any field<br/>conf < 0.7 OR<br/>needs_review OR<br/>required-missing?}
    S -- No --> T[Return Sonnet records]
    S -- Yes --> U[reextract_low_confidence_fields<br/>against Opus 4.7]
    U --> V[Replace records<br/>set model_used=opus-4-7]
    V --> W[Merge: union dedup<br/>by domain_tag, group, index<br/>prefer higher confidence]
    K --> M
    T --> X[Return list ExtractedField]
    W --> X
```

---

## Cache breakpoint placement

Per ARCHITECTURE.md §6.3:

```mermaid
flowchart LR
    SYS["system block:<br/>system_prompt + glossary + examples<br/>cache_control: ephemeral [BP1]"]
    U1["user block 1 (text):<br/>Field Map JSON + notes_for_claude<br/>cache_control: ephemeral [BP2]"]
    U2["user block 2 (document):<br/>PDF base64"]
    U3["user block 3 (text):<br/>per-call instructions"]
    SYS --> U1
    U1 --> U2
    U2 --> U3
```

Both BP1 (5–15K tokens expected) and BP2 (50–80K tokens expected) clear Sonnet 4.6's 2,048-token cache minimum. Verified by tests with explicit token-count fixtures.

---

## Tool schema build

```mermaid
flowchart TD
    A[build_tool_schema field_map] --> B[Call field_map.generate_domain_tag_enum]
    B --> C{enum non-empty?}
    C -- Yes --> D[Add enum constraint<br/>to domain_tag property]
    C -- No --> E[Free-text domain_tag<br/>JIT will catch]
    D --> F[Tool: record_extracted_field<br/>required: domain_tag, value,<br/>source_doc, source_page,<br/>source_quote, confidence]
    E --> F
```

---

## Auto-escalation gate

```mermaid
flowchart TD
    A[For each ExtractedField from Sonnet] --> B{confidence < 0.7?}
    B -- Yes --> X[Add to escalation list]
    B -- No --> C{needs_review == True?}
    C -- Yes --> X
    C -- No --> D{is_required AND value empty?}
    D -- Yes --> X
    D -- No --> Y[Keep as-is]
    X --> Z[reextract_low_confidence_fields]
    Z --> ZZ[Replace records<br/>tag model_used=opus-4-7]
```

---

## Cache verification

```mermaid
flowchart TD
    A[response.usage] --> B[cache_creation = usage.cache_creation_input_tokens]
    A --> C[cache_read = usage.cache_read_input_tokens]
    B --> D{both == 0 AND<br/>expected_cached?}
    C --> D
    D -- Yes --> E[logger.warning claude.cache_miss<br/>with reason hint]
    D -- No --> F[logger.info claude.call]
```

---

## Retry with backoff

```mermaid
flowchart TD
    A[messages.create] --> B{result?}
    B -- success --> C[return]
    B -- RateLimitError --> D[backoff 1s, 2s, 4s, 8s]
    B -- 5xx / InternalServerError --> D
    B -- AuthError 401/403 --> E[raise ClaudeAuthError immediately]
    B -- other 4xx --> F[raise ClaudeError immediately]
    D --> G{attempt < 4?}
    G -- Yes --> A
    G -- No --> H[raise final error]
```

---

## --debug artifact saving

When `debug_dir` is provided:

```
<debug_dir>/<doc_id>-call_<n>.req.json   # full request body, PDF base64 elided to sha256
<debug_dir>/<doc_id>-call_<n>.resp.json  # full response JSON including usage block
```

Caller (extract.py) is responsible for constructing `debug_dir` as `<client>/debug/claude/<run_id>/`. The 5-run-id auto-prune lives in extract.py per ARCHITECTURE.md §6.7 / §10.

---

## SDK verification (open item §14 #2)

- Pinned: `anthropic>=0.42` (note in module-level comment — minimum version that supports Sonnet 4.6 + post-Sonnet-4.6 tool/cache APIs).
- Verified locally against `anthropic==0.97.0`:
  - `Message.usage` exists. `usage.cache_creation_input_tokens` and `usage.cache_read_input_tokens` exist on the `Usage` model. Field names match ARCHITECTURE.md §6.5 exactly — no rename required.
  - Error hierarchy: `RateLimitError`, `InternalServerError`, `AuthenticationError`, `PermissionDeniedError`, `BadRequestError`, all subclasses of `APIStatusError → APIError → AnthropicError`.
  - Mapping to module exceptions:
    - `AuthenticationError`, `PermissionDeniedError` → `ClaudeAuthError`
    - `RateLimitError` → `ClaudeRateLimitError` (retryable)
    - `InternalServerError` and other 5xx (`status_code >= 500`) → `ClaudeServerError` (retryable)
    - Other `APIStatusError` → `ClaudeError` (non-retryable, surface immediately)

No deviations from ARCHITECTURE.md required.

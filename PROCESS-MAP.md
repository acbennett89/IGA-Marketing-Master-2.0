# Process Map — IGA Marketing Master 2.0

End-to-end visual flow from "user has PDFs on the desk" to "data is in EPIC." See [PLAN.md](PLAN.md) for the full plan.

```mermaid
flowchart TD
    A([User: drag/drop 1-N PDFs into GUI for client X]) --> B[GUI loads/creates Working Library/X/state.json]
    B --> C[extract.py sends each PDF to Claude API with cached system prompt + Field Map]
    C --> D[Claude returns tool_use calls keyed by domain_tag]
    D --> D1{Any field confidence below 0.7?}
    D1 -- Yes --> D2[Re-prompt those fields against Opus 4.7] --> E
    D1 -- No --> E[Merge into state.json: dedupe, surface conflicts, write history]
    E --> F[GUI review pane: per-section tables + PDF preview + audit log]
    F --> G{User approves/edits/locks fields}
    G --> H([User navigates EPIC inside our Playwright browser to entry page])
    H --> I[User clicks Begin Entry]
    I --> J[enter.py walks state.json approved fields]
    J --> K{Field tagged with domain_tag in Field Map?}
    K -- No --> K1[Log un-tagged field, ask Claude to propose tag, GUI confirms, write back to Field Map] --> J
    K -- Yes --> L[Resolve EPIC selector via Field Map → Playwright locator]
    L --> M{Selector resolves?}
    M -- No --> M1[Try label fallback page.get_by_label] --> M2{Fallback works?}
    M2 -- Yes --> M3[Auto-update Field Map under --debug; warn] --> N
    M2 -- No --> P[Pause: surface to user via GUI modal]
    M -- Yes --> N[Fill field, await EPIC validation]
    N --> O{EPIC accepts?}
    O -- Yes --> J
    O -- No --> P[Pause: write context to state.json, modal in GUI]
    P --> Q[User fixes manually in EPIC, clicks Resume]
    Q --> R[Re-read DOM via locator.input_value as ground truth]
    R --> J
    J --> S{All approved fields entered?}
    S -- No --> J
    S -- Yes --> T([Run complete: state.json updated to status=entered, runs.log appended])
```

## Major decision points

| Branch | Trigger | Outcome |
|---|---|---|
| `D1` | Any extracted field confidence `< 0.7` (or `needs_review` flag, or required-field missing) | Re-prompt only those fields against Opus 4.7; merge results back |
| `K` | Field encountered during entry has no `domain_tag` in Field Map | JIT propose-and-confirm via Claude + GUI; write back to Field Map |
| `M` / `M2` | EPIC selector doesn't resolve | Try `data-automation-id` → `name` attribute → label fallback; pause if all fail |
| `O` | EPIC rejects entered value (validation error) | Pause for human; on resume, accept post-fix DOM value as ground truth |

## Living document

This map updates whenever `/plan-review` or `/build` discovers new branches (e.g., new failure modes in EPIC, new auto-escalation triggers).

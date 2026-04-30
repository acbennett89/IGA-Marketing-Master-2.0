# Research Findings: IGA Marketing Master 2.0 (Path B)
**Based on:** PLAN-REVIEW.md (Path B chosen 2026-04-30)
**Researched:** 2026-04-30
**Status:** ✅ COMPLETE — Decision A chosen 2026-04-30. Finding 1 (REST API) explicitly rejected by user ("We will not be implementing any API usage"). Findings 2, 3, 4, 5 adopted into PLAN-REVIEW.md as amendments #14-17.

---

## Summary

One finding is genuinely architecture-changing: **Applied EPIC ships a REST API via the Applied Dev Center**, which — if your agency has access — could replace the entire Playwright entry layer with HTTP calls. This would eliminate selector rot, pause-for-human flows, and the OperatorModal class as the dominant maintenance risk. It is the single most important question to resolve before /build.

The remaining findings are minor stack refinements (`keyring` over raw `pywin32` for the secret store, `platformdirs` over `appdirs` for path discovery, plus some Playwright Windows quirks worth knowing). The Anthropic SDK approach in the plan is confirmed current best practice.

No existing end-to-end SaaS adequately solves the documents → review → EPIC pipeline; AI document extraction tools (V7 Go, Extend, Box Extract, Algodocs) handle the extraction half but stop short of EPIC integration, and routing PII through a second SaaS vendor is a worse trust posture than your already-approved direct Anthropic API path.

---

## Stack Assessment

| Component | Verdict | Notes |
|---|---|---|
| Python 3.13 | ✅ Best fit | Standard. No findings. |
| Anthropic SDK (Sonnet 4.6 + caching + tool use + PDFs) | ✅ Best fit | The plan's approach (explicit cache breakpoints, tool use over JSON mode, per-call enum regen, PDF document blocks) matches current 2026 best practice. One caveat below. |
| Playwright (`launch_persistent_context`) | ⚠️ Confirmed with caveats | Real Windows-specific bugs in recent versions; needs version pinning + explicit lock cleanup. Detail in Finding 4. |
| PySide6 + QtPdf 6.6+ | ✅ Best fit | Standard documented APIs. One file-handle lifecycle gotcha noted in Qt forums; not blocking. |
| pywin32 DPAPI for secret store | ⚠️ Better option exists | `keyring` library wraps the same Windows Credential Manager (DPAPI under the hood) with a simpler API. See Finding 2. |
| Working Library path discovery | ⚠️ Better option exists | `appdirs` is deprecated; `platformdirs` is the actively-maintained successor. See Finding 3. |
| **Browser automation as the EPIC entry layer** | 🔴 Significant alternative | Applied EPIC has a REST API. May eliminate the Playwright layer entirely. **See Finding 1 — read this before /build.** |

---

## Bonafide Findings

### Finding 1: Applied Dev Center REST API for Applied EPIC
**Type:** Approach Change
**Replaces:** The entire Playwright entry layer (`enter.py`, `epic_session.py`, selector fallback chain, pause-for-human flow, OperatorModal for entry errors, pre-flight selector smoke test, label-fallback selector strategy, EPIC-related risks #1 and #6 in the plan).

**Description**

Applied Systems publishes a developer portal at [devcenter.myappliedproducts.com](https://devcenter.myappliedproducts.com/docs/overview) with RESTful APIs against Applied EPIC for client, policy, and related data. Third-party integrators (e.g., Canopy Connect, WinSurTech) build commercial Applied EPIC integrations via this API. JavaScript/Python tutorials exist for "get clients" and "get policies" workflows.

**SWOT Analysis**

| | |
|---|---|
| **Strengths** | Eliminates selector rot (the #1 plan risk). Eliminates pause-for-human flow. Headless-capable from day 1 (your v1.5 wishlist gets cheaper). Errors come back as structured HTTP responses, not "EPIC didn't accept this" mystery. Far simpler operator UX (no browser, no manual navigation). Removes Playwright as a dependency. |
| **Weaknesses** | Requires Applied Dev Center account + likely a partner/integration agreement; possibly a fee tier. May not cover every field/screen the UI exposes — custom fields, forms, marketing-submission workflows might be UI-only. Auth and rate limits unknown until you have credentials. Existing 1,631-field scraped Field Map is partially obsolete (selectors don't apply to API; field metadata may still). |
| **Opportunities** | Headless mode comes nearly for free in v1.5. Multi-user works without lock files (the API is the source of truth, not a shared file). EPIC version drift becomes Applied's problem, not yours. Easier to test (mock HTTP, not browser). |
| **Threats** | If your agency *doesn't* have dev center access, sourcing it may take weeks of partner onboarding. If the API doesn't cover marketing submissions specifically (your domain), you'd end up with a hybrid — API for what's supported, Playwright for the rest, which is worse than either pure choice. Rate limits could throttle large submissions. Pricing/terms unknown. |

**Non-Technical Operator Impact**

Materially better. Browser automation is brittle in operator-visible ways (selector breaks → pause → "what do I do?"). REST API failures are structured (e.g., HTTP 422 with field-level error JSON) and translate to operator-readable messages naturally. No "user navigates to entry page first" required — operator picks "Push to EPIC" and it just works.

**Effort Delta**

Net **reduction** if API coverage is adequate. You replace `enter.py` (Playwright + selector strategies + pause modal + DOM-as-truth resume + smoke test) with `epic_api_client.py` (HTTP requests + retry + error mapping). Probably 30-40% less entry-layer code. Field Map enrichment gets simpler too (no selector tracking).

Net **increase** if coverage is partial — hybrid implementations are worse than either pure approach.

**Recommendation:** **Consider — investigate access feasibility before /build.**

Concrete next steps before locking architecture:

1. **Check whether IGA already has Applied Dev Center access.** If your agency has a dev portal account, log in and review available endpoints.
2. **If no access yet:** contact Applied Systems sales/partner team to ask about API access for an internal integration. Get a yes/no within a week if possible.
3. **If access is plausible:** spend a half-day mapping your Field Map's `domain_tag` set to the API's resource model. Identify coverage gaps. If gaps are minor or in out-of-scope-v1 territory, pivot to API-first. If gaps are large (e.g., marketing submission detail screens aren't exposed), stay with Playwright.
4. **If access is blocked or pricing is prohibitive:** stay with Playwright as planned. The plan's mitigations (label fallback + pre-flight smoke + Field Map versioning) are real and address the rot risk credibly.

This decision is significant enough that I recommend pausing /build until it's resolved, even if the answer is "we can't get API access" — you'll want that confirmed, not assumed.

---

### Finding 2: `keyring` library over raw `pywin32` DPAPI
**Type:** Alternative Tool
**Replaces:** Amendment #1 in PLAN-REVIEW (DPAPI-encrypted secret store via `pywin32`).

**Description**

The Python [`keyring`](https://pypi.org/project/keyring/) library wraps Windows Credential Manager (which is DPAPI under the hood) with a 3-line API: `keyring.set_password("IGA Marketing Master", "anthropic_api_key", value)` and `keyring.get_password(...)`. The Windows backend uses `pywin32-ctypes` with `pywin32` fallback. Same security model as raw DPAPI; far simpler code.

**SWOT Analysis**

| | |
|---|---|
| **Strengths** | 3 lines of code instead of ~30 for the same security. Active maintenance (jaraco). Same DPAPI guarantees (per-user encryption). Forward-compatible with macOS/Linux if you ever cross-platform. |
| **Weaknesses** | Adds a dependency (vs. pywin32 which the plan already includes). Behavior on shared/migrated profiles is identical to raw DPAPI — both can fail to decrypt if the user profile changes. |
| **Opportunities** | Operator credential management ("change my API key") becomes trivial; no custom UI code. |
| **Threats** | None material. The library is the de-facto Python standard for credential storage. |

**Non-Technical Operator Impact**

Identical to raw DPAPI from the operator's perspective. Better for the maintainer.

**Effort Delta**

**Reduction** — ~20-30 lines of secret-store code that don't need to be written, debugged, or maintained.

**Recommendation:** **Adopt.** Replace amendment #1's `pywin32 win32crypt` direct usage with `keyring` library. `pywin32` is still needed transitively (`keyring` uses it as a backend), so the dependency surface doesn't change.

---

### Finding 3: `platformdirs` over `appdirs`
**Type:** Alternative Tool
**Replaces:** Path discovery in `config.py` for `%APPDATA%` and `%USERPROFILE%` resolution. The plan didn't pick an explicit library — this confirms which to use.

**Description**

[`appdirs`](https://github.com/ActiveState/appdirs) is officially deprecated (Python 3.12 support issues, no active maintenance). [`platformdirs`](https://github.com/tox-dev/platformdirs) is its actively-maintained fork with the same API plus extras (Desktop, Downloads, etc.).

**SWOT Analysis**

| | |
|---|---|
| **Strengths** | Drop-in replacement for `appdirs`. Active. Standard in modern Python tooling. |
| **Weaknesses** | None material. |
| **Opportunities** | Cross-platform path conventions for free if v1.5 ever runs on Mac/Linux. |
| **Threats** | None. |

**Non-Technical Operator Impact**

None directly visible. Affects where config and secrets are stored — operators don't see the path.

**Effort Delta**

Neutral. Same code shape, different import.

**Recommendation:** **Adopt.** Use `platformdirs` for `user_config_dir()`, `user_data_dir()`, etc. Default Working Library path becomes `Path(platformdirs.user_documents_dir()) / "IGA Marketing Master" / "Working Library"` (or similar — `user_documents_dir()` returns the user's Documents folder on Windows).

---

### Finding 4: Playwright Windows quirks (informational, no architecture change)
**Type:** Risk surface — informational
**Replaces:** Nothing. Adds to the build constraints list.

**Description**

Active Playwright issues on Windows that are relevant to our use:

- **[#35836](https://github.com/microsoft/playwright/issues/35836)** (1.52.0): `launchPersistentContext` may open `about:blank` instead of restoring a profile under specific conditions.
- **[#34700](https://github.com/microsoft/playwright/issues/34700)** (1.50.0): Relative `user_data_dir` paths resolve relative to the browser executable, not CWD. Use absolute paths only.
- **[#21019](https://github.com/microsoft/playwright/issues/21019)**: Edge default `userDataDir` is unsupported. We're using Chromium per plan — confirms we should not point at Chrome's main profile.
- **General:** browsers refuse to launch with a `userDataDir` already in use. If our app crashes hard (kill -9, BSOD), the lock file persists. Plan amendment already calls this out; ensure cleanup-on-launch is robust (delete `SingletonLock`, `LOCK`, etc. if no live PID).

**SWOT Analysis**

| | |
|---|---|
| **Strengths** | All issues are known and have workarounds. None are blockers. |
| **Weaknesses** | First-time setup will hit at least one of these unless we plan for them. |
| **Opportunities** | None. |
| **Threats** | If we don't pin Playwright version, a future minor release could regress. Pin to a known-good version (1.55+ as of April 2026) and only bump after testing. |

**Non-Technical Operator Impact**

Operator might see "browser already running" errors after a crash. Cleanup-on-launch addresses this. Without cleanup, operator has to manually delete a lock file — that's a TROUBLESHOOTING.md entry.

**Effort Delta**

Neutral — already in plan amendments.

**Recommendation:** **Consider** — make this a small, explicit Phase 1 task: pin Playwright version in requirements, write a `cleanup_user_data_dir_lock()` helper that runs before every `launch_persistent_context`, document the lock-file recovery path in TROUBLESHOOTING.md.

This is **moot if Finding 1 (REST API) is adopted.**

---

### Finding 5: Anthropic Sonnet 4.6 cache minimum is 2,048 tokens (not 1,024)
**Type:** Constraint — informational
**Replaces:** Nothing. Adds verification step.

**Description**

[Anthropic SDK Python issue #1194](https://github.com/anthropics/anthropic-sdk-python/issues/1194) and recent guides confirm: **Sonnet 4.6's cache breakpoint minimum is 2,048 tokens**, not the 1,024 documented for older models. If your cached prefix is below this threshold, the request silently succeeds but `cache_creation_input_tokens` returns 0 — meaning you pay full input price every call without knowing.

For our plan, the two cache breakpoints are:
- **Breakpoint 1:** system prompt + glossary + few-shot examples — likely 5-15K tokens, comfortably above threshold.
- **Breakpoint 2:** Epic Field Map — ~50-80K tokens, well above threshold.

Both should be fine. But our `claude_client.py` should explicitly **log `cache_creation_input_tokens` and `cache_read_input_tokens`** on every call so we can confirm hits, not assume them.

**Recommendation:** **Adopt** as a build discipline. Add this to amendment #4 in PLAN-REVIEW (which already calls for cache hit/miss logging — this finding makes the threshold explicit).

---

## Existing Solutions Discovery

Searched for end-to-end "documents → AI extraction → insurance AMS entry" tools.

### AI extraction tools (extraction half only)
**Examples:** [V7 Go](https://www.v7labs.com/), [Extend.ai](https://www.extend.ai/), [Box Extract](https://www.box.com/extract), [Algodocs](https://algodocs.com/), [Parseur](https://parseur.com/), [Xtracta](https://xtracta.com/), [Google Document AI](https://cloud.google.com/document-ai).

**What they solve:** AI extraction from PDFs to structured JSON/CSV. Some have visual grounding (V7) which is nice for audit.

**Gap:** None of them write to Applied EPIC. They produce structured data; the EPIC entry layer is still your problem.

**Trust posture:** Routing PII through a second SaaS vendor is a worse posture than your already-approved direct-to-Anthropic path. Each new vendor is another approval cycle, another DPA, another potential breach surface.

**Cost:** All are paid SaaS, typically $0.50-$2 per document. Comparable to the Sonnet 4.6 cost in the plan, sometimes worse, with less control over the prompt.

**Recommendation:** **Disregard.** The Anthropic-direct path in the plan is purpose-built for your use case and your agency has already approved it. Switching to a SaaS extraction layer trades flexibility for managed convenience, but the EPIC-entry half remains custom either way.

### End-to-end (extract + EPIC entry) commercial tools
**Findings:** None. Commercial Applied EPIC integrations (Canopy Connect, WinSurTech, others surfaced via the Applied Dev Center API) are point-to-point integrations between specific source systems (e.g., insurance carrier APIs, comparative raters) and EPIC — not "drop arbitrary PDFs in, data appears in EPIC." Your Goal is sufficiently specific (multi-carrier dec page extraction → EPIC) that no off-the-shelf tool covers it.

Custom development is warranted **for the document→state.json half**. The EPIC-entry half may be eliminable via the API (Finding 1).

---

## Final Recommendation

**Proceed to /plan-review with three amendments and one architecture question:**

1. **Adopt Finding 2 (`keyring`):** trivial replacement of amendment #1's secret-store code. Simpler, same security.
2. **Adopt Finding 3 (`platformdirs`):** trivial — pin the library and use `user_documents_dir()` for the default Working Library path.
3. **Adopt Finding 5 (cache hit logging discipline):** integrate into existing amendment #4.
4. **Resolve Finding 1 (REST API) before /build.** This is the load-bearing question. The recommended sequence:
   - **Step 1:** Spend 30 minutes checking whether IGA has existing Applied Dev Center access.
   - **Step 2:** If no, contact Applied to ask about API access — get a "yes/no/cost" answer within a week.
   - **Step 3:** Based on the answer, return to /plan to pivot to API-first if access is available and coverage is adequate, or confirm Playwright path if not.

Until Finding 1 is resolved, /build remains locked. The cost of pausing now is low (a week of waiting); the cost of building Playwright entry and then discovering EPIC's API was available all along is months of wasted work.

---

## Decision Gate — RESOLVED

**Chosen: A (with Finding 1 explicitly rejected, not deferred).** User direction: "We will not be implementing any API usage." The Applied Dev Center REST API is off the table for v1 and v1.5. Playwright remains the entry layer.

**Adopted findings (folded into PLAN-REVIEW.md as amendments #14-17):**
- #14 — `keyring` library replaces raw `pywin32 win32crypt` for the secret store (Finding 2)
- #15 — `platformdirs` is the explicit path-discovery library; default Working Library path uses `platformdirs.user_documents_dir()` (Finding 3)
- #16 — Playwright version pinned + explicit `cleanup_user_data_dir_lock()` helper before every `launch_persistent_context` (Finding 4 — no longer moot since API was rejected)
- #17 — `claude_client.py` logs `cache_creation_input_tokens` and `cache_read_input_tokens` on every call to verify cache hits; system prompt + Field Map cache breakpoints both clear Sonnet 4.6's 2,048-token minimum, but verify in practice (Finding 5)

**Next step: `/build` is unblocked.** PLAN-REVIEW.md is the authoritative plan. /plan-review status remains ✅ APPROVED WITH MATERIAL SCOPE CHANGE.

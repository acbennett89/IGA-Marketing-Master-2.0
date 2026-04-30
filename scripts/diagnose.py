#!/usr/bin/env python
"""scripts/diagnose.py -- end-to-end smoke test for IGA Marketing Master 2.0.

Exercises the full extraction pipeline (Field Map -> Claude API ->
state.json) without the GUI, against a fixed test folder you stage
once. Reports prompt token sizes, per-doc results, cache stats, and
estimated cost so you can iterate on real Anthropic calls without
clicking through the UI for every diagnostic run.

Quick start:
    1. Drop test PDFs into:
         Testing and Example Library\\diagnostic_inputs\\

    2. From the project root, with the venv active or via Diagnose.bat:
         python scripts\\diagnose.py --check        (free; no API calls)
         python scripts\\diagnose.py --single       (one PDF; cheap real run)
         python scripts\\diagnose.py                (all PDFs)

The diagnostic uses a separate Working Library
(Testing and Example Library\\diagnostic_workspace\\) so it never
touches your real client data.

Flags:
    --check               Setup verification only; no API calls.
    --single              Extract only the first PDF (cheap iteration).
    --force-opus          Use Opus 4.7 on every call.
    --working-library X   Override the diagnostic Working Library path.
    --client NAME         Override the client folder name (default: _DIAGNOSTIC).
    --no-debug            Disable --debug logging (default ON for diagnostics).
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import replace
from pathlib import Path

# Make the package importable when run from the project root.
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT / "src"))

from iga_marketing_master_2 import claude_client, config, extract, field_map, logger, secret_store  # noqa: E402

# ---- Defaults ---------------------------------------------------------------
DIAGNOSTIC_INPUTS = _ROOT / "Testing and Example Library" / "diagnostic_inputs"
DIAGNOSTIC_WORKSPACE = _ROOT / "Testing and Example Library" / "diagnostic_workspace"
DIAGNOSTIC_CLIENT = "_DIAGNOSTIC"

# Sonnet 4.6 list pricing (per 1M tokens) for cost estimation.
_RATE_INPUT = 3.0
_RATE_OUTPUT = 15.0
_RATE_CACHE_WRITE = 3.75   # 1.25x input
_RATE_CACHE_READ = 0.30    # 0.10x input

CACHE_MIN_TOKENS = 2_048   # Sonnet 4.6 cache breakpoint minimum (RESEARCH.md Finding 5)


# ---- Pretty printing --------------------------------------------------------
def _hr(char: str = "-", width: int = 72) -> str:
    return char * width


def _section(title: str) -> None:
    print()
    print(_hr("="))
    print(f"  {title}")
    print(_hr("="))


def _row(label: str, value: object, width: int = 30) -> None:
    print(f"  {label:<{width}} {value}")


def _approx_tokens(text: str) -> int:
    """Crude token estimate: chars / 4. Matches what claude_client uses."""
    return len(text) // 4


# ---- Setup checks -----------------------------------------------------------
def check_environment(settings: config.Settings) -> bool:
    """Print and validate the runtime environment. Returns True on success."""
    _section("Environment")
    _row("Python", sys.version.split()[0])
    _row("Project root", _ROOT)
    _row("Working Library", settings.working_library)
    _row("Field Map path", settings.field_map_path)
    _row("Log file", settings.log_dir / "iga.log")
    _row("Debug logging", settings.debug)

    _section("Field Map")
    if not settings.field_map_path.exists():
        print(f"  FAILED: Field Map not found at {settings.field_map_path}")
        return False
    try:
        fm = field_map.load(settings.field_map_path)
    except Exception as exc:
        print(f"  FAILED to load Field Map: {exc!r}")
        return False
    enum_values = fm.generate_domain_tag_enum()
    raw_top_level = len(getattr(fm, "raw", {}) or {})
    _row("Loaded", "OK")
    _row("Top-level screens", raw_top_level)
    _row("Populated domain_tags", len(enum_values))
    if enum_values:
        _row("Sample tags", ", ".join(list(enum_values)[:5]))
    else:
        _row("Sample tags", "(none yet -- bootstrap state, JIT proposals expected)")

    _section("Prompt assembly (cached prefix size)")
    sys_prompt = extract._DEFAULT_SYSTEM_PROMPT
    glossary = extract._DEFAULT_GLOSSARY
    fm_block = claude_client._serialize_field_map_for_prompt(fm)
    sys_tokens = _approx_tokens(sys_prompt)
    gloss_tokens = _approx_tokens(glossary)
    fm_tokens = _approx_tokens(fm_block)
    bp1_tokens = sys_tokens + gloss_tokens
    _row("system_prompt.txt", f"{len(sys_prompt):>7,} chars (~{sys_tokens:>5,} tokens)")
    _row("glossary.txt", f"{len(glossary):>7,} chars (~{gloss_tokens:>5,} tokens)")
    _row("Breakpoint 1 (sys+gloss)", f"~{bp1_tokens:>5,} tokens "
                                     f"({'OK' if bp1_tokens >= CACHE_MIN_TOKENS else 'TOO SMALL'})")
    _row("Field Map prompt block", f"{len(fm_block):>7,} chars (~{fm_tokens:>5,} tokens)")
    _row("Breakpoint 2 (field_map)", f"~{fm_tokens:>5,} tokens "
                                     f"({'OK' if fm_tokens >= CACHE_MIN_TOKENS else 'TOO SMALL'})")

    _section("Credentials")
    try:
        key = secret_store.get_anthropic_api_key()
    except Exception as exc:
        print(f"  FAILED to retrieve API key: {exc!r}")
        return False
    if not key:
        print("  No Anthropic API key found in keyring or env.")
        print("  Launch the GUI once to set it via the first-run prompt,")
        print("  or set the ANTHROPIC_API_KEY environment variable.")
        return False
    _row("Anthropic API key", f"...{key[-4:]} (length {len(key)})")
    _row("Default model", claude_client.DEFAULT_SONNET_MODEL)
    _row("Escalation model", claude_client.DEFAULT_OPUS_MODEL)

    return True


def stage_inputs() -> list[Path]:
    """Return the list of PDFs in DIAGNOSTIC_INPUTS, sorted."""
    if not DIAGNOSTIC_INPUTS.exists():
        DIAGNOSTIC_INPUTS.mkdir(parents=True, exist_ok=True)
    return sorted(DIAGNOSTIC_INPUTS.glob("*.pdf"))


def make_settings(working_library: Path | None, debug: bool) -> config.Settings:
    """Build a Settings dataclass for the diagnostic run."""
    base = config.load_settings()
    overrides: dict[str, object] = {"debug": debug}
    if working_library is not None:
        overrides["working_library"] = working_library.resolve()
    return replace(base, **overrides)


# ---- Extraction -------------------------------------------------------------
def _print_per_doc(per_doc: list) -> None:
    _section("Per-doc results")
    for d in per_doc:
        if getattr(d, "error_message", None):
            print(f"  [FAIL] {d.doc_id}: {d.error_message}")
            continue
        ok = d.fields_created > 0 or d.unknown_proposals > 0
        flag = "[OK]  " if ok else "[ZERO]"
        print(f"  {flag} {d.doc_id}")
        _row("    records returned", d.records_returned, width=26)
        _row("    known fields", d.known_records, width=26)
        _row("    JIT proposals", d.unknown_proposals, width=26)
        _row("    malformed dropped", d.malformed_tags_dropped, width=26)
        _row("    fields created", d.fields_created, width=26)
        _row("    fields updated", d.fields_updated, width=26)
        _row("    conflicts", d.conflicts_added, width=26)


def _estimate_cost(cs: extract.CacheStats) -> float:
    """Rough $ estimate from token counts using Sonnet 4.6 list pricing.

    cache_creation tokens are billed at 1.25x input; cache_read at 0.10x.
    The remaining (input_tokens - cache_creation - cache_read) is billed
    at standard input rate.
    """
    cached_creation = cs.cache_creation_input_tokens
    cached_read = cs.cache_read_input_tokens
    standard_input = max(0, cs.input_tokens - cached_creation - cached_read)
    return (
        (standard_input / 1_000_000) * _RATE_INPUT
        + (cached_creation / 1_000_000) * _RATE_CACHE_WRITE
        + (cached_read / 1_000_000) * _RATE_CACHE_READ
        + (cs.output_tokens / 1_000_000) * _RATE_OUTPUT
    )


def run_real_extraction(
    settings: config.Settings,
    pdfs: list[Path],
    *,
    client_name: str,
    single: bool,
    force_opus: bool,
) -> int:
    if not pdfs:
        print(f"\n  No PDFs found in {DIAGNOSTIC_INPUTS}.")
        print("  Drop test files there and re-run.")
        return 1
    if single:
        pdfs = pdfs[:1]

    _section(f"Extracting {len(pdfs)} PDF(s) for client '{client_name}'")
    for p in pdfs:
        _row(p.name, f"{p.stat().st_size:,} bytes", width=42)

    start = time.time()
    try:
        result = extract.run_extraction(
            client_name,
            pdfs,
            force_opus=force_opus,
            settings=settings,
        )
    except extract.ExtractionError as exc:
        print(f"\n  FAILED: {type(exc).__name__}: {exc}")
        return 2
    elapsed = time.time() - start

    _print_per_doc(result.per_doc)

    _section("Run summary")
    _row("Outcome", result.outcome)
    _row("Run ID", result.run_id)
    _row("Elapsed", f"{elapsed:.1f}s")
    _row("Total fields extracted", result.fields_extracted)
    _row("Pending JIT proposals", len(result.pending_proposals))
    _row("Conflicts surfaced", result.conflicts_surfaced)
    _row("Malformed dropped", result.malformed_tags_dropped)

    cs = result.cache_stats
    _section("Cache + token stats")
    _row("API calls", cs.api_calls)
    _row("Input tokens (uncached)", f"{cs.input_tokens:,}")
    _row("  cache write", f"{cs.cache_creation_input_tokens:,}")
    _row("  cache read", f"{cs.cache_read_input_tokens:,}")
    _row("Output tokens", f"{cs.output_tokens:,}")
    if cs.cache_creation_input_tokens == 0 and cs.cache_read_input_tokens == 0 and cs.api_calls > 0:
        print("  WARNING: zero cache write AND zero cache read across all calls.")
        print("           If breakpoint 1+2 cleared 2,048 tokens above, this is unexpected.")
    _row("Estimated cost", f"${_estimate_cost(cs):.4f} (Sonnet 4.6 rates)")

    state_path = settings.working_library / client_name / "state.json"
    _section("Output")
    _row("state.json", state_path)
    if state_path.exists():
        _row("state.json size", f"{state_path.stat().st_size:,} bytes")
    else:
        _row("state.json size", "(missing -- run did not produce a state file)")

    print()
    print("  Verify real Anthropic round-trips:")
    print(f"    grep claude.call_returned \"{settings.log_dir / 'iga.log'}\"")
    print("    response_id should look like: msg_01XXX...")
    print("  If response_ids are missing or oddly formatted, the call did NOT")
    print("  actually reach Anthropic and you have a synthetic-response bug.")

    return 0 if result.outcome == "completed" else 3


# ---- Main -------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnostic smoke test for IGA Marketing Master 2.0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--check", action="store_true",
        help="Only verify setup (Field Map, prompts, API key); no API calls.")
    parser.add_argument("--single", action="store_true",
        help="Extract only the first PDF (cheap iteration).")
    parser.add_argument("--force-opus", action="store_true",
        help="Use Opus 4.7 on every call (more expensive).")
    parser.add_argument("--client", default=DIAGNOSTIC_CLIENT,
        help=f"Client folder name (default: {DIAGNOSTIC_CLIENT}).")
    parser.add_argument("--working-library", type=Path, default=None,
        help="Override Working Library path (default: project-local diagnostic_workspace/).")
    parser.add_argument("--no-debug", action="store_true",
        help="Disable --debug (default ON for diagnostic runs).")
    args = parser.parse_args()

    debug = not args.no_debug
    wl = args.working_library if args.working_library else DIAGNOSTIC_WORKSPACE

    settings = make_settings(wl, debug=debug)
    settings.working_library.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    logger.configure_logging(settings)

    print(_hr("="))
    print("  IGA Marketing Master 2.0  --  diagnostic")
    print(_hr("="))

    if not check_environment(settings):
        return 1

    pdfs = stage_inputs()
    _section(f"Inputs ({DIAGNOSTIC_INPUTS})")
    if not pdfs:
        print("  (no PDFs yet)  --  drop test files into the directory above")
    else:
        for p in pdfs:
            _row(p.name, f"{p.stat().st_size:,} bytes", width=42)

    if args.check:
        _section("Check complete")
        if not pdfs:
            print("  Setup looks good, but no PDFs are staged yet.")
            print("  Drop one or more PDFs into the inputs directory and re-run")
            print("  (without --check) to do a real extraction.")
        else:
            print("  All setup checks passed. Re-run without --check to extract.")
        return 0

    return run_real_extraction(
        settings, pdfs,
        client_name=args.client,
        single=args.single,
        force_opus=args.force_opus,
    )


if __name__ == "__main__":
    sys.exit(main())

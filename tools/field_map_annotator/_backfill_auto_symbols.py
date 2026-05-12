"""One-shot script: tag ACORD 127 covered-auto-symbol fields.

EPIC stores symbols as individual boolean checkboxes (chkLiability1-9,
chkMedical2-8, etc.) — one per (coverage, symbol) pair. For our data
model we expose a single comma-separated ``.symbols`` string per
coverage row (e.g., "1, 7, 8, 9"). This keeps state.json compact and
matches how dec pages typically display the data.

Strategy: tag the FIRST checkbox in each coverage's symbol bank with
the canonical ``policy.auto.<coverage>.symbols`` domain_tag plus a
detailed ``notes_for_claude`` explaining ACORD 127 + state variations.
Sibling checkboxes (chkLiability2..9, chkMedical3..8, etc.) stay
untagged — entry.py will know how to expand the comma-separated value
into individual checkbox clicks.
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "Library" / "Epic Field Map.json"
SCREEN = "Business Auto > CoverageTN"


SYMBOLS_GLOSSARY = (
    "ACORD 127 covered-auto symbols define which auto types a coverage "
    "applies to. Standard symbols:\n"
    "  1  - Any Auto\n"
    "  2  - Owned Autos Only\n"
    "  3  - Owned Private Passenger Autos Only\n"
    "  4  - Owned Autos Other Than Private Passenger Autos Only\n"
    "  5  - Owned Autos Subject to No-Fault\n"
    "  6  - Owned Autos Subject to a Compulsory Uninsured Motorists Law\n"
    "  7  - Specifically Described Autos\n"
    "  8  - Hired Autos Only\n"
    "  9  - Non-Owned Autos Only\n"
    "  19 - Mobile Equipment Subject to Compulsory or Financial Responsibility "
    "or Other Motor Vehicle Insurance Law"
)

STATE_GUIDANCE = (
    "State-specific notes:\n"
    "  - Most states (TN, KY, OH, etc.): Liability typically uses 1, 7, 8, 9. "
    "UM uses 2, 7. Comp/Coll typically use 7 (specifically described).\n"
    "  - No-fault states (FL, MI, NJ, NY, PA, MA, MN, ND, KS, KY-optional, "
    "UT, HI): PIP/No-Fault coverage uses Symbol 5.\n"
    "  - Some states require Symbol 6 (Compulsory UM Law) instead of 2 for "
    "Uninsured Motorists — read the dec page header carefully.\n"
    "  - Mobile equipment (cranes, backhoes, forklifts on public roads): "
    "Symbol 19."
)


# (field_name, symbols_leaf, common_pattern_note)
ENTRIES = [
    (
        "chkLiability1",
        "liability.symbols",
        "On the dec page, look for the 'Liability' or 'BI/PD Liability' row. "
        "Symbols typically shown as numbers in a 'Sym(s)' column or check-marks "
        "under columns labeled 1-9. For most commercial auto policies, the "
        "answer is '1, 7, 8, 9' (Any Auto + Specifically Described + Hired + "
        "Non-Owned). Output as a comma-separated string.",
    ),
    (
        "chkMedical2",
        "medical.symbols",
        "Medical Payments row. Common values: '2, 7' (Owned + Specifically "
        "Described) or '2, 3, 4, 7' (varies by carrier/state). NOT applicable "
        "in PIP/no-fault states for personal-injury equivalents — those use "
        "no_fault.symbols=Symbol 5 instead.",
    ),
    (
        "chkUninsured2",
        "uninsured.symbols",
        "Uninsured/Underinsured Motorist row. Common values: '2, 7' or '6, 7' "
        "depending on whether the state mandates Compulsory UM (Symbol 6) vs "
        "voluntary owned-autos UM (Symbol 2). Read the dec page label — "
        "'Uninsured Motorists - Owned' = 2; 'Uninsured Motorists Compulsory' = 6.",
    ),
    (
        "chkTowing3",
        "towing.symbols",
        "Towing & Labor row. Almost always Symbol 7 only (specifically "
        "described autos). If shown as 'Symbol 3' it's owned private "
        "passenger autos only.",
    ),
    (
        "chkComprehensive2",
        "comprehensive.symbols",
        "Comprehensive (Other Than Collision) row. Almost always Symbol 7 "
        "(specifically described autos). Some carriers add 8 for hired or "
        "use 2 for owned autos broadly.",
    ),
    (
        "chkCauseOfLoss2",
        "specified_causes_loss.symbols",
        "Specified Causes of Loss row (a cheaper alternative to "
        "Comprehensive). Almost always Symbol 7. Mutually exclusive with "
        "Comprehensive on the same vehicle — extract whichever appears.",
    ),
    (
        "chkCollision2",
        "collision.symbols",
        "Collision row. Almost always Symbol 7 (specifically described "
        "autos). Some policies cover hired (8) too.",
    ),
]


def find_field(fm: dict, screen: str, name: str) -> dict | None:
    s = fm.get(screen, {})

    def walk(node):
        if not isinstance(node, dict):
            return
        for f in node.get("fields") or []:
            if isinstance(f, dict):
                yield f
        for tab in node.get("tabs") or []:
            if isinstance(tab, dict):
                yield from walk(tab)
        for st in node.get("sub_tabs") or []:
            if isinstance(st, dict):
                yield from walk(st)

    for f in walk(s):
        if f.get("name") == name:
            return f
    return None


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = SRC.with_suffix(f".json.symbols-{ts}.bak")
    shutil.copy2(SRC, bak)
    print(f"backup: {bak.name}")

    fm = json.loads(SRC.read_text(encoding="utf-8"))

    success = 0
    miss = 0
    for fname, leaf, pattern_note in ENTRIES:
        f = find_field(fm, SCREEN, fname)
        if f is None:
            print(f"  MISS: {fname!r}")
            miss += 1
            continue
        f["domain_tag"] = f"policy.auto.{leaf}"
        f["domain_tag_status"] = "verified"
        if "aliases" not in f:
            f["aliases"] = []
        # Build full note: glossary + per-coverage guidance + state notes.
        # Only the first entry includes the full glossary to avoid bloating
        # the prompt; subsequent entries reference back.
        if leaf == "liability.symbols":
            note = (
                f"Comma-separated string of ACORD 127 covered-auto symbols "
                f"that apply to this coverage line.\n\n"
                f"{SYMBOLS_GLOSSARY}\n\n"
                f"{STATE_GUIDANCE}\n\n"
                f"For this specific coverage: {pattern_note}\n\n"
                f"FORMAT: comma-separated digits, e.g., '1, 7, 8, 9' or '7'. "
                f"Do NOT emit the symbol descriptions — just the numbers."
            )
        else:
            note = (
                f"Comma-separated string of ACORD 127 covered-auto symbols "
                f"for this coverage. See policy.auto.liability.symbols for "
                f"the full symbol glossary and state-specific guidance.\n\n"
                f"For this specific coverage: {pattern_note}\n\n"
                f"FORMAT: comma-separated digits, e.g., '2, 7' or '7'."
            )
        f["notes_for_claude"] = note
        success += 1

    SRC.write_text(
        json.dumps(fm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nSuccess: {success}   Miss: {miss}")
    return 0 if miss == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

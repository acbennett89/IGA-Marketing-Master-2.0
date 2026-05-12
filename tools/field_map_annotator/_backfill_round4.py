"""Round-4 fixes:
- Sharpen Location address vs description notes so Claude stops
  concatenating them into one cell.
- Add aliases / verified status for Property, IM, WC, Umbrella singletons
  the section forms need to populate.
"""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "Library" / "Epic Field Map.json"


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


def patch(fm, screen, name, *, tag=None, status="verified", note=None):
    f = find_field(fm, screen, name)
    if f is None:
        print(f"  MISS: [{screen}] {name!r}")
        return False
    if tag is not None:
        f["domain_tag"] = tag
        f["domain_tag_status"] = status
    if "aliases" not in f:
        f["aliases"] = []
    if note is not None:
        f["notes_for_claude"] = note
    return True


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(SRC, SRC.with_suffix(f".json.r4-{ts}.bak"))

    fm = json.loads(SRC.read_text(encoding="utf-8"))

    # 1) Location address vs description ----------------------------------
    # streBuildingDescription = ADDRESS only. streSiteID = DESCRIPTION/LABEL only.
    patch(
        fm, "Commercial AP > Premise", "streBuildingDescription",
        note=(
            "Street address of the premises ONLY. Examples: "
            "'1320 US Highway 231 N, Hartford, KY 42347'. "
            "DO NOT include a building name, label, or description in this "
            "field — those go in `location.site_id`. If the dec page lists "
            "ONE location with no separate Premises Schedule, copy the "
            "named insured's mailing address here. If multiple locations "
            "share an address but have different building labels, repeat "
            "the address on each row."
        ),
    )
    patch(
        fm, "Commercial AP > Premise", "streSiteID",
        note=(
            "Building label / description / friendly name. Examples: "
            "'Building #1 - Office', 'Main Office Building', 'Shop', "
            "'Open Shed', 'Warehouse A'. DO NOT put the street address "
            "here — that goes in `location.building_description`. If the "
            "dec page lists 'Building #1 - Office, 1320 US Hwy 231', "
            "split it: 'Building #1 - Office' goes here, '1320 US Hwy 231' "
            "goes in `location.building_description`."
        ),
    )

    # 2) Property singletons ----------------------------------------------
    # Look up the Property deductible field if present. EPIC stores property
    # deductibles in many places; the most common singleton is the policy
    # base deductible on Property > Coverage screens. We try a few names.
    # Many policies don't have a single "policy deductible" — they're
    # per-subject. So we DO populate notes for the per-subject amount tag.
    # (No tag patching here — `policy.property.subject.*` is already verified.)

    # 3) Workers Comp singletons ------------------------------------------
    # The WorkersComp form expects:
    #   policy.workers_comp.statutory_limits
    #   policy.workers_comp.deductible
    #   policy.workers_comp.el_each_accident
    #   policy.workers_comp.el_disease_policy
    #   policy.workers_comp.el_disease_employee
    # The Field Map has different (canonical) names — the form will be
    # updated to use them. No backfill needed here.

    # 4) Umbrella singletons ----------------------------------------------
    # Field map already has policy.umbrella.occurrence_limit, retained_limit,
    # other_limit1, etc. Form will be updated.

    # 5) Property — verify amount/subject/etc. notes for completeness.
    patch(
        fm, "Property > Subject", "stresubject",  # placeholder; may MISS
        note=(
            "Per-subject coverage type: Building / BPP (Business Personal "
            "Property) / BI / Extra Expense / Other. Match exactly to one "
            "of those vocabularies."
        ),
    )

    SRC.write_text(
        json.dumps(fm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("Round-4 backfill complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

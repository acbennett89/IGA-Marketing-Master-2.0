"""One-shot script: backfill Additional Coverage screens across all LOBs.

Sets verified ``domain_tag`` and ``notes_for_claude`` on each meaningful
field on the 6 LOB-specific Additional Coverage screens. Used once to
get item #6 (Additional Coverages capture) wired up.
"""
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "Library" / "Epic Field Map.json"


# (screen_name, lob_namespace, field_name, leaf, optional_note)
ENTRIES = [
    # ----- General Liability -----
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streDescription", "name",
     "The plain-English coverage name as it appears on the dec page or "
     "endorsement schedule. Examples: 'Employment Practices Liability', "
     "'Employee Benefits Liability', 'Hired/Non-Owned Auto', 'Liquor "
     "Liability', 'Pollution Liability', 'Cyber Liability'. Match the "
     "dec page text; if the page uses an abbreviation like 'EPLI', use "
     "the full form."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streCode", "code",
     "Short code if shown on the dec page (e.g., 'EPLI', 'EBL', 'POLL'). "
     "Often the same as the form-number prefix. Optional."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streLimit", "each_claim_limit",
     "Primary limit for the additional coverage. For claims-made coverages "
     "this is typically 'Each Claim'. For occurrence coverages it's 'Each "
     "Occurrence'. Strip currency symbols and commas."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streLimit2", "aggregate_limit",
     "Aggregate or secondary limit. Usually labeled 'Total Aggregate Limit' "
     "or 'Aggregate'. Strip currency symbols."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streLimit3", "limit_3",
     "Third limit slot if the endorsement uses one. Rare; leave omitted "
     "unless the dec page explicitly lists three limit values."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streDeductible", "deductible",
     "Deductible amount. Strip currency symbols and commas."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streDeductibleType", "deductible_type",
     "Standard values: 'Each Claim', 'Aggregate', 'Each Occurrence', 'Each "
     "Person'. Match the dec page label."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "streFormNumber", "form_number",
     "Endorsement form number, e.g., 'WB 516 GL 02 26' or 'CG 21 47'. "
     "Match exactly including spaces/punctuation as printed."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "dteCoverageEffectiveDate", "retroactive_date",
     "Retroactive Date for claims-made endorsements (e.g., EPLI, EBL). "
     "Only present on claims-made coverages — leave omitted for occurrence."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "inteLocationNumber", "location_number",
     "1-based location number this coverage applies to. Default '1' if single location."),
    ("General Liability > AdditionalCoverage", "policy.gl.additional_coverage", "inteBuildingNumber", "building_number",
     "1-based building number. Default '1' if not shown."),

    # ----- Property -----
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "streDescription", "name",
     "The plain-English coverage name. Examples: 'Equipment Breakdown', "
     "'Ordinance or Law', 'Spoilage', 'Earthquake', 'Flood'."),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "streCode", "code", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "streLimit1", "each_claim_limit", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "streLimit2", "aggregate_limit", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "streDeductible", "deductible", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "streDedType", "deductible_type", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "streFormNumber", "form_number", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "dteFormDate", "retroactive_date", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "inteLocationNumber", "location_number", None),
    ("Property > AdditionalCoverage", "policy.property.additional_coverage", "inteBuildingNumber", "building_number", None),

    # ----- Business Auto -----
    ("Business Auto > AdditionalCoverage", "policy.auto.additional_coverage", "streDescription", "name",
     "The plain-English coverage name. Examples: 'Hired Auto Physical "
     "Damage', 'Drive Other Car', 'Garage Keepers', 'Roadside Assistance'."),
    ("Business Auto > AdditionalCoverage", "policy.auto.additional_coverage", "streCode", "code", None),
    ("Business Auto > AdditionalCoverage", "policy.auto.additional_coverage", "streLimit1", "each_claim_limit", None),
    ("Business Auto > AdditionalCoverage", "policy.auto.additional_coverage", "streLimit2", "aggregate_limit", None),
    ("Business Auto > AdditionalCoverage", "policy.auto.additional_coverage", "streDeductible", "deductible", None),
    ("Business Auto > AdditionalCoverage", "policy.auto.additional_coverage", "streDeductibleType", "deductible_type", None),
    ("Business Auto > AdditionalCoverage", "policy.auto.additional_coverage", "inteVehicleNumber", "vehicle_number",
     "Vehicle # this coverage applies to (matches policy.auto.vehicle.vehicle_num). "
     "Leave omitted if it applies to all autos."),

    # ----- Inland Marine -----
    ("Inland Marine- Commercial > AdditionalCoverage", "policy.inland_marine.additional_coverage", "streDescription", "name",
     "The plain-English coverage name. Examples: 'Newly Acquired "
     "Equipment', 'Rental Reimbursement', 'Property of Others'."),
    ("Inland Marine- Commercial > AdditionalCoverage", "policy.inland_marine.additional_coverage", "streCode", "code", None),
    ("Inland Marine- Commercial > AdditionalCoverage", "policy.inland_marine.additional_coverage", "streLimit", "each_claim_limit", None),
    ("Inland Marine- Commercial > AdditionalCoverage", "policy.inland_marine.additional_coverage", "streDeductible", "deductible", None),
    ("Inland Marine- Commercial > AdditionalCoverage", "policy.inland_marine.additional_coverage", "inteItemNumber", "item_number", None),

    # ----- Workers Comp -----
    ("Worker's Compensation > AdditionalCoverage", "policy.workers_comp.additional_coverage", "streDescription", "name",
     "The plain-English coverage name. Examples: 'USL&H', 'Voluntary "
     "Compensation', 'Foreign Coverage', 'Alternate Employer'."),
    ("Worker's Compensation > AdditionalCoverage", "policy.workers_comp.additional_coverage", "streCode", "code", None),
    ("Worker's Compensation > AdditionalCoverage", "policy.workers_comp.additional_coverage", "streLimit1", "each_claim_limit", None),
    ("Worker's Compensation > AdditionalCoverage", "policy.workers_comp.additional_coverage", "streLimit2", "aggregate_limit", None),
    ("Worker's Compensation > AdditionalCoverage", "policy.workers_comp.additional_coverage", "streDeductible", "deductible", None),
    ("Worker's Compensation > AdditionalCoverage", "policy.workers_comp.additional_coverage", "dteAddlCoverageDate", "retroactive_date", None),
    ("Worker's Compensation > AdditionalCoverage", "policy.workers_comp.additional_coverage", "cboState", "state", None),

    # ----- Umbrella -----
    ("Commercial Umbrella > AdditionalCoverages", "policy.umbrella.additional_coverage", "streDescription", "name",
     "The plain-English coverage name (excess version of underlying "
     "coverages). Examples: 'Excess EBL', 'Excess EPLI', 'Excess UM/UIM'."),
    ("Commercial Umbrella > AdditionalCoverages", "policy.umbrella.additional_coverage", "streCode", "code", None),
    ("Commercial Umbrella > AdditionalCoverages", "policy.umbrella.additional_coverage", "streLimit1", "each_claim_limit", None),
    ("Commercial Umbrella > AdditionalCoverages", "policy.umbrella.additional_coverage", "streLimit2", "aggregate_limit", None),
    ("Commercial Umbrella > AdditionalCoverages", "policy.umbrella.additional_coverage", "streDeductible", "deductible", None),
    ("Commercial Umbrella > AdditionalCoverages", "policy.umbrella.additional_coverage", "streDeductibleType", "deductible_type", None),
    ("Commercial Umbrella > AdditionalCoverages", "policy.umbrella.additional_coverage", "dteDate-mask", "retroactive_date", None),
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
    bak = SRC.with_suffix(f".json.addcov-{ts}.bak")
    shutil.copy2(SRC, bak)
    print(f"backup: {bak.name}")

    fm = json.loads(SRC.read_text(encoding="utf-8"))

    success = 0
    miss = 0
    for screen, ns, name, leaf, note in ENTRIES:
        f = find_field(fm, screen, name)
        if f is None:
            print(f"  MISS: [{screen}] {name!r}")
            miss += 1
            continue
        f["domain_tag"] = f"{ns}.{leaf}"
        f["domain_tag_status"] = "verified"
        if "aliases" not in f:
            f["aliases"] = []
        if note is not None:
            f["notes_for_claude"] = note
        success += 1

    SRC.write_text(
        json.dumps(fm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"\nSuccess: {success}   Miss: {miss}")
    return 0 if miss == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

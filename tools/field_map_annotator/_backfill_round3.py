"""Round-3 Field Map fixes: Named Insured aliasing, Location notes,
EBL section retag, Aggregate Applies To notes."""
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


def patch(fm, screen, name, *, tag=None, status="verified", aliases=None, note=None):
    f = find_field(fm, screen, name)
    if f is None:
        print(f"  MISS: [{screen}] {name!r}")
        return False
    if tag is not None:
        f["domain_tag"] = tag
        f["domain_tag_status"] = status
    if aliases is not None:
        f["aliases"] = list(aliases)
    elif "aliases" not in f:
        f["aliases"] = []
    if note is not None:
        f["notes_for_claude"] = note
    return True


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    shutil.copy2(SRC, SRC.with_suffix(f".json.r3-{ts}.bak"))

    fm = json.loads(SRC.read_text(encoding="utf-8"))

    # --- 1) Named Insured: re-tag streFNIName as canonical account.named_insured.name ---
    # Path B: ALL named insureds live in the account.named_insured repeatable.
    # The Primary uses the same schema with type=primary.
    patch(
        fm, "Commercial AP > Applicant", "streFNIName",
        tag="account.named_insured.name",
        aliases=["account.named_insured.fni_name"],
        note=(
            "PRIMARY Named Insured's legal entity name. Emit this as the "
            "FIRST item in the `account.named_insured` repeatable group "
            "with `account.named_insured.type` = \"primary\". Do NOT emit "
            "this as a flat singleton — it must be a row inside the "
            "repeatable so the GUI can render it alongside any DBA/additional "
            "named insureds. Match the dec page text exactly (capitalization, "
            "punctuation, LLC/Inc suffixes)."
        ),
    )

    # --- 2) Location notes: building description + fallback to account address ---
    patch(
        fm, "Commercial AP > Premise", "streBuildingDescription",
        note=(
            "Free-text description of the premises. Most commonly the street "
            "address (e.g., '1320 US Highway 231 N, Hartford, KY 42347'). "
            "Can also be a building name (e.g., 'Main Office Building'). "
            "If the dec page lists only ONE location and doesn't break out a "
            "separate Premises Schedule, copy the named insured's mailing "
            "address here as the location description."
        ),
    )

    # --- 3) Remove EBL from additional_coverage examples; retag for its own namespace ---
    # The GL > ClaimsMadeEmployeeBenefits screen has EBL-specific fields.
    EBL_SCREEN = "General Liability > ClaimsMadeEmployeeBenefits"
    EBL_FIELDS = [
        ("streDeductiblePerClaim", "policy.gl.ebl.deductible_each_claim",
         "EBL deductible per claim. Strip currency symbols and commas."),
        ("dteRetroactiveDate", "policy.gl.ebl.retroactive_date",
         "EBL retroactive date — claims-made coverage trigger. Format MM/DD/YYYY."),
        ("dteProposedRetroDate", "policy.gl.ebl.proposed_retro_date", None),
        ("inteNumberOfEmployees", "policy.gl.ebl.number_of_employees",
         "Total employees the EBL endorsement covers."),
        ("inteNumberOfEmployeesCovered", "policy.gl.ebl.number_of_employees_covered", None),
        ("cboTailCoveragePurchasedYesNo", "policy.gl.ebl.tail_coverage_purchased", None),
        ("streTailCoveragePurchasedDescription", "policy.gl.ebl.tail_coverage_description", None),
        ("cboAnyProductExcludedYesNo", "policy.gl.ebl.any_product_excluded", None),
        ("streAnyProductExcludedDescription", "policy.gl.ebl.product_excluded_description", None),
    ]
    for fname, tag, note in EBL_FIELDS:
        patch(fm, EBL_SCREEN, fname, tag=tag, note=note)

    # Update the existing GL.additional_coverage.name note to clarify EBL is OUT.
    patch(
        fm, "General Liability > AdditionalCoverage", "streDescription",
        note=(
            "The plain-English coverage name as it appears on the dec page or "
            "endorsement schedule. EXAMPLES (not exhaustive — extract any "
            "endorsement-style coverage with its own limits/deductible): "
            "'Employment Practices Liability', 'Hired/Non-Owned Auto', "
            "'Liquor Liability', 'Pollution Liability', 'Cyber Liability', "
            "'Stop Gap', 'Limited Pollution', 'Owners and Contractors "
            "Protective', 'Designated Construction Project Aggregate'. "
            "DO NOT include Employee Benefits Liability (EBL) here — EBL "
            "has its own dedicated tags under `policy.gl.ebl.*`. Match the "
            "dec page text; if abbreviated (e.g., 'EPLI'), expand to the "
            "full form."
        ),
    )

    # --- 5) Aggregate Applies To checkbox notes ---
    AGG_NOTES = (
        "TRUE if the GL General Aggregate Limit applies to {scope}. "
        "On the dec page or in attached endorsements, look for: "
        "(1) checkboxes labeled 'General Aggregate Limit Applies To' with "
        "Policy/Location/Project options selected; (2) form numbers like "
        "CG2503 (Designated Construction Project), CG2504 (Designated "
        "Locations), or CG2509 (Amendment of Limits of Insurance for {scope}); "
        "(3) endorsement names mentioning 'Designated {scope}'. "
        "Output 'Yes' if any indicator says this scope applies; 'No' otherwise. "
        "By default GL aggregate applies to Policy unless overridden."
    )
    patch(fm, "General Liability > Coverages", "GLCOVERchkAppPolicy",
          note=AGG_NOTES.format(scope="Policy"))
    patch(fm, "General Liability > Coverages", "GLCOVERchkAppLocation",
          note=AGG_NOTES.format(scope="Location"))
    patch(fm, "General Liability > Coverages", "GLCOVERchkAppProject",
          note=AGG_NOTES.format(scope="Project"))

    SRC.write_text(
        json.dumps(fm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("Round-3 backfill complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

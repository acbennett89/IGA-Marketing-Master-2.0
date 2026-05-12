"""batch_accept.py — accept every proposed tag for unmapped Tier 1 fields.

Run from the project root:
    python tools/field_map_annotator/batch_accept.py

Walks the same Tier 1 fields the annotator walks. For every field whose
status is not already 'verified' or 'out_of_scope', writes the proposed
domain_tag with status='verified'. Skips fields where the proposer
returns nothing or an invalid tag (those need manual review).

Makes a backup at session start; safe to re-run.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from tools.field_map_annotator import registry, walker  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
FIELD_MAP_PATH = PROJECT_ROOT / "Library" / "Epic Field Map.json"
PROGRESS_PATH = PROJECT_ROOT / "docs" / "tier1-annotation-progress.json"


def main() -> int:
    # Backup
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = FIELD_MAP_PATH.with_suffix(f".json.batch-{ts}.bak")
    shutil.copy2(FIELD_MAP_PATH, bak)
    print(f"[batch_accept] backup: {bak.name}")

    fm = json.loads(FIELD_MAP_PATH.read_text(encoding="utf-8"))
    fields = walker.walk(fm)
    print(f"[batch_accept] in-scope fields: {len(fields)}")

    accepted = 0
    skipped_already_verified = 0
    skipped_out_of_scope = 0
    skipped_no_proposal = 0
    skipped_invalid = []  # list of (display_path, name, why)
    invalid_examples: list[tuple[str, str, str | None, str]] = []

    for ref in fields:
        existing_status = (ref.raw or {}).get("domain_tag_status")
        if existing_status == "verified":
            skipped_already_verified += 1
            continue
        if existing_status == "out_of_scope":
            skipped_out_of_scope += 1
            continue

        proposed = walker.propose_tag(ref)
        if not proposed:
            skipped_no_proposal += 1
            invalid_examples.append((ref.display_path(), ref.name, None, "no proposal"))
            continue

        ok, reason = registry.validate_tag(proposed)
        if not ok:
            skipped_invalid.append((ref.display_path(), ref.name, reason or "?"))
            invalid_examples.append((ref.display_path(), ref.name, proposed, reason or "?"))
            continue

        walker.write_annotation(
            fm, ref,
            domain_tag=proposed,
            aliases=[],
            notes_for_claude=None,
            status="verified",
        )
        accepted += 1

    # Save Field Map atomically
    tmp = FIELD_MAP_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(fm, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(FIELD_MAP_PATH)
    print(f"[batch_accept] Field Map saved")

    # Clear the session skip list — we just accepted everything we could
    if PROGRESS_PATH.is_file():
        try:
            prog = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
            prog["skipped_field_ids"] = []
            PROGRESS_PATH.write_text(
                json.dumps(prog, indent=2, sort_keys=True), encoding="utf-8"
            )
            print(f"[batch_accept] cleared skip list in progress file")
        except json.JSONDecodeError:
            pass

    # Summary
    print()
    print("=" * 70)
    print("SUMMARY")
    print("=" * 70)
    print(f"  Accepted (newly verified):    {accepted}")
    print(f"  Already verified (skipped):   {skipped_already_verified}")
    print(f"  Already N/A (skipped):        {skipped_out_of_scope}")
    print(f"  No proposal (left unmapped):  {skipped_no_proposal}")
    print(f"  Invalid proposal (skipped):   {len(skipped_invalid)}")
    print()
    if invalid_examples:
        print(f"Fields needing manual attention ({len(invalid_examples)}):")
        for path, name, proposed, why in invalid_examples[:30]:
            print(f"  [{path}]")
            print(f"    name={name!r}  proposed={proposed!r}")
            print(f"    {why}")
        if len(invalid_examples) > 30:
            print(f"  ... and {len(invalid_examples) - 30} more")
        print()
        print("Run the annotator to handle these manually:")
        print("    python tools/field_map_annotator/annotator.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

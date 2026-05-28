"""tests/test_reconcile.py — unit tests for the Client ↔ Named Insureds reconcile module.

Covers:
1. ``normalize_entity_name`` — punct/case/suffix collapsing.
2. ``find_matching_ni_row`` — match by normalized name; multi-row exact-match
   wins over near-match; no match returns None.
3. ``compute_deltas`` — full 5-kind matrix on a per-field basis (noop,
   silent_to_insured, silent_to_ni, conflict, canon_extraction_diff).
4. ``apply_silent_merges`` — fills the blank side, does NOT touch canon_fields.
5. ``apply_resolutions`` — writes to BOTH sides, adds to canon_fields, pins
   the NI FieldRecord.
6. State round-trip — pinned + canon_fields survive save/load.

The reconcile module is pure logic — no Qt — so these tests run headless.
"""
from __future__ import annotations

import pytest

from iga_marketing_master_2 import reconcile
from iga_marketing_master_2.reconcile import (
    INSURED_NI_FIELD_MAP,
    NAMED_INSURED_GROUP,
    ReconcileDelta,
)
from iga_marketing_master_2.state import (
    FieldRecord,
    Insured,
    State,
    _state_from_dict,
    _state_to_dict,
)


# ── Helpers ───────────────────────────────────────────────────────────────


def make_state(
    *,
    insured_name: str = "Acme LLC",
    insured: dict | None = None,
    ni_rows: list[dict] | None = None,
    canon: list[str] | None = None,
) -> State:
    """Build a minimal State with ``insured`` + an ``account.named_insured`` repeatable.

    ``insured`` is a dict of attr→value to set on Insured (in addition to name).
    ``ni_rows`` is a list of dicts mapping leaf-tag → value; each becomes one
    NI repeatable row with FieldRecord wrappers at confidence 0.9.
    """
    st = State(
        client="acme",
        created_at="2026-05-28T12:00:00",
        updated_at="2026-05-28T12:00:00",
    )
    st.insured = Insured(
        named_insured=insured_name,
        canon_fields=list(canon or []),
        **(insured or {}),
    )
    rows: list[dict] = []
    for row in ni_rows or []:
        rec_row: dict[str, FieldRecord] = {}
        for tag, val in row.items():
            rec_row[tag] = FieldRecord(value=val, confidence=0.9)
        rows.append(rec_row)
    st.repeatables[NAMED_INSURED_GROUP] = rows
    return st


# ── 1. normalize_entity_name ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "a,b",
    [
        ("Acme, LLC", "ACME LLC"),
        ("Acme L.L.C.", "Acme LLC"),
        ("Acme Incorporated", "Acme Inc."),
        ("Acme Co.", "acme co"),
        ("Acme  LLC", "Acme LLC"),  # double space collapses
        ("Acme & Sons LLC", "acme  sons llc"),  # &/whitespace
    ],
)
def test_normalize_equal_pairs(a: str, b: str) -> None:
    assert reconcile.normalize_entity_name(a) == reconcile.normalize_entity_name(b)


@pytest.mark.parametrize(
    "a,b",
    [
        ("Acme LLC", "Acme Holdings LLC"),
        ("Acme LLC", "Acme Subsidiary LLC"),
        ("Acme Corp Holdings", "Acme Corp"),  # only trailing suffix stripped
        ("Beta Inc", "Acme Inc"),
    ],
)
def test_normalize_unequal_pairs(a: str, b: str) -> None:
    assert reconcile.normalize_entity_name(a) != reconcile.normalize_entity_name(b)


def test_normalize_blank() -> None:
    assert reconcile.normalize_entity_name("") == ""
    assert reconcile.normalize_entity_name(None) == ""
    assert reconcile.normalize_entity_name("   ") == ""


# ── 2. find_matching_ni_row ───────────────────────────────────────────────


def test_match_no_ni_rows_returns_none() -> None:
    st = make_state(ni_rows=None)
    assert reconcile.find_matching_ni_row(st) is None


def test_match_blank_insured_name_returns_none() -> None:
    st = make_state(insured_name="", ni_rows=[{"account.named_insured.name": "Acme LLC"}])
    assert reconcile.find_matching_ni_row(st) is None


def test_match_simple_normalized() -> None:
    st = make_state(
        insured_name="Acme, LLC",
        ni_rows=[{"account.named_insured.name": "ACME L.L.C."}],
    )
    assert reconcile.find_matching_ni_row(st) == 0


def test_match_picks_first_when_multiple_rows_match() -> None:
    st = make_state(
        insured_name="Acme LLC",
        ni_rows=[
            {"account.named_insured.name": "Acme Holdings LLC"},
            {"account.named_insured.name": "Acme, LLC"},
            {"account.named_insured.name": "Acme LLC"},
        ],
    )
    # Index 0 doesn't match; indices 1 + 2 both match. First wins.
    assert reconcile.find_matching_ni_row(st) == 1


def test_match_no_normalized_match_returns_none() -> None:
    st = make_state(
        insured_name="Acme LLC",
        ni_rows=[{"account.named_insured.name": "Acme Holdings LLC"}],
    )
    assert reconcile.find_matching_ni_row(st) is None


# ── 3. compute_deltas full matrix ────────────────────────────────────────


def _ni_only(name: str = "Acme LLC", **extras: str) -> list[dict]:
    """Build one NI row with the given name + any extra field values."""
    row = {"account.named_insured.name": name}
    row.update(extras)
    return [row]


def test_compute_no_matched_row_returns_empty() -> None:
    st = make_state(insured_name="Beta LLC", ni_rows=_ni_only("Acme LLC"))
    assert reconcile.compute_deltas(st) == []


def test_compute_all_noop_when_both_blank() -> None:
    st = make_state(ni_rows=_ni_only())
    deltas = reconcile.compute_deltas(st)
    # Every non-name field is blank both sides; the name itself matches by
    # normalization → noop.
    assert {d.kind for d in deltas} == {"noop"}


def test_compute_silent_to_ni_when_client_has_value() -> None:
    st = make_state(insured={"fein": "12-3456789"}, ni_rows=_ni_only())
    deltas = reconcile.compute_deltas(st)
    fein = next(d for d in deltas if d.field == "fein")
    assert fein.kind == "silent_merge_to_ni"
    assert fein.insured_value == "12-3456789"
    assert fein.ni_value == ""


def test_compute_silent_to_insured_when_ni_has_value() -> None:
    st = make_state(
        ni_rows=_ni_only(**{"account.named_insured.fein": "12-3456789"})
    )
    deltas = reconcile.compute_deltas(st)
    fein = next(d for d in deltas if d.field == "fein")
    assert fein.kind == "silent_merge_to_insured"


def test_compute_conflict_when_both_differ() -> None:
    st = make_state(
        insured={"fein": "11-1111111"},
        ni_rows=_ni_only(**{"account.named_insured.fein": "22-2222222"}),
    )
    deltas = reconcile.compute_deltas(st)
    fein = next(d for d in deltas if d.field == "fein")
    assert fein.kind == "conflict"


def test_compute_canon_diff_when_canon_marked_and_disagree() -> None:
    st = make_state(
        insured={"fein": "11-1111111"},
        ni_rows=_ni_only(**{"account.named_insured.fein": "22-2222222"}),
        canon=["fein"],
    )
    deltas = reconcile.compute_deltas(st)
    fein = next(d for d in deltas if d.field == "fein")
    assert fein.kind == "canon_extraction_diff"


def test_compute_name_equality_uses_normalized_comparison() -> None:
    """A name conflict between 'Acme, LLC' and 'ACME LLC' should be a noop."""
    st = make_state(
        insured_name="Acme, LLC",
        ni_rows=[{"account.named_insured.name": "ACME LLC"}],
    )
    deltas = reconcile.compute_deltas(st)
    name = next(d for d in deltas if d.field == "named_insured")
    assert name.kind == "noop"


# ── 4. apply_silent_merges ────────────────────────────────────────────────


def test_silent_merge_fills_insured_blank_from_ni() -> None:
    st = make_state(
        ni_rows=_ni_only(**{"account.named_insured.fein": "12-3456789"})
    )
    deltas = reconcile.compute_deltas(st)
    reconcile.apply_silent_merges(st, deltas)
    assert st.insured.fein == "12-3456789"
    assert "fein" not in st.insured.canon_fields  # NOT marked canon


def test_silent_merge_fills_ni_blank_from_insured() -> None:
    st = make_state(insured={"fein": "98-7654321"}, ni_rows=_ni_only())
    deltas = reconcile.compute_deltas(st)
    reconcile.apply_silent_merges(st, deltas)
    rec = st.repeatables[NAMED_INSURED_GROUP][0]["account.named_insured.fein"]
    assert rec.value == "98-7654321"
    assert rec.pinned is False  # silent merge does not pin


# ── 5. apply_resolutions ──────────────────────────────────────────────────


def test_apply_resolutions_writes_both_sides_and_marks_canon() -> None:
    st = make_state(
        insured={"fein": "11-1111111", "business_type": "LLC"},
        ni_rows=_ni_only(
            **{
                "account.named_insured.fein": "22-2222222",
                "account.named_insured.business_type": "Corporation",
            }
        ),
    )
    # Operator picks: keep client FEIN, use NI business type.
    reconcile.apply_resolutions(
        st, {"fein": "11-1111111", "business_type": "Corporation"}
    )
    # Both sides match.
    assert st.insured.fein == "11-1111111"
    assert st.insured.business_type == "Corporation"
    row = st.repeatables[NAMED_INSURED_GROUP][0]
    assert row["account.named_insured.fein"].value == "11-1111111"
    assert row["account.named_insured.business_type"].value == "Corporation"
    # Both pinned.
    assert row["account.named_insured.fein"].pinned
    assert row["account.named_insured.business_type"].pinned
    # canon_fields tracks both.
    assert set(st.insured.canon_fields) == {"fein", "business_type"}


def test_apply_resolutions_idempotent_canon() -> None:
    """Re-applying a resolution for an already-canon field doesn't duplicate."""
    st = make_state(
        insured={"fein": "11-1111111"},
        ni_rows=_ni_only(**{"account.named_insured.fein": "22-2222222"}),
        canon=["fein"],
    )
    reconcile.apply_resolutions(st, {"fein": "33-3333333"})
    assert st.insured.canon_fields.count("fein") == 1


def test_apply_resolutions_skips_unknown_fields() -> None:
    st = make_state(ni_rows=_ni_only())
    reconcile.apply_resolutions(st, {"made_up_field": "x"})
    assert "made_up_field" not in st.insured.canon_fields


# ── 6. State round-trip ───────────────────────────────────────────────────


def test_round_trip_preserves_canon_and_pinned() -> None:
    st = make_state(
        insured={"fein": "33-3333333"},
        ni_rows=_ni_only(**{"account.named_insured.fein": "33-3333333"}),
        canon=["fein", "business_type"],
    )
    # Pin the NI FEIN record manually (as apply_resolutions would).
    st.repeatables[NAMED_INSURED_GROUP][0]["account.named_insured.fein"].pinned = True

    rt = _state_from_dict(_state_to_dict(st), client_fallback="acme")
    assert rt.insured.canon_fields == ["fein", "business_type"]
    assert rt.repeatables[NAMED_INSURED_GROUP][0][
        "account.named_insured.fein"
    ].pinned is True


def test_round_trip_default_pinned_false_for_legacy() -> None:
    """Old state files without 'pinned' should load as False (no migration)."""
    raw = _state_to_dict(make_state(ni_rows=_ni_only()))
    # Simulate a legacy dict that omits the pinned key entirely.
    rec = raw["repeatables"][NAMED_INSURED_GROUP][0]["account.named_insured.name"]
    rec.pop("pinned", None)
    rt = _state_from_dict(raw, client_fallback="acme")
    assert rt.repeatables[NAMED_INSURED_GROUP][0][
        "account.named_insured.name"
    ].pinned is False


def test_round_trip_default_canon_fields_empty_for_legacy() -> None:
    raw = _state_to_dict(make_state(ni_rows=_ni_only()))
    raw["insured"].pop("canon_fields", None)
    rt = _state_from_dict(raw, client_fallback="acme")
    assert rt.insured.canon_fields == []


# ── 7. Multi-match first-wins (no primary preference now that the merge
#       layer collapses normalized-equivalent rows into one) ───────────────


def test_multi_match_returns_first_when_multiple_normalized_matches() -> None:
    """When several rows happen to share a normalized name (rare after the
    merge-key normalization landed), the first match wins."""
    st = make_state(
        insured_name="Acme LLC",
        ni_rows=[
            {"account.named_insured.name": "Acme, LLC"},
            {"account.named_insured.name": "ACME LLC"},
            {"account.named_insured.name": "Acme L.L.C."},
        ],
    )
    assert reconcile.find_matching_ni_row(st) == 0


# ── 8. Canon detects lost-confidence candidate ────────────────────────────


def test_canon_diff_detected_via_conflicts_when_surface_values_agree() -> None:
    """Operator pinned FEIN. New extraction proposed a different value but
    lost on confidence; it sits in ``FieldRecord.conflicts[]``. The
    reconciliation must still surface the disagreement."""
    from iga_marketing_master_2.state import ConflictCandidate, SourceRef

    st = make_state(
        insured={"fein": "11-1111111"},
        ni_rows=_ni_only(**{"account.named_insured.fein": "11-1111111"}),
        canon=["fein"],
    )
    # Surface values agree (both "11-1111111"). Inject a disagreeing
    # candidate into conflicts[] to simulate the lost-confidence case.
    rec = st.repeatables["account.named_insured"][0]["account.named_insured.fein"]
    rec.conflicts.append(
        ConflictCandidate(
            value="99-9999999",
            confidence=0.55,
            source=SourceRef(doc_id="d1", page=1, quote="…"),
            observed_at="2026-05-28T12:00:00",
            model_used="sonnet-4-6",
        )
    )
    deltas = reconcile.compute_deltas(st)
    fein_deltas = [d for d in deltas if d.field == "fein"]
    assert len(fein_deltas) == 1
    assert fein_deltas[0].kind == "canon_extraction_diff"
    assert fein_deltas[0].ni_value == "99-9999999"


def test_canon_diff_not_raised_when_no_disagreeing_candidate() -> None:
    """Surface values agree, conflicts[] is empty → still a noop."""
    st = make_state(
        insured={"fein": "11-1111111"},
        ni_rows=_ni_only(**{"account.named_insured.fein": "11-1111111"}),
        canon=["fein"],
    )
    deltas = reconcile.compute_deltas(st)
    fein = next(d for d in deltas if d.field == "fein")
    assert fein.kind == "noop"


# ── 9. Merge respects pinned ──────────────────────────────────────────────


def test_merge_repeatable_item_skips_update_when_pinned() -> None:
    """A pinned FieldRecord must not be silently overwritten by extraction
    merge — even when the new candidate has higher confidence."""
    from iga_marketing_master_2.state import (
        ExtractedField,
        MergeReport,
        SourceRef,
        _merge_repeatable_item,
    )

    item: dict = {
        "account.named_insured.fein": FieldRecord(
            value="11-1111111",
            confidence=0.4,  # low — extraction will try to win
            pinned=True,
        )
    }
    new = ExtractedField(
        domain_tag="account.named_insured.fein",
        value="99-9999999",
        source_doc="d2",
        source_page=1,
        source_quote="…",
        confidence=0.95,  # would normally win
        model_used="sonnet-4-6",
        needs_review=False,
    )
    report = MergeReport()
    _merge_repeatable_item(item, new, run_id="r1", report=report)
    rec = item["account.named_insured.fein"]
    assert rec.value == "11-1111111"  # unchanged
    assert rec.pinned is True
    # New value lives in conflicts[] so reconciliation can surface it.
    assert any((c.value == "99-9999999") for c in rec.conflicts)
    assert rec.needs_review is True


def test_ensure_client_insured_in_ni_creates_when_no_match() -> None:
    st = make_state(
        insured_name="Beta Holdings LLC",
        insured={"fein": "55-1234567", "business_type": "Corporation",
                 "street_address": "500 Main St", "city": "Richmond",
                 "state": "VA", "zip_code": "23219"},
        ni_rows=_ni_only("Acme LLC"),  # different entity already present
    )
    created = reconcile.ensure_client_insured_in_ni(st)
    assert created is True
    rows = st.repeatables[reconcile.NAMED_INSURED_GROUP]
    assert len(rows) == 2  # original Acme + new Beta
    new_row = rows[-1]
    # Name set, low confidence, needs_review True, source labeled "Client tab"
    name_rec = new_row["account.named_insured.name"]
    assert name_rec.value == "Beta Holdings LLC"
    assert name_rec.confidence == 0.5
    assert name_rec.needs_review is True
    assert any(s.doc_id == "Client tab" for s in name_rec.source)
    fein_rec = new_row["account.named_insured.fein"]
    assert fein_rec.value == "55-1234567"
    # Other Client tab fields propagated.
    assert new_row["account.named_insured.business_type"].value == "Corporation"
    assert new_row["account.named_insured.address.line_1"].value == "500 Main St"
    # name_type is no longer auto-set on the auto-created row.
    assert "account.named_insured.name_type" not in new_row


def test_ensure_client_insured_in_ni_skips_when_match_exists() -> None:
    """Idempotent: if the Client tab's entity is already an NI row, no-op."""
    st = make_state(
        insured_name="Beta Holdings LLC",
        ni_rows=_ni_only("Beta Holdings LLC"),  # matches by normalize
    )
    created = reconcile.ensure_client_insured_in_ni(st)
    assert created is False
    assert len(st.repeatables[reconcile.NAMED_INSURED_GROUP]) == 1


def test_ensure_client_insured_in_ni_skips_when_blank_name() -> None:
    """No-op when the Client tab Insured Name isn't set."""
    st = make_state(insured_name="", ni_rows=_ni_only("Acme LLC"))
    created = reconcile.ensure_client_insured_in_ni(st)
    assert created is False
    assert len(st.repeatables[reconcile.NAMED_INSURED_GROUP]) == 1


def test_ensure_client_insured_in_ni_propagates_only_populated_fields() -> None:
    """Blank Client tab fields don't create blank FieldRecords."""
    st = make_state(
        insured_name="Gamma Industries LP",
        insured={"city": "Nashville"},  # only city populated
        ni_rows=None,
    )
    created = reconcile.ensure_client_insured_in_ni(st)
    assert created is True
    new_row = st.repeatables[reconcile.NAMED_INSURED_GROUP][0]
    # Name + city only; no fein / business_type / state / zip etc.
    assert "account.named_insured.name" in new_row
    assert "account.named_insured.address.city" in new_row
    assert "account.named_insured.fein" not in new_row
    assert "account.named_insured.business_type" not in new_row
    assert "account.named_insured.name_type" not in new_row


def test_merge_singleton_skips_update_when_pinned() -> None:
    """Pinned singleton FieldRecord is preserved against extraction overwrite."""
    from iga_marketing_master_2.state import (
        ExtractedField,
        MergeReport,
        SourceRef,
        State,
        _merge_singleton,
    )

    st = State(
        client="acme",
        created_at="2026-05-28T12:00:00",
        updated_at="2026-05-28T12:00:00",
    )
    st.fields["insured.fein"] = FieldRecord(
        value="11-1111111", confidence=0.4, pinned=True,
        source=[SourceRef(doc_id="prior", page=1, quote="…")],
    )
    new = ExtractedField(
        domain_tag="insured.fein",
        value="99-9999999",
        source_doc="d2",
        source_page=1,
        source_quote="…",
        confidence=0.95,
        model_used="sonnet-4-6",
        needs_review=False,
    )
    _merge_singleton(st, new, run_id="r1", report=MergeReport())
    rec = st.fields["insured.fein"]
    assert rec.value == "11-1111111"
    assert any(c.value == "99-9999999" for c in rec.conflicts)
    assert rec.needs_review is True

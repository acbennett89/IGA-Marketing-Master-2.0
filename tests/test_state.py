"""tests/test_state.py — unit tests for state.py.

Coverage targets (per build prompt):
1. Schema round-trip
2. Atomic write survives simulated crash mid-write
3. Daily snapshot fires once per day; prunes >30-day-old files
4. Merge with conflict (same domain_tag, two values from two docs) populates conflicts[]
5. Repeatable merge by natural key (vehicles by VIN; appends when VIN missing)
6. Pending pause set/clear/serialize round-trip
7. schema_version honored on read; future versions trigger migration hook
8. Recovery: corrupt state -> .bak; corrupt both -> snapshot; all corrupt -> raise
9. History entry append shape
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from pathlib import Path

import pytest

from iga_marketing_master_2 import state as state_module
from iga_marketing_master_2.state import (
    SNAPSHOT_RETENTION_DAYS,
    STATE_BAK_FILENAME,
    STATE_FILENAME,
    STATE_SCHEMA_VERSION,
    SNAPSHOTS_DIRNAME,
    ConflictCandidate,
    ExtractedField,
    FieldRecord,
    HistoryEntry,
    MergeReport,
    PendingExtraction,
    PendingPause,
    RunHistoryEntry,
    SourceRef,
    State,
    StateCorruptError,
    StateSchemaVersionError,
    append_run_history,
    detect_conflicts,
    get_pending_domain_tag_proposals,
    get_pending_extraction,
    get_pending_pause,
    load,
    merge_extraction,
    natural_key_for,
    new_run_id,
    new_state,
    save_atomic,
    set_pending_domain_tag_proposals,
    set_pending_extraction,
    set_pending_pause,
    take_daily_snapshot,
    write_history_entry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client_path(tmp_path: Path) -> Path:
    """A fresh client directory under tmp_path."""
    cp = tmp_path / "Acme Co"
    cp.mkdir()
    return cp


def _make_extracted(
    *,
    domain_tag: str,
    value: object,
    confidence: float = 0.9,
    source_doc: str = "doc.pdf",
    source_page: int = 1,
    source_quote: str = "verbatim quote",
    model_used: str = "sonnet-4-6",
    needs_review: bool = False,
    repeatable_group: str | None = None,
    repeatable_index: int | None = None,
) -> ExtractedField:
    return ExtractedField(
        domain_tag=domain_tag,
        value=value,  # type: ignore[arg-type]
        source_doc=source_doc,
        source_page=source_page,
        source_quote=source_quote,
        confidence=confidence,
        model_used=model_used,
        needs_review=needs_review,
        repeatable_group=repeatable_group,
        repeatable_index=repeatable_index,
    )


# ---------------------------------------------------------------------------
# 1. Schema round-trip
# ---------------------------------------------------------------------------


def test_round_trip_empty_state(client_path: Path) -> None:
    s = new_state(client_path.name)
    save_atomic(s, client_path)
    reloaded = load(client_path)
    assert reloaded.client == s.client
    assert reloaded.schema_version == STATE_SCHEMA_VERSION
    assert reloaded.fields == {}
    assert reloaded.repeatables == {}
    assert reloaded.pending_pause is None
    assert reloaded.pending_extraction is None
    assert reloaded.run_history == []


def test_round_trip_populated_state(client_path: Path) -> None:
    s = new_state(client_path.name)
    s.fields["account.named_insured"] = FieldRecord(
        value="Acme LLC",
        confidence=0.92,
        status="approved",
        source=[SourceRef(doc_id="dec.pdf", page=1, quote="Named Insured: Acme LLC")],
        conflicts=[],
        history=[
            HistoryEntry(
                run_id="r1",
                ts="2026-04-30T12:00:00+00:00",
                user="andrew",
                actor="extractor",
                action="create",
                prior=None,
                new={"value": "Acme LLC", "status": "pending", "confidence": 0.92},
            )
        ],
        needs_review=False,
        model_used="sonnet-4-6",
    )
    s.repeatables["vehicle"] = [
        {
            "vehicle.year": FieldRecord(value=2022, confidence=0.95),
            "vehicle.vin": FieldRecord(
                value="1HGCM82633A123456", confidence=0.99
            ),
        }
    ]
    s.pending_extraction = PendingExtraction(
        run_id="r2",
        started_at="2026-04-30T13:00:00+00:00",
        pdf_paths=["a.pdf", "b.pdf"],
        completed_pdf_basenames=["a.pdf"],
    )
    save_atomic(s, client_path)
    reloaded = load(client_path)

    assert reloaded.fields["account.named_insured"].value == "Acme LLC"
    assert reloaded.fields["account.named_insured"].status == "approved"
    assert (
        reloaded.fields["account.named_insured"].source[0].quote
        == "Named Insured: Acme LLC"
    )
    assert reloaded.fields["account.named_insured"].history[0].action == "create"

    assert len(reloaded.repeatables["vehicle"]) == 1
    veh = reloaded.repeatables["vehicle"][0]
    assert veh["vehicle.year"].value == 2022
    assert veh["vehicle.vin"].value == "1HGCM82633A123456"

    assert reloaded.pending_extraction is not None
    assert reloaded.pending_extraction.completed_pdf_basenames == ["a.pdf"]


def test_load_missing_returns_fresh_state(client_path: Path) -> None:
    s = load(client_path)
    assert s.client == client_path.name
    assert s.schema_version == STATE_SCHEMA_VERSION
    assert s.fields == {}


# ---------------------------------------------------------------------------
# 2. Atomic write — crash safety
# ---------------------------------------------------------------------------


def test_atomic_write_survives_replace_failure(
    client_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If os.replace raises mid-write, the original state.json must be intact
    AND no orphan tmp file must remain."""
    # First save creates the original.
    s = new_state(client_path.name)
    s.fields["a.b"] = FieldRecord(value="original", confidence=0.8)
    save_atomic(s, client_path)
    original_bytes = (client_path / STATE_FILENAME).read_bytes()

    # Now mutate state and force os.replace to fail.
    s.fields["a.b"] = FieldRecord(value="WOULD-CORRUPT", confidence=0.9)

    real_replace = os.replace

    def boom(_src: str, _dst: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(state_module.os, "replace", boom)

    with pytest.raises(OSError, match="simulated crash"):
        save_atomic(s, client_path)

    # Restore real replace for cleanup.
    monkeypatch.setattr(state_module.os, "replace", real_replace)

    # Original is intact.
    assert (client_path / STATE_FILENAME).read_bytes() == original_bytes
    # No orphan tmp.
    orphans = [
        p for p in client_path.iterdir() if p.name.startswith("state.json.tmp.")
    ]
    assert orphans == []


def test_atomic_write_creates_bak_on_second_save(client_path: Path) -> None:
    s = new_state(client_path.name)
    s.fields["x.y"] = FieldRecord(value="v1", confidence=0.5)
    save_atomic(s, client_path)
    assert not (client_path / STATE_BAK_FILENAME).exists()  # no prior

    s.fields["x.y"] = FieldRecord(value="v2", confidence=0.6)
    save_atomic(s, client_path)
    bak_path = client_path / STATE_BAK_FILENAME
    assert bak_path.exists()
    bak_data = json.loads(bak_path.read_text(encoding="utf-8"))
    # .bak should hold the *prior* state (v1), not the just-saved (v2).
    assert bak_data["fields"]["x.y"]["value"] == "v1"


# ---------------------------------------------------------------------------
# 3. Daily snapshots
# ---------------------------------------------------------------------------


def test_snapshot_fires_once_per_day(client_path: Path) -> None:
    s = new_state(client_path.name)
    save_atomic(s, client_path)
    snap_dir = client_path / SNAPSHOTS_DIRNAME
    assert snap_dir.exists()
    snaps_after_first = list(snap_dir.iterdir())
    assert len(snaps_after_first) == 1

    # Save again same day — no second snapshot.
    s.fields["foo.bar"] = FieldRecord(value="baz", confidence=0.7)
    save_atomic(s, client_path)
    assert len(list(snap_dir.iterdir())) == 1

    # Direct call returns None on same-day re-take.
    assert take_daily_snapshot(client_path) is None


def test_snapshot_prune_drops_stale_files(client_path: Path) -> None:
    """Files older than retention_days must be deleted on save."""
    s = new_state(client_path.name)
    save_atomic(s, client_path)
    snap_dir = client_path / SNAPSHOTS_DIRNAME

    # Forge two stale snapshots (35 days, 100 days old) and one recent (5d).
    today = date.today()
    stale_35 = snap_dir / f"state-{(today - timedelta(days=35)).isoformat()}.json"
    stale_100 = snap_dir / f"state-{(today - timedelta(days=100)).isoformat()}.json"
    recent_5 = snap_dir / f"state-{(today - timedelta(days=5)).isoformat()}.json"
    for p in (stale_35, stale_100, recent_5):
        p.write_text("{}", encoding="utf-8")

    # Trigger another save → prune runs.
    s.fields["k"] = FieldRecord(value="v", confidence=0.8)
    save_atomic(s, client_path)

    remaining = {p.name for p in snap_dir.iterdir()}
    assert stale_35.name not in remaining
    assert stale_100.name not in remaining
    assert recent_5.name in remaining
    # And today's snapshot survives.
    assert f"state-{today.isoformat()}.json" in remaining


def test_snapshot_retention_boundary(client_path: Path) -> None:
    """A snapshot exactly at the retention cutoff should be pruned (strict <)."""
    s = new_state(client_path.name)
    save_atomic(s, client_path)
    snap_dir = client_path / SNAPSHOTS_DIRNAME
    today = date.today()
    boundary = snap_dir / (
        f"state-{(today - timedelta(days=SNAPSHOT_RETENTION_DAYS + 1)).isoformat()}.json"
    )
    boundary.write_text("{}", encoding="utf-8")
    s.fields["k"] = FieldRecord(value="v", confidence=0.5)
    save_atomic(s, client_path)
    assert not boundary.exists()


# ---------------------------------------------------------------------------
# 4. Merge with conflict
# ---------------------------------------------------------------------------


def test_merge_two_docs_creates_conflict(client_path: Path) -> None:
    s = new_state(client_path.name)
    run_id = new_run_id()

    # First doc: aggregate_limit = 1_000_000 (lower confidence)
    r1 = _make_extracted(
        domain_tag="policy.gl.aggregate_limit",
        value=1_000_000,
        confidence=0.7,
        source_doc="schedule.pdf",
        source_page=2,
    )
    # Second doc: aggregate_limit = 2_000_000 (higher confidence)
    r2 = _make_extracted(
        domain_tag="policy.gl.aggregate_limit",
        value=2_000_000,
        confidence=0.95,
        source_doc="renewal-dec.pdf",
        source_page=1,
    )
    rep1 = merge_extraction(s, [r1], run_id=run_id, model_used="sonnet-4-6")
    rep2 = merge_extraction(s, [r2], run_id=run_id, model_used="sonnet-4-6")

    assert rep1.fields_created == 1
    assert rep2.fields_updated == 1
    assert rep2.conflicts_added == 1

    rec = s.fields["policy.gl.aggregate_limit"]
    # Higher-confidence value wins canonical.
    assert rec.value == 2_000_000
    assert rec.confidence == 0.95
    # Loser landed in conflicts[].
    assert len(rec.conflicts) == 1
    assert rec.conflicts[0].value == 1_000_000


def test_merge_lower_confidence_dropped_to_conflicts(client_path: Path) -> None:
    s = new_state(client_path.name)
    run_id = new_run_id()
    high = _make_extracted(
        domain_tag="account.fein", value="12-3456789", confidence=0.95
    )
    low = _make_extracted(
        domain_tag="account.fein", value="98-7654321", confidence=0.6,
        source_doc="other.pdf",
    )
    merge_extraction(s, [high], run_id=run_id, model_used="sonnet-4-6")
    rep = merge_extraction(s, [low], run_id=run_id, model_used="sonnet-4-6")

    assert rep.fields_updated == 0
    assert rep.conflicts_added == 1
    rec = s.fields["account.fein"]
    assert rec.value == "12-3456789"
    assert rec.conflicts[0].value == "98-7654321"


def test_merge_match_appends_source_only(client_path: Path) -> None:
    s = new_state(client_path.name)
    run_id = new_run_id()
    r1 = _make_extracted(
        domain_tag="account.named_insured",
        value="Acme LLC",
        confidence=0.9,
        source_doc="a.pdf",
    )
    r2 = _make_extracted(
        domain_tag="account.named_insured",
        value="acme llc",  # case-insensitive match
        confidence=0.85,
        source_doc="b.pdf",
    )
    merge_extraction(s, [r1], run_id=run_id, model_used="sonnet-4-6")
    rep = merge_extraction(s, [r2], run_id=run_id, model_used="sonnet-4-6")

    rec = s.fields["account.named_insured"]
    assert rec.value == "Acme LLC"
    assert len(rec.source) == 2
    assert rep.conflicts_added == 0


def test_detect_conflicts_no_existing(client_path: Path) -> None:
    s = new_state(client_path.name)
    cand = _make_extracted(domain_tag="x.y", value="z")
    assert detect_conflicts(s, "x.y", cand) == "no_existing"


# ---------------------------------------------------------------------------
# 5. Repeatable merge by natural key
# ---------------------------------------------------------------------------


def test_vehicle_merge_by_vin(client_path: Path) -> None:
    """Two PDFs both list a vehicle with VIN '1FA...' — should merge to one."""
    s = new_state(client_path.name)
    run_id = new_run_id()

    doc_a = [
        _make_extracted(
            domain_tag="vehicle.year",
            value=2020,
            confidence=0.95,
            source_doc="schedule-a.pdf",
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
        _make_extracted(
            domain_tag="vehicle.vin",
            value="1FA6P0HD3K5123456",
            confidence=0.99,
            source_doc="schedule-a.pdf",
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
    ]
    doc_b = [
        # Same VIN, different doc, plus an additional tag.
        _make_extracted(
            domain_tag="vehicle.vin",
            value="1FA6P0HD3K5123456",
            confidence=0.95,
            source_doc="schedule-b.pdf",
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
        _make_extracted(
            domain_tag="vehicle.make",
            value="Ford",
            confidence=0.9,
            source_doc="schedule-b.pdf",
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
    ]
    merge_extraction(s, doc_a, run_id=run_id, model_used="sonnet-4-6")
    rep_b = merge_extraction(s, doc_b, run_id=run_id, model_used="sonnet-4-6")

    # Should have ONE vehicle row, enriched with both VIN/year and make.
    assert len(s.repeatables["vehicle"]) == 1
    veh = s.repeatables["vehicle"][0]
    assert veh["vehicle.vin"].value == "1FA6P0HD3K5123456"
    assert veh["vehicle.year"].value == 2020
    assert veh["vehicle.make"].value == "Ford"
    assert rep_b.repeatable_items_merged == 1


def test_vehicle_merge_different_vins_both_kept(client_path: Path) -> None:
    s = new_state(client_path.name)
    run_id = new_run_id()
    doc = [
        _make_extracted(
            domain_tag="vehicle.vin",
            value="1FA6P0HD3K5111111",
            confidence=0.99,
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
        _make_extracted(
            domain_tag="vehicle.vin",
            value="2HG6P0HD3K5222222",
            confidence=0.99,
            repeatable_group="vehicle",
            repeatable_index=1,
        ),
    ]
    rep = merge_extraction(s, doc, run_id=run_id, model_used="sonnet-4-6")
    assert len(s.repeatables["vehicle"]) == 2
    assert rep.repeatable_items_added == 2
    assert rep.appended_without_key == 0


def test_vehicle_missing_vin_appended_without_merge(client_path: Path) -> None:
    """A vehicle row with no VIN gets appended; subsequent rows without VIN
    are also appended (no merge), and `appended_without_key` is incremented."""
    s = new_state(client_path.name)
    run_id = new_run_id()
    doc = [
        _make_extracted(
            domain_tag="vehicle.year",
            value=2018,
            confidence=0.9,
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
    ]
    rep = merge_extraction(s, doc, run_id=run_id, model_used="sonnet-4-6")
    assert len(s.repeatables["vehicle"]) == 1
    assert rep.appended_without_key == 1
    # A second VIN-less row appends rather than merges.
    doc2 = [
        _make_extracted(
            domain_tag="vehicle.year",
            value=2019,
            confidence=0.9,
            source_doc="other.pdf",
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
    ]
    rep2 = merge_extraction(s, doc2, run_id=run_id, model_used="sonnet-4-6")
    assert len(s.repeatables["vehicle"]) == 2
    assert rep2.appended_without_key == 1


def test_vin_short_treated_as_missing_key(client_path: Path) -> None:
    s = new_state(client_path.name)
    run_id = new_run_id()
    short_vin = "ABC123"
    doc = [
        _make_extracted(
            domain_tag="vehicle.vin",
            value=short_vin,
            confidence=0.9,
            repeatable_group="vehicle",
            repeatable_index=0,
        ),
    ]
    rep = merge_extraction(s, doc, run_id=run_id, model_used="sonnet-4-6")
    assert rep.appended_without_key == 1


def test_loss_payee_merge_by_name_and_address(client_path: Path) -> None:
    s = new_state(client_path.name)
    run_id = new_run_id()
    doc_a = [
        _make_extracted(
            domain_tag="loss_payee.name",
            value="Bank of XYZ",
            confidence=0.9,
            repeatable_group="loss_payee",
            repeatable_index=0,
        ),
        _make_extracted(
            domain_tag="loss_payee.address.line1",
            value="100 Main St",
            confidence=0.9,
            repeatable_group="loss_payee",
            repeatable_index=0,
        ),
    ]
    doc_b = [
        _make_extracted(
            domain_tag="loss_payee.name",
            value="bank of xyz",  # different case, same entity
            confidence=0.92,
            source_doc="other.pdf",
            repeatable_group="loss_payee",
            repeatable_index=0,
        ),
        _make_extracted(
            domain_tag="loss_payee.address.line1",
            value="100 Main St",
            confidence=0.9,
            source_doc="other.pdf",
            repeatable_group="loss_payee",
            repeatable_index=0,
        ),
    ]
    merge_extraction(s, doc_a, run_id=run_id, model_used="sonnet-4-6")
    merge_extraction(s, doc_b, run_id=run_id, model_used="sonnet-4-6")
    assert len(s.repeatables["loss_payee"]) == 1


def test_natural_key_for_unknown_group_returns_none() -> None:
    item = {"foo.bar": FieldRecord(value="x", confidence=0.5)}
    assert natural_key_for("unknown_group", item) is None


def test_repeatable_namespace_mismatch_raises(client_path: Path) -> None:
    s = new_state(client_path.name)
    bad = _make_extracted(
        domain_tag="vehicle.year",
        value=2020,
        confidence=0.9,
        repeatable_group="driver",  # mismatch!
        repeatable_index=0,
    )
    with pytest.raises(state_module.StateMergeError):
        merge_extraction(s, [bad], run_id="r", model_used="sonnet-4-6")


# ---------------------------------------------------------------------------
# 6. Pending-pause set/clear/serialize
# ---------------------------------------------------------------------------


def test_pending_pause_set_clear_round_trip(client_path: Path) -> None:
    s = new_state(client_path.name)
    pause = PendingPause(
        run_id="run-1",
        paused_at="2026-04-30T15:00:00+00:00",
        domain_tag="vehicle.vin",
        screen_code="AUTOLOB",
        reason_code="validation_rejected",
        reason_message="EPIC didn't accept VIN",
        technical_detail="locator: input.vinNum; got 'XYZ'",
        repeatable_group="vehicle",
        repeatable_index=2,
    )
    set_pending_pause(s, pause)
    assert get_pending_pause(s) is not None
    save_atomic(s, client_path)

    reloaded = load(client_path)
    assert reloaded.pending_pause is not None
    assert reloaded.pending_pause.domain_tag == "vehicle.vin"
    assert reloaded.pending_pause.repeatable_index == 2
    assert reloaded.pending_pause.reason_code == "validation_rejected"

    set_pending_pause(reloaded, None)
    save_atomic(reloaded, client_path)

    re2 = load(client_path)
    assert re2.pending_pause is None


def test_pending_extraction_round_trip(client_path: Path) -> None:
    s = new_state(client_path.name)
    pe = PendingExtraction(
        run_id="run-2",
        started_at="2026-04-30T16:00:00+00:00",
        pdf_paths=["a.pdf", "b.pdf", "c.pdf"],
        completed_pdf_basenames=["a.pdf"],
        notes="resumed once",
    )
    set_pending_extraction(s, pe)
    save_atomic(s, client_path)

    re_s = load(client_path)
    assert get_pending_extraction(re_s) is not None
    assert re_s.pending_extraction is not None
    assert re_s.pending_extraction.completed_pdf_basenames == ["a.pdf"]
    assert re_s.pending_extraction.pdf_paths == ["a.pdf", "b.pdf", "c.pdf"]


# ---------------------------------------------------------------------------
# 7. schema_version
# ---------------------------------------------------------------------------


def test_schema_version_future_raises(client_path: Path) -> None:
    """A state.json declaring schema_version higher than this build refuses."""
    bogus = {
        "schema_version": STATE_SCHEMA_VERSION + 5,
        "client": client_path.name,
        "created_at": "2026-04-30T00:00:00+00:00",
        "updated_at": "2026-04-30T00:00:00+00:00",
        "run_history": [],
        "fields": {},
        "repeatables": {},
        "pending_pause": None,
        "pending_extraction": None,
    }
    (client_path / STATE_FILENAME).write_text(
        json.dumps(bogus), encoding="utf-8"
    )
    with pytest.raises(StateSchemaVersionError):
        load(client_path)


def test_schema_version_legacy_no_field_assumed_v1(client_path: Path) -> None:
    """Pre-versioned files (no schema_version key) load as v1."""
    legacy = {
        "client": client_path.name,
        "created_at": "2026-04-30T00:00:00+00:00",
        "updated_at": "2026-04-30T00:00:00+00:00",
        "fields": {},
        "repeatables": {},
        "run_history": [],
    }
    (client_path / STATE_FILENAME).write_text(
        json.dumps(legacy), encoding="utf-8"
    )
    s = load(client_path)
    assert s.schema_version == STATE_SCHEMA_VERSION


def test_schema_version_corrupt_type_raises(client_path: Path) -> None:
    """A non-int schema_version is corruption, not a migration target."""
    bogus = {
        "schema_version": "one",
        "client": client_path.name,
        "created_at": "x",
        "updated_at": "x",
        "fields": {},
        "repeatables": {},
        "run_history": [],
    }
    (client_path / STATE_FILENAME).write_text(
        json.dumps(bogus), encoding="utf-8"
    )
    # First read corrupts; .bak is missing; snapshots missing -> StateCorruptError.
    with pytest.raises(StateCorruptError):
        load(client_path)


# ---------------------------------------------------------------------------
# 8. Recovery chain
# ---------------------------------------------------------------------------


def test_recovery_corrupt_state_falls_back_to_bak(client_path: Path) -> None:
    s = new_state(client_path.name)
    s.fields["a.b"] = FieldRecord(value="from-bak", confidence=0.9)
    save_atomic(s, client_path)
    # Second save creates .bak holding the first save.
    s.fields["a.b"] = FieldRecord(value="from-current", confidence=0.95)
    save_atomic(s, client_path)
    # Corrupt the current state.json.
    (client_path / STATE_FILENAME).write_text("{ this is not json", encoding="utf-8")

    reloaded = load(client_path)
    # We should have fallen back to .bak which holds 'from-bak' (the first save).
    assert reloaded.fields["a.b"].value == "from-bak"


def test_recovery_corrupt_both_falls_back_to_snapshot(client_path: Path) -> None:
    s = new_state(client_path.name)
    s.fields["k"] = FieldRecord(value="snapshot-content", confidence=0.9)
    save_atomic(s, client_path)
    # Snapshot is now on disk holding 'snapshot-content'.

    # Corrupt the live state.json AND the .bak (if any).
    (client_path / STATE_FILENAME).write_text("garbage", encoding="utf-8")
    bak_path = client_path / STATE_BAK_FILENAME
    if bak_path.exists():
        bak_path.write_text("also garbage", encoding="utf-8")

    reloaded = load(client_path)
    assert reloaded.fields["k"].value == "snapshot-content"


def test_recovery_all_corrupt_raises(client_path: Path) -> None:
    s = new_state(client_path.name)
    save_atomic(s, client_path)

    # Corrupt every readable JSON file in the client tree.
    (client_path / STATE_FILENAME).write_text("garbage", encoding="utf-8")
    bak_path = client_path / STATE_BAK_FILENAME
    bak_path.write_text("garbage", encoding="utf-8")
    snap_dir = client_path / SNAPSHOTS_DIRNAME
    for entry in snap_dir.iterdir():
        if entry.is_file():
            entry.write_text("garbage", encoding="utf-8")

    with pytest.raises(StateCorruptError):
        load(client_path)


# ---------------------------------------------------------------------------
# 9. History entry append
# ---------------------------------------------------------------------------


def test_write_history_entry_appends(client_path: Path) -> None:
    s = new_state(client_path.name)
    s.fields["x.y"] = FieldRecord(value="v", confidence=0.5)
    write_history_entry(
        s,
        domain_tag="x.y",
        run_id="r",
        user="andrew",
        actor="gui",
        action="approve",
        prior={"value": "v", "status": "pending", "confidence": 0.5},
        new={"value": "v", "status": "approved", "confidence": 1.0},
    )
    rec = s.fields["x.y"]
    assert len(rec.history) == 1
    assert rec.history[0].action == "approve"
    assert rec.history[0].user == "andrew"
    assert rec.history[0].actor == "gui"


def test_write_history_entry_unknown_field_raises(client_path: Path) -> None:
    s = new_state(client_path.name)
    with pytest.raises(state_module.StateMergeError):
        write_history_entry(
            s,
            domain_tag="missing.tag",
            run_id="r",
            user="andrew",
            actor="gui",
            action="approve",
            prior=None,
            new={"value": "v", "status": "approved", "confidence": 1.0},
        )


def test_run_history_append_and_persist(client_path: Path) -> None:
    s = new_state(client_path.name)
    entry = RunHistoryEntry(
        run_id="r1",
        ts="2026-04-30T17:00:00+00:00",
        user="andrew",
        kind="extraction",
        inputs=["doc.pdf"],
        outcome="completed",
        model_used="mixed",
        forced_opus=False,
    )
    append_run_history(s, entry)
    save_atomic(s, client_path)
    re_s = load(client_path)
    assert len(re_s.run_history) == 1
    assert re_s.run_history[0].kind == "extraction"
    assert re_s.run_history[0].inputs == ["doc.pdf"]
    assert re_s.run_history[0].outcome == "completed"


# ---------------------------------------------------------------------------
# Bonus — sanity on _values_match equality nuances
# ---------------------------------------------------------------------------


def test_merge_singleton_create_logs_creation(client_path: Path) -> None:
    s = new_state(client_path.name)
    rep = merge_extraction(
        s,
        [_make_extracted(domain_tag="submission.name", value="Acme Renewal")],
        run_id="r",
        model_used="sonnet-4-6",
    )
    assert rep.fields_created == 1
    assert s.fields["submission.name"].value == "Acme Renewal"
    assert s.fields["submission.name"].history[0].action == "create"


def test_merge_does_not_save_to_disk(client_path: Path) -> None:
    """Per contract, merge_extraction MUTATES state but does NOT persist."""
    s = new_state(client_path.name)
    merge_extraction(
        s,
        [_make_extracted(domain_tag="x.y", value="z")],
        run_id="r",
        model_used="sonnet-4-6",
    )
    assert not (client_path / STATE_FILENAME).exists()


# ---------------------------------------------------------------------------
# Pending domain_tag proposals (Issue #5 — JIT crash-safe resume)
# ---------------------------------------------------------------------------


def _make_proposal(
    *,
    proposed_tag: str = "endorsement.cyber_liability",
    sample_value: object = "Yes",
    source_doc: str = "renewal.pdf",
    source_page: int = 7,
    source_quote: str = "Cyber Liability: Yes",
    confidence: float = 0.88,
    run_id: str = "r-jit",
) -> dict:
    """Build a JIT domain_tag proposal dict (mirrors extract._proposal_to_dict)."""
    return {
        "proposed_tag": proposed_tag,
        "sample_value": sample_value,
        "source_doc": source_doc,
        "source_page": source_page,
        "source_quote": source_quote,
        "confidence": confidence,
        "run_id": run_id,
        "repeatable_group": None,
        "repeatable_index": None,
    }


def test_pending_domain_tag_proposals_default_empty(client_path: Path) -> None:
    """Fresh State has an empty proposals list; round-trips empty."""
    s = new_state(client_path.name)
    assert get_pending_domain_tag_proposals(s) == []
    save_atomic(s, client_path)
    reloaded = load(client_path)
    assert reloaded.pending_domain_tag_proposals == []


def test_pending_domain_tag_proposals_set_and_round_trip(client_path: Path) -> None:
    """Set proposals, save, reload — proposals survive disk round-trip."""
    s = new_state(client_path.name)
    proposals = [
        _make_proposal(proposed_tag="endorsement.cyber_liability"),
        _make_proposal(
            proposed_tag="surcharge.minimum_premium",
            sample_value=250,
            confidence=0.74,
        ),
    ]
    set_pending_domain_tag_proposals(s, proposals)
    assert get_pending_domain_tag_proposals(s) == proposals
    save_atomic(s, client_path)

    reloaded = load(client_path)
    assert len(reloaded.pending_domain_tag_proposals) == 2
    assert (
        reloaded.pending_domain_tag_proposals[0]["proposed_tag"]
        == "endorsement.cyber_liability"
    )
    assert reloaded.pending_domain_tag_proposals[1]["sample_value"] == 250
    assert reloaded.pending_domain_tag_proposals[1]["confidence"] == 0.74


def test_pending_domain_tag_proposals_clear(client_path: Path) -> None:
    """Setting an empty list clears prior proposals across save/load."""
    s = new_state(client_path.name)
    set_pending_domain_tag_proposals(s, [_make_proposal()])
    save_atomic(s, client_path)
    reloaded = load(client_path)
    assert len(reloaded.pending_domain_tag_proposals) == 1

    set_pending_domain_tag_proposals(reloaded, [])
    save_atomic(reloaded, client_path)
    re2 = load(client_path)
    assert re2.pending_domain_tag_proposals == []


def test_pending_domain_tag_proposals_survives_merge_extraction(
    client_path: Path,
) -> None:
    """merge_extraction must not touch pending_domain_tag_proposals."""
    s = new_state(client_path.name)
    proposals = [_make_proposal()]
    set_pending_domain_tag_proposals(s, proposals)

    merge_extraction(
        s,
        [_make_extracted(domain_tag="account.named_insured", value="Acme LLC")],
        run_id="r-after",
        model_used="sonnet-4-6",
    )

    # Pending proposals untouched in memory.
    assert s.pending_domain_tag_proposals == proposals

    # And after save/reload.
    save_atomic(s, client_path)
    reloaded = load(client_path)
    assert len(reloaded.pending_domain_tag_proposals) == 1
    assert (
        reloaded.pending_domain_tag_proposals[0]["proposed_tag"]
        == "endorsement.cyber_liability"
    )
    # And the merged field landed.
    assert reloaded.fields["account.named_insured"].value == "Acme LLC"


def test_pending_domain_tag_proposals_setter_copies_input(client_path: Path) -> None:
    """Mutating the caller's list after the setter must not affect state."""
    s = new_state(client_path.name)
    src = [_make_proposal()]
    set_pending_domain_tag_proposals(s, src)
    src.append(_make_proposal(proposed_tag="endorsement.flood"))
    assert len(s.pending_domain_tag_proposals) == 1

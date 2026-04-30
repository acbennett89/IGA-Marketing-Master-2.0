"""Tests for `iga_marketing_master_2.extract`.

These tests mock the `state`, `field_map`, and `claude_client` modules
because they are sibling agent stubs at this build phase. The extraction
agent's contract (per ARCHITECTURE.md §§ 4-6) is verified at the call
boundary, not against real implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from iga_marketing_master_2 import claude_client as claude_client_mod
from iga_marketing_master_2 import config, extract
from iga_marketing_master_2 import field_map as field_map_mod
from iga_marketing_master_2 import state as state_mod


# -------------------------------------------------------------------------
# Fakes — the minimal surface area extract.py touches on its dependencies.
# -------------------------------------------------------------------------


@dataclass
class FakeExtractedField:
    """Mirrors claude_client.ExtractedField fields used by extract.py."""

    domain_tag: str
    value: Any
    source_doc: str
    source_page: int
    source_quote: str
    confidence: float
    needs_review: bool = False
    repeatable_group: str | None = None
    repeatable_index: int | None = None
    model_used: str = "sonnet-4-6"


@dataclass
class FakePendingExtraction:
    run_id: str
    started_at: str
    pdf_paths: list[str]
    completed_pdf_basenames: list[str]
    notes: str | None = None


@dataclass
class FakeRunHistoryEntry:
    run_id: str
    ts: str
    user: str
    kind: str
    inputs: list[str]
    outcome: str
    model_used: str | None = None
    forced_opus: bool = False
    notes: str | None = None


@dataclass
class FakeMergeReport:
    fields_created: int = 0
    fields_updated: int = 0
    conflicts_added: int = 0
    repeatable_items_added: int = 0
    domain_tag_proposals: list[Any] = field(default_factory=list)


class FakeFieldMap:
    """Stand-in for field_map.FieldMap with a settable enum."""

    def __init__(self, known_tags: list[str]) -> None:
        self.known_tags = list(known_tags)


class FakeClaudeError(Exception):
    """Stand-in for claude_client.ClaudeError."""


class FakeState:
    """Minimal `state` object the extraction agent reads/writes."""

    def __init__(self, client_name: str) -> None:
        self.client = client_name
        self.fields: dict[str, dict[str, Any]] = {}
        self.repeatables: dict[str, list[Any]] = {}
        self.run_history: list[FakeRunHistoryEntry] = []
        self.pending_extraction: FakePendingExtraction | None = None
        self.pending_domain_tag_proposals: list[Any] = []
        self.save_call_count: int = 0
        self.merge_calls: list[
            tuple[list[FakeExtractedField], str, str]
        ] = []


# -------------------------------------------------------------------------
# Fixtures
# -------------------------------------------------------------------------


@pytest.fixture()
def working_library(tmp_path: Path) -> Path:
    wl = tmp_path / "Working Library"
    wl.mkdir()
    return wl


@pytest.fixture()
def settings(working_library: Path, tmp_path: Path) -> config.Settings:
    return config.Settings(
        working_library=working_library,
        user_config_dir=tmp_path / "config",
        user_data_dir=tmp_path / "data",
        playwright_profile=tmp_path / "data" / "playwright-profile",
        field_map_path=tmp_path / "Library" / "Epic Field Map.json",
        log_dir=tmp_path / "data" / "logs",
    )


@pytest.fixture()
def fake_pdfs(tmp_path: Path) -> list[Path]:
    pdfs = []
    for name in ("renewal-dec.pdf", "current-schedule.pdf"):
        p = tmp_path / name
        p.write_bytes(b"%PDF-1.4 fake bytes for tests")
        pdfs.append(p)
    return pdfs


def _wire_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fake_state: FakeState,
    fake_fm: FakeFieldMap,
    extract_returns: list[Any],
    merge_reports: list[FakeMergeReport] | None = None,
) -> dict[str, Any]:
    """Patch state / field_map / claude_client to use FakeState etc.

    Returns a small dict of recorded calls so individual tests can
    introspect what extract.py did.
    """
    recorder: dict[str, Any] = {
        "save_atomic_calls": 0,
        "merge_calls": [],
        "extract_pdf_calls": [],
        "appended_history": [],
    }

    # field_map
    monkeypatch.setattr(field_map_mod, "load", lambda: fake_fm, raising=False)
    monkeypatch.setattr(
        field_map_mod,
        "generate_domain_tag_enum",
        lambda fm: list(fm.known_tags),
        raising=False,
    )

    # state — types
    monkeypatch.setattr(
        state_mod, "PendingExtraction", FakePendingExtraction, raising=False
    )
    monkeypatch.setattr(
        state_mod, "RunHistoryEntry", FakeRunHistoryEntry, raising=False
    )

    # state — io
    def _load(client_path: Path) -> FakeState:
        recorder["loaded_client_path"] = client_path
        return fake_state

    def _save(state_obj: FakeState, client_path: Path) -> None:
        recorder["save_atomic_calls"] += 1
        recorder.setdefault("save_paths", []).append(client_path)
        # Snapshot the pending state at each save for ordering checks.
        recorder.setdefault("pending_at_save", []).append(
            None
            if state_obj.pending_extraction is None
            else (
                state_obj.pending_extraction.run_id,
                list(state_obj.pending_extraction.completed_pdf_basenames),
            )
        )

    def _get_pending(s: FakeState) -> FakePendingExtraction | None:
        return s.pending_extraction

    def _set_pending(s: FakeState, p: FakePendingExtraction | None) -> None:
        s.pending_extraction = p

    def _set_proposals(s: FakeState, proposals: list[Any]) -> None:
        s.pending_domain_tag_proposals = list(proposals)

    def _merge(
        s: FakeState,
        records: list[Any],
        run_id: str,
        model_used: str,
    ) -> FakeMergeReport:
        s.merge_calls.append((list(records), run_id, model_used))
        recorder["merge_calls"].append((list(records), run_id, model_used))
        if merge_reports is not None and len(recorder["merge_calls"]) <= len(
            merge_reports
        ):
            return merge_reports[len(recorder["merge_calls"]) - 1]
        # Default: pretend every record is a new field.
        return FakeMergeReport(fields_created=len(records))

    def _append_history(s: FakeState, entry: FakeRunHistoryEntry) -> None:
        s.run_history.append(entry)
        recorder["appended_history"].append(entry)

    monkeypatch.setattr(state_mod, "load", _load, raising=False)
    monkeypatch.setattr(state_mod, "save_atomic", _save, raising=False)
    monkeypatch.setattr(
        state_mod, "get_pending_extraction", _get_pending, raising=False
    )
    monkeypatch.setattr(
        state_mod, "set_pending_extraction", _set_pending, raising=False
    )
    monkeypatch.setattr(
        state_mod,
        "set_pending_domain_tag_proposals",
        _set_proposals,
        raising=False,
    )
    monkeypatch.setattr(state_mod, "merge_extraction", _merge, raising=False)
    monkeypatch.setattr(
        state_mod, "append_run_history", _append_history, raising=False
    )

    # claude_client (ClaudeError already exists; override to our local class so
    # `isinstance` checks in tests stay simple)
    monkeypatch.setattr(
        claude_client_mod, "ClaudeError", FakeClaudeError, raising=False
    )

    call_iter = iter(extract_returns)

    def _extract_from_pdf(
        pdf_path: Path,
        fm: Any,
        glossary: str,
        system_prompt: str,
        *,
        force_opus: bool,
        run_id: str,
        debug_dir: Path | None,
    ) -> Any:
        recorder["extract_pdf_calls"].append(
            {
                "pdf_path": pdf_path,
                "force_opus": force_opus,
                "run_id": run_id,
                "debug_dir": debug_dir,
            }
        )
        try:
            return next(call_iter)
        except StopIteration:
            return []

    monkeypatch.setattr(
        claude_client_mod, "extract_from_pdf", _extract_from_pdf, raising=False
    )

    return recorder


# -------------------------------------------------------------------------
# Tests
# -------------------------------------------------------------------------


def test_run_extraction_happy_path_merges_known_tags(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    fake_state = FakeState("Bobby Luttrell & Sons")
    fake_fm = FakeFieldMap(["account.named_insured", "submission.name"])
    doc1_records = [
        FakeExtractedField(
            domain_tag="account.named_insured",
            value="Bobby Luttrell & Sons LLC",
            source_doc="renewal-dec.pdf",
            source_page=1,
            source_quote="Named Insured: Bobby Luttrell & Sons LLC",
            confidence=0.97,
        ),
        FakeExtractedField(
            domain_tag="submission.name",
            value="Bobby Luttrell & Sons - 2026 Renewal",
            source_doc="renewal-dec.pdf",
            source_page=1,
            source_quote="Submission: Bobby Luttrell & Sons",
            confidence=0.93,
        ),
    ]
    doc2_records = [
        FakeExtractedField(
            domain_tag="account.named_insured",
            value="Bobby Luttrell & Sons LLC",
            source_doc="current-schedule.pdf",
            source_page=1,
            source_quote="Insured: Bobby Luttrell & Sons LLC",
            confidence=0.95,
        ),
    ]
    recorder = _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[doc1_records, doc2_records],
    )

    result = extract.run_extraction(
        "Bobby Luttrell & Sons",
        fake_pdfs,
        settings=settings,
    )

    assert result.outcome == "completed"
    assert result.pdf_count == 2
    assert result.fields_extracted == 3
    assert result.unknown_tags_queued == 0
    assert result.malformed_tags_dropped == 0
    assert result.error_message is None
    assert len(recorder["extract_pdf_calls"]) == 2
    assert len(recorder["merge_calls"]) == 2
    # Merge received only known-tag records.
    for merged_records, _run_id, model in recorder["merge_calls"]:
        assert all(
            r.domain_tag in fake_fm.known_tags for r in merged_records
        )
        assert model == "sonnet-4-6"
    # Pending block was set then cleared.
    assert fake_state.pending_extraction is None
    assert recorder["save_atomic_calls"] >= 3  # initial + per-doc + final
    # run_history appended exactly once with outcome=completed.
    assert len(fake_state.run_history) == 1
    assert fake_state.run_history[0].outcome == "completed"
    assert fake_state.run_history[0].kind == "extraction"
    assert fake_state.run_history[0].inputs == [
        "renewal-dec.pdf",
        "current-schedule.pdf",
    ]


def test_conflict_count_surfaces_in_result(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """Two docs return different submission.name values; the merge report
    records a conflict; ExtractionResult.conflicts_surfaced reflects it.
    """
    fake_state = FakeState("Bobby Luttrell & Sons")
    fake_fm = FakeFieldMap(["submission.name"])

    doc1 = [
        FakeExtractedField(
            domain_tag="submission.name",
            value="Bobby Luttrell & Sons - 2026",
            source_doc="renewal-dec.pdf",
            source_page=1,
            source_quote="Submission: Bobby Luttrell & Sons - 2026",
            confidence=0.95,
        )
    ]
    doc2 = [
        FakeExtractedField(
            domain_tag="submission.name",
            value="Bobby Luttrell & Sons LLC - Renewal",
            source_doc="current-schedule.pdf",
            source_page=1,
            source_quote="Submission: Bobby Luttrell & Sons LLC - Renewal",
            confidence=0.88,
        )
    ]
    # First doc creates the field; second doc detects a conflict.
    reports = [
        FakeMergeReport(fields_created=1),
        FakeMergeReport(fields_updated=0, conflicts_added=1),
    ]
    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[doc1, doc2],
        merge_reports=reports,
    )

    result = extract.run_extraction(
        "Bobby Luttrell & Sons",
        fake_pdfs,
        settings=settings,
    )

    assert result.conflicts_surfaced == 1
    assert result.per_doc[0].conflicts_added == 0
    assert result.per_doc[1].conflicts_added == 1


def test_jit_unknown_tag_is_queued_not_written(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """A well-formed `domain_tag` not in the Field Map enum becomes a
    pending proposal. Field Map functions are NOT called.
    """
    fake_state = FakeState("Bobby Luttrell & Sons")
    fake_fm = FakeFieldMap(["account.named_insured"])

    doc1 = [
        FakeExtractedField(
            domain_tag="account.named_insured",
            value="Bobby Luttrell & Sons LLC",
            source_doc="renewal-dec.pdf",
            source_page=1,
            source_quote="Named Insured: Bobby Luttrell & Sons LLC",
            confidence=0.97,
        ),
        FakeExtractedField(
            domain_tag="loss.adjuster_name",  # NEW — not in enum
            value="Jane Q. Adjuster",
            source_doc="renewal-dec.pdf",
            source_page=4,
            source_quote="Adjuster: Jane Q. Adjuster",
            confidence=0.91,
        ),
    ]
    doc2: list[FakeExtractedField] = []

    # Sentinels to detect any forbidden field_map mutation.
    forbidden_calls: list[str] = []

    def _forbidden_update(*args: Any, **kwargs: Any) -> None:
        forbidden_calls.append("update_field")

    def _forbidden_save(*args: Any, **kwargs: Any) -> None:
        forbidden_calls.append("save_atomic")

    monkeypatch.setattr(
        field_map_mod, "update_field", _forbidden_update, raising=False
    )
    monkeypatch.setattr(
        field_map_mod, "save_atomic", _forbidden_save, raising=False
    )

    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[doc1, doc2],
    )

    result = extract.run_extraction(
        "Bobby Luttrell & Sons",
        fake_pdfs,
        settings=settings,
    )

    assert forbidden_calls == [], (
        "extract.py must NEVER call field_map.update_field or save_atomic"
    )
    assert result.unknown_tags_queued == 1
    assert len(result.pending_proposals) == 1
    proposal = result.pending_proposals[0]
    assert proposal.proposed_tag == "loss.adjuster_name"
    assert proposal.sample_value == "Jane Q. Adjuster"
    assert proposal.run_id == result.run_id
    # State block carries proposals so a crash before GUI handoff is recoverable.
    assert len(fake_state.pending_domain_tag_proposals) == 1
    # Known tag still merged.
    merged_records = fake_state.merge_calls[0][0]
    assert len(merged_records) == 1
    assert merged_records[0].domain_tag == "account.named_insured"


def test_malformed_tag_is_recovered_as_jit_proposal(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """A malformed tag (uppercase, no dot) is recovered: rewritten into
    valid grammar AND queued as a JIT proposal with ``reformatted_from``
    set so the operator can confirm or rename in the GUI.

    The ``malformed_tags_dropped`` counter still ticks (it's a diagnostic
    counter for the bootstrap-state recovery rate), but the data is no
    longer lost — it's surfaced for human review instead.
    """
    fake_state = FakeState("Bobby")
    fake_fm = FakeFieldMap(["account.named_insured"])

    doc1 = [
        FakeExtractedField(
            domain_tag="Submission Name",  # malformed — uppercase + space
            value="X",
            source_doc="renewal-dec.pdf",
            source_page=1,
            source_quote="...",
            confidence=0.5,
        ),
        FakeExtractedField(
            domain_tag="no_dot_at_all",  # malformed — no namespace separator
            value="Y",
            source_doc="renewal-dec.pdf",
            source_page=2,
            source_quote="...",
            confidence=0.5,
        ),
        FakeExtractedField(
            domain_tag="account.named_insured",
            value="Bobby Luttrell & Sons LLC",
            source_doc="renewal-dec.pdf",
            source_page=1,
            source_quote="Named Insured: Bobby Luttrell & Sons LLC",
            confidence=0.97,
        ),
    ]
    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[doc1, []],
    )

    result = extract.run_extraction("Bobby", fake_pdfs, settings=settings)

    # The recovery counter still bumps so diagnostics can show the
    # bootstrap-state recovery rate.
    assert result.malformed_tags_dropped == 2
    # But the values are now queued as proposals instead of dropped.
    assert result.unknown_tags_queued == 2
    assert result.fields_extracted == 1
    # Proposals carry reformatted_from so the GUI knows to flag them.
    reformatted = [p for p in result.pending_proposals if p.reformatted_from]
    assert len(reformatted) == 2
    assert {p.reformatted_from for p in reformatted} == {
        "Submission Name",
        "no_dot_at_all",
    }
    # Rewritten tags are valid dotted grammar.
    for p in reformatted:
        assert "." in p.proposed_tag
        assert p.proposed_tag == p.proposed_tag.lower()


def test_pending_extraction_block_persists_across_doc_loop(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """After each PDF, the pending block records the completed basename
    and is durably saved."""
    fake_state = FakeState("Bobby")
    fake_fm = FakeFieldMap(["account.named_insured"])
    rec = FakeExtractedField(
        domain_tag="account.named_insured",
        value="X",
        source_doc="renewal-dec.pdf",
        source_page=1,
        source_quote="...",
        confidence=0.9,
    )
    recorder = _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[[rec], [rec]],
    )

    extract.run_extraction("Bobby", fake_pdfs, settings=settings)

    # Inspect the recorded sequence of pending blocks at each save.
    pending_at_save = recorder["pending_at_save"]
    # First save: pending set, completed empty.
    assert pending_at_save[0] is not None
    assert pending_at_save[0][1] == []
    # Some intermediate save records "renewal-dec.pdf" as completed.
    saw_first_done = any(
        snap is not None and snap[1] == ["renewal-dec.pdf"]
        for snap in pending_at_save
    )
    assert saw_first_done
    # Final save: pending cleared.
    assert pending_at_save[-1] is None


def test_pending_extraction_detected_on_resume_without_run_id(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """If state.pending_extraction is non-None and the caller doesn't
    pass resume_run_id, run_extraction raises PendingExtractionDetectedError.
    """
    fake_state = FakeState("Bobby")
    fake_state.pending_extraction = FakePendingExtraction(
        run_id="prior-run-uuid",
        started_at="2026-04-29T20:00:00+00:00",
        pdf_paths=[str(p) for p in fake_pdfs],
        completed_pdf_basenames=["renewal-dec.pdf"],
    )
    fake_fm = FakeFieldMap(["account.named_insured"])
    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[],
    )

    with pytest.raises(extract.PendingExtractionDetectedError) as exc_info:
        extract.run_extraction("Bobby", fake_pdfs, settings=settings)

    assert exc_info.value.pending.run_id == "prior-run-uuid"
    # Claude was NOT called.
    assert fake_state.merge_calls == []


def test_resume_with_matching_run_id_skips_completed(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """Passing resume_run_id matching the pending block's run_id continues
    where the previous run left off; already-completed PDFs are skipped."""
    fake_state = FakeState("Bobby")
    fake_state.pending_extraction = FakePendingExtraction(
        run_id="prior-run-uuid",
        started_at="2026-04-29T20:00:00+00:00",
        pdf_paths=[str(p) for p in fake_pdfs],
        completed_pdf_basenames=["renewal-dec.pdf"],
    )
    fake_fm = FakeFieldMap(["account.named_insured"])
    rec = FakeExtractedField(
        domain_tag="account.named_insured",
        value="X",
        source_doc="current-schedule.pdf",
        source_page=1,
        source_quote="...",
        confidence=0.9,
    )
    recorder = _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[[rec]],  # only one Claude call expected
    )

    result = extract.run_extraction(
        "Bobby",
        fake_pdfs,
        settings=settings,
        resume_run_id="prior-run-uuid",
    )

    assert result.run_id == "prior-run-uuid"
    assert result.outcome == "completed"
    # Only the un-completed PDF was extracted.
    extracted_pdfs = [
        c["pdf_path"].name for c in recorder["extract_pdf_calls"]
    ]
    assert extracted_pdfs == ["current-schedule.pdf"]


def test_resume_extraction_clear_drops_pending_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake_state = FakeState("Bobby")
    fake_state.pending_extraction = FakePendingExtraction(
        run_id="prior-run-uuid",
        started_at="2026-04-29T20:00:00+00:00",
        pdf_paths=[],
        completed_pdf_basenames=[],
    )
    monkeypatch.setattr(
        state_mod, "PendingExtraction", FakePendingExtraction, raising=False
    )
    monkeypatch.setattr(state_mod, "load", lambda p: fake_state, raising=False)
    saves: list[Path] = []
    monkeypatch.setattr(
        state_mod,
        "save_atomic",
        lambda s, p: saves.append(p),
        raising=False,
    )
    monkeypatch.setattr(
        state_mod,
        "set_pending_extraction",
        lambda s, p: setattr(s, "pending_extraction", p),
        raising=False,
    )

    extract.resume_extraction_clear(tmp_path / "Bobby")

    assert fake_state.pending_extraction is None
    assert saves == [tmp_path / "Bobby"]


def test_duplicate_basename_guard_raises_before_api_call(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    tmp_path: Path,
) -> None:
    """Two PDFs with the same basename in different folders trigger the
    guard before any state or claude_client work happens."""
    sub_a = tmp_path / "A"
    sub_b = tmp_path / "B"
    sub_a.mkdir()
    sub_b.mkdir()
    p1 = sub_a / "renewal-dec.pdf"
    p2 = sub_b / "renewal-dec.pdf"
    p1.write_bytes(b"%PDF-fake-A")
    p2.write_bytes(b"%PDF-fake-B")

    # Tripwire: state.load must not even be invoked.
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "state.load must not be called before duplicate-basename guard"
        )

    monkeypatch.setattr(state_mod, "load", _boom, raising=False)

    with pytest.raises(extract.DuplicatePdfBasenameError) as exc_info:
        extract.run_extraction("Bobby", [p1, p2], settings=settings)

    assert exc_info.value.basename == "renewal-dec.pdf"
    assert set(exc_info.value.paths) == {p1, p2}


def test_empty_pdf_list_raises_input_error(
    settings: config.Settings,
) -> None:
    with pytest.raises(extract.ExtractionInputError):
        extract.run_extraction("Bobby", [], settings=settings)


def test_missing_pdf_raises_input_error(
    settings: config.Settings,
    tmp_path: Path,
) -> None:
    nonexistent = tmp_path / "ghost.pdf"
    with pytest.raises(extract.ExtractionInputError):
        extract.run_extraction("Bobby", [nonexistent], settings=settings)


def test_claude_error_aborts_run_and_records_partial_outcome(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """A ClaudeError on doc 2 leaves doc 1's merge in place, marks the run
    aborted, and clears the pending block."""
    fake_state = FakeState("Bobby")
    fake_fm = FakeFieldMap(["account.named_insured"])
    doc1 = [
        FakeExtractedField(
            domain_tag="account.named_insured",
            value="X",
            source_doc="renewal-dec.pdf",
            source_page=1,
            source_quote="...",
            confidence=0.9,
        )
    ]

    class _BoomClient:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, *args: Any, **kwargs: Any) -> list[Any]:
            self.calls += 1
            if self.calls == 1:
                return doc1
            raise FakeClaudeError("simulated outage")

    boom = _BoomClient()
    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[],  # ignored by override below
    )
    monkeypatch.setattr(
        claude_client_mod, "extract_from_pdf", boom, raising=False
    )

    result = extract.run_extraction("Bobby", fake_pdfs, settings=settings)

    assert result.outcome == "aborted"
    assert result.error_message is not None
    assert "simulated outage" in result.error_message
    # First doc still merged; second doc has an error summary.
    assert len(result.per_doc) == 2
    assert result.per_doc[0].error is None
    assert result.per_doc[1].error is not None
    # run_history entry is present with outcome=aborted.
    assert len(fake_state.run_history) == 1
    assert fake_state.run_history[0].outcome == "aborted"
    # Pending block cleared so a future run starts fresh
    # (the operator can choose to redo or not).
    assert fake_state.pending_extraction is None


def test_force_opus_propagates_to_claude_client(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    fake_state = FakeState("Bobby")
    fake_fm = FakeFieldMap(["account.named_insured"])
    rec = FakeExtractedField(
        domain_tag="account.named_insured",
        value="X",
        source_doc="renewal-dec.pdf",
        source_page=1,
        source_quote="...",
        confidence=0.99,
        model_used="opus-4-7",
    )
    recorder = _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[[rec], [rec]],
    )

    result = extract.run_extraction(
        "Bobby",
        fake_pdfs,
        force_opus=True,
        settings=settings,
    )

    assert all(
        c["force_opus"] is True for c in recorder["extract_pdf_calls"]
    )
    assert fake_state.run_history[0].forced_opus is True
    assert result.per_doc[0].model_used == "opus-4-7"


# --------------------------------------------------------------------------- #
# Prompt-asset regressions (Bug 4)
# --------------------------------------------------------------------------- #


def test_default_system_prompt_clears_2048_token_cache_minimum() -> None:
    """Regression for Bug 4: the system prompt must comfortably exceed
    the 2,048-token Sonnet cache-prefix minimum so the first
    cache_control breakpoint actually activates."""
    approx_tokens = len(extract._DEFAULT_SYSTEM_PROMPT) // 4
    assert approx_tokens > 2048, (
        f"Default system prompt only ~{approx_tokens} tokens — Sonnet's "
        f"cache threshold won't activate."
    )
    # Spot-check load-bearing content per the audit list.
    assert "domain_tag" in extract._DEFAULT_SYSTEM_PROMPT
    assert "needs_review" in extract._DEFAULT_SYSTEM_PROMPT
    assert "source_quote" in extract._DEFAULT_SYSTEM_PROMPT
    assert "confidence" in extract._DEFAULT_SYSTEM_PROMPT
    assert "record_extracted_field" in extract._DEFAULT_SYSTEM_PROMPT


def test_default_glossary_clears_2048_token_cache_minimum_combined() -> None:
    """Combined system+glossary block must clear 2048 tokens.

    The cache breakpoint covers BOTH text segments, so a meaty glossary
    that complements a meaty system prompt is what actually crosses the
    threshold in production. We assert the combined size like
    claude_client does internally.
    """
    combined = (
        extract._DEFAULT_SYSTEM_PROMPT
        + "\n\n=== GLOSSARY ===\n"
        + extract._DEFAULT_GLOSSARY
    )
    approx_tokens = len(combined) // 4
    assert approx_tokens > 2048, (
        f"system+glossary combined only ~{approx_tokens} tokens"
    )
    # LOB acronyms the audit calls out are present (case-insensitive
    # because the glossary section headers are uppercase but body text
    # mixed-case).
    glossary_lower = extract._DEFAULT_GLOSSARY.lower()
    for term in ("gl", "bap", "wc", "umbrella", "cyber", "additional insured"):
        assert term in glossary_lower, f"missing term: {term}"


def test_default_system_prompt_has_output_protocol_section() -> None:
    """fix-pass-3: the system prompt must describe the iterative tool-use
    loop accurately — Sonnet 4.6 emits one tool_use per response and the
    pipeline drives the multi-turn agentic loop documented at
    https://docs.anthropic.com/en/docs/build-with-claude/tool-use.

    Without this section Claude doesn't know it can keep going; it
    extracts one field and stops (the 'output=175 tokens, 1 field
    extracted on a 30-field page' bug from fix-pass-2 diagnostics).
    """
    text = extract._DEFAULT_SYSTEM_PROMPT
    text_lower = text.lower()
    # Header for the section.
    assert "output protocol" in text_lower
    # Semantic requirements: iterative tool-use loop with continue-until-done.
    assert "loop" in text_lower
    assert "iterative" in text_lower or "multi-turn" in text_lower
    # Anti-pattern: don't stop early.
    assert "do not stop" in text_lower or "every extractable field" in text_lower
    # End-turn-when-done callout (the loop's exit condition).
    assert "end your turn" in text_lower
    # Repeatable-group guidance lives in this section so Claude sees it
    # in the most-prominent position.
    assert "repeatable_group" in text
    assert "repeatable_index" in text
    # Token-budget sanity: the new section adds ~200 tokens; the prompt
    # must still clear the 2048-token cache minimum (already covered by
    # the test above, but we re-assert here so a regression that drops
    # the protocol section trips THIS test rather than the more general
    # one).
    approx_tokens = len(text) // 4
    assert approx_tokens > 2048, (
        f"system prompt with OUTPUT PROTOCOL only ~{approx_tokens} tokens"
    )


# --------------------------------------------------------------------------- #
# Test-isolation canary (Bug 1)
# --------------------------------------------------------------------------- #


def test_save_user_config_via_extract_settings_never_writes_localappdata(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    tmp_path: Path,
) -> None:
    """Regression for Bug 1: a test fixture that builds a Settings()
    pointing user_config_dir at tmp_path must NEVER reach into the real
    ``%LOCALAPPDATA%/IGA Marketing Master/`` when ``save_user_config`` is
    invoked — the autouse conftest fixture monkeypatches platformdirs so
    even a misconfigured Settings() can't escape.

    Before the fix, a test that constructed Settings() without isolating
    platformdirs would write the operator's real config.json with
    ``working_library`` set to the test's tmp_path.
    """
    import os

    config.save_user_config(settings)

    # save_user_config wrote into tmp_path/config (the fixture's chosen
    # user_config_dir), not into the real %LOCALAPPDATA%.
    written = settings.user_config_dir / config.CONFIG_FILENAME
    assert written.exists()
    assert str(tmp_path) in str(written)

    # Sanity-check: the real %LOCALAPPDATA%/IGA Marketing Master/config.json
    # path was not touched by this test. We can't read it directly here
    # (the conftest canary handles restoration), but we can verify our
    # write target stayed under tmp_path.
    real_localappdata = os.environ.get("LOCALAPPDATA")
    if real_localappdata:
        real_path = Path(real_localappdata) / "IGA Marketing Master"
        assert str(real_path) not in str(written)


def test_cache_stats_aggregate_across_docs(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """When claude_client attaches `cache_usage` to its returned list, the
    extraction agent aggregates and surfaces the totals."""

    class _RecordList(list):
        cache_usage: dict[str, int] | None = None

    rec_a = FakeExtractedField(
        domain_tag="account.named_insured",
        value="X",
        source_doc="a.pdf",
        source_page=1,
        source_quote="...",
        confidence=0.95,
    )
    rec_b = FakeExtractedField(
        domain_tag="account.named_insured",
        value="X",
        source_doc="b.pdf",
        source_page=1,
        source_quote="...",
        confidence=0.95,
    )
    list_a = _RecordList([rec_a])
    list_a.cache_usage = {
        "cache_creation_input_tokens": 72341,
        "cache_read_input_tokens": 0,
        "input_tokens": 800,
        "output_tokens": 200,
    }
    list_b = _RecordList([rec_b])
    list_b.cache_usage = {
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 72341,
        "input_tokens": 600,
        "output_tokens": 180,
    }

    fake_state = FakeState("Bobby")
    fake_fm = FakeFieldMap(["account.named_insured"])
    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[list_a, list_b],
    )

    result = extract.run_extraction("Bobby", fake_pdfs, settings=settings)

    assert result.cache_stats.api_calls == 2
    assert result.cache_stats.cache_creation_input_tokens == 72341
    assert result.cache_stats.cache_read_input_tokens == 72341
    assert result.cache_stats.input_tokens == 1400
    assert result.cache_stats.output_tokens == 380


def test_cache_stats_aggregates_real_records_list_from_claude_client(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """Bug 7 end-to-end regression: when claude_client returns its real
    ``_RecordsList`` (a list subclass) with a ``_CallUsage`` dataclass on
    the ``cache_usage`` attribute, extract.run_extraction must aggregate
    the totals correctly into ``ExtractionResult.cache_stats``.

    Previously the per-call numbers were captured in claude_client._log_call
    but never propagated, so the run-end aggregate showed all zeros even on
    a successful run with real billing (the operator's diagnostic showed
    cache_creation=42772 per-call but cache_creation=0 / api_calls=0
    aggregate).
    """
    rec_a = FakeExtractedField(
        domain_tag="account.named_insured",
        value="A",
        source_doc="a.pdf",
        source_page=1,
        source_quote="Named Insured: A",
        confidence=0.95,
    )
    rec_b = FakeExtractedField(
        domain_tag="account.named_insured",
        value="B",
        source_doc="b.pdf",
        source_page=1,
        source_quote="Named Insured: B",
        confidence=0.95,
    )
    # Use the exact shapes claude_client now returns: a _RecordsList with
    # a _CallUsage dataclass on .cache_usage. The accumulator must accept
    # attribute-bearing objects (not just dicts) — this is the contract
    # extract.py's _accumulate_cache_stats was already designed for.
    list_a = claude_client_mod._RecordsList([rec_a])
    list_a.cache_usage = claude_client_mod._CallUsage(
        cache_creation_input_tokens=42_772,
        cache_read_input_tokens=0,
        input_tokens=23_217,
        output_tokens=175,
        api_calls=1,
    )
    list_b = claude_client_mod._RecordsList([rec_b])
    list_b.cache_usage = claude_client_mod._CallUsage(
        cache_creation_input_tokens=0,
        cache_read_input_tokens=42_772,
        input_tokens=1_500,
        output_tokens=120,
        api_calls=1,
    )

    fake_state = FakeState("Bobby")
    fake_fm = FakeFieldMap(["account.named_insured"])
    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[list_a, list_b],
    )

    result = extract.run_extraction("Bobby", fake_pdfs, settings=settings)

    # Each per-doc call counts as one api_calls increment in
    # _accumulate_cache_stats — the per-call _CallUsage already carries
    # api_calls=1 inside, but the extraction-agent treats one Claude
    # round-trip per PDF as the unit.
    assert result.cache_stats.api_calls == 2
    assert result.cache_stats.cache_creation_input_tokens == 42_772
    assert result.cache_stats.cache_read_input_tokens == 42_772
    assert result.cache_stats.input_tokens == 23_217 + 1_500
    assert result.cache_stats.output_tokens == 175 + 120


def test_cache_stats_aggregates_multi_chunk_call_usage(
    monkeypatch: pytest.MonkeyPatch,
    settings: config.Settings,
    fake_pdfs: list[Path],
) -> None:
    """When claude_client sums usage across PDF split chunks before
    returning, extract.py treats the resulting ``_CallUsage`` as a single
    aggregated object — the api_calls counter on the dataclass already
    reflects the chunk count, but the extraction-agent's own api_calls
    counter still ticks once per PDF (one extract_from_pdf invocation =
    one logical 'doc'). The cache + input/output token totals from the
    chunk sum still flow through faithfully."""
    rec = FakeExtractedField(
        domain_tag="account.named_insured",
        value="MultiChunk",
        source_doc="big.pdf",
        source_page=1,
        source_quote="...",
        confidence=0.95,
    )
    # Simulate the result of summing 3 chunk usages inside claude_client.
    summed = claude_client_mod._CallUsage(
        cache_creation_input_tokens=60_000,
        cache_read_input_tokens=30_000,
        input_tokens=6_000,
        output_tokens=600,
        api_calls=3,
    )
    records = claude_client_mod._RecordsList([rec])
    records.cache_usage = summed

    fake_state = FakeState("Bobby")
    fake_fm = FakeFieldMap(["account.named_insured"])
    _wire_fakes(
        monkeypatch,
        fake_state=fake_state,
        fake_fm=fake_fm,
        extract_returns=[records],
    )

    result = extract.run_extraction(
        "Bobby", [fake_pdfs[0]], settings=settings
    )

    # Per-doc cache stats must reflect the summed totals from the chunks.
    assert result.cache_stats.cache_creation_input_tokens == 60_000
    assert result.cache_stats.cache_read_input_tokens == 30_000
    assert result.cache_stats.input_tokens == 6_000
    assert result.cache_stats.output_tokens == 600

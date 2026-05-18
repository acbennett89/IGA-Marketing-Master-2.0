"""Tests for ``iga_marketing_master_2.enter``.

All Playwright + state interactions are mocked; no real browser runs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from iga_marketing_master_2 import enter, epic_session
from iga_marketing_master_2.config import Settings
from iga_marketing_master_2.enter import (
    EntryResult,
    PauseInfo,
    PauseResolution,
    run_entry_session,
)


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _StubFieldEntry:
    """Mirrors the subset of FieldEntry that enter.py reaches into."""

    raw: dict[str, Any]
    name: str
    label: str
    type: str = "text"
    hint: str | None = None
    enum_values: list[str] | None = None
    domain_tag: str | None = None
    screen_code: str | None = None


@dataclass(slots=True)
class _StubRecord:
    """Mirrors the subset of FieldRecord that enter.py reaches into."""

    value: Any
    status: str = "approved"
    confidence: float = 0.9
    history: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _StubState:
    fields: dict[str, _StubRecord] = field(default_factory=dict)
    repeatables: dict[str, list[dict[str, _StubRecord]]] = field(default_factory=dict)
    pending_pause: Any = None
    run_history: list[dict[str, Any]] = field(default_factory=list)


def _settings_for(tmp_path: Path, *, debug: bool = False) -> Settings:
    return Settings(  # type: ignore[call-arg]
        debug=debug,
        working_library=tmp_path / "wl",
        user_config_dir=tmp_path / "cfg",
        user_data_dir=tmp_path / "data",
        playwright_profile=tmp_path / "profile",
        field_map_path=tmp_path / "fieldmap.json",
        log_dir=tmp_path / "logs",
    )


def _make_field_map_with_entries(
    entries: dict[str, _StubFieldEntry],
) -> Any:
    """Build a stub FieldMap whose lookup_by_domain_tag returns ``entries[tag]``.

    We patch the module-level lookup function in tests rather than fake the
    full FieldMap shape.
    """
    fm = MagicMock(name="FieldMap")
    fm._entries = entries
    return fm


def _make_browser_context(page: Any) -> MagicMock:
    ctx = MagicMock(name="BrowserContext")
    ctx.pages = [page]
    ctx.tracing = MagicMock()
    return ctx


def _make_page() -> MagicMock:
    return MagicMock(name="Page")


def _make_locator(input_value: str = "", checked: bool = False) -> MagicMock:
    loc = MagicMock(name="Locator")
    loc.input_value.return_value = input_value
    loc.is_checked.return_value = checked
    loc.count.return_value = 1
    # Inline-error probe: empty by default
    err_loc = MagicMock()
    err_loc.count.return_value = 0
    loc.first = err_loc
    return loc


# --------------------------------------------------------------------------- #
# Happy-path: every approved field enters cleanly
# --------------------------------------------------------------------------- #


def test_run_entry_session_enters_approved_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Note: the "approved-only" gate has been replaced with an "any with a
    # non-empty value" gate (operator never asked for a manual review
    # step). The test name is retained for git-blame continuity; what it
    # actually exercises now is the happy-path entry of every enterable
    # field. Empty-valued fields stay skipped.
    state = _StubState(
        fields={
            "account.named_insured": _StubRecord(
                value="Bobby Luttrell & Sons LLC", status="approved"
            ),
            "submission.name": _StubRecord(value="2026 Renewal", status="approved"),
            # Empty value -> never enterable, no entry needed.
            "account.dba": _StubRecord(value="", status="pending"),
        }
    )
    entries = {
        "account.named_insured": _StubFieldEntry(
            raw={"automation_id": "id1"},
            name="cstName",
            label="Named Insured",
            type="text",
        ),
        "submission.name": _StubFieldEntry(
            raw={"automation_id": "id2"},
            name="submName",
            label="Submission Name",
            type="text",
        ),
    }

    fm = _make_field_map_with_entries(entries)
    page = _make_page()
    ctx = _make_browser_context(page)

    fill_calls: list[tuple[str, str]] = []

    def fake_resolve(p, e, *, screen_container=None):  # noqa: ANN001
        loc = _make_locator(input_value=str(state.fields[e.domain_tag].value))
        # Hook to track fill argument and ensure subsequent input_value matches.
        def fill(value: str) -> None:
            fill_calls.append((e.domain_tag, value))
            loc.input_value.return_value = value

        loc.fill.side_effect = fill
        return loc, "automation_id"

    # Ensure each entry knows its domain_tag so fake_resolve can index back.
    for tag, e in entries.items():
        e.domain_tag = tag

    monkeypatch.setattr(epic_session, "resolve_locator", fake_resolve)
    monkeypatch.setattr(
        epic_session,
        "preflight_selector_smoke",
        lambda *a, **kw: epic_session.SmokeReport(elapsed_ms=0.0, entries=[]),
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )
    # No-op debounce for fast tests.
    monkeypatch.setattr(enter, "_debounce", lambda _ms: None)

    pause_cb = MagicMock(side_effect=AssertionError("pause should not be called"))
    saves: list[Any] = []

    result = run_entry_session(
        state,
        fm,
        ctx,
        on_pause_callback=pause_cb,
        settings=_settings_for(tmp_path),
        save_state_callback=lambda s: saves.append(s),
    )

    assert isinstance(result, EntryResult)
    assert result.outcome == "completed"
    assert result.fields_entered == 2
    assert result.fields_skipped == 0
    assert result.fields_paused == 0
    assert state.fields["account.named_insured"].status == "entered"
    assert state.fields["submission.name"].status == "entered"
    # Skipped field stays at its prior status.
    assert state.fields["account.dba"].status == "pending"
    pause_cb.assert_not_called()
    # save_state_callback was invoked at least once.
    assert saves


# --------------------------------------------------------------------------- #
# Pause-for-human: validation rejected -> resume re-reads DOM as canonical
# --------------------------------------------------------------------------- #


def test_validation_pause_resume_rereads_dom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _StubState(
        fields={
            "vehicle.vin": _StubRecord(value="XYZ123", status="approved"),
        }
    )
    entries = {
        "vehicle.vin": _StubFieldEntry(
            raw={"automation_id": "vin-input"},
            name="vinTxt",
            label="VIN",
            type="text",
            domain_tag="vehicle.vin",
            screen_code="AUTOLOB",
        )
    }

    page = _make_page()
    ctx = _make_browser_context(page)

    # The locator's read-back will mismatch the intended value initially,
    # but on the post-resume re-read it returns the corrected VIN.
    locator = _make_locator(input_value="XYZ123")
    locator.fill = MagicMock()

    # First validation read: returns the bad value -> mismatch is OK actually
    # for our test we want to FORCE a validation rejection. Do that by making
    # the inline-error probe report a populated error locator.
    err_loc = MagicMock()
    err_loc.count.return_value = 1
    err_loc.text_content.return_value = "VIN must be 17 characters"

    inner_locator = MagicMock(first=err_loc)
    page.locator = MagicMock(return_value=inner_locator)

    # After "resume", the operator has fixed the field manually; input_value
    # now reads the corrected 17-char VIN.
    resume_iter = iter(["XYZ123", "1FA6P0HD3K5123456"])

    def input_value_side_effect():  # noqa: ANN202
        try:
            return next(resume_iter)
        except StopIteration:
            return "1FA6P0HD3K5123456"

    locator.input_value.side_effect = input_value_side_effect

    monkeypatch.setattr(
        epic_session, "resolve_locator", lambda p, e, **kw: (locator, "automation_id")
    )
    monkeypatch.setattr(
        epic_session,
        "preflight_selector_smoke",
        lambda *a, **kw: epic_session.SmokeReport(elapsed_ms=0.0, entries=[]),
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )
    monkeypatch.setattr(enter, "_debounce", lambda _ms: None)

    pause_cb = MagicMock(return_value="resume")

    result = run_entry_session(
        state,
        _make_field_map_with_entries(entries),
        ctx,
        on_pause_callback=pause_cb,
        settings=_settings_for(tmp_path),
    )

    pause_cb.assert_called_once()
    pause_arg = pause_cb.call_args.args[0]
    assert isinstance(pause_arg, PauseInfo)
    assert pause_arg.domain_tag == "vehicle.vin"
    assert pause_arg.reason_code == "validation_rejected"
    assert "VIN" in pause_arg.field_label

    # After resume, the DOM value should be canonical.
    assert state.fields["vehicle.vin"].value == "1FA6P0HD3K5123456"
    assert state.fields["vehicle.vin"].status == "entered"
    assert state.fields["vehicle.vin"].confidence == 1.0
    # History should record the post-pause resolution.
    history_actions = [h["action"] for h in state.fields["vehicle.vin"].history]
    assert "resumed_after_pause" in history_actions
    # pending_pause must be cleared.
    assert state.pending_pause is None
    assert result.fields_paused == 1
    assert result.fields_entered == 1
    assert result.outcome == "completed"


def test_pause_callback_abort_returns_aborted_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _StubState(
        fields={
            "account.fein": _StubRecord(value="12-3456789", status="approved"),
        }
    )
    entries = {
        "account.fein": _StubFieldEntry(
            raw={"automation_id": "fein-input"},
            name="feinTxt",
            label="FEIN",
            type="text",
            domain_tag="account.fein",
            screen_code="ACCDET",
        )
    }

    page = _make_page()
    ctx = _make_browser_context(page)

    def raise_unresolved(*a, **kw):  # noqa: ANN001
        raise epic_session.SelectorUnresolvedError(
            field_label="FEIN",
            field_name="feinTxt",
            attempts={"automation_id": "count=0", "name": "count=0", "label_fallback": "count=0"},
        )

    monkeypatch.setattr(epic_session, "resolve_locator", raise_unresolved)
    monkeypatch.setattr(
        epic_session,
        "preflight_selector_smoke",
        lambda *a, **kw: epic_session.SmokeReport(elapsed_ms=0.0, entries=[]),
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )

    result = run_entry_session(
        state,
        _make_field_map_with_entries(entries),
        ctx,
        on_pause_callback=lambda _: "abort",
        settings=_settings_for(tmp_path),
    )

    assert result.outcome == "aborted"
    assert result.fields_entered == 0
    assert result.pause_reason is not None
    assert result.pause_reason.reason_code == "selector_unresolved"
    # Pause must be cleared on abort exit.
    assert state.pending_pause is None


def test_pause_callback_skip_continues_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _StubState(
        fields={
            "account.fein": _StubRecord(value="12-3456789", status="approved"),
            "account.named_insured": _StubRecord(value="Acme", status="approved"),
        }
    )
    entries = {
        "account.fein": _StubFieldEntry(
            raw={"automation_id": "fein"}, name="fein", label="FEIN",
            domain_tag="account.fein", screen_code="ACCDET",
        ),
        "account.named_insured": _StubFieldEntry(
            raw={"automation_id": "ni"}, name="ni", label="Named Insured",
            domain_tag="account.named_insured", screen_code="ACCDET",
        ),
    }

    # FEIN raises unresolved; named_insured is fine.
    def fake_resolve(page, e, **kw):  # noqa: ANN001
        if e.domain_tag == "account.fein":
            raise epic_session.SelectorUnresolvedError(
                field_label="FEIN", field_name="fein",
                attempts={"automation_id": "count=0", "name": "count=0", "label_fallback": "count=0"},
            )
        loc = _make_locator(input_value="Acme")
        loc.fill = MagicMock(side_effect=lambda v: setattr(loc.input_value, "return_value", v))
        return loc, "automation_id"

    monkeypatch.setattr(epic_session, "resolve_locator", fake_resolve)
    monkeypatch.setattr(
        epic_session,
        "preflight_selector_smoke",
        lambda *a, **kw: epic_session.SmokeReport(elapsed_ms=0.0, entries=[]),
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )
    monkeypatch.setattr(enter, "_debounce", lambda _ms: None)

    page = _make_page()
    ctx = _make_browser_context(page)

    result = run_entry_session(
        state,
        _make_field_map_with_entries(entries),
        ctx,
        on_pause_callback=lambda _: "skip",
        settings=_settings_for(tmp_path),
    )

    assert result.outcome == "completed"
    assert result.fields_skipped == 1  # FEIN
    assert result.fields_paused == 1
    assert result.fields_entered == 1  # Named Insured
    # FEIN is left at status "approved" per skip semantics.
    assert state.fields["account.fein"].status == "approved"


# --------------------------------------------------------------------------- #
# Repeatables iteration
# --------------------------------------------------------------------------- #


def test_run_walks_repeatables_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vehicle_records = [
        {"vehicle.year": _StubRecord(value="2022", status="approved"),
         "vehicle.vin": _StubRecord(value="1HGCM82633A004352", status="approved")},
        {"vehicle.year": _StubRecord(value="2024", status="approved"),
         "vehicle.vin": _StubRecord(value="WBA3A5G54DNP18079", status="approved")},
    ]
    state = _StubState(repeatables={"vehicle": vehicle_records})

    entries = {
        "vehicle.year": _StubFieldEntry(
            raw={"automation_id": "year"}, name="year", label="Year",
            domain_tag="vehicle.year", screen_code="AUTOLOB",
        ),
        "vehicle.vin": _StubFieldEntry(
            raw={"automation_id": "vin"}, name="vin", label="VIN",
            domain_tag="vehicle.vin", screen_code="AUTOLOB",
        ),
    }

    seen: list[tuple[str, Any]] = []

    def fake_resolve(page, e, **kw):  # noqa: ANN001
        loc = _make_locator()
        def fill(v):  # noqa: ANN202
            seen.append((e.domain_tag, v))
            loc.input_value.return_value = v
        loc.fill.side_effect = fill
        return loc, "automation_id"

    monkeypatch.setattr(epic_session, "resolve_locator", fake_resolve)
    monkeypatch.setattr(
        epic_session,
        "preflight_selector_smoke",
        lambda *a, **kw: epic_session.SmokeReport(elapsed_ms=0.0, entries=[]),
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )
    monkeypatch.setattr(enter, "_debounce", lambda _ms: None)

    page = _make_page()
    ctx = _make_browser_context(page)

    result = run_entry_session(
        state,
        _make_field_map_with_entries(entries),
        ctx,
        on_pause_callback=lambda _: "abort",
        settings=_settings_for(tmp_path),
    )

    assert result.fields_entered == 4
    # Item 0 fields, then item 1 fields, in tag-sorted order.
    assert seen == [
        ("vehicle.vin", "1HGCM82633A004352"),
        ("vehicle.year", "2022"),
        ("vehicle.vin", "WBA3A5G54DNP18079"),
        ("vehicle.year", "2024"),
    ]


# --------------------------------------------------------------------------- #
# Pre-flight smoke surfaces a warning but doesn't block
# --------------------------------------------------------------------------- #


def test_preflight_stale_surfaces_warning_but_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _StubState(
        fields={"account.named_insured": _StubRecord(value="Acme", status="approved")}
    )
    entries = {
        "account.named_insured": _StubFieldEntry(
            raw={"automation_id": "ni"}, name="ni", label="Named Insured",
            domain_tag="account.named_insured", screen_code="ACCDET",
        )
    }

    def fake_resolve(p, e, **kw):  # noqa: ANN001
        loc = _make_locator()
        loc.fill.side_effect = lambda v: setattr(loc.input_value, "return_value", v)
        return loc, "automation_id"

    monkeypatch.setattr(epic_session, "resolve_locator", fake_resolve)

    stale_report = epic_session.SmokeReport(
        elapsed_ms=2400.0,
        entries=[
            epic_session.SmokeReportEntry(
                screen_code="ACCDET",
                domain_tag="account.named_insured",
                field_name="ni",
                resolved=True,
                strategy="label_fallback",
                stale=True,
                elapsed_ms=12.0,
            )
        ],
    )
    monkeypatch.setattr(epic_session, "preflight_selector_smoke", lambda *a, **kw: stale_report)
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )
    monkeypatch.setattr(enter, "_debounce", lambda _ms: None)

    page = _make_page()
    ctx = _make_browser_context(page)

    result = run_entry_session(
        state,
        _make_field_map_with_entries(entries),
        ctx,
        on_pause_callback=lambda _: "abort",
        settings=_settings_for(tmp_path),
    )

    assert result.outcome == "completed"
    assert result.smoke_stale_count == 1
    assert result.warnings, "stale pre-flight should produce a warning string"
    assert "stale" in result.warnings[0]


# --------------------------------------------------------------------------- #
# --debug discipline
# --------------------------------------------------------------------------- #


def test_debug_mode_stops_tracing_and_writes_trace_zip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _StubState(
        fields={"account.named_insured": _StubRecord(value="Acme", status="approved")}
    )
    entries = {
        "account.named_insured": _StubFieldEntry(
            raw={"automation_id": "ni"}, name="ni", label="Named Insured",
            domain_tag="account.named_insured", screen_code="ACCDET",
        )
    }

    page = _make_page()
    ctx = _make_browser_context(page)

    def fake_resolve(p, e, **kw):  # noqa: ANN001
        loc = _make_locator()
        loc.fill.side_effect = lambda v: setattr(loc.input_value, "return_value", v)
        return loc, "automation_id"

    monkeypatch.setattr(epic_session, "resolve_locator", fake_resolve)
    monkeypatch.setattr(
        epic_session,
        "preflight_selector_smoke",
        lambda *a, **kw: epic_session.SmokeReport(elapsed_ms=0.0, entries=[]),
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )
    monkeypatch.setattr(enter, "_debounce", lambda _ms: None)

    client_path = tmp_path / "client"
    client_path.mkdir()

    run_entry_session(
        state,
        _make_field_map_with_entries(entries),
        ctx,
        on_pause_callback=lambda _: "abort",
        settings=_settings_for(tmp_path, debug=True),
        client_path=client_path,
    )

    # tracing.stop must have been called with a path under the run_id dir.
    ctx.tracing.stop.assert_called_once()
    stop_kwargs = ctx.tracing.stop.call_args.kwargs
    assert "trace.zip" in stop_kwargs.get("path", "")
    # Per-action screenshots called.
    assert page.screenshot.call_count >= 1


def test_debug_prune_keeps_last_5_runs(tmp_path: Path) -> None:
    parent = tmp_path / "client" / "debug" / "playwright"
    parent.mkdir(parents=True)

    # Create 7 run dirs with monotonically increasing mtime.
    import time as _time

    for i in range(7):
        (parent / f"run-{i:02d}").mkdir()
        # Force ordering by mtime.
        _time.sleep(0.01)
        os_t = parent / f"run-{i:02d}"
        os_t.touch()

    enter._prune_debug_runs(tmp_path / "client", retention=5)
    remaining = sorted(p.name for p in parent.iterdir())
    # The two oldest should have been deleted.
    assert len(remaining) == 5
    assert "run-00" not in remaining
    assert "run-01" not in remaining
    assert "run-06" in remaining


# --------------------------------------------------------------------------- #
# pending_pause persistence around the callback
# --------------------------------------------------------------------------- #


def test_pending_pause_set_before_callback_and_cleared_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _StubState(
        fields={"account.fein": _StubRecord(value="12-3456789", status="approved")}
    )
    entries = {
        "account.fein": _StubFieldEntry(
            raw={"automation_id": "fein"}, name="fein", label="FEIN",
            domain_tag="account.fein", screen_code="ACCDET",
        )
    }

    page = _make_page()
    ctx = _make_browser_context(page)

    def raise_unresolved(*a, **kw):  # noqa: ANN001
        raise epic_session.SelectorUnresolvedError(
            field_label="FEIN", field_name="fein",
            attempts={"automation_id": "count=0", "name": "count=0", "label_fallback": "count=0"},
        )

    monkeypatch.setattr(epic_session, "resolve_locator", raise_unresolved)
    monkeypatch.setattr(
        epic_session,
        "preflight_selector_smoke",
        lambda *a, **kw: epic_session.SmokeReport(elapsed_ms=0.0, entries=[]),
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.lookup_by_domain_tag",
        lambda field_map, tag: entries.get(tag),
    )

    seen_inside: list[Any] = []

    def callback(pause: PauseInfo) -> PauseResolution:
        # Capture the live pending_pause inside the callback - must be set.
        seen_inside.append(state.pending_pause)
        return "skip"

    saves: list[Any] = []
    run_entry_session(
        state,
        _make_field_map_with_entries(entries),
        ctx,
        on_pause_callback=callback,
        settings=_settings_for(tmp_path),
        save_state_callback=lambda s: saves.append(getattr(s, "pending_pause", None)),
    )

    # While the callback ran, pending_pause was set.
    assert seen_inside and isinstance(seen_inside[0], PauseInfo)
    # After the run, pending_pause is cleared.
    assert state.pending_pause is None
    # save_state_callback was called at least once with a pending pause set
    # (durable persistence before the modal opened).
    assert any(isinstance(s, PauseInfo) for s in saves)

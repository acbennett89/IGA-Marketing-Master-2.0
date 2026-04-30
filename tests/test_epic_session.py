"""Tests for ``iga_marketing_master_2.epic_session``.

Mocks the Playwright API end-to-end. No real browser is launched.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from iga_marketing_master_2 import epic_session
from iga_marketing_master_2.epic_session import (
    LOCK_FILES,
    PlaywrightProfileInUseError,
    SelectorUnresolvedError,
    SmokeReport,
    STRATEGY_AUTOMATION_ID,
    STRATEGY_LABEL_FALLBACK,
    STRATEGY_NAME,
    cleanup_user_data_dir_lock,
    preflight_selector_smoke,
    resolve_locator,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _FakeFieldEntry:
    """Stand-in for :class:`field_map.FieldEntry` that resolve_locator works
    against (it accesses ``raw``, ``name``, ``label``, ``domain_tag``,
    ``screen_code``)."""

    raw: dict[str, Any]
    name: str
    label: str
    domain_tag: str | None = None
    screen_code: str | None = None

    @property
    def type(self) -> str:
        return self.raw.get("type", "text")

    @property
    def hint(self) -> str | None:
        return self.raw.get("hint")

    @property
    def enum_values(self) -> list[str] | None:
        return self.raw.get("enum_values")


def _make_locator(count: int = 0) -> MagicMock:
    loc = MagicMock(name=f"Locator(count={count})")
    loc.count.return_value = count
    return loc


def _make_root(strategy_counts: dict[str, int]) -> MagicMock:
    """Build a fake Playwright Page-or-Locator root.

    ``strategy_counts`` maps a probe identifier ("automation_id", "name",
    "label_fallback") to the count() the resulting Locator should report.
    Missing keys default to 0.
    """
    root = MagicMock(name="Root")

    def locator(selector: str) -> MagicMock:
        # selector is e.g. [data-automation-id="..."] or [name="..."]
        if "data-automation-id" in selector:
            return _make_locator(strategy_counts.get("automation_id", 0))
        if selector.startswith("[name="):
            return _make_locator(strategy_counts.get("name", 0))
        return _make_locator(0)

    def get_by_label(label: str, exact: bool = False) -> MagicMock:
        return _make_locator(strategy_counts.get("label_fallback", 0))

    root.locator.side_effect = locator
    root.get_by_label.side_effect = get_by_label
    return root


# --------------------------------------------------------------------------- #
# cleanup_user_data_dir_lock
# --------------------------------------------------------------------------- #


def test_cleanup_lock_skips_missing_dir(tmp_path: Path) -> None:
    target = tmp_path / "missing-profile"
    # Should not raise, should not create the directory.
    cleanup_user_data_dir_lock(target)
    assert not target.exists()


def test_cleanup_lock_rejects_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        cleanup_user_data_dir_lock(Path("relative-profile"))


def test_cleanup_lock_removes_stale_files_when_no_pid(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    # Create one of each known lock file with no parseable PID.
    for filename in ("SingletonLock", "LOCK", "lockfile"):
        (profile / filename).write_text("", encoding="utf-8")

    cleanup_user_data_dir_lock(profile)

    for filename in ("SingletonLock", "LOCK", "lockfile"):
        assert not (profile / filename).exists(), f"{filename} should be cleaned"


def test_cleanup_lock_removes_lock_with_dead_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    # Chromium-style content "<pid>-<host>".
    (profile / "SingletonLock").write_text("999999999-deadhost.local", encoding="utf-8")

    monkeypatch.setattr(epic_session, "_is_pid_alive", lambda _pid: False)

    cleanup_user_data_dir_lock(profile)
    assert not (profile / "SingletonLock").exists()


def test_cleanup_lock_raises_when_live_pid_owns_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("12345-host", encoding="utf-8")

    monkeypatch.setattr(epic_session, "_is_pid_alive", lambda _pid: True)

    with pytest.raises(PlaywrightProfileInUseError) as exc_info:
        cleanup_user_data_dir_lock(profile)
    assert "12345" in str(exc_info.value)
    # The lock must NOT be deleted because a live owner exists.
    assert (profile / "SingletonLock").exists()


def test_cleanup_lock_handles_unparseable_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "SingletonLock").write_text("not-a-number", encoding="utf-8")
    # Even though _is_pid_alive would say "True", the PID is unparseable so
    # we treat as stale.
    monkeypatch.setattr(epic_session, "_is_pid_alive", lambda _pid: True)

    cleanup_user_data_dir_lock(profile)
    assert not (profile / "SingletonLock").exists()


def test_lock_files_constant_includes_documented_set() -> None:
    """Sanity check: the LOCK_FILES list matches DECISION-MAP-epic-driver-agent.md."""
    documented = {
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "LOCK",
        "lockfile",
        "parent.lock",
    }
    assert documented.issubset(set(LOCK_FILES))


# --------------------------------------------------------------------------- #
# resolve_locator chain
# --------------------------------------------------------------------------- #


def test_resolve_locator_prefers_automation_id() -> None:
    page = _make_root({"automation_id": 1, "name": 1, "label_fallback": 1})
    entry = _FakeFieldEntry(
        raw={"automation_id": "submission-name-input", "type": "text"},
        name="submName",
        label="Submission Name",
        domain_tag="submission.name",
        screen_code="MKMMSDET",
    )
    locator, strategy = resolve_locator(page, entry)
    assert strategy == STRATEGY_AUTOMATION_ID
    assert locator is not None
    # We should have stopped at automation_id - never queried the others.
    assert page.get_by_label.call_count == 0


def test_resolve_locator_falls_through_to_name() -> None:
    page = _make_root({"automation_id": 0, "name": 1, "label_fallback": 1})
    entry = _FakeFieldEntry(
        raw={"automation_id": "missing-id", "type": "text"},
        name="streName",
        label="Street",
    )
    locator, strategy = resolve_locator(page, entry)
    assert strategy == STRATEGY_NAME
    assert locator is not None
    # Label fallback should not have been consulted.
    assert page.get_by_label.call_count == 0


def test_resolve_locator_falls_through_to_label_fallback() -> None:
    page = _make_root({"automation_id": 0, "name": 0, "label_fallback": 1})
    entry = _FakeFieldEntry(
        raw={"automation_id": "missing-id", "type": "text"},
        name="streName",
        label="Street",
    )
    locator, strategy = resolve_locator(page, entry)
    assert strategy == STRATEGY_LABEL_FALLBACK
    assert locator is not None
    page.get_by_label.assert_called_once_with("Street", exact=True)


def test_resolve_locator_raises_when_all_strategies_fail() -> None:
    page = _make_root({"automation_id": 0, "name": 0, "label_fallback": 0})
    entry = _FakeFieldEntry(
        raw={"automation_id": "x", "type": "text"},
        name="streName",
        label="Street",
    )
    with pytest.raises(SelectorUnresolvedError) as exc_info:
        resolve_locator(page, entry)
    assert "Street" in str(exc_info.value)
    # All three attempts should be recorded.
    attempts = exc_info.value.attempts
    assert STRATEGY_AUTOMATION_ID in attempts
    assert STRATEGY_NAME in attempts
    assert STRATEGY_LABEL_FALLBACK in attempts


def test_resolve_locator_skips_automation_id_when_absent() -> None:
    page = _make_root({"name": 1})
    entry = _FakeFieldEntry(
        raw={"type": "text"},  # no automation_id key
        name="streName",
        label="Street",
    )
    locator, strategy = resolve_locator(page, entry)
    assert strategy == STRATEGY_NAME
    # Confirm the page.locator was called for [name=...] but never for
    # data-automation-id since automation_id was absent.
    selectors_used = [c.args[0] for c in page.locator.call_args_list]
    assert any(s.startswith("[name=") for s in selectors_used)
    assert not any("data-automation-id" in s for s in selectors_used)


def test_resolve_locator_uses_screen_container_when_provided() -> None:
    """When ``screen_container`` is supplied, queries should go through it,
    not the bare page."""
    page = _make_root({"automation_id": 0, "name": 0, "label_fallback": 0})
    container = _make_root({"automation_id": 1})
    entry = _FakeFieldEntry(
        raw={"automation_id": "x", "type": "text"},
        name="streName",
        label="Street",
    )
    locator, strategy = resolve_locator(page, entry, screen_container=container)
    assert strategy == STRATEGY_AUTOMATION_ID
    container.locator.assert_called()
    # Page should never have been queried.
    page.locator.assert_not_called()


# --------------------------------------------------------------------------- #
# preflight_selector_smoke
# --------------------------------------------------------------------------- #


def _make_field_map_stub(
    entries: list[_FakeFieldEntry],
    screens_by_tag: dict[str, str],
) -> Any:
    """Build a stub object compatible with the field_map module's API."""
    fm = MagicMock(name="FieldMap")
    fm._entries = entries
    fm._screens_by_tag = screens_by_tag
    return fm


def test_preflight_smoke_returns_clean_report_when_all_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _make_root({"automation_id": 1})

    e1 = _FakeFieldEntry(
        raw={"automation_id": "id1", "type": "text"},
        name="n1",
        label="Field One",
        domain_tag="account.named_insured",
        screen_code="ACCDET",
    )
    e2 = _FakeFieldEntry(
        raw={"automation_id": "id2", "type": "text"},
        name="n2",
        label="Field Two",
        domain_tag="submission.name",
        screen_code="MKMMSDET",
    )

    fm = _make_field_map_stub(
        [e1, e2],
        {"account.named_insured": "ACCDET", "submission.name": "MKMMSDET"},
    )

    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.screens_touched_by_domain_tags",
        lambda field_map, tags: {"ACCDET", "MKMMSDET"},
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.fields_for_screen",
        lambda field_map, screen: [e for e in [e1, e2] if e.screen_code == screen],
    )

    report = preflight_selector_smoke(
        page, ["account.named_insured", "submission.name"], fm
    )
    assert isinstance(report, SmokeReport)
    assert report.stale_count == 0
    assert report.unresolved_count == 0
    assert len(report.entries) == 2
    assert all(e.resolved for e in report.entries)
    assert all(e.strategy == STRATEGY_AUTOMATION_ID for e in report.entries)


def test_preflight_smoke_flags_label_fallback_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _make_root({"automation_id": 0, "name": 0, "label_fallback": 1})

    e1 = _FakeFieldEntry(
        raw={"automation_id": "stale-id", "type": "text"},
        name="n1",
        label="Stale Field",
        domain_tag="account.named_insured",
        screen_code="ACCDET",
    )

    fm = _make_field_map_stub([e1], {"account.named_insured": "ACCDET"})
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.screens_touched_by_domain_tags",
        lambda field_map, tags: {"ACCDET"},
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.fields_for_screen",
        lambda field_map, screen: [e1],
    )

    report = preflight_selector_smoke(page, ["account.named_insured"], fm)
    assert report.stale_count == 1
    assert report.entries[0].stale is True
    assert report.entries[0].strategy == STRATEGY_LABEL_FALLBACK


def test_preflight_smoke_records_unresolved_as_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _make_root({"automation_id": 0, "name": 0, "label_fallback": 0})

    e1 = _FakeFieldEntry(
        raw={"automation_id": "x", "type": "text"},
        name="n1",
        label="Phantom",
        domain_tag="account.fein",
        screen_code="ACCDET",
    )

    fm = _make_field_map_stub([e1], {"account.fein": "ACCDET"})
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.screens_touched_by_domain_tags",
        lambda field_map, tags: {"ACCDET"},
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.fields_for_screen",
        lambda field_map, screen: [e1],
    )

    report = preflight_selector_smoke(page, ["account.fein"], fm)
    assert report.unresolved_count == 1
    assert report.stale_count == 1
    assert report.entries[0].resolved is False
    assert report.entries[0].strategy is None
    # Smoke is non-blocking - no exception bubbled.


def test_preflight_smoke_skips_fields_outside_input_tag_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _make_root({"automation_id": 1})
    wanted = _FakeFieldEntry(
        raw={"automation_id": "wanted", "type": "text"},
        name="n1",
        label="Wanted",
        domain_tag="account.named_insured",
        screen_code="ACCDET",
    )
    other = _FakeFieldEntry(
        raw={"automation_id": "other", "type": "text"},
        name="n2",
        label="Other",
        domain_tag="account.dba",
        screen_code="ACCDET",
    )

    fm = _make_field_map_stub(
        [wanted, other],
        {"account.named_insured": "ACCDET", "account.dba": "ACCDET"},
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.screens_touched_by_domain_tags",
        lambda field_map, tags: {"ACCDET"},
    )
    monkeypatch.setattr(
        "iga_marketing_master_2.field_map.fields_for_screen",
        lambda field_map, screen: [wanted, other],
    )

    report = preflight_selector_smoke(page, ["account.named_insured"], fm)
    # Only one entry should appear in the report - the unrelated tag was skipped.
    assert len(report.entries) == 1
    assert report.entries[0].domain_tag == "account.named_insured"


# --------------------------------------------------------------------------- #
# launch_with_persistent_context (mocked Playwright import)
# --------------------------------------------------------------------------- #


def test_launch_rejects_relative_user_data_dir(tmp_path: Path) -> None:
    """We never want a relative path - Playwright #34700."""
    with pytest.raises(ValueError):
        epic_session.launch_with_persistent_context(Path("relative-profile"))


def test_launch_calls_cleanup_then_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"

    fake_context = MagicMock(name="BrowserContext")
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context.return_value = fake_context

    fake_pw = MagicMock()
    fake_pw.chromium = fake_chromium

    fake_sync = MagicMock()
    fake_sync.start.return_value = fake_pw

    # Provide the playwright.sync_api module to the lazy import.
    import sys
    import types

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: fake_sync  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    cleanup_calls: list[Path] = []
    real_cleanup = epic_session.cleanup_user_data_dir_lock

    def spy_cleanup(path: Path) -> None:
        cleanup_calls.append(path)
        real_cleanup(path)

    monkeypatch.setattr(epic_session, "cleanup_user_data_dir_lock", spy_cleanup)

    ctx = epic_session.launch_with_persistent_context(profile, headed=True, debug=False)

    assert ctx is fake_context
    assert cleanup_calls == [profile]
    fake_chromium.launch_persistent_context.assert_called_once()
    kwargs = fake_chromium.launch_persistent_context.call_args.kwargs
    assert kwargs["user_data_dir"] == str(profile)
    assert kwargs["headless"] is False
    # Tracing not started without debug.
    fake_context.tracing.start.assert_not_called()


def test_launch_starts_tracing_in_debug_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile"

    fake_context = MagicMock(name="BrowserContext")
    fake_chromium = MagicMock()
    fake_chromium.launch_persistent_context.return_value = fake_context

    fake_pw = MagicMock()
    fake_pw.chromium = fake_chromium

    fake_sync = MagicMock()
    fake_sync.start.return_value = fake_pw

    import sys
    import types

    fake_module = types.ModuleType("playwright.sync_api")
    fake_module.sync_playwright = lambda: fake_sync  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    epic_session.launch_with_persistent_context(profile, debug=True)

    fake_context.tracing.start.assert_called_once_with(
        screenshots=True, snapshots=True, sources=True
    )

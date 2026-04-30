"""Tests for ``iga_marketing_master_2.field_map``.

These tests use a tmp copy of ``Library/Epic Field Map.json`` so the real
file is never modified by the test session. The fixture copy is mutated and
re-saved to verify round-tripping and atomic-write behavior.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from iga_marketing_master_2 import field_map as fm
from iga_marketing_master_2.field_map import (
    ALLOWED_PATCH_KEYS,
    DEFAULT_FIELD_MAP_PATH,
    FieldMap,
    FieldMapValidationError,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def real_field_map_path() -> Path:
    """Absolute path to the real Field Map JSON in the repo."""
    assert DEFAULT_FIELD_MAP_PATH.exists(), (
        f"Real Field Map missing: {DEFAULT_FIELD_MAP_PATH}"
    )
    return DEFAULT_FIELD_MAP_PATH


@pytest.fixture
def tmp_field_map(real_field_map_path: Path, tmp_path: Path) -> Path:
    """Copy the real Field Map into a tmp location for safe mutation."""
    dest = tmp_path / "Epic Field Map.json"
    shutil.copy2(real_field_map_path, dest)
    return dest


@pytest.fixture
def loaded(tmp_field_map: Path) -> FieldMap:
    return fm.load(tmp_field_map)


# --------------------------------------------------------------------------- #
# load() basics
# --------------------------------------------------------------------------- #


def test_load_returns_fieldmap_with_indices(loaded: FieldMap) -> None:
    assert isinstance(loaded, FieldMap)
    # Real scrape has many screens.
    assert len(loaded.raw) > 10
    # Real scrape indexes plenty of fields.
    assert len(loaded._by_screen_name) > 100


def test_load_default_path_works(real_field_map_path: Path) -> None:
    """Loading without an explicit path defaults to the canonical location."""
    fmap = fm.load()
    assert fmap.source_path == real_field_map_path
    assert isinstance(fmap.raw, dict)


def test_load_idempotent(tmp_field_map: Path) -> None:
    a = fm.load(tmp_field_map)
    b = fm.load(tmp_field_map)
    assert a.raw == b.raw
    assert a is not b


def test_load_rejects_invalid_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(FieldMapValidationError, match="not valid JSON"):
        fm.load(bad)


def test_load_rejects_non_object_root(tmp_path: Path) -> None:
    bad = tmp_path / "list.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(FieldMapValidationError, match="must be a JSON object"):
        fm.load(bad)


def test_load_rejects_field_missing_required_key(tmp_path: Path) -> None:
    bad = tmp_path / "missing-name.json"
    payload = {
        "Screen X": {
            "screen_code": "SX",
            "fields": [
                # Missing "name".
                {"type": "text", "label": "Foo", "format": "old"}
            ],
            "tabs": [],
        }
    }
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FieldMapValidationError, match="missing required"):
        fm.load(bad)


# --------------------------------------------------------------------------- #
# Lookup APIs
# --------------------------------------------------------------------------- #


def test_lookup_by_name_finds_a_top_level_field(loaded: FieldMap) -> None:
    """`Submission Detail > streName` is the canonical first-page text field."""
    entry = fm.lookup_by_name(loaded, "MKMMSDET", "streName")
    assert entry is not None
    assert entry.label == "Name"
    assert entry.type == "text"


def test_lookup_by_name_finds_a_nested_subtab_field(loaded: FieldMap) -> None:
    """A field defined inside `tabs[] > sub_tabs[] > fields[]` must resolve."""
    # `Submission Detail > Lines > Line > streLineIDNumber` per the real scrape.
    entry = fm.lookup_by_name(loaded, "MKMMSDET", "streLineIDNumber")
    assert entry is not None
    assert entry.label == "Line I D Number"
    assert entry.type == "text"


def test_lookup_by_name_misses_unknown(loaded: FieldMap) -> None:
    assert fm.lookup_by_name(loaded, "NOPE", "nope") is None
    assert fm.lookup_by_name(loaded, "MKMMSDET", "xxxNotAField") is None


def test_fields_for_screen_includes_tab_and_subtab_fields(
    loaded: FieldMap,
) -> None:
    fields = fm.fields_for_screen(loaded, "MKMMSDET")
    names = {e.name for e in fields}
    # Top-level field on the screen.
    assert "streName" in names
    # Sub-tab field on the same screen.
    assert "streLineIDNumber" in names
    # Servicing-tab field on the same screen.
    assert "streDescription1" in names
    # Many fields on this screen total.
    assert len(fields) >= 4


# --------------------------------------------------------------------------- #
# generate_domain_tag_enum
# --------------------------------------------------------------------------- #


def test_generate_domain_tag_enum_empty_when_no_tags(
    loaded: FieldMap,
) -> None:
    """The real scrape has no domain_tags yet; the enum starts empty."""
    enum = fm.generate_domain_tag_enum(loaded)
    assert enum == []


def test_generate_domain_tag_enum_after_updates_is_sorted_unique(
    loaded: FieldMap,
) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name"},
    )
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="dteEffective",
        patch={"domain_tag": "submission.effective_date"},
    )
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="dteExpiration",
        patch={"domain_tag": "submission.expiration_date"},
    )
    enum = fm.generate_domain_tag_enum(loaded)
    assert enum == [
        "submission.effective_date",
        "submission.expiration_date",
        "submission.name",
    ]


def test_generate_domain_tag_enum_skips_aliases(loaded: FieldMap) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={
            "domain_tag": "submission.name",
            "aliases": ["submission.display_name", "marketing.submission.name"],
        },
    )
    enum = fm.generate_domain_tag_enum(loaded)
    # Only the primary appears.
    assert enum == ["submission.name"]


# --------------------------------------------------------------------------- #
# update_field validation
# --------------------------------------------------------------------------- #


def test_update_field_rejects_unknown_field(loaded: FieldMap) -> None:
    with pytest.raises(FieldMapValidationError, match="No field"):
        fm.update_field(
            loaded,
            screen_code="MKMMSDET",
            name="not_a_real_name",
            patch={"domain_tag": "x.y"},
        )


def test_update_field_rejects_unknown_patch_key(loaded: FieldMap) -> None:
    with pytest.raises(FieldMapValidationError, match="Unknown patch key"):
        fm.update_field(
            loaded,
            screen_code="MKMMSDET",
            name="streName",
            patch={"version": 2},
        )


def test_update_field_rejects_bad_value_type(loaded: FieldMap) -> None:
    with pytest.raises(FieldMapValidationError, match="expects"):
        fm.update_field(
            loaded,
            screen_code="MKMMSDET",
            name="streName",
            patch={"is_required": "not-a-bool"},
        )


def test_update_field_rejects_non_string_in_list_patch(
    loaded: FieldMap,
) -> None:
    with pytest.raises(FieldMapValidationError, match="list\\[str\\]"):
        fm.update_field(
            loaded,
            screen_code="MKMMSDET",
            name="streName",
            patch={"aliases": ["ok", 42]},
        )


def test_update_field_atomic_on_failure(loaded: FieldMap) -> None:
    """A failing patch must leave the field unchanged (validate-before-apply)."""
    before = dict(loaded.raw["Submission Detail"]["fields"][0])
    with pytest.raises(FieldMapValidationError):
        fm.update_field(
            loaded,
            screen_code="MKMMSDET",
            name="streName",
            patch={
                "domain_tag": "submission.name",
                "is_required": "not-a-bool",  # invalid -> rolls back the whole patch
            },
        )
    after = dict(loaded.raw["Submission Detail"]["fields"][0])
    assert before == after


def test_update_field_preserves_existing_keys(loaded: FieldMap) -> None:
    """JIT enrichment must be additive only - never break the scrape keys."""
    entry = fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name", "is_required": True},
    )
    # Existing scrape keys remain intact.
    assert entry.type == "text"
    assert entry.label == "Name"
    assert entry.format == "old"
    assert entry.name == "streName"
    # New metadata applied.
    assert entry.domain_tag == "submission.name"
    assert entry.is_required is True


def test_update_field_changes_reflected_in_indices(loaded: FieldMap) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name"},
    )
    found = fm.lookup_by_domain_tag(loaded, "submission.name")
    assert found is not None
    assert found.name == "streName"


def test_update_field_aliases_resolve_back_to_primary(
    loaded: FieldMap,
) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={
            "domain_tag": "submission.name",
            "aliases": ["submission.display_name"],
        },
    )
    via_alias = fm.lookup_by_domain_tag(loaded, "submission.display_name")
    via_primary = fm.lookup_by_domain_tag(loaded, "submission.name")
    assert via_alias is not None
    assert via_primary is not None
    assert via_alias.name == via_primary.name == "streName"


def test_update_field_rename_drops_old_tag_index(loaded: FieldMap) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name"},
    )
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.display_name"},
    )
    assert fm.lookup_by_domain_tag(loaded, "submission.name") is None
    found = fm.lookup_by_domain_tag(loaded, "submission.display_name")
    assert found is not None and found.name == "streName"


# --------------------------------------------------------------------------- #
# screens_touched_by_domain_tags
# --------------------------------------------------------------------------- #


def test_screens_touched_by_domain_tags(loaded: FieldMap) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name"},
    )
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streLineIDNumber",
        patch={"domain_tag": "submission.line_id"},
    )
    touched = fm.screens_touched_by_domain_tags(
        loaded, ["submission.name", "submission.line_id", "totally.unknown"]
    )
    assert touched == {"MKMMSDET"}


def test_screens_touched_by_domain_tags_resolves_aliases(
    loaded: FieldMap,
) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={
            "domain_tag": "submission.name",
            "aliases": ["submission.alias_one"],
        },
    )
    touched = fm.screens_touched_by_domain_tags(
        loaded, ["submission.alias_one"]
    )
    assert touched == {"MKMMSDET"}


# --------------------------------------------------------------------------- #
# Round-trip and atomic write
# --------------------------------------------------------------------------- #


def test_round_trip_preserves_shape(tmp_field_map: Path) -> None:
    """load -> save_atomic -> reload must produce the identical raw structure."""
    a = fm.load(tmp_field_map)
    fm.save_atomic(a)
    b = fm.load(tmp_field_map)
    assert a.raw == b.raw


def test_save_atomic_creates_bak_file(tmp_field_map: Path) -> None:
    bak = tmp_field_map.with_suffix(tmp_field_map.suffix + ".bak")
    assert not bak.exists()
    a = fm.load(tmp_field_map)
    fm.save_atomic(a)
    assert bak.exists()
    # Bak holds the prior good copy (== current after the first save, since
    # we wrote the same content).
    bak_loaded = fm.load(bak)
    assert bak_loaded.raw == a.raw


def test_save_atomic_writes_then_reload_includes_patch(
    tmp_field_map: Path,
) -> None:
    a = fm.load(tmp_field_map)
    fm.update_field(
        a,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name", "is_required": True},
    )
    fm.save_atomic(a)
    b = fm.load(tmp_field_map)
    entry = fm.lookup_by_domain_tag(b, "submission.name")
    assert entry is not None
    assert entry.name == "streName"
    assert entry.is_required is True


def test_save_atomic_simulated_crash_leaves_original_intact(
    tmp_field_map: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails, the canonical file must be untouched."""
    a = fm.load(tmp_field_map)
    original_bytes = tmp_field_map.read_bytes()

    fm.update_field(
        a,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name"},
    )

    # Simulate the crash: os.replace raises after the .tmp file is written.
    real_replace = os.replace

    def fake_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(OSError, match="simulated crash"):
        fm.save_atomic(a)

    # Restore for the rest of the assertions.
    monkeypatch.setattr(os, "replace", real_replace)

    # Canonical file is byte-identical to its pre-crash content.
    assert tmp_field_map.read_bytes() == original_bytes
    # And reloading still works.
    b = fm.load(tmp_field_map)
    # The patch was only in-memory on `a`; on disk (and thus on `b`) the tag
    # was never persisted.
    assert fm.lookup_by_domain_tag(b, "submission.name") is None


def test_save_atomic_cleans_up_or_leaves_no_corrupt_canonical(
    tmp_field_map: Path,
) -> None:
    """A clean save followed by reload returns a structurally-identical map.

    Specifically: keys in the same order at the top level, same screen_code
    for every screen.
    """
    a = fm.load(tmp_field_map)
    fm.save_atomic(a)
    b = fm.load(tmp_field_map)
    assert list(a.raw.keys()) == list(b.raw.keys())
    for k in a.raw:
        assert a.raw[k].get("screen_code") == b.raw[k].get("screen_code")


# --------------------------------------------------------------------------- #
# Schema discipline (Path B)
# --------------------------------------------------------------------------- #


def test_no_version_key_in_allowed_patch_keys() -> None:
    """Path B / amendment #11: there is no `version` field on the Field Map."""
    assert "version" not in ALLOWED_PATCH_KEYS


def test_update_field_does_not_introduce_version_key(loaded: FieldMap) -> None:
    fm.update_field(
        loaded,
        screen_code="MKMMSDET",
        name="streName",
        patch={"domain_tag": "submission.name"},
    )
    assert "version" not in loaded.raw["Submission Detail"]["fields"][0]

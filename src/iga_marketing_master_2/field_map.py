"""field_map.py - load, query, and JIT-enrich the EPIC Field Map.

Reads ``Library/Epic Field Map.json`` (the authoritative ~1,631-entry field
universe). Provides lookups by ``domain_tag`` and ``(screen_code, name)``,
generates the ``domain_tag`` enum used in the Claude tool-use schema, and
supports JIT enrichment via :func:`update_field` + :func:`save_atomic`.

JIT enrichment writes new metadata back to the JSON file via plain atomic
replace (no ``version`` field, no compare-and-swap - single-user only per
Path B / PLAN-REVIEW amendment #11).

Schema fields managed (additive on top of existing scrape keys):
``is_required``, ``is_repeatable``, ``repeatable_group``, ``parent_field``,
``enum_values``, ``validation_pattern``, ``depends_on``, ``default_value``,
``domain_tag``, ``aliases``, ``last_verified_at``, ``epic_build_version``,
``notes_for_claude``.

See ARCHITECTURE.md sections 2 (naming), 3 (domain_tag grammar), 4 (Field Map
schema), 13 (rulings) and DECISION-MAP-field-map-agent.md.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "FieldEntry",
    "FieldMap",
    "FieldMapValidationError",
    "DEFAULT_FIELD_MAP_PATH",
    "ALLOWED_PATCH_KEYS",
    "load",
    "save_atomic",
    "lookup_by_domain_tag",
    "lookup_by_name",
    "fields_for_screen",
    "generate_domain_tag_enum",
    "update_field",
    "screens_touched_by_domain_tags",
]


_LOG = logging.getLogger("iga.field_map")


# --------------------------------------------------------------------------- #
# Constants / paths
# --------------------------------------------------------------------------- #


def _default_field_map_path() -> Path:
    """Return the canonical on-disk Field Map path (repo-root anchored).

    The module file lives at ``src/iga_marketing_master_2/field_map.py``;
    the repo root is therefore three levels up.
    """
    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    return repo_root / "Library" / "Epic Field Map.json"


DEFAULT_FIELD_MAP_PATH: Final[Path] = _default_field_map_path()


# Existing scrape keys that MUST be present on every leaf field.
_REQUIRED_EXISTING_KEYS: Final[frozenset[str]] = frozenset(
    {"type", "label", "name", "format"}
)

# All metadata keys that may be set via ``update_field`` and their accepted
# Python types. ``None`` is accepted for every nullable key (``str | None``,
# ``list | None``, etc.) per the schema in ARCHITECTURE.md section 4.2.
_PATCH_KEY_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "is_required": (bool,),
    "is_repeatable": (bool,),
    "repeatable_group": (str, type(None)),
    "parent_field": (str, type(None)),
    "enum_values": (list, type(None)),
    "validation_pattern": (str, type(None)),
    "depends_on": (list, type(None)),
    "default_value": (str, type(None)),
    "domain_tag": (str, type(None)),
    "aliases": (list,),
    "last_verified_at": (str, type(None)),
    "epic_build_version": (str, type(None)),
    "notes_for_claude": (str, type(None)),
}

ALLOWED_PATCH_KEYS: Final[frozenset[str]] = frozenset(_PATCH_KEY_TYPES.keys())


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class FieldMapValidationError(Exception):
    """Raised when the Field Map JSON is malformed, a required key is missing,
    or an :func:`update_field` patch contains unknown keys / invalid types /
    references a non-existent field.
    """


# --------------------------------------------------------------------------- #
# Typed views
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FieldEntry:
    """Typed view over a single leaf field in the Field Map.

    Wraps the underlying raw dict so mutations made via :func:`update_field`
    are reflected here automatically (the dataclass holds a reference to the
    dict, not a copy of its contents).

    Attribute access reads through the wrapped dict. Use the typed properties
    for clarity at call sites; the raw dict is available as :attr:`raw` for
    advanced cases.
    """

    raw: dict[str, Any]
    screen_code: str | None
    """The ``screen_code`` of the screen (or tab/sub-tab) that owns this field.

    Note: in the current scrape, sub-tabs inherit their parent screen's
    ``screen_code``, so this is the effective ``(screen_code, name)`` key
    that ``lookup_by_name`` accepts. May be ``None`` for fields living
    directly under a placeholder screen (rare).
    """

    # ---- Existing scrape keys (required) ----
    @property
    def type(self) -> str:
        return self.raw["type"]

    @property
    def label(self) -> str:
        return self.raw["label"]

    @property
    def name(self) -> str:
        return self.raw["name"]

    @property
    def format(self) -> str:
        return self.raw["format"]

    # ---- Existing optional scrape keys ----
    @property
    def required_legacy(self) -> bool:
        """Legacy ``required`` flag from the scraper. Preserved per
        ARCHITECTURE.md section 4.2; runtime decisions should use
        :attr:`is_required` instead.
        """
        return bool(self.raw.get("required", False))

    @property
    def hint(self) -> str | None:
        return self.raw.get("hint")

    @property
    def disabled(self) -> bool | None:
        return self.raw.get("disabled")

    @property
    def readonly(self) -> bool | None:
        return self.raw.get("readonly")

    @property
    def maxlength(self) -> int | None:
        return self.raw.get("maxlength")

    # ---- New JIT-enriched metadata keys ----
    @property
    def is_required(self) -> bool:
        return bool(self.raw.get("is_required", False))

    @property
    def is_repeatable(self) -> bool:
        return bool(self.raw.get("is_repeatable", False))

    @property
    def repeatable_group(self) -> str | None:
        return self.raw.get("repeatable_group")

    @property
    def parent_field(self) -> str | None:
        return self.raw.get("parent_field")

    @property
    def enum_values(self) -> list[str] | None:
        return self.raw.get("enum_values")

    @property
    def validation_pattern(self) -> str | None:
        return self.raw.get("validation_pattern")

    @property
    def depends_on(self) -> list[str] | None:
        return self.raw.get("depends_on")

    @property
    def default_value(self) -> str | None:
        return self.raw.get("default_value")

    @property
    def domain_tag(self) -> str | None:
        return self.raw.get("domain_tag")

    @property
    def aliases(self) -> list[str]:
        return list(self.raw.get("aliases", []))

    @property
    def last_verified_at(self) -> str | None:
        return self.raw.get("last_verified_at")

    @property
    def epic_build_version(self) -> str | None:
        return self.raw.get("epic_build_version")

    @property
    def notes_for_claude(self) -> str | None:
        return self.raw.get("notes_for_claude")


@dataclass(slots=True)
class FieldMap:
    """In-memory representation of the EPIC Field Map.

    Holds the raw nested JSON (so :func:`save_atomic` round-trips it exactly)
    plus three lookup indices. Indices reference the same field dicts that
    live inside :attr:`raw`, so mutations made via :func:`update_field` are
    visible without rebuilding.
    """

    source_path: Path
    raw: dict[str, Any]
    _by_screen_name: dict[tuple[str, str], FieldEntry] = field(default_factory=dict)
    _by_domain_tag: dict[str, FieldEntry] = field(default_factory=dict)
    _aliases: dict[str, str] = field(default_factory=dict)
    """Maps alias domain_tag -> primary domain_tag."""


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def load(path: Path | str | None = None) -> FieldMap:
    """Read the Field Map from disk into a :class:`FieldMap`.

    Validates structural integrity: the root must be a JSON object, every
    leaf field must carry the required existing keys (``type``, ``label``,
    ``name``, ``format``). Does NOT validate semantic content of new
    metadata (those are advisory).

    Idempotent; safe to call repeatedly. The returned object is independent
    on every call (no shared mutable state).

    :param path: Optional override; defaults to :data:`DEFAULT_FIELD_MAP_PATH`.
    :raises FieldMapValidationError: On malformed JSON, non-object root, or
        a leaf field missing one of ``{type, label, name, format}``.
    :raises OSError: If the file cannot be read.
    """
    target = Path(path) if path is not None else DEFAULT_FIELD_MAP_PATH
    try:
        with open(target, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except json.JSONDecodeError as exc:
        raise FieldMapValidationError(
            f"Field Map at {target} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise FieldMapValidationError(
            f"Field Map root must be a JSON object; got {type(raw).__name__}"
        )

    field_map = FieldMap(source_path=target, raw=raw)
    _build_indices(field_map)
    _LOG.debug(
        "field_map.loaded",
        extra={
            "path": str(target),
            "screen_count": len(raw),
            "indexed_fields": len(field_map._by_screen_name),
            "tagged_fields": len(field_map._by_domain_tag),
        },
    )
    return field_map


def save_atomic(field_map: FieldMap, path: Path | str | None = None) -> None:
    """Write the FieldMap back to disk via tmp + ``os.replace``.

    Updates a rolling ``<path>.bak`` (overwrites previous) before writing
    the new file. Single-writer assumption (Path B); no locking, no version
    field, no compare-and-swap.

    Atomic only on the same Windows volume; if the user picks a Working
    Library on a network drive, ``os.replace`` may raise ``OSError`` and the
    original file remains intact.

    :param field_map: The map to write.
    :param path: Optional override; defaults to ``field_map.source_path``.
    :raises OSError: Passed through from the underlying file ops.
    """
    target = Path(path) if path is not None else field_map.source_path
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    bak_path = target.with_suffix(target.suffix + ".bak")

    payload = json.dumps(field_map.raw, indent=2, ensure_ascii=False)
    payload_bytes = payload.encode("utf-8")

    # Three-step atomic write: copy current to .bak (rollback target), write
    # to .tmp on the same volume so os.replace is truly atomic, then swap. If
    # any step before the swap fails, the original Field Map is untouched.

    # Step 1: rotate the previous-good copy to .bak (only if canonical exists).
    if target.exists():
        shutil.copy2(target, bak_path)

    # Step 2: write to .tmp on the same volume.
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_path, "wb") as handle:
        handle.write(payload_bytes)
        handle.flush()
        try:
            os.fsync(handle.fileno())
        except OSError:
            # Some filesystems (notably mocked / network) do not support fsync;
            # the os.replace below is the actual atomicity guarantee.
            pass

    # Step 3: atomic swap.
    os.replace(tmp_path, target)
    _LOG.info(
        "field_map.saved",
        extra={"path": str(target), "bytes": len(payload_bytes)},
    )


def lookup_by_domain_tag(
    field_map: FieldMap, domain_tag: str
) -> FieldEntry | None:
    """Return the canonical entry for ``domain_tag``, resolving aliases.

    Returns ``None`` if no field carries that tag (or alias). Caller decides
    whether to treat that as JIT-proposal territory or as an error.
    """
    if not isinstance(domain_tag, str):
        return None
    primary = field_map._aliases.get(domain_tag, domain_tag)
    return field_map._by_domain_tag.get(primary)


def lookup_by_name(
    field_map: FieldMap, screen_code: str, name: str
) -> FieldEntry | None:
    """Return the entry for ``(screen_code, name)``.

    Used by selector resolution and by the Extractor's JIT proposal flow.
    Returns ``None`` if no such field exists.
    """
    return field_map._by_screen_name.get((screen_code, name))


def fields_for_screen(
    field_map: FieldMap, screen_code: str
) -> list[FieldEntry]:
    """Return all leaf fields whose effective ``screen_code`` matches.

    "Effective" means: a field defined inside a tab or sub-tab inherits the
    enclosing screen's ``screen_code`` (the on-disk scrape sets this on every
    tab/sub_tab object), so this returns the union of fields directly under
    the screen plus those under all of its tabs and sub-tabs.

    Order follows the on-disk traversal (depth-first, screen-first then tabs
    then sub-tabs), but callers should not rely on it for correctness.
    """
    return [
        entry
        for (sc, _name), entry in field_map._by_screen_name.items()
        if sc == screen_code
    ]


def generate_domain_tag_enum(field_map: FieldMap) -> list[str]:
    """Return the sorted list of all primary ``domain_tag`` values currently
    populated, suitable for inlining into the Claude tool-use schema.

    Aliases are excluded by construction. Returns ``[]`` when no fields have
    a tag yet (early v1 case) - the Extractor then drops the ``enum``
    constraint per ARCHITECTURE.md section 6.2.
    """
    return sorted(field_map._by_domain_tag.keys())


def update_field(
    field_map: FieldMap,
    *,
    screen_code: str,
    name: str,
    patch: dict[str, Any],
) -> FieldEntry:
    """Apply a metadata patch to a single field, in place.

    Mutates the in-memory FieldMap only - the caller is responsible for
    calling :func:`save_atomic` afterward (per ARCHITECTURE section 4.3 and
    DECISION-MAP-field-map-agent.md). This decouples write batching from
    the patch step: the GUI may flush per call (default) or after a
    confirmation batch.

    :param field_map: The map to mutate.
    :param screen_code: Effective ``screen_code`` of the target field.
    :param name: EPIC ``name`` attribute of the target field.
    :param patch: Mapping of new-metadata keys to values. Keys must be in
        :data:`ALLOWED_PATCH_KEYS`; values must match the documented type.
    :returns: The :class:`FieldEntry` view of the patched field.
    :raises FieldMapValidationError: Field not found, unknown patch key, or
        invalid value type.
    """
    entry = field_map._by_screen_name.get((screen_code, name))
    if entry is None:
        raise FieldMapValidationError(
            f"No field with (screen_code={screen_code!r}, name={name!r}) "
            f"in Field Map at {field_map.source_path}"
        )

    # Validate the whole patch up front so a half-applied patch never reaches
    # the entry. Either every key in the patch lands or none of them do.
    for key, value in patch.items():
        if key not in _PATCH_KEY_TYPES:
            raise FieldMapValidationError(
                f"Unknown patch key {key!r}; allowed: {sorted(ALLOWED_PATCH_KEYS)}"
            )
        allowed_types = _PATCH_KEY_TYPES[key]
        if not isinstance(value, allowed_types):
            allowed_names = ", ".join(t.__name__ for t in allowed_types)
            raise FieldMapValidationError(
                f"Patch key {key!r} expects {allowed_names}; "
                f"got {type(value).__name__}"
            )
        # Extra check for list-of-string keys.
        if key in ("aliases", "depends_on", "enum_values") and isinstance(
            value, list
        ):
            for i, item in enumerate(value):
                if not isinstance(item, str):
                    raise FieldMapValidationError(
                        f"Patch key {key!r} requires list[str]; "
                        f"item {i} is {type(item).__name__}"
                    )

    # Capture prior tag/aliases to keep indices in sync.
    prior_tag = entry.raw.get("domain_tag")
    prior_aliases = list(entry.raw.get("aliases", []))

    # Apply the patch.
    for key, value in patch.items():
        entry.raw[key] = value

    # The lookup indices live alongside the raw dict; if domain_tag or
    # aliases just changed, we have to rebuild those entries or future
    # lookups by tag will hit the wrong field (or miss entirely).
    new_tag = entry.raw.get("domain_tag")
    new_aliases = list(entry.raw.get("aliases", []))

    if prior_tag != new_tag:
        if prior_tag is not None:
            field_map._by_domain_tag.pop(prior_tag, None)
        if new_tag is not None:
            field_map._by_domain_tag[new_tag] = entry

    if prior_aliases != new_aliases or prior_tag != new_tag:
        # Drop any alias mappings that pointed at this entry's prior tag.
        if prior_tag is not None:
            stale = [a for a, p in field_map._aliases.items() if p == prior_tag]
            for a in stale:
                field_map._aliases.pop(a, None)
        # Re-register aliases against the (possibly new) primary tag.
        if new_tag is not None:
            for alias in new_aliases:
                field_map._aliases[alias] = new_tag

    _LOG.info(
        "field_map.update_field",
        extra={
            "screen_code": screen_code,
            "name": name,
            "patch_keys": sorted(patch.keys()),
            "domain_tag": new_tag,
        },
    )
    return entry


def screens_touched_by_domain_tags(
    field_map: FieldMap, tags: list[str]
) -> set[str]:
    """Return the set of ``screen_code`` values containing any of ``tags``.

    Used by ``enter.py`` (pre-flight selector smoke; ARCHITECTURE.md section
    7.3) so that startup checks only the screens this run will actually
    touch. Aliases are resolved. Tags that don't match any field, or fields
    whose ``screen_code`` is ``None``, contribute nothing to the result.
    """
    out: set[str] = set()
    for tag in tags:
        entry = lookup_by_domain_tag(field_map, tag)
        if entry is not None and entry.screen_code is not None:
            out.add(entry.screen_code)
    return out


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _build_indices(field_map: FieldMap) -> None:
    """Walk the raw nested JSON, validate required keys, and build indices.

    Traverses every screen at the root, then recurses through each screen's
    ``tabs[]`` and each tab's ``sub_tabs[]``, indexing every leaf field.

    A leaf field's ``screen_code`` is the screen_code of its immediate
    container (tab/sub_tab/screen). The on-disk scrape already propagates
    parent ``screen_code`` onto each tab/sub_tab object, so we read it
    locally; if it is missing or ``None``, the field is still indexable by
    ``(domain_tag)`` but not by ``(screen_code, name)``.
    """
    raw = field_map.raw

    for screen_label, screen_obj in raw.items():
        if not isinstance(screen_obj, dict):
            raise FieldMapValidationError(
                f"Screen {screen_label!r} is not a JSON object"
            )
        _walk_container(field_map, screen_obj, screen_label_path=screen_label)


def _walk_container(
    field_map: FieldMap,
    container: dict[str, Any],
    *,
    screen_label_path: str,
) -> None:
    """Recurse a container (screen / tab / sub_tab) and index its fields."""
    container_screen_code = container.get("screen_code")

    fields = container.get("fields", []) or []
    if not isinstance(fields, list):
        raise FieldMapValidationError(
            f"'fields' under {screen_label_path!r} is not a list"
        )
    for raw_field in fields:
        if not isinstance(raw_field, dict):
            raise FieldMapValidationError(
                f"Non-object field under {screen_label_path!r}"
            )
        _validate_required_keys(raw_field, screen_label_path)
        _index_field(field_map, raw_field, container_screen_code)

    tabs = container.get("tabs", []) or []
    if not isinstance(tabs, list):
        raise FieldMapValidationError(
            f"'tabs' under {screen_label_path!r} is not a list"
        )
    for tab in tabs:
        if not isinstance(tab, dict):
            raise FieldMapValidationError(
                f"Non-object tab under {screen_label_path!r}"
            )
        tab_label = tab.get("label", "<unnamed-tab>")
        _walk_container(
            field_map,
            tab,
            screen_label_path=f"{screen_label_path} > tab[{tab_label}]",
        )

    sub_tabs = container.get("sub_tabs", []) or []
    if not isinstance(sub_tabs, list):
        raise FieldMapValidationError(
            f"'sub_tabs' under {screen_label_path!r} is not a list"
        )
    for sub_tab in sub_tabs:
        if not isinstance(sub_tab, dict):
            raise FieldMapValidationError(
                f"Non-object sub_tab under {screen_label_path!r}"
            )
        sub_label = sub_tab.get("label", "<unnamed-sub_tab>")
        _walk_container(
            field_map,
            sub_tab,
            screen_label_path=f"{screen_label_path} > sub_tab[{sub_label}]",
        )


def _validate_required_keys(raw_field: dict[str, Any], path: str) -> None:
    missing = _REQUIRED_EXISTING_KEYS - raw_field.keys()
    if missing:
        raise FieldMapValidationError(
            f"Field at {path!r} (name={raw_field.get('name')!r}) "
            f"missing required existing keys: {sorted(missing)}"
        )


def _index_field(
    field_map: FieldMap,
    raw_field: dict[str, Any],
    container_screen_code: str | None,
) -> None:
    entry = FieldEntry(raw=raw_field, screen_code=container_screen_code)
    name = raw_field["name"]

    if container_screen_code is not None:
        # On collision (same (screen_code, name)), the last-walked wins.
        # In the wild this is rare; tabs and sub_tabs each carry a unique
        # field set per the scrape.
        field_map._by_screen_name[(container_screen_code, name)] = entry

    domain_tag = raw_field.get("domain_tag")
    if isinstance(domain_tag, str) and domain_tag:
        field_map._by_domain_tag[domain_tag] = entry

    aliases = raw_field.get("aliases", [])
    if isinstance(aliases, list) and isinstance(domain_tag, str) and domain_tag:
        for alias in aliases:
            if isinstance(alias, str) and alias:
                field_map._aliases[alias] = domain_tag

"""walker.py — walk the Field Map and produce annotation candidates.

Responsibilities:
  - Recursively visit every field, including those nested in tabs / sub_tabs.
  - Filter to Tier 1 scope per the locked decisions:
      * Out: premium fields (label/name contains "premium" or starts with "cure*Prem*")
      * Out: lookup widgets (streLookup, streLookupCode, streAutoLine, streGLLine,
        streELLine, streOtherLine, streOtherLine2)
      * Out: screens not in the Tier 1 surface
  - Stamp each candidate with the path needed to write_back its annotation.
  - Propose a `domain_tag` using heuristics + screen→namespace mapping.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Tier 1 in-scope screens
# ---------------------------------------------------------------------------

# Screens we walk for Tier 1. Anything not in this set is deferred.
TIER1_SCREENS: frozenset[str] = frozenset({
    "Submission Detail",

    "Commercial AP > Applicant",
    "Commercial AP > OtherNamedInsureds",
    "Commercial AP > Premise",

    "General Liability > Coverages",
    "General Liability > Hazards",
    "General Liability > AdditionalInterest",
    "General Liability > FormEndorsement",

    "Property > Subject",
    "Property > Premise",
    "Property > AdditionalInterest",
    "Property > FormEndorsement",
    "Property > Main",

    "Business Auto > Vehicle",
    "Business Auto > Driver",
    "Business Auto > CoverageTN",
    "Business Auto > AdditionalInterest",
    "Business Auto > FormEndorsement",
    "Business Auto > GeneralInfo201005",

    "Inland Marine- Commercial > CoverageDeductible",
    "Inland Marine- Commercial > ScheduledEquipment",
    "Inland Marine- Commercial > UnscheduledEquipment",
    "Inland Marine- Commercial > AdditionalInterest",
    "Inland Marine- Commercial > FormEndorsement",
    "Inland Marine- Commercial > GeneralInformation",

    "Worker's Compensation > Applicant",
    "Worker's Compensation > PolicyInfoTotalPremium",
    "Worker's Compensation > Location",
    "Worker's Compensation > FormEndorsement",

    "Commercial Umbrella > PolicyInformation",
    "Commercial Umbrella > UnderlyingInsurance",
    "Commercial Umbrella > AdditionalInterest",
    "Commercial Umbrella > FormEndorsement",
    "Commercial Umbrella > GeneralInformation",
})

# Order in which screens are walked. Matches the GUI tab order.
SCREEN_ORDER: tuple[str, ...] = (
    "Submission Detail",
    "Commercial AP > Applicant",
    "Commercial AP > OtherNamedInsureds",
    "Commercial AP > Premise",
    "Property > Premise",
    "Property > Main",

    "General Liability > Coverages",
    "General Liability > Hazards",
    "General Liability > AdditionalInterest",
    "General Liability > FormEndorsement",

    "Property > Subject",
    "Property > AdditionalInterest",
    "Property > FormEndorsement",

    "Business Auto > GeneralInfo201005",
    "Business Auto > CoverageTN",
    "Business Auto > Vehicle",
    "Business Auto > Driver",
    "Business Auto > AdditionalInterest",
    "Business Auto > FormEndorsement",

    "Inland Marine- Commercial > GeneralInformation",
    "Inland Marine- Commercial > CoverageDeductible",
    "Inland Marine- Commercial > ScheduledEquipment",
    "Inland Marine- Commercial > UnscheduledEquipment",
    "Inland Marine- Commercial > AdditionalInterest",
    "Inland Marine- Commercial > FormEndorsement",

    "Worker's Compensation > Applicant",
    "Worker's Compensation > PolicyInfoTotalPremium",
    "Worker's Compensation > Location",
    "Worker's Compensation > FormEndorsement",

    "Commercial Umbrella > GeneralInformation",
    "Commercial Umbrella > PolicyInformation",
    "Commercial Umbrella > UnderlyingInsurance",
    "Commercial Umbrella > AdditionalInterest",
    "Commercial Umbrella > FormEndorsement",
)

# Lookup-widget field names that should be skipped (per Q6 decision).
LOOKUP_NAMES: frozenset[str] = frozenset({
    "streLookup", "streLookupCode",
    "streAutoLine", "streGLLine", "streELLine", "streOtherLine", "streOtherLine2",
})

# UI-control field names with no data semantics (master toggle, etc.).
UI_CONTROL_NAMES: frozenset[str] = frozenset({
    "check-all",
})

# Regex for premium field names (Q6 global premiums-out rule).
_PREMIUM_NAME_RE = re.compile(r"premium|prem(?![a-z])", re.IGNORECASE)
_PREMIUM_LABEL_RE = re.compile(r"\bpremium\b", re.IGNORECASE)

# EPIC name prefixes the proposer strips when deriving a tag leaf.
_NAME_PREFIXES = (
    "stre", "inte", "dece", "dec", "cure", "curc", "cur",
    "pere", "per", "dte", "cbo", "chk", "phe", "sece", "secc", "sec",
    "ade", "fra",  # frame/radio
)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class FieldRef:
    """One annotation candidate. Carries the path needed to write back."""
    screen: str                     # top-level screen key, e.g. "General Liability > Coverages"
    nested_path: list[Any] = field(default_factory=list)
    """Path inside the screen dict to the field, e.g.
       [('tabs', 0), ('fields', 5)] = screen['tabs'][0]['fields'][5].
       Top-level fields use [('fields', N)]."""
    name: str = ""
    label: str = ""
    type: str = ""
    automation_id: str | None = None
    nested_label_path: str = ""     # human-readable, e.g. "/tab:Vehicle"
    raw: dict | None = None         # the actual field dict (for in-place edits)
    proposed_tag: str | None = None
    proposed_aliases: list[str] = field(default_factory=list)
    proposed_notes: str = ""

    def display_path(self) -> str:
        if self.nested_label_path:
            return f"{self.screen}{self.nested_label_path}"
        return self.screen

    def field_id(self) -> str:
        """Stable identifier for resume tracking."""
        return f"{self.screen}|{self.nested_label_path}|{self.name}"


# ---------------------------------------------------------------------------
# Walker
# ---------------------------------------------------------------------------


def _is_in_scope(field_dict: dict) -> tuple[bool, str | None]:
    """Return (in_scope, reason_if_out). Premiums and lookups are out."""
    if not isinstance(field_dict, dict):
        return False, "not a dict"
    name = field_dict.get("name") or ""
    label = field_dict.get("label") or ""
    if name in LOOKUP_NAMES:
        return False, "lookup widget (Q6)"
    if name in UI_CONTROL_NAMES:
        return False, "UI control (no data)"
    if _PREMIUM_NAME_RE.search(name) or _PREMIUM_LABEL_RE.search(label):
        return False, "premium field (Q6 global)"
    return True, None


def _walk_recursive(node: dict, path: list[Any], label_path: str, screen: str):
    """Yield FieldRef for every field in this node and its tabs/sub_tabs."""
    if not isinstance(node, dict):
        return

    fields = node.get("fields") or []
    for i, f in enumerate(fields):
        if not isinstance(f, dict):
            continue
        in_scope, _ = _is_in_scope(f)
        if not in_scope:
            continue
        yield FieldRef(
            screen=screen,
            nested_path=path + [("fields", i)],
            name=f.get("name") or "",
            label=f.get("label") or "",
            type=f.get("type") or "",
            automation_id=f.get("automation_id"),
            nested_label_path=label_path,
            raw=f,
        )

    for ti, tab in enumerate(node.get("tabs") or []):
        if isinstance(tab, dict):
            tlabel = tab.get("label", "?")
            yield from _walk_recursive(
                tab,
                path + [("tabs", ti)],
                f"{label_path}/tab:{tlabel}",
                screen,
            )

    for si, sub in enumerate(node.get("sub_tabs") or []):
        if isinstance(sub, dict):
            slabel = sub.get("label", "?")
            yield from _walk_recursive(
                sub,
                path + [("sub_tabs", si)],
                f"{label_path}/subtab:{slabel}",
                screen,
            )


def walk(field_map: dict) -> list[FieldRef]:
    """Walk in-scope screens in canonical order. Returns FieldRef list."""
    out: list[FieldRef] = []
    seen_screens: set[str] = set()
    # Walk in canonical order first.
    for screen_name in SCREEN_ORDER:
        if screen_name not in TIER1_SCREENS:
            continue
        screen = field_map.get(screen_name)
        if not isinstance(screen, dict):
            continue
        seen_screens.add(screen_name)
        out.extend(_walk_recursive(screen, [], "", screen_name))
    # Catch any in-scope screen that wasn't in SCREEN_ORDER.
    for screen_name in TIER1_SCREENS - seen_screens:
        screen = field_map.get(screen_name)
        if isinstance(screen, dict):
            out.extend(_walk_recursive(screen, [], "", screen_name))
    return out


# ---------------------------------------------------------------------------
# Tag proposer — heuristic, not authoritative
# ---------------------------------------------------------------------------


# Map (screen, nested_label_path) → namespace prefix. The annotator can override.
_SCREEN_NAMESPACES: dict[str, str] = {
    "Submission Detail":                              "submission",
    "Commercial AP > Applicant":                      "account.named_insured",
    "Commercial AP > OtherNamedInsureds":             "account.named_insured",
    "Commercial AP > Premise":                        "location",
    "Property > Premise":                             "location",
    "Property > Main":                                "policy.property",

    "General Liability > Coverages":                  "policy.gl",
    "General Liability > Hazards":                    "policy.gl.hazard",
    "General Liability > AdditionalInterest":         "policy.gl.additional_interest",
    "General Liability > FormEndorsement":            "policy.gl.policy_form",

    "Property > Subject":                             "policy.property.subject",
    "Property > AdditionalInterest":                  "policy.property.additional_interest",
    "Property > FormEndorsement":                     "policy.property.policy_form",

    "Business Auto > GeneralInfo201005":              "policy.auto",
    "Business Auto > CoverageTN":                     "policy.auto",
    "Business Auto > Vehicle":                        "policy.auto.vehicle",
    "Business Auto > Driver":                         "policy.auto.driver",
    "Business Auto > AdditionalInterest":             "policy.auto.additional_interest",
    "Business Auto > FormEndorsement":                "policy.auto.policy_form",

    "Inland Marine- Commercial > GeneralInformation": "policy.inland_marine",
    "Inland Marine- Commercial > CoverageDeductible": "policy.inland_marine",
    "Inland Marine- Commercial > ScheduledEquipment": "policy.inland_marine.scheduled_item",
    "Inland Marine- Commercial > UnscheduledEquipment":"policy.inland_marine.unscheduled_item",
    "Inland Marine- Commercial > AdditionalInterest": "policy.inland_marine.additional_interest",
    "Inland Marine- Commercial > FormEndorsement":    "policy.inland_marine.policy_form",

    "Worker's Compensation > Applicant":              "policy.workers_comp",
    "Worker's Compensation > PolicyInfoTotalPremium": "policy.workers_comp",
    "Worker's Compensation > Location":               "policy.workers_comp.class_code",
    "Worker's Compensation > FormEndorsement":        "policy.workers_comp.policy_form",

    "Commercial Umbrella > GeneralInformation":       "policy.umbrella",
    "Commercial Umbrella > PolicyInformation":        "policy.umbrella",
    "Commercial Umbrella > UnderlyingInsurance":      "policy.umbrella.underlying",  # special-cased
    "Commercial Umbrella > AdditionalInterest":       "policy.umbrella.additional_interest",
    "Commercial Umbrella > FormEndorsement":          "policy.umbrella.policy_form",
}


def _strip_name_prefix(name: str) -> str:
    """Strip a known EPIC type-hint prefix from a field name."""
    n = name
    for p in _NAME_PREFIXES:
        if n.lower().startswith(p) and len(n) > len(p) and n[len(p)].isupper():
            n = n[len(p):]
            break
    # Strip common EPIC widget suffixes
    for suffix in ("-mask", "-text", "_mask"):
        if n.endswith(suffix):
            n = n[: -len(suffix)]
    return n


def _camel_to_snake(s: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def _label_to_snake(label: str) -> str:
    s = label.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s


def _normalize_leaf_segments(tag: str) -> str:
    """Final cleanup of an entire dotted tag: each segment must be safe."""
    parts = tag.split(".")
    cleaned = []
    for seg in parts:
        s = seg.lower()
        s = re.sub(r"[^a-z0-9_]+", "_", s)
        s = re.sub(r"_+", "_", s).strip("_")
        if not s:
            continue
        # Segment must start with a letter
        if s[0].isdigit():
            s = f"x_{s}"
        cleaned.append(s)
    return ".".join(cleaned)


def _propose_address_subtag(name: str) -> str | None:
    """For composite address fields, return the address sub-segment.

    e.g., 'adeMailing-streetLine' -> 'mailing_address.line_1'
          'adeMailing-state'      -> 'mailing_address.state'
          'adeMailing'            -> 'mailing_address'  (parent only)
    """
    if not name.lower().startswith("ade"):
        return None
    # Body is whatever comes after 'ade'
    body = name[3:]
    parts = re.split(r"[-_]", body, maxsplit=1)
    base = parts[0]
    suffix = parts[1] if len(parts) > 1 else ""

    base_snake = _camel_to_snake(base)
    # 'Mailing' -> 'mailing_address', 'Primary' -> 'primary_address',
    # 'Garage' -> 'garage_address', 'Interest' -> 'interest_address', etc.
    if not base_snake.endswith("address"):
        base_snake = f"{base_snake}_address"

    if not suffix:
        return base_snake  # parent only — caller must expand or emit one tag

    suffix_lc = suffix.lower()
    if "street" in suffix_lc:
        return f"{base_snake}.line_1"
    if suffix_lc in {"city"}:
        return f"{base_snake}.city"
    if suffix_lc in {"state"}:
        return f"{base_snake}.state"
    if suffix_lc in {"zip", "zip_code"}:
        return f"{base_snake}.zip"
    if suffix_lc == "country":
        return f"{base_snake}.country"
    return f"{base_snake}.{_camel_to_snake(suffix)}"


def _propose_underlying_tag(field_ref: FieldRef) -> str | None:
    """Special-case proposer for Commercial Umbrella > UnderlyingInsurance.

    EPIC has parallel field blocks: streAutoLine, streGLPolNum, streELDiseaseEmpLimit,
    streOther*1, streOther*2. Map by scanning the name for a known LOB token.
    """
    name = field_ref.name
    body = _strip_name_prefix(name)

    # Detect line and slot from the body.
    line: str | None = None
    if body.startswith("Auto"):
        line = "auto"
        body = body[4:]
    elif body.startswith("GL"):
        line = "gl"
        body = body[2:]
    elif body.startswith("EL"):
        line = "el"
        body = body[2:]
    elif body.startswith("Other"):
        # Other repeatable; trailing '2' indicates slot 2.
        line = "other"
        body = body[5:]
        # Strip trailing slot number
        body = re.sub(r"^([A-Za-z]+?)(\d+)$", r"\1", body) if body and body[-1].isdigit() else body

    if line is None:
        return None

    leaf = _camel_to_snake(body) if body else "value"
    if line == "other":
        return _normalize_leaf_segments(f"policy.umbrella.underlying.other.{leaf}")
    return _normalize_leaf_segments(f"policy.umbrella.underlying.{line}.{leaf}")


def propose_tag(field_ref: FieldRef) -> str | None:
    """Propose a domain_tag based on the field's screen + name + label."""
    screen_ns = _SCREEN_NAMESPACES.get(field_ref.screen)

    # Special: Commercial Umbrella > UnderlyingInsurance has parallel blocks.
    if field_ref.screen == "Commercial Umbrella > UnderlyingInsurance":
        return _propose_underlying_tag(field_ref)

    if screen_ns is None:
        return None

    name = field_ref.name
    label = field_ref.label

    # Composite address fields get expanded to the new 7-sub-tag schema.
    # If the name is the parent only (e.g., 'adeMailing'), default to '.line_1'
    # — the operator can edit if they want a different sub-tag, and the JIT
    # enrichment plan covers filling the rest at entry time.
    addr = _propose_address_subtag(name)
    if addr is not None:
        if "." not in addr:
            addr = f"{addr}.line_1"
        return _normalize_leaf_segments(f"{screen_ns}.{addr}")

    # Strip EPIC type-hint prefix; convert to snake_case.
    body = _strip_name_prefix(name)
    leaf = _camel_to_snake(body) if body else _label_to_snake(label)
    if not leaf:
        leaf = _label_to_snake(label) or "field"
    # Tidy common patterns.
    leaf = leaf.replace("__", "_").strip("_")

    if not leaf:
        return None

    # If this field is a date and the leaf doesn't end in '_date', add it.
    # (Convention from earlier decisions: dates use '_date' suffix.)
    if field_ref.type in {"date", "datetime"} and not leaf.endswith("_date"):
        leaf = f"{leaf}_date"

    return _normalize_leaf_segments(f"{screen_ns}.{leaf}")


# ---------------------------------------------------------------------------
# Write-back
# ---------------------------------------------------------------------------


def write_annotation(
    field_map: dict,
    ref: FieldRef,
    *,
    domain_tag: str | None,
    aliases: list[str],
    notes_for_claude: str | None,
    status: str,
) -> dict:
    """Mutate the field dict in-place and return it.

    `status` is "verified", "proposed", or "unmapped".
    """
    target = ref.raw
    if target is None:
        # Re-resolve via the path in case raw wasn't kept.
        screen = field_map[ref.screen]
        node: Any = screen
        for key, idx in ref.nested_path[:-1]:
            node = node[key][idx]
        last_key, last_idx = ref.nested_path[-1]
        target = node[last_key][last_idx]
    assert isinstance(target, dict)

    target["domain_tag"] = domain_tag
    target["aliases"] = list(aliases or [])
    target["notes_for_claude"] = notes_for_claude or None
    target["domain_tag_status"] = status
    return target

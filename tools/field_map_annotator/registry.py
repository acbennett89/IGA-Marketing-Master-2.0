"""registry.py — namespace registry and grammar validation.

Single source of truth for which `domain_tag` namespaces are allowed
and how they map to GUI tabs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# domain_tag grammar: lowercase ASCII, underscores, dots between segments
_TAG_RE = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")


@dataclass(frozen=True)
class Namespace:
    prefix: str               # e.g. "policy.gl" or "account.named_insured"
    display: str              # e.g. "General Liability"
    is_repeatable: bool       # repeatable group?
    hidden: bool              # extracted but no GUI tab?


# The locked registry from our Q&A. Order matters for prefix matching:
# longer prefixes must come first so "policy.auto.vehicle" matches before "policy.auto".
NAMESPACES: tuple[Namespace, ...] = (
    # 3-segment LOB-nested repeatables
    Namespace("policy.auto.vehicle",                "Vehicles (Business Auto)",       True,  False),
    Namespace("policy.auto.driver",                 "Drivers (Business Auto)",        True,  False),
    Namespace("policy.auto.additional_interest",    "Additional Interests (Auto)",    True,  False),
    Namespace("policy.auto.additional_coverage",    "Additional Coverages (Auto)",    True,  False),
    Namespace("policy.auto.policy_form",            "Policy Forms (Auto)",            True,  False),
    Namespace("policy.gl.hazard",                   "Hazards (GL)",                   True,  False),
    Namespace("policy.gl.additional_interest",      "Additional Interests (GL)",      True,  False),
    Namespace("policy.gl.additional_coverage",      "Additional Coverages (GL)",      True,  False),
    Namespace("policy.gl.policy_form",              "Policy Forms (GL)",              True,  False),
    Namespace("policy.property.subject",            "Coverage Subjects (Property)",   True,  False),
    Namespace("policy.property.additional_interest","Additional Interests (Property)",True,  False),
    Namespace("policy.property.additional_coverage","Additional Coverages (Property)",True,  False),
    Namespace("policy.property.policy_form",        "Policy Forms (Property)",        True,  False),
    Namespace("policy.inland_marine.scheduled_item",   "Scheduled Equipment (IM)",    True,  False),
    Namespace("policy.inland_marine.unscheduled_item", "Unscheduled Equipment (IM)",  True,  False),
    Namespace("policy.inland_marine.additional_interest","Additional Interests (IM)", True,  False),
    Namespace("policy.inland_marine.additional_coverage","Additional Coverages (IM)", True,  False),
    Namespace("policy.inland_marine.policy_form",   "Policy Forms (IM)",              True,  False),
    Namespace("policy.workers_comp.class_code",     "Class Codes (WC)",               True,  False),
    Namespace("policy.workers_comp.additional_coverage","Additional Coverages (WC)",  True,  False),
    Namespace("policy.workers_comp.policy_form",    "Policy Forms (WC)",              True,  False),
    Namespace("policy.umbrella.underlying.auto",    "Underlying Auto (Umbrella)",     False, False),
    Namespace("policy.umbrella.underlying.gl",      "Underlying GL (Umbrella)",       False, False),
    Namespace("policy.umbrella.underlying.el",      "Underlying EL (Umbrella)",       False, False),
    Namespace("policy.umbrella.underlying.other",   "Underlying Other (Umbrella)",    True,  False),
    Namespace("policy.umbrella.additional_interest","Additional Interests (Umbrella)",True,  False),
    Namespace("policy.umbrella.additional_coverage","Additional Coverages (Umbrella)",True,  False),
    Namespace("policy.umbrella.policy_form",        "Policy Forms (Umbrella)",        True,  False),

    # 2-segment LOB singletons
    Namespace("policy.gl",                          "General Liability",              False, False),
    Namespace("policy.property",                    "Property",                       False, False),
    Namespace("policy.auto",                        "Business Auto",                  False, False),
    Namespace("policy.inland_marine",               "Inland Marine",                  False, False),
    Namespace("policy.workers_comp",                "Workers Compensation",           False, False),
    Namespace("policy.umbrella",                    "Umbrella / Excess",              False, False),

    # 2-segment account repeatable
    Namespace("account.named_insured",              "Named Insureds",                 True,  False),

    # 1-segment cross-LOB repeatables
    Namespace("location",                           "Locations",                      True,  False),
    Namespace("prior_carrier",                      "Prior Carriers (hidden)",        True,  True),
    Namespace("loss",                               "Loss History (hidden)",          True,  True),

    # 1-segment singletons
    Namespace("submission",                         "Submission (hidden)",            False, True),
    Namespace("producer",                           "Producer (hidden)",              False, True),
    Namespace("notes",                              "Notes",                          False, False),
)


def find_namespace(tag: str) -> Namespace | None:
    """Return the registered namespace whose prefix matches ``tag``, longest first."""
    for ns in NAMESPACES:
        if tag == ns.prefix or tag.startswith(ns.prefix + "."):
            return ns
    return None


def validate_tag(tag: str) -> tuple[bool, str | None]:
    """Return (is_valid, reason_if_invalid)."""
    if not isinstance(tag, str) or not tag:
        return False, "tag must be a non-empty string"
    if not _TAG_RE.match(tag):
        return False, "tag must match grammar: lowercase letters/digits/underscores, dot-separated"
    if find_namespace(tag) is None:
        return False, f"tag does not start with a registered namespace"
    return True, None


# Common allowed enum values for cross-cutting fields
ADDITIONAL_INTEREST_TYPES: tuple[str, ...] = (
    "loss_payee",
    "additional_insured",
    "mortgagee",
    "lender_loss_payee",
    "certificate_holder",
)

NAMED_INSURED_TYPES: tuple[str, ...] = (
    "primary",
    "dba",
    "additional",
)

PROPERTY_COVERAGE_TYPES: tuple[str, ...] = (
    "building",
    "bpp",
    "bi",
    "extra_expense",
    "other",
)

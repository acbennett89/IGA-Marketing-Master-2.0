"""section_forms_layout.py — column-to-EPIC-field mappings per section form.

Bridges the GUI's curated column layout to the Field Map's auto-generated
``domain_tag``s. Each :class:`ColumnSpec` declares: this column shows the
field at ``(screen, name)``; the section form resolves to the canonical
``domain_tag`` via :func:`field_map.tag_for_field` at refresh time.

Why this exists: the Field Map's ``domain_tag``s are auto-proposed from
EPIC labels and follow EPIC's terse vocabulary
(``policy.gl.gen_aggr_app_limit``). The section forms' columns use the
operator's vocabulary (``General Aggregate``). Both live in their own
files and are bridged here.

Adding a new form? Define its ``ColumnSpec`` tuple here and import it from
``section_forms.py``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnSpec:
    """One column in a section-form table or one input in a section-form grid.

    :ivar label:  Operator-facing column header / input label.
    :ivar screen: Field Map screen display name (e.g., ``"Commercial AP > Applicant"``).
    :ivar name:   EPIC field ``name`` attribute.
    """

    label: str
    screen: str
    name: str


# ---------------------------------------------------------------------------
# Named Insureds tab
# ---------------------------------------------------------------------------
# All named insureds are rows in a single repeatable group
# `account.named_insured.*`. All columns resolve via the OtherNamedInsureds
# screen, since EPIC stores every named insured on that one screen.
NAMED_INSUREDS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("Entity Name",      "Commercial AP > OtherNamedInsureds", "streName"),
    ColumnSpec("Business Type",    "Commercial AP > OtherNamedInsureds", "cboBusinessType"),
    ColumnSpec("FEIN / Tax ID",    "Commercial AP > OtherNamedInsureds", "streFEIN"),
    ColumnSpec("Address",          "Commercial AP > OtherNamedInsureds", "adeMailing-streetLine"),
    ColumnSpec("State",            "Commercial AP > OtherNamedInsureds", "adeMailing-state"),
    ColumnSpec("Email",            "Commercial AP > OtherNamedInsureds", "streEmail"),
    ColumnSpec("Website",          "Commercial AP > OtherNamedInsureds", "streWebsite"),
)


# ---------------------------------------------------------------------------
# Locations tab
# ---------------------------------------------------------------------------
LOCATIONS_COLUMNS: tuple[ColumnSpec, ...] = (
    ColumnSpec("Loc #",             "Commercial AP > Premise", "synthetic_LocationNumber"),
    ColumnSpec("Bldg #",            "Commercial AP > Premise", "inteBuildingNumber"),
    # Address column. EPIC's `streBuildingDescription` typically holds the
    # full street address; if a separate description field exists in the
    # data it'll show in the next column.
    ColumnSpec("Address",           "Commercial AP > Premise", "streBuildingDescription"),
    # Description column — distinct from address. Synthetic tag for now;
    # extraction can populate it when the dec page lists a description
    # separate from the address (e.g., "Main Office Building").
    ColumnSpec("Description",       "Commercial AP > Premise", "streSiteID"),
    ColumnSpec("Annual Revenue",    "Commercial AP > Premise", "deceAnnualRev"),
    ColumnSpec("FT Employees",      "Commercial AP > Premise", "inteNumberEmployees"),
    ColumnSpec("PT Employees",      "Commercial AP > Premise", "inteNumPartTimeEmployees"),
    ColumnSpec("Total Area",        "Commercial AP > Premise", "inteTotalArea"),
)


# ---------------------------------------------------------------------------
# General Liability tab — singleton inputs
# ---------------------------------------------------------------------------
# These fields render as text inputs / dropdowns at the top of the GL form,
# not as table rows. Each has a (label, screen, name) tuple resolved to a
# domain_tag at form-build time.
GL_TOP_FIELDS: tuple[ColumnSpec, ...] = (
    # Top-row info
    # NB: Policy #, eff/exp dates aren't on the Coverages screen — they're
    # on the broader "Status" / GeneralInfo screen. Annotator captured these
    # under (Commercial AP > Status) for now.
    # Limits (all on General Liability > Coverages)
    ColumnSpec("General Aggregate",                 "General Liability > Coverages", "streGenAggrAppLimit"),
    ColumnSpec("Products / Comp-Ops Aggregate",     "General Liability > Coverages", "streProdOperLimit"),
    ColumnSpec("Personal & Advertising Injury",     "General Liability > Coverages", "strePersAdvInjLimit"),
    ColumnSpec("Each Occurrence",                   "General Liability > Coverages", "streEachOccLimit"),
    ColumnSpec("Damage to Premises Rented",         "General Liability > Coverages", "streDamPremLimit"),
    ColumnSpec("Medical Payments",                  "General Liability > Coverages", "streMedLimit"),
    ColumnSpec("Employee Benefits Limit",           "General Liability > Coverages", "streEmpBenLimit"),
    ColumnSpec("BI Deductible",                     "General Liability > Coverages", "streBIDed"),
    ColumnSpec("PD Deductible",                     "General Liability > Coverages", "strePDDed"),
    ColumnSpec("Premium Basis",                     "General Liability > Coverages", "cboBasis"),
)


# ---------------------------------------------------------------------------
# Business Auto tab — singleton inputs (top + coverages)
# ---------------------------------------------------------------------------
AUTO_TOP_FIELDS: tuple[ColumnSpec, ...] = (
    # All on Business Auto > CoverageTN. This is the TN-state-specific
    # screen the scraper captured; per-state variants exist but the names
    # match closely enough that the verified tag is what matters.
    ColumnSpec("Combined Single Limit (CSL)",       "Business Auto > CoverageTN", "streLiabilityCSLLimit1"),
    ColumnSpec("Bodily Injury — Per Accident",      "Business Auto > CoverageTN", "streLiabilityBILimit1"),
    ColumnSpec("Bodily Injury — Per Person",        "Business Auto > CoverageTN", "streLiabilityBILimit2"),
    ColumnSpec("Property Damage",                   "Business Auto > CoverageTN", "streLiabilityPDLimit1"),
    ColumnSpec("Medical Payments",                  "Business Auto > CoverageTN", "streMedicalLimit1"),
    ColumnSpec("Uninsured Motorist (CSL)",          "Business Auto > CoverageTN", "streUninsuredCSLLimit1"),
    ColumnSpec("Uninsured Motorist (BI/Acc)",       "Business Auto > CoverageTN", "streUninsuredBILimit1"),
    ColumnSpec("Uninsured Motorist (BI/Person)",    "Business Auto > CoverageTN", "streUninsuredBILimit2"),
    ColumnSpec("Uninsured PD Each Accident",        "Business Auto > CoverageTN", "streUninsuredPDEachAccident"),
    ColumnSpec("Uninsured PD Deductible",           "Business Auto > CoverageTN", "streUninsuredPDDeductible"),
    ColumnSpec("Towing Limit",                      "Business Auto > CoverageTN", "streTowingLimit1"),
    ColumnSpec("Comprehensive Deductible",          "Business Auto > CoverageTN", "streComprehensiveDeductible1"),
    ColumnSpec("Cause of Loss Deductible",          "Business Auto > CoverageTN", "streCauseOfLossDeductible1"),
    ColumnSpec("Collision Deductible",              "Business Auto > CoverageTN", "streCollisionDeductible1"),
)

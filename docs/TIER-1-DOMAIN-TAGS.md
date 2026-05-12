# Tier 1 Domain Tag Candidate List

**Status:** 🟡 DRAFT — for review before annotator tool is built.
**Generated:** 2026-05-01
**Scope:** every EPIC field that backs a column or input on a section form today.

This document is the **input** to the annotator tool. Once you sign off on the
coverage and tag names below, the annotator walks each row, you Accept/Edit/Skip,
and the verified `domain_tag` is written back into `Library/Epic Field Map.json`.

Conventions:
- **EPIC field** = the screen + `name` attribute we need to back a tag.
- **Proposed tag** = the canonical `domain_tag` we will write.
- **Repeat?** = whether the tag is part of a repeatable group (rendered as a table row).
- **Verify** = items I want a sanity-check eye on before annotation runs.

---

## Section 1 — Submission (hidden tab, but extracted)

These are extracted into the hidden `submission` namespace so we have them
internally, but no GUI tab is rendered.

| EPIC field | Proposed tag | Label on form |
|---|---|---|
| `Submission Detail` → `streName` | `submission.name` | Name |
| `Submission Detail` → `dteEffective` | `submission.effective_date` | Effective |
| `Submission Detail` → `dteExpiration` | `submission.expiration_date` | Expiration |
| `Submission Detail` → `cboSource` | `submission.source` | Source |

---

## Section 2 — Named Insureds tab

The "Primary" row uses fields from `Commercial AP > Applicant`. Additional rows
come from `Commercial AP > OtherNamedInsureds` and are repeatable.

### 2A — Primary named insured (1 row, drawn from singleton fields)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Entity Name | `Commercial AP > Applicant` → `streFNIName` | `account.named_insured` |
| FEIN / Tax ID | `Commercial AP > Applicant` → `streFEIN` | `account.fein` |
| DBA / Trade Name | (no direct EPIC field — usually entered as a 2nd named insured row) | `account.dba` *(no EPIC mapping; sole-prop edge case)* |
| State of Incorp. | (no direct field on Applicant; may live elsewhere) | `account.state_of_incorporation` *(verify before annotation)* |
| Address | `Commercial AP > Applicant` → `adeMailing` (composite) | `account.mailing_address` *(parent — see decomposition below)* |
| Email | `Commercial AP > Applicant` → `streFNIEmail` | `account.email` |
| Phone | `Commercial AP > Applicant` → `pheFirstInsured` | `account.phone` |

**Address decomposition** — EPIC's `adeMailing` is a composite address widget. The
Field Map likely emits sub-name attributes (`adeMailing-streetLine`,
`adeMailing-city`, `adeMailing-state`, `adeMailing-zip`). We tag each sub-field:

| Sub-field | Proposed tag |
|---|---|
| `adeMailing-streetLine` | `account.mailing_address.street` |
| `adeMailing-city` | `account.mailing_address.city` |
| `adeMailing-state` | `account.mailing_address.state` |
| `adeMailing-zip` | `account.mailing_address.zip` |

> ⚠️ **Verify:** Open Field Map and confirm `adeMailing-*` sub-fields exist on
> the Applicant screen. If the Field Map only stores the parent and not the
> children, the annotator needs to know to expand them on the fly.

### 2B — Repeatable named insureds (rendered as additional rows in the same table)

All fields below are on `Commercial AP > OtherNamedInsureds` and repeat per item.

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Item # | `inteItemNum` | `account.other_named_insured.item_number` |
| Entity Name | `streName` | `account.other_named_insured.name` |
| FEIN / Tax ID | `streFEIN` | `account.other_named_insured.fein` |
| DBA / Trade Name | (uses Name Type = "DBA"; no separate field) | n/a |
| Email | `streEmail` | `account.other_named_insured.email` |
| Phone | (need to verify field name) | `account.other_named_insured.phone` |
| Address | `adeMailing-streetLine` etc. | `account.other_named_insured.address.*` |
| Name Type | `cboNameType` | `account.other_named_insured.type` |
| Business Type | `cboBusinessType` | `account.other_named_insured.business_type` |

> ⚠️ **Decision needed:** I've been describing primary + repeatable as one
> table. Two paths:
> - **Path A (current):** primary is special, separate `account.*` tags;
>   additional rows use `account.other_named_insured.*` namespace. Two paths
>   into the same table.
> - **Path B (cleaner):** convert *all* named insureds into a single repeatable
>   `account.named_insured` group. The "Primary" row is just the first item with
>   `type=primary`. Single set of tags, one schema.
>
> Path B is conceptually nicer and matches how EPIC actually stores them
> (everyone's a row in OtherNamedInsureds, with the "primary" flag elsewhere).
> But Path A matches the *visual* design where Primary is rendered specially.
> **Tell me which.**

---

## Section 3 — Locations tab

Repeatable group: `location.*` (cross-LOB; flat namespace per the registry).

Source screen: `Commercial AP > Premise`.

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Loc # | (Premise screen has `inteBuildingNumber` but no `inteLocationNumber`?) | `location.number` *(verify)* |
| Bldg # | `Commercial AP > Premise` → `inteBuildingNumber` | `location.building_number` |
| Name / Description | `streBuildingDescription` | `location.description` |
| Address | (verify — Premise may store address as composite) | `location.address.street` |
| City | (composite child) | `location.address.city` |
| State | (composite child) | `location.address.state` |
| ZIP | (composite child) | `location.address.zip` |

Plus several non-displayed-but-extracted fields on the same screen:
| EPIC field | Proposed tag |
|---|---|
| `inteNumberEmployees` | `location.full_time_employees` |
| `inteNumPartTimeEmployees` | `location.part_time_employees` |
| `deceAnnualRev` | `location.annual_revenue` |
| `inteOccupiedArea` | `location.occupied_area` |
| `inteTotalArea` | `location.total_area` |
| `cboInterest` | `location.interest_type` |

> ⚠️ **Verify:** EPIC seems to store the location number on a different screen
> than Premise (Premise is per-building under a parent location). Need to walk
> the Field Map's Premise hierarchy to confirm we tag the right level.

---

## Section 4 — General Liability tab

### 4A — Top fields (singleton)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Policy Number | (need to verify — likely `General Liability > GeneralInfo201404`) | `policy.gl.policy_number` |
| Effective | (same screen, `dteEffective` or similar) | `policy.gl.effective_date` |
| Expiration | (same screen) | `policy.gl.expiration_date` |
| Occurrence / Claims Made | `General Liability > Coverages` → `pnl4` (radio) | `policy.gl.occurrence_or_claims_made` |

### 4B — Limits (all singleton, all on `General Liability > Coverages`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| General Aggregate | `streGenAggrAppLimit` | `policy.gl.general_aggregate_limit` |
| Products – Comp/Ops Aggregate | `streProdOperLimit` | `policy.gl.products_completed_ops_limit` |
| Personal & Advertising Injury | `strePersAdvInjLimit` | `policy.gl.personal_advertising_injury_limit` |
| Each Occurrence | `streEachOccLimit` | `policy.gl.each_occurrence_limit` |
| Damage to Named Premises | `streDamPremLimit` | `policy.gl.damage_to_premises_limit` |
| Medical Payments | `streMedLimit` | `policy.gl.medical_payments_limit` |
| Employee Benefits Limit | `streEmpBenLimit` | `policy.gl.employee_benefits_limit` |
| BI Deductible | `streBIDed` | `policy.gl.bi_deductible` |
| PD Deductible | `strePDDed` | `policy.gl.pd_deductible` |

### 4C — Hazard Schedule (repeatable, source: `General Liability > Hazards`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| (Loc #) | `inteLocationNumber` | `policy.gl.hazard.location_number` |
| (Bldg #) | `inteBuildingNumber` | `policy.gl.hazard.building_number` |
| (Hazard #) | `inteHazardNumber` | `policy.gl.hazard.number` |
| Class Code | `streClassCode` | `policy.gl.hazard.class_code` |
| Description | `streClassification` | `policy.gl.hazard.description` |
| Exposure Basis | `cboPremiumBasis` | `policy.gl.hazard.exposure_basis` |
| Exposure Amount | `streExposure` | `policy.gl.hazard.exposure_amount` |
| Premium | `curePremiseOpPremium` | `policy.gl.hazard.premium` |

---

## Section 5 — Property tab

### 5A — Top fields (singleton)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Policy Number | (verify on `Property > GeneralInformation` or equivalent) | `policy.property.policy_number` |
| Effective | (same screen) | `policy.property.effective_date` |
| Expiration | (same screen) | `policy.property.expiration_date` |
| Deductible | (verify — likely on Subject or AdditionalCoverage) | `policy.property.deductible` |
| Coinsurance | (verify) | `policy.property.coinsurance` |

### 5B — Property Coverage Schedule (repeatable, source: `Property > Subject`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Loc # | `inteLocationNumber` | `policy.property.subject.location_number` |
| Bldg # | `inteBuildingNumber` | `policy.property.subject.building_number` |
| (Subject #) | `inteSubject` | `policy.property.subject.number` |
| Coverage Type | `cboSubject` | `policy.property.subject.coverage_type` |
| Building Limit | `deceAmount` *(when subject = building)* | `policy.property.subject.amount` |
| BPP Limit | `deceAmount` *(when subject = BPP)* | (same tag, different row) |
| Description | `streDescription` | `policy.property.subject.description` |
| Valuation | `cboValuation1` | `policy.property.subject.valuation` |
| Inflation Guard | `pereInflationGuard` | `policy.property.subject.inflation_guard` |
| Form # | `streFormNumber` | `policy.property.subject.form_number` |

> ⚠️ **Decision needed:** GUI table has separate "Building Limit" and "BPP Limit"
> columns, but EPIC stores them as separate **rows** under the same `deceAmount`
> field with `cboSubject` distinguishing what the amount applies to. Either:
> - Render as one row per amount (matches EPIC), or
> - Pivot in the GUI (more readable for ops).
> Section forms team should decide. Affects how Tier 1 tags get shaped.

---

## Section 6 — Business Auto tab

### 6A — Top fields (singleton)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Policy Number | (verify on `Business Auto > GeneralInfo201005`) | `policy.auto.policy_number` |
| Effective | (same) | `policy.auto.effective_date` |
| Expiration | (same) | `policy.auto.expiration_date` |

### 6B — Coverage limits (singleton, source: `Business Auto > CoverageTN`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Combined Single Limit (CSL) | `streLiabilityCSLLimit1` | `policy.auto.csl_limit` |
| BI – Per Person | `streLiabilityBILimit1` | `policy.auto.bi_per_person` |
| BI – Per Accident | `streLiabilityBILimit2` | `policy.auto.bi_per_accident` |
| Property Damage | `streLiabilityPDLimit1` | `policy.auto.property_damage` |
| Medical Payments | `streMedicalLimit1` | `policy.auto.medical_payments` |
| Uninsured / Underinsured Motorist | `streUninsuredCSLLimit1` | `policy.auto.uninsured_motorist` |
| Comprehensive Deductible | `streComprehensiveDeductible1` | `policy.auto.comprehensive_deductible` |
| Collision Deductible | `streCollisionDeductible1` | `policy.auto.collision_deductible` |
| Towing Limit | `streTowingLimit1` | `policy.auto.towing_limit` |

### 6C — Vehicles schedule (repeatable, **GAP IN FIELD MAP**)

> 🔴 **Blocker:** `Business Auto > Vehicle` screen has **0 fields** scraped.
> The screen exists in the Field Map but the field list is empty. We have two
> options:
> 1. Re-scrape that screen (probably 30 min of work in the existing scraper)
> 2. Tag as **proposed-only** for now using EPIC ACORD form analogs
>
> I recommend option 1 before annotation runs. Tags below are what we'll
> propose once the screen is populated.

| Form column | Proposed tag |
|---|---|
| # | `policy.auto.vehicle.number` |
| Year | `policy.auto.vehicle.year` |
| Make | `policy.auto.vehicle.make` |
| Model | `policy.auto.vehicle.model` |
| VIN | `policy.auto.vehicle.vin` |
| Type | `policy.auto.vehicle.type` |
| Garaging Address | `policy.auto.vehicle.garaging_address` |
| GVW | `policy.auto.vehicle.gvw` |
| Cost New | `policy.auto.vehicle.cost_new` |

### 6D — Driver schedule (repeatable, source: `Business Auto > Driver`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| # | `inteDriverNumber` | `policy.auto.driver.number` |
| Driver Name | `streName` | `policy.auto.driver.name` |
| License # | `seceDriversLicenseNumber` | `policy.auto.driver.license_number` |
| State | `cboState` | `policy.auto.driver.state` |
| Date of Birth | `dteBirth-mask` | `policy.auto.driver.date_of_birth` |
| Year Licensed | `inteYearLicensed` | `policy.auto.driver.year_licensed` |
| Years Experience | `inteYearsExperience` | `policy.auto.driver.years_experience` |
| Date Hired | `dteHired-mask` | `policy.auto.driver.date_hired` |
| Driver Type | `cboDriverType` | `policy.auto.driver.type` |

GUI's "Violations" column has no direct EPIC field — flag as out-of-scope.

---

## Section 7 — Inland Marine tab

### 7A — Top fields (singleton)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Policy Number | (verify on `Inland Marine- Commercial > GeneralInformation`) | `policy.inland_marine.policy_number` |
| Effective | (same) | `policy.inland_marine.effective_date` |
| Expiration | (same) | `policy.inland_marine.expiration_date` |
| Coverage Form | (verify) | `policy.inland_marine.coverage_form` |
| Deductible | `Inland Marine- Commercial > CoverageDeductible` → `streACVReplacementCostDeductible` | `policy.inland_marine.deductible` |
| Total Schedule Amount | `streTotalScheduledAmount` | `policy.inland_marine.total_scheduled_amount` |
| Coinsurance % | `perCoinsPercent` | `policy.inland_marine.coinsurance_percent` |

### 7B — Scheduled equipment (repeatable, source: `Inland Marine- Commercial > ScheduledEquipment`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| # | `inteItemNumber` | `policy.inland_marine.scheduled_item.number` |
| Item Description | `streDescription` | `policy.inland_marine.scheduled_item.description` |
| Type | `streType` | `policy.inland_marine.scheduled_item.type` |
| Serial Number / ID | `streSerialNumber` | `policy.inland_marine.scheduled_item.serial_number` |
| Manufacturer | `streManufacturer` | `policy.inland_marine.scheduled_item.manufacturer` |
| Model | `streModel` | `policy.inland_marine.scheduled_item.model` |
| Model Year | `inteModelYear` | `policy.inland_marine.scheduled_item.model_year` |
| Limit | `streAmtInsurance` | `policy.inland_marine.scheduled_item.amount_insured` |
| Date Purchased | `dtePurchased-mask` | `policy.inland_marine.scheduled_item.date_purchased` |

### 7C — Unscheduled equipment (repeatable, source: `Inland Marine- Commercial > UnscheduledEquipment`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Description | `streDescription` | `policy.inland_marine.unscheduled_item.description` |
| Maximum | `streMaximum` | `policy.inland_marine.unscheduled_item.maximum` |
| Limit | `streAmtInsurance` | `policy.inland_marine.unscheduled_item.amount_insured` |
| Coinsurance | `inteCoInsurance` | `policy.inland_marine.unscheduled_item.coinsurance` |

---

## Section 8 — Worker's Compensation tab

### 8A — Top fields (singleton, source: `Worker's Compensation > PolicyInfoTotalPremium` + GeneralInformation)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Policy Number | (verify on `> GeneralInformation` or `> Status`) | `policy.workers_comp.policy_number` |
| Effective | (same) | `policy.workers_comp.effective_date` |
| Expiration | (same) | `policy.workers_comp.expiration_date` |
| Statutory Limits | (verify — likely a checkbox on PolicyInfoTotalPremium) | `policy.workers_comp.statutory_limits` |
| Deductible | `cboDeductibles` | `policy.workers_comp.deductible` |

### 8B — Employer Liability limits (singleton, source: `Worker's Compensation > PolicyInfoTotalPremium`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Each Accident | `streEachAccident` | `policy.workers_comp.el_each_accident` |
| Disease – Policy Limit | `streDiseasePolicyLimit` | `policy.workers_comp.el_disease_policy_limit` |
| Disease – Each Employee | `streDiseaseEachEmployee` | `policy.workers_comp.el_disease_each_employee` |

Plus other singleton fields on the screen:
| EPIC field | Proposed tag |
|---|---|
| `cureTotalEstimatedAnnualPremium` | `policy.workers_comp.total_estimated_annual_premium` |
| `cureTotalMinimumPremium` | `policy.workers_comp.total_minimum_premium` |
| `cureTotalDepositPremium` | `policy.workers_comp.total_deposit_premium` |
| `chkUSLH` | `policy.workers_comp.uslh` |
| `chkVoluntaryCompensation` | `policy.workers_comp.voluntary_compensation` |

### 8C — Class codes (repeatable, source: `Worker's Compensation > Location`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| State | `cboState` | `policy.workers_comp.class_code.state` |
| Class Code | `streClassCode` | `policy.workers_comp.class_code.code` |
| Description | `streDescriptionCode` | `policy.workers_comp.class_code.description` |
| Payroll | (verify — may be `streCategories` or on a sibling screen) | `policy.workers_comp.class_code.payroll` |
| Full Time | `inteFullTime` | `policy.workers_comp.class_code.full_time` |
| Part Time | `intePartTime` | `policy.workers_comp.class_code.part_time` |

---

## Section 9 — Umbrella / Excess tab

### 9A — Top fields (singleton, source: `Commercial Umbrella > PolicyInformation`)

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Policy Number | `streExpiringPolNum` | `policy.umbrella.policy_number` |
| Effective | (verify on `> GeneralInformation`) | `policy.umbrella.effective_date` |
| Expiration | (same) | `policy.umbrella.expiration_date` |
| Each Occurrence Limit | `streOccurrenceLimit` | `policy.umbrella.each_occurrence_limit` |
| Aggregate Limit | (verify — may be elsewhere) | `policy.umbrella.aggregate_limit` |
| Retention / SIR | `streRetainedLimit` | `policy.umbrella.retention` |
| Follow Form | (verify checkbox) | `policy.umbrella.follow_form` |

### 9B — Underlying policies table (repeatable; source: `Commercial Umbrella > UnderlyingInsurance`)

UnderlyingInsurance is structured oddly — the fields are *prefixed by line of
underlying coverage* (`streAutoLine`, `streGLLine`, `streELLine`,
`streOtherLine`). So one EPIC screen drives **multiple repeatable rows in our
GUI**, each with a fixed line label. We have two paths:

- **Path A:** treat each `<line>Line` block as a separate set of fields, all
  rolling up to one `policy.umbrella.underlying.*` repeatable group, with a
  `line` discriminator. The row for Auto extracts from `streAuto*` fields.
- **Path B:** drop UnderlyingInsurance from Tier 1 and surface it later — it's
  a complex shape and your broker dec pages may not include it consistently.

> **Recommendation:** Path B for Tier 1. Mark these `unmapped` and revisit when
> you have a real submission with underlying policy data on the dec page.

---

## Section 10 — Additional Interests (embedded per coverage)

Repeatable group `policy.<lob>.additional_interest.*`. Source screens are
**five separate but nearly-identical** AdditionalInterest screens, one per LOB.

| LOB | Source screen |
|---|---|
| GL | `General Liability > AdditionalInterest` *(but ⚠️ this screen looks like a Products schedule — see note below)* |
| Property | `Property > AdditionalInterest` |
| Auto | `Business Auto > AdditionalInterest` |
| Inland Marine | `Inland Marine- Commercial > AdditionalInterest` |
| Umbrella | `Commercial Umbrella > AdditionalInterest` |

Common fields across the LOB-specific AdditionalInterest screens:

| Form column | EPIC field | Proposed tag (per LOB) |
|---|---|---|
| Type | `cboInterest` | `policy.<lob>.additional_interest.type` |
| Name | `streName` | `policy.<lob>.additional_interest.name` |
| Address (street) | `adePrimary-streetLine` | `policy.<lob>.additional_interest.address.street` |
| Address (city) | (composite) | `policy.<lob>.additional_interest.address.city` |
| Address (state) | `adePrimary-state` | `policy.<lob>.additional_interest.address.state` |
| Address (zip) | (composite) | `policy.<lob>.additional_interest.address.zip` |
| Email | `streEmail` | `policy.<lob>.additional_interest.email` |
| Phone | `phePrimary` | `policy.<lob>.additional_interest.phone` |
| Reason for Interest | `streReasonForInt` | `policy.<lob>.additional_interest.reason` |
| Reference / Loan # | `streReferenceNumber` | `policy.<lob>.additional_interest.reference_number` |
| Lien Amount | `inteLienAmount` | `policy.<lob>.additional_interest.lien_amount` |
| Interest End Date | `dteInterestEnd-mask` | `policy.<lob>.additional_interest.interest_end_date` |
| Subject reference | (varies per LOB: `inteVehicleNumber`, `inteItemNumber`, `inteSubjectNumber`) | `policy.<lob>.additional_interest.subject_reference` |
| Certificate | `chkCertificate` | `policy.<lob>.additional_interest.certificate` |
| Send Bill | `chkSendBill` | `policy.<lob>.additional_interest.send_bill` |

> 🔴 **Investigation needed:** `General Liability > AdditionalInterest` in the
> Field Map has fields like `streProduct`, `streAnnualGrossSales`, `Edit0`-`Edit9`
> — that's a **Products schedule, not an Additional Interest screen**. The
> Field Map's data on this screen is wrong/mis-scraped. We need to either
> re-scrape this screen or accept that GL Additional Interests come from a
> different EPIC location entirely (which is actually possible — GL might
> handle them through endorsements rather than a dedicated screen).

---

## Section 11 — Policy Forms & Endorsements (embedded per coverage)

Repeatable group `policy.<lob>.policy_form.*`. Common fields per LOB:

| Form column | EPIC field | Proposed tag |
|---|---|---|
| Form Number | `streNumber` | `policy.<lob>.policy_form.number` |
| Form Name | `streName` | `policy.<lob>.policy_form.name` |
| Edition Date | `dteEdition` or `dteEdition-mask` | `policy.<lob>.policy_form.edition_date` |
| Copyright Code | `streCopyrightCode` | `policy.<lob>.policy_form.copyright_code` |
| Copyright Type | `cboCopyrightType` | `policy.<lob>.policy_form.copyright_type` |
| Premium | `curePremium` (where present) | `policy.<lob>.policy_form.premium` |
| Subject reference | varies (`inteLocationNumber`, `inteVehicleNumber`, `inteItemNumber`) | `policy.<lob>.policy_form.subject_reference` |

---

## Section 12 — Notes tab

| Form field | Source | Proposed tag |
|---|---|---|
| Notes text | (no EPIC field — operator-only) | `notes.text` (no Field Map row to tag — handled in code) |

`notes.text` does not need a `domain_tag` annotation in the Field Map. It's a
GUI-only concept that the entry stage doesn't push into EPIC.

---

## Summary

| Section | Tag count | Status |
|---|---|---|
| Submission (hidden) | 4 | ✅ ready |
| Named Insureds | ~16 | ⚠️ needs Path-A-vs-B decision; address sub-fields TBD |
| Locations | ~13 | ⚠️ verify location-number screen |
| General Liability | ~17 | ✅ mostly ready; verify policy-info screen |
| Property | ~16 | ⚠️ Building/BPP rendering decision pending |
| Business Auto | ~24 | 🔴 Vehicle screen has 0 fields — needs re-scrape |
| Inland Marine | ~16 | ✅ ready |
| Workers Comp | ~14 | ✅ mostly ready |
| Umbrella | ~7 | ⚠️ Underlying policy table deferred |
| Additional Interests (5 LOBs × 14 fields) | ~70 | ⚠️ GL screen mis-scraped — needs investigation |
| Policy Forms (6 LOBs × 7 fields) | ~42 | ✅ ready |
| **TOTAL** | **~239 tags across ~165 unique EPIC fields** | |

(Number is higher than the original 100–150 estimate because Additional
Interests + Policy Forms duplicate 14 + 7 fields per LOB. Each LOB's copy is a
separate Field Map row that needs its own annotation.)

---

## Open decisions before annotator runs

1. **Named Insureds Path A vs B** (primary as special row, or all repeatable).
2. **Property Building/BPP** rendering: one row per amount, or pivot in GUI.
3. **Address sub-field expansion** in the Field Map — does the scraper already emit `adeMailing-streetLine` etc., or do we need to teach the annotator to expand them?
4. **Vehicle screen re-scrape** — yes/no before Tier 1.
5. **GL Additional Interest screen** — re-scrape, or accept the gap.
6. **Underlying policies (umbrella)** — Tier 1 or defer.

Once these are resolved, we lock the list and build the annotator.

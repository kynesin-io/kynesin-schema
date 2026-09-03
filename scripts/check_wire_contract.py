#!/usr/bin/env python3
"""Wire-contract drift check: JSON Schema <-> skill-template contract block <-> Apex validator
<-> DML validation rules.

Four layers declare or enforce the Kynesin interchange contract independently:

  1. the JSON Schema        (kynesin-schema repo, versions/kynesin-interchange-X.Y.Z.schema.json)
  2. the skill template     (kynesin-skill/template/SKILL.md, machine-readable
                             contract block under the <!-- kynesin-payload-contract --> marker)
  3. the Apex validator     (force-app/main/default/classes/KynesinIngestion.cls constants)
  4. the validation rules   (force-app/main/default/objects/*/validationRules/
                             *.validationRule-meta.xml — Criterion__c rules that
                             duplicate contract conditionals at the DML layer)

Eleven wire-format drifts accumulated across these with nothing comparing them,
and a live run silently lost its provenance graph. This script makes ANY future
divergence fail a build: it compares required blocks, all thirteen closed enums
the contract block declares — including every one whose values Apex persists or
enforces (origin, basis_class, payload_scope, metric.type) and the
register_status proposed-entry scope, which is checked schema<->template only
because Apex deliberately ignores that block — both open-enum core sets,
supported versions, and the structural shapes, three ways, and exits nonzero
listing EVERY mismatch (it never stops at the first). The DML layer is checked
too (Claude Science's staging verification, item 5, proved the gap: a payload
passed all three compared layers and would still have died at insert on a
validation rule): each contract-coupled Criterion__c validation rule must
exist, be active, and still reference the fields its conditional couples, and
every criteria-level schema conditional must map to a rule assertion or be
recorded as deliberately uncovered — so a new schema conditional with no DML
twin is flagged here, not at the first insert. Schema enums outside the
contract block (identifier namespace/level/status, decided_by, chain_role,
destination, prerequisites.blocks, indication.design, origin_detail values) are
deliberately untracked: the template contract block does not carry them, and
the ingestion treats the ones it stores (chain_role, destination) as open
strings — there is no third artifact to drift against.

Deliberate strictness: comparisons are exact. Do not "fix" a failure by
loosening this script — fix the artifact that drifted. A check that passes by
being lenient is the failure mode this script exists to kill.

Modes:
  - All three artifacts present: full three-way check.
  - Template absent (public CI has no private kynesin-skill checkout): the
    template checks are SKIPPED LOUDLY and the schema<->Apex checks still run.
    Absence does not fail the build; silence about absence is forbidden.

stdlib only; python3.8+.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

# Defaults are resolved relative to the repo this script lives in (its parent
# directory), so the check works from any cwd, in CI, and in the synced copy
# inside the kynesin-schema repo (where these paths are absent and the template
# check loudly skips unless --template/--apex point at a kynesin-sfdx checkout).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TEMPLATE = os.path.join(_REPO_ROOT, "kynesin-skill", "template", "SKILL.md")
DEFAULT_APEX = os.path.join(_REPO_ROOT, "force-app", "main", "default", "classes", "KynesinIngestion.cls")
DEFAULT_OBJECTS = os.path.join(_REPO_ROOT, "force-app", "main", "default", "objects")

SCHEMA_CLONE_HELP = (
    "No schema provided. The schema lives in the public repo "
    "github.com/kynesin-io/kynesin-schema (this script never fetches it). "
    "Clone it and point at it:\n"
    "    git clone https://github.com/kynesin-io/kynesin-schema ../kynesin-schema\n"
    "then pass --schema ../kynesin-schema (or a specific "
    "versions/kynesin-interchange-X.Y.Z.schema.json file), or set "
    "KYNESIN_SCHEMA_PATH to either."
)

CONTRACT_MARKER = "<!-- kynesin-payload-contract -->"

# The exact key inventory the template contract block must carry (check f).
CONTRACT_TOP_KEYS = {"contract", "kynesin_version", "required_blocks", "enums", "core_enums", "shapes"}
CONTRACT_ENUM_KEYS = {
    "criterion_type", "polarity", "strength_normalized", "source_completeness",
    "review_priority", "review_action", "relationship", "pointer_type",
    "origin", "basis_class", "payload_scope", "metric_type", "register_status_scope",
    "threshold_operator",
}
CONTRACT_CORE_KEYS = {"criterion_domain", "evidence_class"}
CONTRACT_SHAPE_KEYS = {
    "refs", "derivation_refs", "derived_from_evidence", "grounded", "metric",
    "provenance", "review_summary", "enum_extensions", "deleted_from_precedent",
    "considered_and_rejected", "top_level_additional_properties",
    "threshold", "register", "attach",
}

# Where each closed enum lives in the schema. Only pointer_type is in $defs;
# the rest are inline — resolved by JSON path, not by guessing.
SCHEMA_ENUM_PATHS = {
    "criterion_type":      ("properties", "criteria", "items", "properties", "criterion_type", "enum"),
    "polarity":            ("properties", "evidence", "items", "properties", "polarity", "enum"),
    "strength_normalized": ("properties", "evidence", "items", "properties", "strength_normalized", "enum"),
    "source_completeness": ("properties", "evidence", "items", "properties", "source_completeness", "enum"),
    "review_priority":     ("properties", "criteria", "items", "properties", "review_priority", "enum"),
    "review_action":       ("properties", "criteria", "items", "properties", "review_action", "enum"),
    "relationship":        ("properties", "criteria", "items", "properties", "derived_from_evidence",
                            "items", "properties", "relationship", "enum"),
    "pointer_type":        ("$defs", "pointer", "properties", "type", "enum"),
    "origin":              ("properties", "criteria", "items", "properties", "origin", "items", "enum"),
    "basis_class":         ("properties", "criteria", "items", "properties", "basis_class", "items", "enum"),
    "payload_scope":       ("properties", "payload_scope", "enum"),
    "metric_type":         ("$defs", "metric", "properties", "type", "enum"),
    "register_status_scope": ("properties", "register_status", "properties", "proposed_entries",
                              "items", "properties", "scope", "enum"),
    "threshold_operator":  ("properties", "criteria", "items", "properties", "threshold",
                            "properties", "operator", "enum"),
}

# Which Apex Set<String> constant answers for each enum. None means the Apex
# side of the three-way check is deliberately absent for that enum:
# register_status_scope is compared schema<->template only, because Apex
# ignores the register_status block by design — register amendments are
# proposals a human register owner adopts, never data the ingestion validates
# or persists (see the REQUIRED_BLOCKS comment in KynesinIngestion.cls).
APEX_ENUM_CONSTANTS = {
    "criterion_type":      "E_CRITERION_TYPE",
    "polarity":            "E_POLARITY",
    "strength_normalized": "E_STRENGTH_NORM",       # covers strength_normalized
    "source_completeness": "E_SOURCE_COMPLETENESS",
    "review_priority":     "E_REVIEW_PRIORITY",
    "review_action":       "E_REVIEW_ACTION",
    "relationship":        "E_RELATIONSHIP",
    "pointer_type":        "E_POINTER_TYPE",
    "origin":              "E_ORIGIN",
    "basis_class":         "E_BASIS_CLASS",
    "payload_scope":       "PAYLOAD_SCOPES",        # Apex enforces payload_scope directly
    "metric_type":         "E_METRIC_TYPE",
    "register_status_scope": None,                  # schema<->template only, by design
    "threshold_operator":  "E_THRESHOLD_OPERATOR",
}

# Open-enum core sets: schema `examples` arrays <-> template core_enums <-> Apex CORE_*.
SCHEMA_CORE_PATHS = {
    "criterion_domain": ("properties", "criteria", "items", "properties", "criterion_domain", "examples"),
    "evidence_class":   ("properties", "evidence", "items", "properties", "evidence_class", "examples"),
}
APEX_CORE_CONSTANTS = {
    "criterion_domain": "CORE_CRITERION_DOMAIN",
    "evidence_class":   "CORE_EVIDENCE_CLASS",
}

# Apex code-assertion greps (check e, Apex side). These cover contract rules the
# Apex enforces in CODE rather than in a data constant, so a set-comparison
# cannot see their deletion. Each pattern is anchored to the enforcing
# expression itself (not to an error-message string, which legitimate rewording
# could change): if the enforcement line is deleted or the constant is unhooked
# from the check, the grep fails.
APEX_CODE_ASSERTIONS = [
    # Detects deletion of the grounded-must-be-a-JSON-boolean type check
    # (contract: shapes.grounded.type == boolean; the string "true" once slipped through).
    ("apex-grounded-boolean-check",
     r"groundedO\s+instanceof\s+Boolean",
     "grounded is validated as a JSON boolean (not a string/number)"),
    # Detects deletion of the v0.13.0 conditional: an edge-bearing payload
    # must carry its evidence base (the provenance-stripped state must stay
    # unrepresentable). Paired with the schema-side conditional check below.
    ("apex-evidence-required-when-edges",
     r"firstEdgeBearer\s*!=\s*null\s*&&\s*evidence\.isEmpty\(\)",
     "evidence block is required whenever any criterion carries edges"),
    # Detects deletion of pointer-object validation on evidence refs items —
    # the check that each refs[] item is an object whose type is a pointer_type.
    ("apex-refs-pointer-type-check",
     r"checkEnum\(\s*ref\.get\(\s*'type'\s*\)\s*,\s*E_POINTER_TYPE",
     "evidence refs[] items are validated as typed pointer objects"),
    # Detects deletion of the per-edge relationship enum check on
    # derived_from_evidence — the edge type whose loss destroyed the provenance graph.
    ("apex-edge-relationship-check",
     r"checkEnum\(\s*edge\.get\(\s*'relationship'\s*\)\s*,\s*E_RELATIONSHIP",
     "derived_from_evidence edges carry a validated per-edge relationship"),
    # Detects deletion of edge referential integrity: every edge's evidence_id
    # must exist in the payload's evidence[] (a dangling edge is a silent hole
    # in the provenance graph).
    ("apex-edge-referential-integrity",
     r"evidenceIds\.contains\(\s*refEid\s*\)",
     "derived_from_evidence.evidence_id must resolve within the payload"),
    # Detects deletion of conditional rule (a): grounded=false requires gap_description.
    ("apex-ungrounded-gap-check",
     r"isBlank\(\s*c\.get\(\s*'gap_description'\s*\)\s*\)",
     "grounded=false requires gap_description"),
    # Detects deletion of conditional rule (b): criterion_type=neither requires
    # deleted_from_precedent or considered_and_rejected.
    ("apex-neither-detail-check",
     r"'neither'\.equals\(\s*str\(\s*c\.get\(\s*'criterion_type'\s*\)\s*\)\s*\)",
     "criterion_type=neither requires an explicit non-criterion detail"),
    # Detects deletion of conditional rule (c): origin containing model_derived
    # requires review_focus.
    ("apex-model-derived-review-focus",
     r"modelDerived\s*&&\s*isBlank\(\s*c\.get\(\s*'review_focus'\s*\)\s*\)",
     "origin containing model_derived requires review_focus"),
    # Detects unhooking of the version gate from SUPPORTED_VERSIONS.
    ("apex-version-gate",
     r"SUPPORTED_VERSIONS\.contains\(\s*ver\s*\)",
     "kynesin_version is gated against SUPPORTED_VERSIONS"),
    # Detects deletion of the required-blocks loop (constant present but unused
    # would otherwise pass the data comparison).
    ("apex-required-blocks-loop",
     r"for\s*\(\s*String\s+\w+\s*:\s*REQUIRED_BLOCKS\s*\)",
     "REQUIRED_BLOCKS is actually iterated in validation"),
    # Detects unhooking of the open-enum extension checks from the core sets
    # (criterion_domain / evidence_class must pass through checkExtensible).
    ("apex-open-enum-criterion-domain",
     r"checkExtensible\([^;]*CORE_CRITERION_DOMAIN",
     "criterion_domain is validated as core-or-declared-extension"),
    ("apex-open-enum-evidence-class",
     r"checkExtensible\([^;]*CORE_EVIDENCE_CLASS",
     "evidence_class is validated as core-or-declared-extension"),
    # 0.14.0: between requires value_upper (enforced in code, not a constant).
    ("apex-threshold-between-upper",
     r"'between'\.equals\(",
     "threshold operator between requires value_upper"),
    # 0.14.0 attach: a target study's live handoff must equal the resolved
    # supersede target or the ingest is rejected (explicit-supersedes-required).
    ("apex-attach-live-handoff-guard",
     r"liveHandoffId\s*!=\s*supersedesId",
     "attach to a study with a live handoff requires provenance.supersedes to name it"),
    # 0.14.0 register block read with the <=0.13 enum_extensions fallback.
    ("apex-register-block-fallback",
     r"LEGACY_METHOD_REGISTER_SLUG",
     "register identity reads the 0.14 block with the enum_extensions fallback intact"),
    # Detects a change to the decision-pointer FORM check. Template v2.6 states
    # ^[DM]\d{3} as its own citation convention with the server as membership
    # authority; that division only holds while the server's form check stays
    # ^D\d+$ / ^M\d+$ on the uppercased value. If this fires, re-read the
    # template's pointer-form rule before fixing either side.
    ("apex-decision-ref-regex",
     r"Pattern\.matches\('\^D\\\\d\+\$',\s*upper\).*\n.*Pattern\.matches\('\^M\\\\d\+\$',\s*upper\)",
     "decision-pointer form check is ^D\\d+$ / ^M\\d+$ on the uppercased value"),
    # The "nothing auto-approves" non-negotiable, enforced at last. Every layer
    # asserted it and none enforced it: the schema types human_confirmed as a
    # plain boolean (so false validates), the skill only ever shows true, and
    # the Apex coerced the value onto the record and wrote the handoff anyway.
    # If this fires, a payload no human reviewed can reach the system of record.
    ("apex-human-confirmed-true",
     r"hcRaw\s+instanceof\s+Boolean\s*&&\s*!\(\(Boolean\)\s*hcRaw\)",
     "provenance.human_confirmed=false is rejected, never coerced"),
    # generated_at must parse. parseDateTime() returns null on garbage, so an
    # unparseable timestamp used to ingest clean and store a null provenance
    # date. The schema's format:date-time is annotation-only and catches nothing.
    ("apex-generated-at-parseable",
     r"parseDateTime\(genAt\)\s*==\s*null",
     "provenance.generated_at must parse as a date-time, not silently null"),
    # An empty criteria[] must not ingest. It reported success, created a
    # childless handoff, and could supersede a good one — supersession is
    # decided from session and register, never from content.
    ("apex-criteria-non-empty",
     r"p\.get\('criteria'\)\s*!=\s*null\s*&&\s*criteria\.isEmpty\(\)",
     "criteria[] must carry at least one criterion"),
    # The cross-programme supersede guard. session_id identifies a session that
    # RAN, not a programme, and one relay can derive two — on 2026-08-25 a
    # single session produced crohns-il23p19 and lpa-ascvd under one session_id
    # and would have retired the crohns handoff silently. If this fires, a
    # second programme derived in one session can destroy the first's handoff.
    ("apex-cross-programme-supersede-guard",
     r"!supersedeWasExplicit\s*&&\s*sessionDerivedPrior\s*!=\s*null",
     "supersede across programmes is rejected unless explicitly named"),
    # The PHI screen must cover every free string that PERSISTS. Pointer
    # locator/data_cut and threshold.unit were stored verbatim but unscreened,
    # against this class's own stated rule. This is the enforcement point for
    # the "No PHI reaches Claude Science" non-negotiable.
    ("apex-phi-screen-pointers",
     r"phiScreenPointers\(\s*arr\(c\.get\('derivation_refs'\)\)",
     "pointer free strings (value/locator/data_cut) are PHI-screened"),
    # 0.14 attach: every ResearchStudyProtocolInfo block names the handoff that
    # wrote it. Re-attaching used to duplicate synopsis blocks with nothing on
    # the record saying which handoff produced which.
    ("apex-protocolinfo-attribution-sentinel",
     r"String\s+sentinel\s*=\s*IEC_SENTINEL_PREFIX\s*\+\s*hid",
     "every ResearchStudyProtocolInfo block names the handoff that wrote it"),
    # criteria[].destination was persisted with no enum check at all, so a
    # value outside the closed schema set reached the record.
    ("apex-destination-enum-check",
     r"checkEnum\(\s*c\.get\(\s*'destination'\s*\)\s*,\s*E_DESTINATION",
     "criteria[].destination is validated against the closed schema enum"),
    # session_id is capped so every key composed FROM it still fits its
    # 255-char unique field — the cap is on the input, not on the composite,
    # because a composite check would fail one stage too late.
    ("apex-session-id-length-cap",
     r"sid\.length\(\)\s*>\s*SESSION_ID_MAX",
     "provenance.session_id is capped so every composed key fits its unique field"),
    # The mirror of the unused-enum_extension warning: evidence nothing cites
    # is surfaced rather than silently carried.
    ("apex-uncited-evidence-warning",
     r"citedEvidenceIds\.contains\(\s*uncited\s*\)",
     "an evidence row no criterion cites is surfaced as a warning"),
    # The 0.14 block gate must be a FLOOR. It was `V14.equals(ver)`, which
    # rejected attach/register and silently skipped threshold on every version
    # ABOVE 0.14.0 too — invisible while 0.14.0 was newest, and it would have
    # fired on the first payload of the next version as three constructs
    # quietly not doing their jobs.
    ("apex-version-gate-is-a-floor",
     r"Boolean\s+isV14\s*=\s*atLeast\(\s*ver\s*,\s*V14\s*\)",
     "the 0.14 block gate is a version FLOOR, not an equality"),
    # The boundary gate's consumer. population.boundary_decisions has been legal
    # on the wire since 0.14.0 and was read by NOTHING — template v2.6 gates
    # derivation on count(decided_by == 'agent_unilateral') == 0 and that gate
    # lived entirely in skill prose. CLAUDE.md's lesson from the same run: an
    # accurate field with no consumer is not a control.
    # decided_by='open' arrived at 0.15.0 so a boundary RAISED and not yet
    # decided could be represented at all. It must never be a way to PASS the
    # gate by declining to decide, so it is counted with agent_unilateral into
    # Unilateral_Boundary_Count__c — and warned SEPARATELY, because "never
    # decided" and "decided by the agent" have different remedies and a reader
    # must not be told a question was decided badly when it was not decided.
    # Superseding a handoff must retire its NATIVE projections, not only mark
    # the Kynesin records. Measured before the change: 61 eligibility rules on
    # one study where 34 were current, and a reviewer could not separate them
    # without parsing SourceSystemIdentifier. Guarded on SourceSystem='Kynesin'
    # so a sponsor's own records are never touched.
    # register_status.proposed_entries had NO consumer. The block is still not
    # validated or persisted — adopting a register amendment is an owner's act,
    # not ingestion's — but a deferral left no trace, and organ-function
    # decisions deferred at one coverage census arrived as a live unanswered
    # boundary question one derivation later.
    ("apex-proposed-entries-counted",
     r"h\.Proposed_Entry_Count__c = proposedEntries",
     "register amendments proposed by a payload are counted, though never adopted"),
    ("apex-supersede-retires-natives",
     r"private static void retireSupersededProjections\(",
     "a superseded handoff's native projections are retired, not left beside their replacement"),
    ("apex-open-boundary-not-a-pass",
     r"boundaryUnilateral \+ boundaryOpen",
     "decided_by=open counts as unowned, never as a pass"),
    ("apex-boundary-gate-consumer",
     r"'agent_unilateral'\.equals\(\s*str\(\s*bd\.get\(\s*'decided_by'\s*\)\s*\)\s*\)",
     "agent_unilateral boundary decisions are counted and surfaced"),
    # The supersede lookup's tiebreak. CreatedDate has second resolution, so
    # handoffs pushed within one second tie and SOQL leaves ties undefined —
    # "the most recent prior handoff" then resolves arbitrarily and the payload
    # supersedes whichever sorted first. Found as a test that passed 18/18 and
    # 17/18 on identical code.
    ("apex-supersede-order-tiebreak",
     r"ORDER BY CreatedDate DESC, Id DESC",
     "the supersede lookup breaks CreatedDate ties deterministically on Id"),
]

# Enforcement that lives in a class OTHER than KynesinIngestion. APEX_CODE_ASSERTIONS
# scans only the --apex file, which is the ingestion validator; a tripwire written
# against any other class silently never matched until this list existed. Each entry
# is (check code, class file name, regex, what it enforces) and the file is resolved
# relative to the same classes/ directory the --apex file sits in.
SIBLING_CODE_ASSERTIONS = [
    # A caveat is a finding the decision SURVIVED — carried knowingly, not
    # corrected. Without a durable home the disposition lives only in a session
    # artifact, and the next derivation re-discovers the finding and may dispose
    # it differently: four findings on ovarian-parp-hrd-mono@1.0 were in exactly
    # that state on 2026-09-02. If the health action stops reporting them, the
    # object still holds them and nothing reads them, which is the same failure
    # with an extra table.
    ("apex-caveat-disposition-surfaced",
     "KynesinGetRegisterHealth.cls",
     r"result\.put\(\s*'blocking_caveat_count'",
     "disposed verification findings are surfaced, blocking ones counted"),
    # Verified_Against__c names the SOURCE; Verified_By__c names the VERIFIER.
    # All ten ovarian decisions carried the first and not the second — the
    # identical string "step-3/V-09 verification pass 2026-08-13", a label for a
    # step of the session that AUTHORED the register — and the action reported
    # 10/10 verified, 0 errors while four discrepancies stood. A stamp applied by
    # the authoring session is not verification and must not read as one.
    ("apex-stamp-is-not-verification",
     "KynesinGetRegisterHealth.cls",
     r"result\.put\(\s*'stamped_without_verifier_count'",
     "a verification stamp with no named verifier is reported, not counted as proof"),
    # A correction dated AFTER the criterion was received may have outdated the
    # text above it. CLAUDE.md recorded this as "a presentation gap, not an
    # action gap": both dates were already in the packet and nothing compared
    # them, so the reader had to.
    ("apex-correction-staleness-disclosure",
     "KynesinGetCriterionProvenance.cls",
     r"'same_day'\.equals\(rel\)",
     "correction-vs-criterion ordering is THREE-valued; same_day is not silence"),
    # The review action must write in SYSTEM mode. Kynesin_Reviewer deliberately
    # withholds edit on Review_Status__c and the two audit fields so a reviewer
    # cannot forge WHO reviewed; the invocable runs AS the reviewer, so without
    # this the stamp it exists to apply is the one thing it cannot write, and
    # every review fails. The lockdown and this line must move together.
    ("apex-review-writes-in-system-mode",
     "KynesinReview.cls",
     r"AccessLevel\.SYSTEM_MODE",
     "KynesinReview writes review outcomes in explicit system mode"),
]


# Validation-rule assertions (check g). These Criterion__c validation rules
# duplicate contract conditionals at the DML layer — the layer none of the
# three compared artifacts can see. Claude Science's staging verification
# (VERIFICATION_AND_STAGING, item 5) proved the gap: a payload passed schema,
# template and Apex and would still have died at insert on the first two rules
# below. So a deleted or deactivated rule is contract drift exactly like a
# dropped enum value. Each entry: (check code, object directory, rule
# fullName, tokens the errorConditionFormula must still contain, what the
# rule enforces). Token presence detects deletion and decoupling, not
# semantic equivalence — the formula's actual behaviour is proven at DML
# (staging dry-runs), not by this parse.
VALIDATION_RULE_ASSERTIONS = [
    ("vr-ungrounded-gap", "Criterion__c", "Ungrounded_needs_gap",
     ["Grounded__c", "Gap_Description__c"],
     "grounded=false requires gap_description"),
    ("vr-model-derived-review-focus", "Criterion__c", "Model_derived_needs_review_focus",
     ["Origin__c", "model_derived", "Review_Focus__c"],
     "origin containing model_derived requires review_focus"),
    ("vr-between-upper", "Criterion__c", "Between_needs_upper_bound",
     ["Threshold_Operator__c", "between", "Threshold_Value_Upper__c"],
     "threshold operator 'between' iff Threshold_Value_Upper__c populated"),
    ("vr-neither-detail", "Criterion__c", "Neither_needs_detail",
     ["Criterion_Type__c", "neither", "Non_Criterion_Detail__c"],
     "criterion_type=neither requires a stored non-criterion detail"),
]

# Which DML validation-rule assertion answers for each criteria-level schema
# conditional (the allOf if/then list on criteria items), keyed by the
# conditional's if-trigger property. None means the conditional is
# DELIBERATELY uncovered at the DML layer, with the reason printed — the
# absence is explicit rather than silent. A NEW schema conditional must land
# here in the same change: either with a matching validation rule plus a
# VALIDATION_RULE_ASSERTIONS entry, or as a None with its reason. The
# threshold between-conditional lives inside the threshold object, not this
# allOf; its layers are shape-threshold (schema), apex-threshold-between-upper
# (Apex) and vr-between-upper (DML).
SCHEMA_CONDITIONAL_COVERAGE = {
    "grounded": ("vr-ungrounded-gap", None),
    "origin":   ("vr-model-derived-review-focus", None),
    # Both anyOf branches (deleted_from_precedent / considered_and_rejected)
    # land serialised in the one long-text Non_Criterion_Detail__c, so a
    # single blank-check on that field covers the schema's disjunction.
    "criterion_type": ("vr-neither-detail", None),
}


class Report:
    def __init__(self):
        self.failures = []   # (code, message)
        self.passes = []     # (code, message)
        self.notes = []      # informational, never fail

    def ok(self, code, message):
        self.passes.append((code, message))

    def fail(self, code, message):
        self.failures.append((code, message))

    def note(self, message):
        self.notes.append(message)

    def check_eq_sets(self, code, label, a_name, a_list, b_name, b_list):
        """Set equality with ordering reported as a note (order-insensitive pass)."""
        a_set, b_set = set(a_list), set(b_list)
        if a_set != b_set:
            only_a = sorted(a_set - b_set)
            only_b = sorted(b_set - a_set)
            self.fail(code,
                      f"{label}: {a_name} != {b_name}.\n"
                      f"      {a_name}: {a_list}\n"
                      f"      {b_name}: {b_list}\n"
                      f"      only in {a_name}: {only_a}\n"
                      f"      only in {b_name}: {only_b}")
            return False
        if list(a_list) != list(b_list):
            self.note(f"{label}: {a_name} and {b_name} agree as sets but differ in order "
                      f"({a_name}={a_list} vs {b_name}={b_list}).")
        self.ok(code, f"{label}: {a_name} == {b_name} ({len(a_set)} values)")
        return True

    def check_eq_exact(self, code, label, a_name, a_val, b_name, b_val):
        if a_val != b_val:
            self.fail(code,
                      f"{label}: {a_name} != {b_name}.\n"
                      f"      {a_name}: {a_val!r}\n"
                      f"      {b_name}: {b_val!r}")
            return False
        self.ok(code, f"{label}: {a_name} == {b_name} ({a_val!r})")
        return True


def dig(obj, path, artifact_name, rep, code):
    """Resolve a JSON path tuple; a missing step is itself a contract failure."""
    cur = obj
    walked = []
    for key in path:
        walked.append(str(key))
        if not isinstance(cur, dict) or key not in cur:
            rep.fail(code, f"{artifact_name}: path {'/'.join(walked)} not found — "
                           f"the schema no longer declares this piece of the contract "
                           f"where the checker (and the template) expect it.")
            return None
        cur = cur[key]
    return cur


def load_json_file(path, what, rep, code):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        rep.fail(code, f"{what} not found at {path}")
    except json.JSONDecodeError as e:
        rep.fail(code, f"{what} at {path} is not valid JSON: {e}")
    return None


# ---------------------------------------------------------------- schema side

VERSION_FILE_RE = re.compile(r"^kynesin-interchange-(\d+\.\d+\.\d+)\.schema\.json$")


def version_key(v):
    return tuple(int(p) for p in v.split("."))


def resolve_schema_path(arg_path, rep):
    """Accept a schema FILE, a repo/versions DIRECTORY (resolve newest), or nothing (error)."""
    path = arg_path or os.environ.get("KYNESIN_SCHEMA_PATH")
    if not path:
        rep.fail("schema-path", SCHEMA_CLONE_HELP)
        return None
    path = os.path.abspath(path)
    if os.path.isdir(path):
        versions_dir = path
        if os.path.isdir(os.path.join(path, "versions")):
            versions_dir = os.path.join(path, "versions")
        candidates = {}
        try:
            for name in os.listdir(versions_dir):
                m = VERSION_FILE_RE.match(name)
                if m:
                    candidates[m.group(1)] = os.path.join(versions_dir, name)
        except OSError as e:
            rep.fail("schema-path", f"Cannot list schema directory {versions_dir}: {e}")
            return None
        if not candidates:
            rep.fail("schema-path",
                     f"No kynesin-interchange-*.schema.json found under {versions_dir}. " + SCHEMA_CLONE_HELP)
            return None
        newest = max(candidates, key=version_key)
        rep.note(f"Schema resolved from directory: newest version {newest} at {candidates[newest]}")
        return candidates[newest]
    if not os.path.isfile(path):
        rep.fail("schema-path", f"Schema path {path} does not exist. " + SCHEMA_CLONE_HELP)
        return None
    return path


# -------------------------------------------------------------- template side

def load_template_contract(template_path, rep):
    """Extract and parse the machine-readable contract block. Any structural
    deviation from the agreed interface is a failure (check f)."""
    with open(template_path, "r", encoding="utf-8") as f:
        text = f.read()
    if CONTRACT_MARKER not in text:
        rep.fail("template-contract-marker",
                 f"Template {template_path} has no '{CONTRACT_MARKER}' marker line — "
                 f"the machine-readable contract block is missing.")
        return None
    after = text.split(CONTRACT_MARKER, 1)[1]
    m = re.search(r"```json\s*\n(.*?)\n```", after, re.DOTALL)
    if not m:
        rep.fail("template-contract-fence",
                 "No fenced ```json block directly follows the contract marker in the template.")
        return None
    # The fence must follow the marker directly (only whitespace between).
    prefix = after[:m.start()]
    if prefix.strip() != "":
        rep.fail("template-contract-fence",
                 f"Content found between the contract marker and the ```json fence "
                 f"(expected the fence immediately after the marker): {prefix.strip()[:120]!r}")
    try:
        contract = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        rep.fail("template-contract-json", f"Template contract block is not valid JSON: {e}")
        return None
    if not isinstance(contract, dict):
        rep.fail("template-contract-json", "Template contract block is not a JSON object.")
        return None
    ok = True
    ok &= _exact_keys(rep, "template-contract-keys", "contract block top level",
                      set(contract.keys()), CONTRACT_TOP_KEYS)
    if contract.get("contract") != "kynesin-payload":
        rep.fail("template-contract-keys",
                 f"contract key must be 'kynesin-payload', found {contract.get('contract')!r}")
        ok = False
    if isinstance(contract.get("enums"), dict):
        ok &= _exact_keys(rep, "template-contract-keys", "contract enums",
                          set(contract["enums"].keys()), CONTRACT_ENUM_KEYS)
    else:
        rep.fail("template-contract-keys", "contract 'enums' is missing or not an object")
        ok = False
    if isinstance(contract.get("core_enums"), dict):
        ok &= _exact_keys(rep, "template-contract-keys", "contract core_enums",
                          set(contract["core_enums"].keys()), CONTRACT_CORE_KEYS)
    else:
        rep.fail("template-contract-keys", "contract 'core_enums' is missing or not an object")
        ok = False
    if isinstance(contract.get("shapes"), dict):
        ok &= _exact_keys(rep, "template-contract-keys", "contract shapes",
                          set(contract["shapes"].keys()), CONTRACT_SHAPE_KEYS)
    else:
        rep.fail("template-contract-keys", "contract 'shapes' is missing or not an object")
        ok = False
    if ok:
        rep.ok("template-contract-parse",
               "template contract block parses as JSON with exactly the specified keys")
    return contract


def _exact_keys(rep, code, label, got, want):
    if got != want:
        rep.fail(code,
                 f"{label}: keys differ from the agreed interface.\n"
                 f"      expected: {sorted(want)}\n"
                 f"      found:    {sorted(got)}\n"
                 f"      missing: {sorted(want - got)}  unexpected: {sorted(got - want)}")
        return False
    return True


# ------------------------------------------------------------------ apex side

def parse_apex_constants(apex_text, rep):
    """Parse Set<String>/List<String> constant initializers out of the Apex source.

    Matches: [modifiers] final Set<String>|List<String> NAME = new ...{ 'a', 'b' };
    Multi-line bodies included. Returns {name: (kind, [values in source order])}.
    """
    consts = {}
    pattern = re.compile(
        r"(Set|List)<\s*String\s*>\s+(\w+)\s*=\s*new\s+(?:Set|List)<\s*String\s*>\s*\{(.*?)\}\s*;",
        re.DOTALL,
    )
    for m in pattern.finditer(apex_text):
        kind, name, body = m.group(1), m.group(2), m.group(3)
        values = re.findall(r"'((?:[^'\\]|\\.)*)'", body)
        consts[name] = (kind, values)
    if not consts:
        rep.fail("apex-parse", "No Set<String>/List<String> constants could be parsed from the Apex file — "
                               "the parser or the file has structurally changed.")
    return consts


def apex_values(consts, name, rep):
    if name not in consts:
        rep.fail("apex-constant-missing",
                 f"Apex constant {name} not found in KynesinIngestion.cls — "
                 f"renamed or deleted; the contract comparison cannot see it.")
        return None
    return consts[name][1]


# ---------------------------------------------------------------- the checks

def check_required_blocks(schema, contract, consts, rep):
    code = "required-blocks"
    schema_req = dig(schema, ("required",), "schema", rep, code)
    if schema_req is None:
        return
    apex_req = apex_values(consts, "REQUIRED_BLOCKS", rep)
    if apex_req is not None:
        rep.check_eq_sets(code, "required top-level blocks", "schema.required", schema_req,
                          "Apex REQUIRED_BLOCKS", apex_req)
    if contract is not None:
        tmpl_req = contract.get("required_blocks")
        if not isinstance(tmpl_req, list):
            rep.fail(code, "template contract required_blocks is missing or not an array")
        else:
            rep.check_eq_sets(code, "required top-level blocks", "schema.required", schema_req,
                              "template required_blocks", tmpl_req)


def check_closed_enums(schema, contract, consts, rep):
    for enum_name, path in SCHEMA_ENUM_PATHS.items():
        code = f"enum-{enum_name}"
        schema_vals = dig(schema, path, "schema", rep, code)
        if schema_vals is None:
            continue
        apex_const = APEX_ENUM_CONSTANTS[enum_name]
        if apex_const is None:
            # Apex deliberately has no constant for this enum (register_status
            # is ignored by the ingestion by design) — schema<->template only.
            rep.note(f"enum {enum_name}: Apex comparison intentionally skipped "
                     f"(Apex ignores the register_status block by design).")
        else:
            apex_vals = apex_values(consts, apex_const, rep)
            if apex_vals is not None:
                rep.check_eq_sets(code, f"enum {enum_name}", "schema", schema_vals,
                                  f"Apex {apex_const}", apex_vals)
        if contract is not None:
            tmpl_vals = (contract.get("enums") or {}).get(enum_name)
            if not isinstance(tmpl_vals, list):
                rep.fail(code, f"template contract enums.{enum_name} is missing or not an array")
            else:
                rep.check_eq_sets(code, f"enum {enum_name}", "schema", schema_vals,
                                  f"template enums.{enum_name}", tmpl_vals)


def check_core_enums(schema, contract, consts, rep):
    for core_name, path in SCHEMA_CORE_PATHS.items():
        code = f"core-{core_name}"
        schema_vals = dig(schema, path, "schema", rep, code)
        if schema_vals is None:
            continue
        apex_const = APEX_CORE_CONSTANTS[core_name]
        apex_vals = apex_values(consts, apex_const, rep)
        if apex_vals is not None:
            rep.check_eq_sets(code, f"core open-enum set {core_name}", "schema examples", schema_vals,
                              f"Apex {apex_const}", apex_vals)
        if contract is not None:
            tmpl_vals = (contract.get("core_enums") or {}).get(core_name)
            if not isinstance(tmpl_vals, list):
                rep.fail(code, f"template contract core_enums.{core_name} is missing or not an array")
            else:
                rep.check_eq_sets(code, f"core open-enum set {core_name}", "schema examples", schema_vals,
                                  f"template core_enums.{core_name}", tmpl_vals)


def check_versions(schema, contract, consts, schema_file, rep):
    code = "versions"
    apex_versions = apex_values(consts, "SUPPORTED_VERSIONS", rep)
    schema_const = dig(schema, ("properties", "kynesin_version", "const"), "schema", rep, code)

    if apex_versions is not None and schema_const is not None:
        if schema_const not in apex_versions:
            rep.fail(code,
                     f"schema kynesin_version const {schema_const!r} is not in "
                     f"Apex SUPPORTED_VERSIONS {apex_versions} — the org would reject "
                     f"a payload conforming to this schema.")
        else:
            rep.ok(code, f"schema const {schema_const!r} is in Apex SUPPORTED_VERSIONS {apex_versions}")

    if contract is not None:
        tmpl_ver = contract.get("kynesin_version")
        if apex_versions is not None:
            if tmpl_ver not in apex_versions:
                rep.fail(code,
                         f"template kynesin_version {tmpl_ver!r} is not in "
                         f"Apex SUPPORTED_VERSIONS {apex_versions}.")
            else:
                rep.ok(code, f"template kynesin_version {tmpl_ver!r} is in Apex SUPPORTED_VERSIONS")
        if schema_const is not None:
            rep.check_eq_exact(code, "declared wire version", "schema const", schema_const,
                               "template kynesin_version", tmpl_ver)

    # A schema file must exist for every Apex-supported version, in the
    # directory the provided schema came from.
    if apex_versions is not None:
        versions_dir = os.path.dirname(os.path.abspath(schema_file))
        for v in apex_versions:
            expected = os.path.join(versions_dir, f"kynesin-interchange-{v}.schema.json")
            if os.path.isfile(expected):
                rep.ok(code, f"schema file exists for Apex-supported version {v} ({expected})")
            else:
                rep.fail(code,
                         f"Apex SUPPORTED_VERSIONS includes {v!r} but no schema file "
                         f"{expected} exists — the server claims to accept a version "
                         f"nobody can validate against.")


def _schema_item_shape(schema, obj_path, rep, code, artifact="schema"):
    """(required, optional) key lists for an object schema at obj_path."""
    node = dig(schema, obj_path, artifact, rep, code)
    if node is None:
        return None, None
    required = node.get("required", [])
    props = list((node.get("properties") or {}).keys())
    optional = [k for k in props if k not in required]
    return required, optional


def check_shapes(schema, contract, apex_text, rep, apex_path=DEFAULT_APEX):
    """Check (e): shape spot-checks schema <-> template contract block, plus
    Apex code-assertion greps for rules enforced in code rather than data."""

    # -- pointer shape backs both refs and derivation_refs (both are arrays of $defs/pointer)
    ptr_req, ptr_opt = _schema_item_shape(schema, ("$defs", "pointer"), rep, "shape-pointer")
    if ptr_req is not None:
        # 'type' is consumed by the pointer-type enum check; the item_required
        # contract lists the pointer's own required keys.
        for shape_key in ("refs", "derivation_refs"):
            code = f"shape-{shape_key}"
            # verify the schema really uses $defs/pointer for this field
            if shape_key == "refs":
                ref_node = dig(schema, ("properties", "evidence", "items", "properties", "refs"),
                               "schema", rep, code)
            else:
                ref_node = dig(schema, ("properties", "criteria", "items", "properties", "derivation_refs"),
                               "schema", rep, code)
            if ref_node is not None:
                if ref_node.get("type") != "array" or (ref_node.get("items") or {}).get("$ref") != "#/$defs/pointer":
                    rep.fail(code,
                             f"schema {shape_key} is no longer an array of #/$defs/pointer "
                             f"(found: {json.dumps(ref_node)[:200]}) — the pointer shape "
                             f"comparison below no longer describes this field.")
                else:
                    rep.ok(code, f"schema {shape_key} is an array of #/$defs/pointer")
            if contract is not None:
                tshape = (contract.get("shapes") or {}).get(shape_key) or {}
                rep.check_eq_exact(code, f"{shape_key} container type", "schema", "array",
                                   "template", tshape.get("type"))
                rep.check_eq_sets(code, f"{shape_key} item required keys", "schema pointer.required",
                                  ptr_req, f"template shapes.{shape_key}.item_required",
                                  tshape.get("item_required") or [])
                rep.check_eq_sets(code, f"{shape_key} item optional keys",
                                  "schema pointer optional (properties minus required)",
                                  ptr_opt,
                                  f"template shapes.{shape_key}.item_optional",
                                  tshape.get("item_optional") or [])

    # -- derived_from_evidence item shape
    code = "shape-derived-from-evidence"
    dfe_req, dfe_opt = _schema_item_shape(
        schema, ("properties", "criteria", "items", "properties", "derived_from_evidence", "items"),
        rep, code)
    if dfe_req is not None and contract is not None:
        tshape = (contract.get("shapes") or {}).get("derived_from_evidence") or {}
        rep.check_eq_exact(code, "derived_from_evidence container type", "schema", "array",
                           "template", tshape.get("type"))
        rep.check_eq_sets(code, "derived_from_evidence item required keys", "schema", dfe_req,
                          "template item_required", tshape.get("item_required") or [])
        rep.check_eq_sets(code, "derived_from_evidence item optional keys",
                          "schema (properties minus required)", dfe_opt,
                          "template item_optional", tshape.get("item_optional") or [])

    # -- grounded is a boolean
    code = "shape-grounded"
    g = dig(schema, ("properties", "criteria", "items", "properties", "grounded", "type"),
            "schema", rep, code)
    if g is not None:
        rep.check_eq_exact(code, "grounded type", "schema", g, "expected", "boolean")
        if contract is not None:
            tshape = (contract.get("shapes") or {}).get("grounded") or {}
            rep.check_eq_exact(code, "grounded type", "schema", g, "template shapes.grounded.type",
                               tshape.get("type"))

    # -- metric: an object, never required (template: object_or_omit)
    code = "shape-metric"
    metric_type = dig(schema, ("$defs", "metric", "type"), "schema", rep, code)
    ev_required = dig(schema, ("properties", "evidence", "items", "required"), "schema", rep, code)
    if metric_type is not None and ev_required is not None:
        if metric_type != "object":
            rep.fail(code, f"schema $defs.metric.type is {metric_type!r}, expected 'object'.")
        elif "metric" in ev_required:
            rep.fail(code, "schema now REQUIRES evidence.metric — template 'object_or_omit' no longer holds.")
        else:
            rep.ok(code, "schema metric is an optional object (object_or_omit)")
        if contract is not None:
            tshape = (contract.get("shapes") or {}).get("metric") or {}
            rep.check_eq_exact(code, "metric shape", "expected", "object_or_omit",
                               "template shapes.metric.type", tshape.get("type"))

    # -- provenance: required keys + additionalProperties: false
    code = "shape-provenance"
    prov = dig(schema, ("properties", "provenance"), "schema", rep, code)
    if prov is not None:
        if contract is not None:
            tshape = (contract.get("shapes") or {}).get("provenance") or {}
            rep.check_eq_sets(code, "provenance required keys", "schema", prov.get("required", []),
                              "template shapes.provenance.required", tshape.get("required") or [])
            rep.check_eq_exact(code, "provenance additionalProperties", "schema",
                               prov.get("additionalProperties"),
                               "template (additional_properties)", tshape.get("additional_properties"))
        elif prov.get("additionalProperties") is not False:
            rep.fail(code, f"schema provenance.additionalProperties is "
                           f"{prov.get('additionalProperties')!r}, expected False.")

    # -- review_summary required keys
    code = "shape-review-summary"
    rs_req = dig(schema, ("properties", "review_summary", "required"), "schema", rep, code)
    if rs_req is not None and contract is not None:
        tshape = (contract.get("shapes") or {}).get("review_summary") or {}
        rep.check_eq_sets(code, "review_summary required keys", "schema", rs_req,
                          "template shapes.review_summary.required", tshape.get("required") or [])

    # -- enum_extensions allowed property names (+ additionalProperties: false)
    code = "shape-enum-extensions"
    ee = dig(schema, ("properties", "enum_extensions"), "schema", rep, code)
    if ee is not None:
        allowed = list((ee.get("properties") or {}).keys())
        if ee.get("additionalProperties") is not False:
            rep.fail(code, f"schema enum_extensions.additionalProperties is "
                           f"{ee.get('additionalProperties')!r}, expected False — 'allowed_keys' "
                           f"is only meaningful under additionalProperties: false.")
        else:
            rep.ok(code, "schema enum_extensions has additionalProperties: false")
        if contract is not None:
            tshape = (contract.get("shapes") or {}).get("enum_extensions") or {}
            rep.check_eq_sets(code, "enum_extensions allowed keys", "schema properties", allowed,
                              "template shapes.enum_extensions.allowed_keys",
                              tshape.get("allowed_keys") or [])

    # -- threshold (0.14.0): optional per-criterion object with a between conditional
    code = "shape-threshold"
    th = dig(schema, ("properties", "criteria", "items", "properties", "threshold"),
             "schema", rep, code)
    if th is not None:
        th_req = th.get("required") or []
        th_opt = sorted(set((th.get("properties") or {}).keys()) - set(th_req))
        crit_required = dig(schema, ("properties", "criteria", "items", "required"), "schema", rep, code) or []
        if "threshold" in crit_required:
            rep.fail(code, "schema now REQUIRES criteria.threshold — absence is the honest "
                           "signal for criteria with no numeric cutoff; it must stay optional.")
        elif th.get("additionalProperties") is not False:
            rep.fail(code, "schema threshold.additionalProperties must be false.")
        elif (th.get("then") or {}).get("required") is None or "value_upper" not in (th.get("then") or {}).get("required", []):
            rep.fail(code, "schema threshold lost its between-requires-value_upper conditional.")
        else:
            rep.ok(code, "schema threshold is optional, strict, and between requires value_upper")
        if contract is not None:
            tshape = (contract.get("shapes") or {}).get("threshold") or {}
            rep.check_eq_exact(code, "threshold shape", "expected", "object_or_omit",
                               "template shapes.threshold.type", tshape.get("type"))
            rep.check_eq_sets(code, "threshold required keys", "schema", th_req,
                              "template shapes.threshold.item_required", tshape.get("item_required") or [])
            rep.check_eq_sets(code, "threshold optional keys", "schema (properties minus required)", th_opt,
                              "template shapes.threshold.item_optional", tshape.get("item_optional") or [])

    # -- register (0.14.0): optional first-class identity block
    code = "shape-register"
    regblk = dig(schema, ("properties", "register"), "schema", rep, code)
    if regblk is not None:
        top_required = schema.get("required") or []
        if "register" in top_required:
            rep.fail(code, "schema now REQUIRES the register block — 0.11-0.13 payloads carry "
                           "identity in enum_extensions and Apex enforces at-least-one-source; "
                           "requiring it here would fail every older payload at the schema.")
        elif regblk.get("additionalProperties") is not False:
            rep.fail(code, "schema register.additionalProperties must be false.")
        else:
            rep.ok(code, "schema register block is optional and strict")
        if contract is not None:
            tshape = (contract.get("shapes") or {}).get("register") or {}
            rep.check_eq_exact(code, "register shape", "expected", "object_or_omit",
                               "template shapes.register.type", tshape.get("type"))
            rep.check_eq_sets(code, "register required keys", "schema", regblk.get("required") or [],
                              "template shapes.register.required", tshape.get("required") or [])

    # -- attach (0.14.0): optional destination instruction
    code = "shape-attach"
    att = dig(schema, ("properties", "attach"), "schema", rep, code)
    if att is not None:
        top_required = schema.get("required") or []
        if "attach" in top_required:
            rep.fail(code, "schema now REQUIRES attach — create mode is the default and must stay so.")
        elif att.get("additionalProperties") is not False:
            rep.fail(code, "schema attach.additionalProperties must be false.")
        else:
            rep.ok(code, "schema attach block is optional and strict")
        if contract is not None:
            tshape = (contract.get("shapes") or {}).get("attach") or {}
            rep.check_eq_exact(code, "attach shape", "expected", "object_or_omit",
                               "template shapes.attach.type", tshape.get("type"))
            rep.check_eq_sets(code, "attach required keys", "schema", att.get("required") or [],
                              "template shapes.attach.required", tshape.get("required") or [])

    # -- deleted_from_precedent item keys
    code = "shape-deleted-from-precedent"
    dfp_req, dfp_opt = _schema_item_shape(
        schema, ("properties", "criteria", "items", "properties", "deleted_from_precedent", "items"),
        rep, code)
    if dfp_req is not None and contract is not None:
        tshape = (contract.get("shapes") or {}).get("deleted_from_precedent") or {}
        rep.check_eq_sets(code, "deleted_from_precedent item required keys", "schema", dfp_req,
                          "template item_required", tshape.get("item_required") or [])
        rep.check_eq_sets(code, "deleted_from_precedent item optional keys", "schema", dfp_opt,
                          "template item_optional", tshape.get("item_optional") or [])

    # -- considered_and_rejected keys (an OBJECT in the schema, not an array —
    #    the template contract's item_required/item_optional name its keys)
    code = "shape-considered-and-rejected"
    car_req, car_opt = _schema_item_shape(
        schema, ("properties", "criteria", "items", "properties", "considered_and_rejected"),
        rep, code)
    if car_req is not None and contract is not None:
        tshape = (contract.get("shapes") or {}).get("considered_and_rejected") or {}
        rep.check_eq_sets(code, "considered_and_rejected required keys", "schema", car_req,
                          "template item_required", tshape.get("item_required") or [])
        rep.check_eq_sets(code, "considered_and_rejected optional keys", "schema", car_opt,
                          "template item_optional", tshape.get("item_optional") or [])

    # -- top-level additionalProperties: false
    code = "shape-top-level-additional-properties"
    top_ap = schema.get("additionalProperties", "MISSING")
    if top_ap is not False:
        rep.fail(code, f"schema top-level additionalProperties is {top_ap!r}, expected False — "
                       f"an unknown top-level key would silently pass validation.")
    else:
        rep.ok(code, "schema top-level additionalProperties is false")
    if contract is not None:
        tval = (contract.get("shapes") or {}).get("top_level_additional_properties", "MISSING")
        rep.check_eq_exact(code, "top-level additionalProperties", "schema", top_ap,
                           "template shapes.top_level_additional_properties", tval)

    # -- v0.13.0 conditional: schema side. The newest schema must declare the
    # root if/then making an edge-bearing payload require its evidence base.
    # (Apex side is the apex-evidence-required-when-edges assertion below.)
    code = "shape-conditional-evidence-required"
    cond_if, cond_then = schema.get("if"), schema.get("then")
    declares = (isinstance(cond_if, dict) and isinstance(cond_then, dict)
                and "derived_from_evidence" in json.dumps(cond_if)
                and "evidence" in (cond_then.get("required") or []))
    if declares:
        rep.ok(code, "schema declares the evidence-required-when-edges conditional (if/then)")
    else:
        rep.fail(code, "schema does NOT declare the evidence-required-when-edges conditional — "
                       "a payload with derived_from_evidence edges and no evidence block would "
                       "validate clean, permitting the provenance-stripped state by design "
                       "(required as of interchange 0.13.0).")

    # -- Apex code assertions: enforcement that lives in code, not constants
    for code, pattern, meaning in APEX_CODE_ASSERTIONS:
        if re.search(pattern, apex_text):
            rep.ok(code, f"Apex enforcement present: {meaning}")
        else:
            rep.fail(code,
                     f"Apex enforcement NOT FOUND: {meaning}.\n"
                     f"      grep pattern: {pattern}\n"
                     f"      Either the enforcement was deleted from KynesinIngestion.cls "
                     f"(a real contract break) or it was refactored — if refactored, update "
                     f"this pattern IN THE SAME CHANGE and re-verify it still fails when the "
                     f"enforcement is removed.")

    # -- sibling-class assertions: enforcement outside the ingestion validator
    classes_dir = os.path.dirname(os.path.abspath(apex_path))
    for code, filename, pattern, meaning in SIBLING_CODE_ASSERTIONS:
        sibling = os.path.join(classes_dir, filename)
        try:
            with open(sibling, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as e:
            rep.fail(code, f"cannot read {filename}: {e}")
            continue
        if re.search(pattern, text):
            rep.ok(code, f"Apex enforcement present ({filename}): {meaning}")
        else:
            rep.fail(code,
                     f"Apex enforcement NOT FOUND in {filename}: {meaning}.\n"
                     f"      grep pattern: {pattern}\n"
                     f"      Either the enforcement was deleted (a real contract break) or it "
                     f"was refactored — if refactored, update this pattern IN THE SAME CHANGE "
                     f"and re-verify it still fails when the enforcement is removed.")

# ------------------------------------------------------- validation-rule side

def resolve_objects_dir(arg_objects, apex_path, rep):
    """Locate force-app/main/default/objects. Order: explicit --objects, the
    repo this script lives in, then the checkout the Apex file came from (the
    synced copy in kynesin-schema runs with only --apex pointing at a
    kynesin-sfdx clone, and the rules live beside that Apex). The rules are a
    contract layer like the other three, so if the directory cannot be found
    the check FAILS rather than silently skipping."""
    candidates = []
    if arg_objects:
        candidates.append(os.path.abspath(arg_objects))
    else:
        candidates.append(DEFAULT_OBJECTS)
        if apex_path:
            candidates.append(os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(apex_path))), "objects"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    rep.fail("vr-objects-path",
             "force-app objects directory not found (tried: "
             + ", ".join(candidates) + ") — the validation-rule layer of the "
             "contract cannot be checked. Pass --objects pointing at a "
             "kynesin-sfdx checkout's force-app/main/default/objects.")
    return None


def check_validation_rules(objects_dir, rep):
    """Check (g): the DML validation-rule layer. For each contract-coupled
    rule: the metadata file must exist, be active, and its
    errorConditionFormula must still reference every field/value the contract
    couples — so deleting a rule, flipping <active>, or rewriting the formula
    away from its fields all fail here instead of surfacing as a surprise
    DML rejection (or, worse, a silently-missing rejection)."""
    for code, obj, rule, tokens, meaning in VALIDATION_RULE_ASSERTIONS:
        path = os.path.join(objects_dir, obj, "validationRules",
                            rule + ".validationRule-meta.xml")
        if not os.path.isfile(path):
            rep.fail(code,
                     f"validation rule {obj}.{rule} NOT FOUND at {path} — the DML-layer "
                     f"enforcement of '{meaning}' is gone. Restore the rule or, if it was "
                     f"deliberately retired, remove the schema/Apex conditional it mirrors "
                     f"and this assertion IN THE SAME CHANGE.")
            continue
        try:
            root = ET.parse(path).getroot()
        except ET.ParseError as e:
            rep.fail(code, f"validation rule file {path} is not parseable XML: {e}")
            continue
        fields = {child.tag.rsplit('}', 1)[-1]: (child.text or "") for child in root}
        if fields.get("active", "").strip() != "true":
            rep.fail(code,
                     f"validation rule {obj}.{rule} is present but NOT ACTIVE "
                     f"(active={fields.get('active', 'MISSING')!r}) — deactivation is the "
                     f"quiet way to defang '{meaning}'; reactivate it or retire the "
                     f"contract conditional it mirrors.")
            continue
        formula = fields.get("errorConditionFormula", "")
        if not formula.strip():
            rep.fail(code, f"validation rule {obj}.{rule} has an empty or missing "
                           f"errorConditionFormula — active but enforcing nothing.")
            continue
        missing = [t for t in tokens if t not in formula]
        if missing:
            rep.fail(code,
                     f"validation rule {obj}.{rule} no longer references {missing} in its "
                     f"errorConditionFormula — the contract couples {tokens} "
                     f"for '{meaning}'.\n"
                     f"      formula: {' '.join(formula.split())}")
            continue
        rep.ok(code, f"validation rule {obj}.{rule} exists, is active, and references "
                     f"{tokens} ({meaning})")


def check_schema_conditional_coverage(schema, rep):
    """Check (h): every criteria-level conditional the schema declares must be
    accounted for at the DML layer — mapped to a validation-rule assertion or
    explicitly recorded as deliberately uncovered. A new schema conditional
    whose trigger is in neither list fails here: that is exactly the drift
    Claude Science flagged, a constraint added to schema and Apex with no DML
    twin, invisible to the three compared artifacts until the first insert."""
    code = "vr-conditional-coverage"
    allof = dig(schema, ("properties", "criteria", "items", "allOf"), "schema", rep, code)
    if allof is None:
        return
    declared = []
    for i, cond in enumerate(allof):
        if not isinstance(cond, dict) or not isinstance(cond.get("if"), dict):
            rep.fail(code, f"schema criteria allOf[{i}] is not an if/then conditional — "
                           f"this coverage check no longer understands the schema's shape; "
                           f"update it in the same change as the schema.")
            continue
        declared.append("+".join(sorted((cond["if"].get("properties") or {}).keys())))
    known_vr_codes = {c for c, _obj, _rule, _tokens, _meaning in VALIDATION_RULE_ASSERTIONS}
    for trigger in declared:
        if trigger not in SCHEMA_CONDITIONAL_COVERAGE:
            rep.fail(code,
                     f"schema declares a criteria conditional triggered by {trigger!r} that "
                     f"the DML coverage map does not know — a payload could pass every other "
                     f"layer and hit (or silently miss) DML enforcement nobody asserted. Add "
                     f"the matching validation rule plus a VALIDATION_RULE_ASSERTIONS entry, "
                     f"or record it in SCHEMA_CONDITIONAL_COVERAGE as deliberately uncovered "
                     f"with the reason, in the same change.")
            continue
        vr_code, reason = SCHEMA_CONDITIONAL_COVERAGE[trigger]
        if vr_code is None:
            rep.ok(code, f"schema conditional on {trigger!r} accounted for: deliberately "
                         f"uncovered at DML — {reason}")
        elif vr_code not in known_vr_codes:
            rep.fail(code,
                     f"coverage map sends the {trigger!r} conditional to {vr_code!r}, but no "
                     f"VALIDATION_RULE_ASSERTIONS entry carries that code — the map points at "
                     f"an assertion that does not exist.")
        else:
            rep.ok(code, f"schema conditional on {trigger!r} is covered by validation-rule "
                         f"assertion {vr_code}")
    declared_set = set(declared)
    for trigger, (vr_code, _reason) in sorted(SCHEMA_CONDITIONAL_COVERAGE.items()):
        if trigger not in declared_set:
            rep.fail(code,
                     f"coverage map lists a criteria conditional on {trigger!r} but the "
                     f"schema no longer declares one — the schema half of the couple "
                     f"vanished. Remove the map entry (and retire its rule) or restore the "
                     f"conditional, in the same change.")



def check_checker_twin(schema_arg, rep):
    """The checker keeps a copy in the schema repo so that repo's CI is
    self-contained. Until now the two were held in step by a comment saying
    "keep the two in sync" — which is exactly the arrangement this whole script
    exists to prove does not work. A contract maintained by convention across
    two locations drifts; that is the thesis. This is the script applying it to
    itself.

    Only meaningful when --schema points at a clone (a bare .schema.json file
    has no scripts/ beside it), and only when the twin exists — a schema-only
    contributor who deleted their copy is not committing drift.
    """
    if not schema_arg or not os.path.isdir(schema_arg):
        return
    twin = os.path.join(schema_arg, "scripts", "check_wire_contract.py")
    if not os.path.isfile(twin):
        rep.note(f"checker twin not present at {twin} — nothing to compare.")
        return
    mine = os.path.abspath(__file__)
    if os.path.abspath(twin) == mine:
        return  # running the schema repo's own copy against itself
    def digest(path):
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    a, b = digest(mine), digest(twin)
    if a == b:
        rep.ok("checker-twin-sync",
               f"checker copies are byte-identical (sha256 {a[:12]})")
        return
    rep.fail("checker-twin-sync",
             f"the two copies of check_wire_contract.py have DRIFTED.\n"
             f"      this copy: {mine}\n        sha256 {a}\n"
             f"      twin:      {twin}\n        sha256 {b}\n"
             f"      Copy the newer over the older. The schema repo's CI runs its own\n"
             f"      copy, so a drifted twin means the two repos are enforcing\n"
             f"      different contracts while both report OK.")


# ----------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Kynesin wire-contract drift check "
                                             "(schema <-> template <-> Apex).")
    ap.add_argument("--schema", default=None,
                    help="Path to the interchange schema JSON file, or to a clone of "
                         "kynesin-io/kynesin-schema (newest versions/*.schema.json is used). "
                         "Default: $KYNESIN_SCHEMA_PATH.")
    ap.add_argument("--template", default=DEFAULT_TEMPLATE,
                    help="Path to kynesin-skill/template/SKILL.md (absent => loud skip).")
    ap.add_argument("--apex", default=DEFAULT_APEX,
                    help="Path to KynesinIngestion.cls.")
    ap.add_argument("--objects", default=None,
                    help="Path to force-app/main/default/objects (validation-rule layer). "
                         "Default: this repo's copy, else derived from --apex; if neither "
                         "exists the validation-rule checks FAIL rather than skip.")
    args = ap.parse_args(argv)

    rep = Report()

    schema_file = resolve_schema_path(args.schema, rep)
    schema = None
    if schema_file:
        schema = load_json_file(schema_file, "schema", rep, "schema-parse")

    apex_text = None
    try:
        with open(args.apex, "r", encoding="utf-8") as f:
            apex_text = f.read()
    except OSError as e:
        rep.fail("apex-read", f"Cannot read Apex validator at {args.apex}: {e}")

    template_absent = not os.path.isfile(args.template)
    contract = None
    if template_absent:
        print("=" * 78)
        print("TEMPLATE ABSENT — schema<->Apex checks only "
              "(template checks run locally via pre-commit)")
        print(f"  looked for: {args.template}")
        print("=" * 78)
    else:
        contract = load_template_contract(args.template, rep)
        # A PRESENT template whose contract block cannot be parsed is a failure
        # (load_template_contract already recorded it); contract stays None and
        # the three-way checks degrade to schema<->Apex, but the parse failure
        # itself fails the build.

    if schema is not None and apex_text is not None:
        consts = parse_apex_constants(apex_text, rep)
        check_required_blocks(schema, contract, consts, rep)
        check_closed_enums(schema, contract, consts, rep)
        check_core_enums(schema, contract, consts, rep)
        check_versions(schema, contract, consts, schema_file, rep)
        check_shapes(schema, contract, apex_text, rep, args.apex)
        objects_dir = resolve_objects_dir(args.objects, args.apex, rep)
        if objects_dir is not None:
            check_validation_rules(objects_dir, rep)
        check_schema_conditional_coverage(schema, rep)

    check_checker_twin(args.schema, rep)

    # ---- report
    print()
    for code, msg in rep.passes:
        print(f"PASS  [{code}] {msg}")
    for note in rep.notes:
        print(f"NOTE  {note}")
    print()
    if rep.failures:
        print(f"WIRE-CONTRACT DRIFT: {len(rep.failures)} failure(s). "
              f"Fix the drifted artifact — do not loosen this check.")
        for code, msg in rep.failures:
            print(f"FAIL  [{code}] {msg}")
        return 1
    n = len(rep.passes)
    scope = "schema<->Apex (template absent)" if template_absent else "schema<->template<->Apex"
    print(f"WIRE CONTRACT OK: {n} checks passed, 0 failures ({scope}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

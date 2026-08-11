#!/usr/bin/env python3
"""Wire-contract drift check: JSON Schema <-> skill-template contract block <-> Apex validator.

Three artifacts declare the Kynesin interchange contract independently:

  1. the JSON Schema        (kynesin-schema repo, versions/kynesin-interchange-X.Y.Z.schema.json)
  2. the skill template     (kynesin-skill/template/SKILL.md, machine-readable
                             contract block under the <!-- kynesin-payload-contract --> marker)
  3. the Apex validator     (force-app/main/default/classes/KynesinIngestion.cls constants)

Eleven wire-format drifts accumulated across these with nothing comparing them,
and a live run silently lost its provenance graph. This script makes ANY future
divergence fail a build: it compares required blocks, all thirteen closed enums
the contract block declares — including every one whose values Apex persists or
enforces (origin, basis_class, payload_scope, metric.type) and the
register_status proposed-entry scope, which is checked schema<->template only
because Apex deliberately ignores that block — both open-enum core sets,
supported versions, and the structural shapes, three ways, and exits nonzero
listing EVERY mismatch (it never stops at the first). Schema enums outside the
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
import json
import os
import re
import sys

# Defaults are resolved relative to the repo this script lives in (its parent
# directory), so the check works from any cwd, in CI, and in the synced copy
# inside the kynesin-schema repo (where these paths are absent and the template
# check loudly skips unless --template/--apex point at a kynesin-sfdx checkout).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_TEMPLATE = os.path.join(_REPO_ROOT, "kynesin-skill", "template", "SKILL.md")
DEFAULT_APEX = os.path.join(_REPO_ROOT, "force-app", "main", "default", "classes", "KynesinIngestion.cls")

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
}
CONTRACT_CORE_KEYS = {"criterion_domain", "evidence_class"}
CONTRACT_SHAPE_KEYS = {
    "refs", "derivation_refs", "derived_from_evidence", "grounded", "metric",
    "provenance", "review_summary", "enum_extensions", "deleted_from_precedent",
    "considered_and_rejected", "top_level_additional_properties",
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
]


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


def check_shapes(schema, contract, apex_text, rep):
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
        check_shapes(schema, contract, apex_text, rep)

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

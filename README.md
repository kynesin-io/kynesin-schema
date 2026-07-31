# Kynesin Discovery-to-Protocol Interchange Schema

A versioned JSON Schema that carries the scientific reasoning behind a
validated drug target and candidate into a clinical trial's population
definition and eligibility criteria, with provenance preserved.

## What this is

The schema defines an interchange payload: target, candidate, indication,
population, the eligibility criteria derived for the trial, the evidence those
criteria rest on, and the provenance of the derivation. Each criterion records
not only what it says but why — its derivation, the evidence it draws on, and
the per-edge relationship to that evidence (whether the evidence supports,
complicates, or contradicts the criterion).

## Why it exists

No widely-used format carries the derivation reasoning behind an eligibility
criterion, or the link from a criterion to the evidence it rests on. Protocols
carry criteria; they do not carry *why*. A criterion that cites evidence which
undermines it is indistinguishable, in a protocol document, from one that does
not — yet that distinction is exactly what a reviewer needs. This schema makes
the reasoning and its evidence base explicit and machine-checkable.

## Authorship and status

Authored by Babu Palanisamy.

- **First public version: v0.10.0, 2026-07-31.**
- **Status: pre-1.0.** Breaking changes are possible between minor versions
  until 1.0.

## Version history

- **0.6.0** — added `origin`, `origin_detail`, `review_priority`,
  `review_summary`.
- **0.7.0** — consequence-based review priority.
- **0.8.0** — revisions from hand-population against a real run: separated
  deletion from consideration-and-rejection, linked prerequisites and boundary
  decisions to the criteria they concern, allowed register-carried figures to
  be cited in contested evidence, admitted prose metrics.
- **0.9.0** — added the required `review_action` field (`own` vs `verify`),
  orthogonal to `review_priority`.
- **0.10.0** — `derived_from_evidence` became an array of
  `{evidence_id, relationship}` objects, so each criterion-to-evidence edge
  carries its own relationship (`derived_from` / `supports` / `complicates` /
  `contradicts`) rather than a flat citation list.

## Layout

- `versions/kynesin-interchange-<version>.schema.json` — the versioned schemas.
- `examples/act3-payload-rund.json` — a worked example payload.

Reference a specific version file directly — e.g.
`versions/kynesin-interchange-0.10.0.schema.json`. There is deliberately no
moving `current` pointer: for a versioned interchange format, a consumer should
pin the exact version it was written against and migrate deliberately, not
track a target that shifts under it.

## How to validate

The schema targets JSON Schema Draft 2020-12. To validate a payload against a
specific version with the Python `jsonschema` library:

```python
import json
from jsonschema import Draft202012Validator

schema = json.load(open("versions/kynesin-interchange-0.10.0.schema.json"))
payload = json.load(open("examples/act3-payload-rund.json"))
errors = list(Draft202012Validator(schema).iter_errors(payload))
print("valid" if not errors else errors)
```

`examples/act3-payload-rund.json` validates against
`versions/kynesin-interchange-0.10.0.schema.json`; use it as a reference for the
expected shape. It is a `partial` payload — its `review_summary` describes the
nine criteria it carries, not the full run they were drawn from.

## Scope

This schema is published openly under CC-BY-4.0 (see `LICENSE`). Reference
implementations and the Salesforce package that consume it are separate works
and are **not** covered by this licence. The decision-register method is
likewise separate and not part of this specification.

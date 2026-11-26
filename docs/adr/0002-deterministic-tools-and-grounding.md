# ADR 0002: Deterministic tools produce every number; a grounding gate enforces it

Status: accepted (2026-11-26)

## Context
The customer's main complaint is inaccurate answers. Language models make arithmetic and recall errors,
especially with Indian number formats and compound financial calculations.

## Decision
The model only chooses tools and phrases results. A `Decimal` finance library and versioned policy rules
(with clause IDs) compute every amount, date, count and eligibility decision. Text replies pass a grounding
gate: unsupported figures trigger one regeneration with feedback, then a templated answer built from tool
values. Golden-set evals check that replies quote the values the tools returned in the same run.

## Consequences
- Numbers are testable (property tests, golden values) and explainable (clause IDs).
- New questions need a tool, not a prompt tweak: one function plus a registry line.
- Non-numeric claims are not machine-checked; they rely on tool-first prompting and evals.

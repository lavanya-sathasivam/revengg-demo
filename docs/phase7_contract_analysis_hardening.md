# Phase 7 Contract-Analysis Hardening

**Status:** PASS for the local/test scope described below.

**Date:** 2026-10-23

**Scope:** `clauses.py`, `adapter.py`, `db.py`, `impact.py`, `api_b.py`,
`application.py`, `tests/test_phase7_hardening.py`, and the related handoff
documentation. No frontend, template, CSS, JavaScript, Phase 8, or database
schema migration was added.

**Acceptance rule:** Phase 7 is marked PASS only for checks that were actually
run. The results in section 8 are from this working tree; the changes remain
uncommitted.

---

## 1. What was wrong before Phase 7

The Phase 5/6 engine was structurally sound but had six correctness and
provenance gaps:

1. A payment-period value could be read from any number in the clause, so a
   percentage or rupee amount could be treated as a day count.
2. Month, year, and business-day periods had no explicit failure path; a caller
   could supply a value that happened to match an unrelated number.
3. The counterfactual emitted an "Interest accrual window" row derived from the
   payment period. The corpus starts interest from the Payment Due Date, not
   from the end of the cure period, so that row was false.
4. Evidence was silently truncated. A long clause was cut to 400/600
   characters without telling the caller or retaining the full text.
5. A stored graph was read without proving that it was built from the current
   clause set. Saving clauses did not invalidate the graph pointer.
6. Analysis output did not say which contract version, clause hash, graph build,
   or rule catalogue produced it, so a result could not be reproduced or
   distinguished from a stale one.

---

## 2. Workstream A - units, values, and fail-closed arithmetic

### 2.1 Unit-aware durations

`clauses.find_within_period()` is now the single duration reader. It returns:

```json
{
  "days": 30,
  "unit": "days",
  "supported": true,
  "ambiguous": false,
  "reason": null
}
```

Supported input is a calendar-day period stated as digits (`within 30 days`) or
a simple English number word (`within thirty days`, `within twenty-five days`).

Months, years, and business/working days are parsed only to be rejected. No
month length, year length, month-end convention, or business-day calendar is
configured, and no conversion is performed:

- `within 2 months` - unsupported; `days` is `null`.
- `within 1 year` - unsupported; `days` is `null`.
- `within 10 business days` - unsupported and ambiguous.
- two competing `within` periods - ambiguous; the engine does not choose one.

`find_within_days()` returns a value only when the period is a single supported
calendar-day period. `within_period_error()` exposes a safe rejection message
for ambiguous cases.

### 2.2 Percentages and money are not durations

`impact._days_in()` only accepts a supported `within` period or a single
distinct day value. Percentages and rupee amounts are never converted to days.
`clauses.find_numbers()` returns exact `Decimal` values for days/months/years
and percentages; `clauses.find_amounts()` returns exact `Decimal` rupee amounts.
Money is a separate parser, not a duration unit.

`impact._stored_values()` applies the unit implied by the modelled clause type:

- payment changes compare against calendar days only;
- SLA changes compare against percentages only;
- other clause types retain the previous number-only check because the engine
  models no arithmetic for them.

A payment clause that states only `2% per month`, or an SLA clause with no
percentage, is rejected as `unsupported_unit`/`unsupported_clause_value` rather
than reinterpreted.

### 2.3 Ambiguity and invalid values fail closed

Stable error codes added or used by this workstream:

| Code | Meaning |
|---|---|
| `invalid_change_value` | Not a finite, non-negative numeric value. |
| `missing_change_value` | `old_value` or `new_value` was omitted. |
| `stale_old_value` | `old_value` does not match the value stored in this contract. |
| `unsupported_unit` | The stored clause states a different unit; no conversion is performed. |
| `unsupported_clause_value` | The modelled unit has no stored value to compare. |
| `ambiguous_clause_value` | Competing or non-calendar periods cannot be selected safely. |
| `sla_out_of_range` | An SLA value is outside `[0, 100]`. |

SLA values are validated inclusively at `0` and `100`; zero is a valid value.
Values below zero are rejected by the general non-negative rule; values above
100 use `sla_out_of_range`.

### 2.4 Decimal handling

Clause numbers, service-credit percentages, rupee amounts, and counterfactual
arithmetic use `Decimal`. Float inputs are converted once through
`Decimal(str(value))`, not `Decimal(float)`, so a caller's binary representation
error does not become a contract value. `_fmt_inr()` does no rounding. It
preserves the exact numeric value and normalises trailing fractional zeros;
this avoids the previous `int(round(value))` banker's-rounding defect.

### 2.5 Interest row removed

`_payment_counterfactual()` no longer emits an interest-accrual row. If an
interest clause exists, the response carries an explicit note that interest
accrual is not derived from the payment period until the accrual start and end
events are modelled. This is the one intentional backward-incompatible output
change in Phase 7.

---

## 3. Workstream B - graph and analysis provenance

### 3.1 One authoritative graph

`contracts.current_graph_build_id` is the only graph pointer used by analysis.
`adapter.get_graph_binding(contract_id)` classifies the graph:

| State | Meaning | Analysis |
|---|---|---|
| `current` | Published build bound to the current clause version, with matching row counts. | Allowed; `supported`. |
| `stale` | Pointer invalidated, version mismatch, failed/missing build, missing binding columns, or row-count mismatch. | HTTP `409`; graph rows are not returned. |
| `missing` | No published graph for this contract. | HTTP `409`. |
| `legacy_unbound` | Read-only pre-binding database with graph rows but no pointer column. | Allowed as partial evidence. |

`adapter.get_nodes()` and `adapter.get_edges()` return rows only from the
current build. Edges are still scoped on the edge row and on both endpoint node
rows. Legacy rows remain readable without DDL, but are labelled partial.

### 3.2 Atomic invalidation

`db.save_clauses()` clears `current_graph_build_id` in the same transaction that
replaces the clause rows. A reader can never see the new clause set with the old
graph pointer. The invalidation is rolled back with the rest of the transaction
if the save fails.

`db.create_contract_version()` also clears the pointer when it makes a new
version current. The old graph rows and build record remain for audit, but are
not analysis-readable.

### 3.3 Analysis binding

Every successful report carries an `analysis_binding` object (also attached to
`counterfactual`) with:

```json
{
  "contract_id": 1,
  "clause_id": "7.2",
  "clause_version_id": 1,
  "clause_sha256": "...",
  "contract_version_id": 1,
  "graph_build_id": 1,
  "graph_build_version_id": 1,
  "graph_build_version": "phase5.1",
  "graph_build_hash": "...",
  "graph_state": "current",
  "graph_reason_code": "graph_current",
  "rules_fingerprint": "...",
  "expected_clause_sha256": null
}
```

The top-level `rules_fingerprint` matches the counterfactual and the binding.
The catalogue version is `impact.IMPACT_RULES_VERSION` (`phase7.1`).

### 3.4 Optional expected clause hash

`POST /impact` accepts an optional `expected_clause_sha256` (exactly 64 hex
characters). When supplied, it must equal the stored hash of the clause being
changed:

- match - analysis proceeds;
- mismatch - HTTP `409`, `error_code: "clause_hash_mismatch"`;
- clause has no stored hash - HTTP `409`,
  `error_code: "clause_hash_unavailable"`.

Omitting the field preserves backward compatibility.

### 3.5 HTTP error shape

`api_b.AnalysisError` keeps the existing string `detail` and adds a stable
`error_code`. The application handler returns:

```json
{
  "detail": "safe, human-readable message",
  "error_code": "graph_stale"
}
```

Safe graph errors:

| Status | `error_code` |
|---:|---|
| 409 | `graph_stale` |
| 409 | `graph_not_built` |
| 409 | `graph_build_failed` |
| 409 | `clause_hash_mismatch` |
| 409 | `clause_hash_unavailable` |

These messages never include clause text, stored hashes, graph rows, or parser
detail.

---

## 4. Workstream C - evidence and status taxonomy

### 4.1 No silent truncation

Evidence and the changed clause retain their complete `text` and a
`text_length`. The shorter display field is named `excerpt` (evidence) or
`text_excerpt` (changed clause). When it is shorter than the source, it ends
with an explicit marker:

```text
[EXPLICIT EXCERPT: 400 of 3120 characters shown; 2720 omitted.
 The full clause text is in the 'text' field.]
```

`excerpt_truncated` / `text_excerpt_truncated` is a boolean. The HTTP response
therefore contains the full clause text even when a UI chooses to render only
the excerpt.

### 4.2 Status taxonomy

The public statuses are:

- `supported` - the graph is current and the model has what it needs;
- `partially_supported` - legacy/unbound graph, or the report used a note-based
  limitation;
- `unsupported` - no arithmetic is modelled for the requested change type.

`analysis_status` is attached to the report. Evidence items, doubts, ranked
review points, conflicts, and `counterfactual` carry their own status. A
read-only legacy graph is always `partially_supported`, even when its rows are
otherwise sufficient.

### 4.3 Calculation traces

Every counterfactual row carries:

- `rule_id` from the published rule catalogue;
- `assumptions` (a list);
- `calculation` with `rule_id`, `formula`, `inputs`, and `assumptions`.

The counterfactual also returns the sorted `rule_ids`, the union of
assumptions, and `rules_fingerprint`.

### 4.4 Type-specific doubts and conflicts

Doubt and conflict wording uses the changed clause type and its evidence. A
payment change talks about a payment period in days; an SLA change talks about
service-level percentages. Definition collisions, cross-reference mismatches,
and definition drift carry the clauses, values, hashes, and statuses that
support them.

---

## 5. Compatibility

- Existing response keys and the 12 MVP items are preserved.
- `detail` in HTTP errors remains a string; `error_code` is additive.
- Omitting `expected_clause_sha256` behaves as before.
- `GET /graph/{id}` and `GET /clauses/{id}` keep their existing payloads and add
  `binding`/`analysis_ready` and clause version/hash fields.
- Legacy read-only databases remain readable without migration or DDL and are
  labelled `legacy_unbound` / `partially_supported`.
- The intentional change is the removal of the false interest-accrual
  counterfactual row, replaced by an explanatory note.
- Existing counterfactual row names and calculations for payment and SLA are
  otherwise unchanged.
- No schema migration is required: the work uses the Phase 5
  `current_graph_build_id`, `graph_builds`, node/edge `build_id`, and clause
  `version_id`/`clause_sha256` columns.

---

## 6. Tests added

`tests/test_phase7_hardening.py` contains 26 focused tests:

- duration units, number words, ambiguity, months/years/business days;
- percentages and money are not day counts;
- Decimal formatting without banker's rounding;
- SLA `[0, 100]` bounds with zero accepted;
- interest-row removal and explanatory note;
- calculation traces, assumptions, rule IDs, and fingerprint;
- full evidence text plus explicit excerpts on long clauses;
- supported / partially supported / unsupported statuses;
- type-specific doubt and conflict evidence;
- current graph binding, build hash, clause hash, and version binding;
- atomic pointer invalidation on a successful clause save;
- rollback preserving the pointer when a clause save fails;
- new contract version invalidating the pointer without deleting graph rows;
- HTTP `409` for stale and missing graphs;
- current-build-only node/edge reads;
- row-count tampering making the graph stale;
- matching, mismatching, unavailable, and malformed expected hashes;
- a read-only legacy graph remaining usable but partial, with no DDL.

---

## 7. Verification commands

```powershell
python -m unittest discover -s tests -p "test_phase7_hardening.py" -v
python -m unittest discover -s tests -v
python -m py_compile adapter.py api_b.py application.py clauses.py db.py impact.py tests/test_phase7_hardening.py
git diff --check
git status --short
git diff --stat
```

All tests use temporary databases. `demo.db`, `app.db`, the real uploads
directory, and `.env` were not modified.

---

## 8. Results

| Check | Result |
|---|---|
| Phase 7 focused suite | **26 tests, OK** |
| Full `tests/` suite | **262 tests, OK** (67.4 s) |
| `py_compile` on changed modules and the new test | **OK** |
| `git diff --check` | **Clean** (Git only warned that `db.py` CRLF will normalise to LF on the next touch) |
| Phase 5 legacy/read-only compatibility | **OK** (included in the full suite) |
| Person A compatibility subprocess | **OK** (included in the full suite) |
| Frontend/template changes | **None** |
| Database schema migration | **None** |

The full suite increased from the Phase 6 baseline of 236 tests to 262 because
of the 26 new Phase 7 tests. The root `demo.db`/`app.db` files are not part of
these checks.

---

## 9. Residual risks and limitations

- This is local SQLite/test evidence, not production validation.
- Graph binding verifies the build record, version, status, and row counts. It
  does not recompute the stored build hash against every row on every read, so a
  deliberate row edit that preserves the counts would require a separate
  integrity check.
- Legacy databases are read as partial evidence by design. They cannot supply a
  graph build hash, clause version, or clause hash.
- The unit model is still limited to the change types the API models: calendar
  days for payment and percentages for SLA. Months, years, business days, and
  other conventions are rejected, not supported.
- The rule catalogue is a deterministic rule engine, not a general
  defeasible-reasoning system.
- Evidence excerpts are display aids; consumers must use `text` when the full
  clause matters.
- The stale/missing graph response requires a caller to rebuild the graph. No
  automatic rebuild is attempted during analysis.
- No commit, push, deployment, package installation, or external service call
  was performed.

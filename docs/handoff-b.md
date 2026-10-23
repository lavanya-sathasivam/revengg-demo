# Handoff B -> A : what changed on branch `b-graph`

Person B rebased `b-graph` onto `origin/a-backend`. A's `db.py`, `extract.py`,
`app.py` and the three A test files are **untouched**. Everything below is
what branch B adds or needs from A.

---

## 0. NEW (2026-10-20): what changed on `b-graph` since the last handoff

Read this part first if you have not pulled `b-graph` today.

## 0. NEW (2026-10-23): Phase 7 contract-analysis hardening

Read this first. It changes analysis behaviour and adds provenance fields, but
it does not change the database schema or the frontend.

### 0.0 What Phase 7 fixed

| area | change |
|---|---|
| units | Durations are unit-aware. Only a single calendar-day `within N days` period is usable. Months, years, business/working days, competing periods, percentages, and money fail closed; no conversion or guessing. |
| SLAs | Values are validated inclusively at `0` and `100`. Zero is valid. |
| interest | The false "Interest accrual window" counterfactual row is **removed**. A note says interest is not derived from the payment period. This is the one intentional output change. |
| evidence | Evidence and the changed clause keep their full `text`/`text_length`. Short display fields are `excerpt`/`text_excerpt` and say explicitly when they omit characters. |
| graph binding | Analysis uses `contracts.current_graph_build_id`. Saving clauses or creating a current version atomically invalidates the pointer. Stale/missing graphs return HTTP `409`, not old graph rows. |
| reproducibility | Reports carry `analysis_binding` (contract, clause version/hash, contract version, build id/version/hash, graph state, rules fingerprint). `POST /impact` accepts an optional 64-hex `expected_clause_sha256`. |
| statuses | `supported`, `partially_supported`, `unsupported` are explicit on the report, evidence, doubts, conflicts, and counterfactual. Read-only legacy graphs are `legacy_unbound` / `partially_supported`. |
| traces | Every counterfactual row has `rule_id`, `assumptions`, and a `calculation` object. |

Full report: `docs/phase7_contract_analysis_hardening.md`.

### 0.0.1 API additions C must handle

`POST /impact` request may now include:

```json
{
  "contract_id": 1,
  "clause_id": "7.2",
  "old_value": 30,
  "new_value": 15,
  "expected_clause_sha256": "64 lowercase/uppercase hex characters"
}
```

The field is optional. When supplied, the server checks it against the stored
clause hash.

Errors keep the old string `detail` and add a stable `error_code`:

```json
{ "detail": "safe message", "error_code": "graph_stale" }
```

| HTTP | `error_code` | Meaning |
|---:|---|---|
| 409 | `graph_stale` | A graph exists but is not bound to the current clause set; rebuild it. |
| 409 | `graph_not_built` | No published graph exists; build it. |
| 409 | `graph_build_failed` | Graph construction failed for the contract. |
| 409 | `clause_hash_mismatch` | The clause changed after the request was prepared. |
| 409 | `clause_hash_unavailable` | The clause has no stored hash; expected-hash verification is impossible. |
| 400 | `unsupported_unit` | The stored value uses a unit the API does not model; no conversion. |
| 400 | `unsupported_clause_value` | No stored value in the modelled unit. |
| 400 | `ambiguous_clause_value` | Competing or non-calendar periods cannot be selected safely. |
| 400 | `sla_out_of_range` | SLA value outside `[0, 100]`. |
| 400 | `stale_old_value` | `old_value` does not match the stored clause. |

`GET /graph/{id}` and `GET /clauses/{id}` now include `binding` and
`analysis_ready`; `/clauses` also includes each clause's `version_id` and
`clause_sha256`. The UI can use these to explain why analysis is unavailable.

The report also carries `analysis_status`, `analysis_binding`,
`rules_fingerprint`, and (inside `counterfactual`) the rule trace fields. The
old 12 MVP keys are still present. `counterfactual.notes` remains the honest
explanation when a calculation is not modelled.

### 0.0.2 Verification after pulling Phase 7

```powershell
py -3.11 -m unittest discover -s tests -p "test_phase7_hardening.py" -v
py -3.11 -m unittest discover -s tests -v
```

The focused Phase 7 suite has 26 tests; the full suite has 262 tests (Phase 6
baseline was 236). Everything uses temporary databases. Do not run the demo
against `app.db` from branch B.

---

### 0.1 The evaluation exists now

`tools/evaluate.py` scores the engine against `data/ground_truth.json`.
This is the "Results" chapter of the report.

```powershell
py -3.11 tools/evaluate.py                                   # print the tables
py -3.11 tools/evaluate.py --out docs/evaluation_report.md   # regenerate the report
```

`docs/evaluation_report.md` is the generated report (committed).
`data/ground_truth.json` was extended: it now holds a hand-written label for
**every** clause in both contracts (type), the extracted numbers, the full
set of cross-references, and the impact sets for 9 change scenarios — not
just the handful the old tests asserted.

The report states its own limits: these numbers are self-consistency against
hand annotations on two synthetic contracts, not accuracy on real contracts.
Please do not quote them as accuracy.

### 0.2 Four real defects were found and fixed

The evaluation harness caught things the demo did not:

| defect | why it mattered |
|---|---|
| `notice_vs_cure` never fired | the doubt rule read the heading-only anchor "14. NOTICES" instead of Clause 14.1, found no day count and skipped the doubt **silently** |
| 15 clause-type errors | keyword matching was substring-based, so `term` matched `termination` |
| a bogus obligation node | Clause 8.3 ("... are not a penalty") created a "pay late charges" obligation — a false statement about the contract |
| a bogus deadline node | Clause 7.2 got a "notify the Supplier of a dispute within 30 days" deadline, because `"disput" in text` matches "**undisputed** Invoice" |

If you rely on a clause type or a node, re-run the evaluation after pulling.

### 0.3 Behaviour changes C must know about

The **API response shape is unchanged** — same keys, same nesting. But some
values changed:

| what | before | now |
|---|---|---|
| clause types | substring keyword matching | word-boundary matching + heading cues, so `7. PAYMENT` is a payment clause and `Survival` is not a termination clause |
| doubt codes | 7 | **9** (`definition_drift` and `sla_schedule_mismatch` added) |
| counterfactual for a non-payment, non-SLA change | misleading "Payment deadline ... 30 days -> 98 days" rows | **no rows**, plus a note saying the arithmetic is not modelled — the UI must handle empty `counterfactual.rows` and show `counterfactual.notes` |
| element node details | one template per clause type | per-clause details (Clause 7.1 is an invoicing duty, Clause 7.3 is a duty to pay the undisputed balance) |
| SLA impact | changing 9.1 reported **no** impact | Clause 9.2 (Service Credits) is now correctly impacted, via a new inverse rule |
| new demo | — | `py -3.11 cli.py --contract 1 --change 9.1:99:98` — the service-credit counterfactual |

**Please show `counterfactual.notes` in the screen.** When a change type has
no modelled arithmetic, the note is the honest answer, and it is the
difference between "the system says it does not know" and "the system is
broken".

### 0.4 The LLM path can now be exercised without a key

`tools/llm_probe.py` shows exactly what the model is asked and what it
answered:

```powershell
py -3.11 tools/llm_probe.py --contract 1 --clause 3.3
py -3.11 tools/llm_probe.py --contract 1 --clause 3.3 --rebuild
```

With `LLM_PROVIDER=mock` it says the LLM is not configured and exits cleanly
— which is the point: **the core demo needs no key**. The LLM branch of the
graph builder is now covered by tests (`tests/test_evaluate.py`), so it
cannot rot unnoticed. When you add a real key to `.env`, run the probe with
`--rebuild` to see which edges the model contributes.

### 0.5 README repair

`README.md` was double-encoded (UTF-8 bytes read as cp1252), so it rendered
as `—` and `→` everywhere. I repaired the encoding and updated the stale
facts (test count, doubt families, the counterfactual's scope). The diff
looks large; it is one mechanical repair plus the content lines. If you have
edited the README, take ours and re-apply your lines.

### 0.6 The patent note exists — and the disclosure rules now apply to all of us

`docs/invention_note.md` is the invention note for the patent agent. It
describes the one narrow mechanism worth claiming, the novelty argument
against Kira / Luminance / Harvey / CoCounsel, the §3(k) analysis and a claim
sketch.

**Until the provisional is filed: no public repo, no demo video, no talk,
paper or blog post describing the mechanism, and no showing the demo to
anyone outside the three of us.** The note has a disclosure log — if anyone
discloses anything, it gets a row. A disclosure before filing can destroy
novelty, and the whole project is worthless if that happens.

One question in the note needs an honest answer from all three of us:
**inventorship**. Under Indian practice the inventors are the natural persons
who contributed to the inventive step. Please settle a written account of who
designed the relation table, who designed the traversal, who designed the
doubt rules and who built the evaluation **before** filing.

---

## 1. What branch B ADDS (new files, A does not need to touch them)

| file | what it is |
|---|---|
| `clauses.py` | sub-clause analysis on top of A's `extract.split_clauses()` |
| `adapter.py` | the seam: the ONLY file that talks to A's `db.py` |
| `graph.py` | nodes, typed edges, relation table, BFS impact traversal |
| `impact.py` | counterfactual, doubts, conflict detection, cross-contract compare |
| `api_b.py` | **the HTTP API** — router with 5 endpoints, mounted by `server.py` |
| `server.py` | runs A's `app.py` + B's `api_b.py` in one process (demo server) |
| `llm.py` | one LLM helper that never raises |
| `cli.py` | terminal demo of the impact engine |
| `seed.py` | loads the two synthetic contracts into `demo.db` |
| `tests/test_graph.py` | 41 tests for the relationship model and the API |
| `tests/test_evaluate.py` | 53 tests for the evaluator, the rules it locked in, and the LLM path |
| `tests/test_phase7_hardening.py` | 26 Phase 7 correctness, provenance, and evidence tests |
| `tools/evaluate.py` | scores the engine against `data/ground_truth.json` |
| `tools/llm_probe.py` | exercises the LLM path (works with no key) |
| `docs/evaluation_report.md` | the generated evaluation report |
| `docs/invention_note.md` | the patent invention note (CONFIDENTIAL, pre-filing) |
| `data/contracts/contract_a.txt`, `contract_b.txt` | synthetic Indian commercial contracts |
| `data/ground_truth.json` | hand-annotated truth set (seed AND test set) |

---

## 1a. The API — what C should build the screen against

Person C: **you do not need to wait for anyone.** Branch B now ships a working
HTTP API. Start the demo server:

```powershell
py -3.11 seed.py
py -3.11 -m uvicorn server:app --reload
open http://127.0.0.1:8000/docs     # interactive API docs, try it live
```

| endpoint | what it returns |
|---|---|
| `GET /graph/{contract_id}` | every node and every typed edge (draw your graph here) |
| `GET /clauses/{contract_id}` | clauses + type + references + numbers |
| `POST /impact` | **the headline endpoint** — all 12 MVP spec items, plus binding and status fields |
| `GET /compare/{a}/{b}` | cross-contract comparison + portfolio anomaly |
| `GET /demo/{contract_id}/suggest` | a ready-made example change (one click, no typo) |

`POST /impact` request body (the hash is optional):

```json
{
  "contract_id": 1,
  "clause_id": "7.2",
  "old_value": 30,
  "new_value": 15,
  "expected_clause_sha256": "64 hex characters (optional)"
}
```

Response keys — the 12 MVP items are unchanged; Phase 7 adds provenance and
status fields:

```
changed_clause, direct, indirect, chain,
obligations, conditions, consequences, deadlines, rights, definitions,
counterfactual, doubts, conflicts, evidence, review_points,
ranked_review_points, summary,
analysis_status, analysis_binding, rules_fingerprint
```

`counterfactual` additionally contains `status`, `rule_ids`, `assumptions`,
`rules_fingerprint`, and `analysis_binding`. Each row contains `rule_id`,
`assumptions`, and a `calculation` object.

Each item in `obligations` / `conditions` / `consequences` / `deadlines` is:

```json
{ "clause": "8.1", "kind": "obligation", "detail": "pay late charges on any overdue Invoice" }
```

Each doubt is:

```json
{ "code": "definition_collision", "severity": "high",
  "question": "...", "evidence": [ {...} ], "review_point": "...",
  "rule_id": "doubt.definition_collision", "status": "supported" }
```

When `POST /impact` is unavailable because the graph is stale or missing, the
response is HTTP `409` with `{"detail": "...", "error_code": "graph_stale"}` or
`{"detail": "...", "error_code": "graph_not_built"}`. Rebuild the graph before
retrying. See section 0.0.1 for the full code list.

**C should show `counterfactual.notes`, `analysis_status`, and the 409
`error_code`.** That is the difference between "we do not know" and "the system
is broken".

**C should write the screen against these endpoints.** C's `reasoning.py` (the
IRAC-style narrative) is a separate layer on top — it consumes this JSON and
writes the lawyer-facing sentences. Every number it needs is already there.

---

## 1b. How A can mount the routes into app.py (optional)

Branch B ships `server.py`, which imports A's `app.py` and mounts B's router
without editing A's file. If A would rather have the routes inside `app.py`,
the change is two lines:

```python
from api_b import router
app.include_router(router)
```

That is the whole change. Nothing else in `app.py` moves.

---

## 2. The three questions A must answer

### Q1. Do you want to fold the graph columns into `db.init_db()`?

A's `nodes` and `edges` tables were designed for a node-to-node graph. Branch B
needs three more columns. **Right now `adapter.py` adds them with a safe
`ALTER TABLE` migration** (the same pattern A already used for
`contracts.raw_text`), so nothing is blocked.

If A prefers, please add these three columns directly to `db.init_db()`:

```sql
nodes.contract_id INTEGER     -- so "7.2" in Contract A and "7.2" in Contract B differ
nodes.detail      TEXT        -- the human-readable text of the node
edges.contract_id INTEGER     -- so edges can be scoped to one contract
```

After that, delete the `_GRAPH_MIGRATIONS` list in `adapter.py` (or leave it -
`ensure_graph_schema` checks `PRAGMA table_info` first and does nothing when
the column already exists).

### Q2. Should `/upload` use the fine-grained splitter?

A's `extract.split_clauses()` detects top-level numbered sections only
(`1.`, `2.`, `3.` ...). Branch B's `clauses.split_clauses()` calls A's function
and then subdivides each section into its sub-clauses (`1.1`, `1.2`, `7.2`).

Right now:
- the **upload route** (`app.py`) uses A's splitter -> top-level sections only
- the **demo** (`seed.py`) uses B's splitter -> full hierarchy

A one-line change in `app.py` would make the upload route use the hierarchy too:

```python
-from extract import extract_text, split_clauses
+from extract import extract_text
+from clauses import split_clauses
```

**B's recommendation: yes, do it.** But it changes what `/upload` returns
(clause count goes up), so it is A's call.

### Q3. Should the demo live in `demo.db` or `app.db`?

**B's recommendation: keep it in `demo.db`.** A's `test_split_clauses.py`
reads contract 1 from `app.db` and asserts it is a real 24-section document
that A uploaded. When branch B seeded its synthetic contracts into `app.db`,
that test failed (`17 != 24`). `demo.db` is a separate, git-ignored file, so
the two never collide.

---

## 3. How branch B consumes A's API (the seam)

Branch B never imports `db` for anything except `get_conn()` and
`get_db_path()`. All data access goes through `adapter.py`:

```python
adapter.init_db()
adapter.list_contracts()
adapter.get_contract(cid)
adapter.create_contract(filename, raw_text)   # A has no INSERT helper, so the seam adds one
adapter.save_clauses(cid, clauses)            # A's db.save_clauses
adapter.list_clauses(cid)                     # A's db.list_clauses, re-sorted numerically
adapter.clause_metadata(row)                  # parsed metadata dict
adapter.clear_graph(cid)
adapter.insert_clause_node(cid, sec_no, type, detail)
adapter.insert_element_node(cid, ref, kind, detail)
adapter.insert_edge(cid, source_node, target_node, rel)
adapter.get_nodes(cid)
adapter.get_edges(cid)
```

**If A changes the schema, only `adapter.py` changes.** That is the whole
point of the seam, and it is what kept the rebase to zero conflicts.

### One behaviour worth knowing

A's `db.list_clauses()` orders by `CAST(section_number AS INTEGER)`, which puts
`2.10` before `2.2` and `SCHEDULE A` before section 1. `adapter.list_clauses()`
re-sorts numerically so the graph is built in reading order. A's own ordering is
left untouched - `adapter` does not modify A's function.

---

## 4. Edge vocabulary and the direction rule (DO NOT change)

| rel | direction | meaning |
|---|---|---|
| `creates` | clause node -> element node | 7.2 creates a payment duty |
| `depends_on` | **dependent -> dependency** | 10.1 depends_on 8.1 |
| `subject_to` | element node -> element node | payment subject_to 30-day deadline |
| `causes` | cause -> effect | reserved |
| `references` | referrer -> referenced | 7.2 references 2.4 |

**`depends_on` is stored dependent-first.** Impact of X therefore walks
`depends_on` backwards. This is the single easiest thing to get wrong - if
you change it, branch B's chain breaks.

Node kinds: `obligation, condition, right, deadline, payment, termination, definition`

Clause types: `definition, payment, fee, interest, default, cure, termination,
notice, sla, confidentiality, indemnity, limitation, force_majeure,
governing_law, dispute, warranty, term, other`

---

## 5. What branch B needs from A next

1. Answer Q1 (fold the three columns into `db.init_db()`, or leave the
   migration in `adapter.py`).
2. Answer Q2 (should `/upload` use the fine-grained splitter?).
3. Tell C that `clauses.py` exists, so C's reasoning layer can pull clause
   type, references and numbers from the same place branch B uses.

---

## 6. How to verify the integration

```powershell
py -3.11 seed.py                                  # builds demo.db
py -3.11 cli.py --contract 1 --change 7.2:30:15   # the headline demo (terminal)
py -3.11 -m uvicorn server:app --reload           # the headline demo (browser)
py -3.11 -m unittest discover -s tests -v          # 262 tests, must all pass
```

The last test in the suite runs A's own three test files in a clean subprocess,
so branch B cannot silently break branch A. The Phase 7 focused suite can be run
alone with `py -3.11 -m unittest discover -s tests -p "test_phase7_hardening.py" -v`
(26 tests).

### One gotcha we already hit (so nobody hits it again)

A's `test_split_clauses.py` skips its real-document tests **only when `app.db`
is absent**. An EMPTY `app.db` (0 bytes) makes those tests run and fail with
`no such table: contracts`. So:

- branch B never writes to `app.db` — it uses `demo.db`
- `cli.py` always points at `demo.db`, never `app.db`
- if you ever see `no such table: contracts` in A's tests, check for a stray
  empty `app.db` and delete it

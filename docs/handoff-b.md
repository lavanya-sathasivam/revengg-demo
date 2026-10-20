# Handoff B -> A : what changed on branch `b-graph`

Person B rebased `b-graph` onto `origin/a-backend`. A's `db.py`, `extract.py`,
`app.py` and the three A test files are **untouched**. Everything below is
what branch B adds or needs from A.

---

## 1. What branch B ADDS (new files, A does not need to touch them)

| file | what it is |
|---|---|
| `clauses.py` | sub-clause analysis on top of A's `extract.split_clauses()` |
| `adapter.py` | the seam: the ONLY file that talks to A's `db.py` |
| `graph.py` | nodes, typed edges, relation table, BFS impact traversal |
| `impact.py` | counterfactual, doubts, conflict detection, cross-contract compare |
| `llm.py` | one LLM helper that never raises |
| `cli.py` | terminal demo of the impact engine |
| `seed.py` | loads the two synthetic contracts into `demo.db` |
| `tests/test_graph.py` | 29 tests for the relationship model |
| `data/contracts/contract_a.txt`, `contract_b.txt` | synthetic Indian commercial contracts |
| `data/ground_truth.json` | hand-annotated truth set (seed AND test set) |

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
py -3.11 cli.py --contract 1 --change 7.2:30:15   # the headline demo
py -3.11 -m unittest discover -s tests -v          # 29 tests, must all pass
```

The last test in the suite runs A's own three test files in a clean subprocess,
so branch B cannot silently break branch A.

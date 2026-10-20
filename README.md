# revengg-demo — Contract Intelligence & Legal Reasoning Engine

Reverse-engineering academic project (3rd year). An AI decision-support tool
for lawyers that understands a contract as a **structure of linked obligations**,
not as a bag of text chunks.

**Branch `b-graph` — Person B (clauses → relationship model → impact analysis),
rebased onto `origin/a-backend`.**

> Status: the vertical slice for the headline demo
> *"change payment period from 30 days to 15 days"* works end to end.
> 29/29 tests pass, including Person A's own three test files.
> There is no web UI yet (that is branch `c-ui`); everything runs from the
> terminal.

---

## 1. What it does (no jargon)

Give it a contract and a proposed change. It tells you:

1. **which clause you changed**
2. **which other clauses that touches** (directly, and through a chain)
3. **what actually changes in days** (default now bites on day 30, not day 45)
4. **what it is NOT sure about** (Definition 2.4 still says 30 days)
5. **the proof** — every line points back to a clause id

```
py -3.11 cli.py --contract 1 --change 7.2:30:15
```

```
DIRECT IMPACT
  - Clause 8.1     (late charges)
  - Clause 8.2     (interest)
  - Clause 10.2    (cure period)

INDIRECT IMPACT
  - Clause 10.1  (hop 2)   (default)
  - Clause 12.3  (hop 3)   (termination for non-payment)

CAUSAL CHAIN
  7.2  ->  8.1  ->  10.1  ->  12.3

OLD vs NEW
  Payment deadline (Clause 7.2)               30 days   ->  15 days   [CHANGED]
  Default can be declared on (payment + cure)  day 45    ->  day 30    [CHANGED]
  Cure period (Clause 10.2)                   15 days   ->  15 days   [unchanged]

UNCERTAINTY
  [definition_collision / high] Clause 7.2 changes the payment period to 15 days,
  but Definition 2.4 separately defines the term and states 30 days. ...
```

---

## 2. Run it (no Docker, no server, one file database)

```powershell
py -3.11 -m pip install -r requirements.txt
copy .env.example .env          # optional: add an LLM key
py -3.11 seed.py                # creates demo.db, loads 2 synthetic contracts, builds the graph
py -3.11 cli.py --list
py -3.11 cli.py --contract 1 --change 7.2:30:15
py -3.11 cli.py --contract 1 --graph
py -3.11 cli.py --compare 1 2
py -3.11 -m unittest discover -s tests -v
```

**Two database files, on purpose:**
- `app.db` — Person A's upload flow. A's `test_split_clauses.py` reads
  contract 1 from here and asserts it is a real 24-section contract.
- `demo.db` — branch B's synthetic demo contracts.

Both are git-ignored. Both are rebuilt locally. They never collide.

---

## 3. The model (this is the part that is not a chatbot)

```
CLAUSE 7.2  (payment)
   |  creates
   v
NODE  7.2::payment      "pay the amount stated in each undisputed Invoice"
   |  subject_to
   v
NODE  7.2::deadline     "pay within 30 days of the Invoice date"

CLAUSE 8.1 (fee)  --depends_on-->  CLAUSE 7.2
CLAUSE 10.1 (default) --depends_on--> CLAUSE 8.1
CLAUSE 12.3 (termination) --depends_on--> CLAUSE 10.1
CLAUSE 7.2 --references--> CLAUSE 2.4 (definition)   <-- becomes a DOUBT, not an impact
```

Five edge types, one direction rule:

| rel | direction | used for |
|---|---|---|
| `creates` | clause → node | what a clause brings into existence |
| `depends_on` | **dependent → dependency** | the real dependency chain |
| `subject_to` | node → node | a duty conditional on a deadline |
| `causes` | cause → effect | reserved |
| `references` | referrer → referenced | a cross-reference: a *interpretation target* |

**Direction rule (do not break this):** `depends_on` is stored
dependent-first, so *impact of X = walk `depends_on` backwards*.

**Why not "similar text"?** Because `10.1` (default) and `8.1` (late charges)
have almost no words in common. A clause-similarity search will never connect
them. A typed relation over clause types does, every time, and a lawyer can
audit which rule fired.

The relation table is in `graph.py::DEPENDENCY_RULES`. The LLM is only used to
fill gaps the table does not cover — so the core demo runs with **no API key**
and is fully reproducible.

---

## 4. Uncertainty-aware reasoning (Deep feature 5)

The system never guesses. Seven rule families produce explicit doubts, each
with a clause id as evidence:

| code | raises the question |
|---|---|
| `definition_collision` | does this amendment also move Definition 2.4? |
| `cross_ref_mismatch` | 7.2 says 15 days, 2.4 still says 30 — self-contradictory? |
| `unchanged_dependent_param` | 8.1 still says 1.5% per month — intended? |
| `notice_vs_cure` | can 7 days' notice be served before default on day 30? |
| `undefined_term` | what is "Payment Due Date"? nothing defines it |
| `amendment_form` | Clause 17.1 requires a signed written amendment |
| `scope_of_change` | 5.1, 5.2 and 6.1 were not recalculated |

---

## 5. Files and who owns them

| file | owner | purpose |
|---|---|---|
| `db.py` | **A** | database (untouched by B) |
| `extract.py` | **A** | PDF text + top-level section splitting (untouched by B) |
| `app.py` | **A** | FastAPI routes (untouched by B) |
| `test_api.py`, `test_db_clauses.py`, `test_split_clauses.py` | **A** | A's tests (untouched by B) |
| `adapter.py` | **B** | THE SEAM: the only file that talks to `db.py` |
| `clauses.py` | **B** | sub-clause analysis on top of A's `extract.py` |
| `graph.py` | **B** | nodes, typed edges, relation table, BFS impact |
| `impact.py` | **B** | counterfactual, doubts, conflicts, cross-contract compare |
| `llm.py` | **B** | one LLM helper; never raises |
| `cli.py` | **B** | terminal demo (C builds the web UI) |
| `retrieval.py`, `reasoning.py`, `templates/` | **C** | legal authority + explanation + screen |
| `data/contracts/*.txt` | all 3 | two synthetic Indian commercial contracts |
| `data/ground_truth.json` | **B** | hand-annotated truth set (seed AND test set) |

**Never edit another person's file.** If you need a schema change, ask A; if
you need a new field from B, ask B. `adapter.py` is why a schema change does
not break branch B.

### How B builds on A's splitter

A's `extract.split_clauses()` answers *"where does each top-level section
end?"*. B's `clauses.split_clauses()` calls it and then subdivides each section
into `1.1`, `1.2`, `7.2` … So if A improves the PDF handling or the
section-detection rules, branch B inherits it for free.

---

## 6. Data

Both contracts are **synthetic** and written for this project. No real company
contract was used (copyright + privacy).

* **Contract A** — Supplier Service Agreement (59 clauses). Payment 30 days,
  late charges 1.5%/month, interest 12% p.a., cure 15 days, SLA 99%, notice
  7 days, India law + arbitration. Deliberately contains a *definition
  collision*: Definition 2.4 fixes "Payment Due Date" at 30 days separately
  from Clause 7.2.
* **Contract B** — Customer Services Agreement (22 clauses). Payment 45 days,
  SLA 99.5%, and a **stale definition** (1.2 says 30 days, 5.2 says 45) so
  the conflict detector has something real to find.

---

## 7. Honest limitations

* The counterfactual arithmetic is implemented for **payment-period changes**.
  For an SLA % change the "old vs new" table is not yet modelled.
* The doubt engine has 7 rule families. It is a rule engine, not a
  general defeasible-reasoning system.
* Contract B has no cure clause, so no default-trigger date can be computed
  for it — the system says so instead of inventing one.
* The synthetic contracts are deliberately well-drafted. Real contracts are
  messier; the extractor would need work on scanned PDFs and broken numbering.
* The `/upload` route still uses A's top-level splitter, so an uploaded PDF
  currently yields section-level clauses, not the full sub-clause hierarchy.
  See `docs/handoff-b.md` Q2.
* There is no retrieval of case law yet (that is C's branch).

---

## 8. Git

```powershell
git checkout b-graph
git pull origin b-graph
# work only on your own files
git add <your files>
git commit -m "day6: counterfactual + doubt engine"
git push origin b-graph
```

The repo must stay **private** until the provisional patent application is
filed. Do not push to a public repo, do not post a demo video, do not upload
the report to a public link.

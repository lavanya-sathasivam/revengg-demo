================================================================================
COPY EVERYTHING BELOW THIS LINE INTO THE NEW CHAT
================================================================================

# PROJECT HANDOFF — Contract Intelligence & Legal Reasoning Engine

You are continuing work on an existing private repo. Read this fully before
doing anything. Do not redefine the project — it is already scoped, designed
and half built.

## 0. WHO I AM AND WHAT I NEED

I am one of three third-year students on this team. **I am Person B** (branch
`b-graph`). Person A owns the backend (`db.py`, `extract.py`, `app.py`). Person
C owns the legal retrieval, the reasoning narrative and the screen. All three
of us are using opencode to build, learning the stack as we go.

My job right now: finish the impact-analysis branch, then produce evaluation
metrics and the patent invention note.

## 1. WHAT THIS PROJECT IS

An AI decision-support tool for lawyers that understands a contract as a
**structure of linked obligations** rather than a bag of text chunks.

The core question it answers:

> "If Clause 7.2 changes the payment period from 30 days to 15 days, what
> else is affected, what does that mean in days, which legal authority
> supports the analysis, what could contradict it, and where does the lawyer
> still need to decide?"

The LLM does language work only. Everything structural — linking, traversal,
arithmetic, doubts, verification — is deterministic Python. That is the whole
point and it must not be diluted.

The demo is deliberately a **reverse-engineering project**: we tore down what
Kira / Luminance / Harvey / CoCounsel do, rebuilt a thin version, and added one
real differentiator — a typed dependency graph with evidence-linked,
uncertainty-aware reasoning.

**Jurisdiction: India. Contract domain: Indian commercial service agreements.**

## 2. REPO FACTS (verified, not guessed)

Repo: `https://github.com/lavanya-sathasivam/revengg-demo.git` (PRIVATE)

| branch | commit | owner | state |
|---|---|---|---|
| `main` | `853c6a0` | integration | default branch. A's backend + B's engine merged. 39/39 tests green |
| `b-graph` | `853c6a0` | **me (B)** | identical to `main` right now |
| `a-backend` | `72f5835` | A | **3 commits behind `main`** — A needs to rebase |
| `c-ui` | `72f5835` | C | **still at A's old commit** — C needs to merge `main` in |

There is no `main` on the remote other than the one above. The repo was created
by me; A and C are collaborators.

**The credentials are already cached on this machine** — `git clone` and
`git ls-remote` work without a prompt. You can fetch and inspect the real
repo. Do that before assuming anything.

## 3. TECH STACK (locked, do not change)

```
Python 3.11 (use `py -3.11`, NOT `python` - 3.13 is also installed)
FastAPI + Uvicorn + Jinja2 + python-dotenv
SQLite (plain sqlite3, no ORM) - single file database
PyMuPDF (PDF text extraction - Person A's code)
Git
One LLM API (Gemini or OpenAI) - optional, core demo needs NO key
```

**HARD CONSTRAINTS:**
- **NO Docker**, no containers, no Kubernetes. The user cannot use Docker.
- **NO Neo4j, Elasticsearch, vector DB, LangChain, fine-tuning, multi-agent.**
  With 2 synthetic contracts and ~15 legal authorities, keyword matching is
  correct and testable. Adding a vector DB is a 2-day setup cost for zero demo
  benefit.
- **NO React/npm.** One HTML file served by FastAPI.
- The demo must run on a plain student laptop with `py -3.11 seed.py` and one
  command. It must never require a container.

## 4. FILE OWNERSHIP — THE MOST IMPORTANT RULE

**Never edit another person's file.** This is why the rebase had zero code
conflicts.

| file | owner |
|---|---|
| `db.py`, `extract.py`, `app.py`, `test_api.py`, `test_db_clauses.py`, `test_split_clauses.py` | **A** |
| `adapter.py`, `clauses.py`, `graph.py`, `impact.py`, `api_b.py`, `server.py`, `llm.py`, `cli.py`, `seed.py`, `tests/test_graph.py` | **B (me)** |
| `retrieval.py`, `reasoning.py`, `templates/`, `data/authorities.json` | **C** |
| `README.md`, `requirements.txt`, `.gitignore`, `docs/` | shared — say so in the daily call |

### `adapter.py` IS THE SEAM — understand why it exists

Person A owns `db.py` and is free to change it. Branch B therefore **never
imports `db` except for `get_conn()` and `get_db_path()`**. All data access
goes through `adapter.py`. When A changes the schema, only `adapter.py`
changes. This is why the rebase onto `a-backend` took minutes instead of days.

### `clauses.py` BUILDS ON A's `extract.py` — do not reimplement it

- A's `extract.split_clauses()` finds top-level sections (`1.`, `2.`, `3.`) and
  is PDF-aware. It fails safe to `[]`.
- B's `clauses.split_clauses()` **calls A's function** and then subdivides each
  section into sub-clauses (`1.1`, `7.2`). If A improves the splitter, B
  inherits it free.

## 5. THE DATA MODEL — DO NOT BREAK THE DIRECTION RULE

### Edge vocabulary (frozen)

| rel | direction | meaning |
|---|---|---|
| `creates` | clause node → element node | 7.2 creates a payment duty |
| `depends_on` | **dependent → dependency** | 10.1 depends_on 8.1 |
| `subject_to` | element node → element node | payment subject_to 30-day deadline |
| `causes` | cause → effect | reserved, not emitted by default |
| `references` | referrer → referenced | 7.2 references 2.4 |

**`depends_on` is stored dependent-first. Impact of X = walk `depends_on`
BACKWARDS.** This is the single easiest thing to get wrong. If you flip it,
the impact chain reverses and the demo breaks. It is documented in
`graph.py` and `docs/handoff-b.md`.

### Node kinds
`obligation, condition, right, deadline, payment, termination, definition`

### Clause types (18)
`definition, payment, fee, interest, default, cure, termination, notice, sla,
confidentiality, indemnity, limitation, force_majeure, governing_law, dispute,
warranty, term, other`

### The typed relation table — `graph.py::DEPENDENCY_RULES`

```python
("default","fee"), ("default","payment"), ("default","interest"),
("default","cure"), ("cure","default"), ("cure","payment"),
("termination","default"), ("termination","cure"), ("termination","payment"),
("fee","payment"), ("interest","payment"), ("sla","payment"),
```

**Why a table and not the LLM?** Because "is a late-fee clause a dependency of
a default clause?" is a *legal* question, not a similarity question. `10.1`
(default) and `8.1` (late charges) share almost no words — similarity search
will never connect them. The typed rule does, every time, reproducibly, and a
lawyer can audit which rule fired. The LLM only fills gaps the table misses.

**This is the strongest answer to "what is different about your system vs
Kira/Harvey?" — have it ready.**

## 6. WHAT IS ALREADY BUILT AND VERIFIED

Everything below is tested and working. Do not rebuild it.

### The relationship model
- 59 clauses in Contract A (60 incl. top-level anchors), 22 in Contract B
- 101 nodes / 69 edges for Contract A
- Dependencies found: `8.1→7.2`, `8.2→7.2`, `10.2→7.2`, `10.1→8.1`,
  `10.1→10.2`, `12.3→10.1`
- Reference found: `7.2→2.4` (a definition → becomes a DOUBT, not damage)

### Impact analysis (BFS, hop-based)
Change 7.2 from 30 days → 15 days:
```
DIRECT:   8.1 (late charges), 8.2 (interest), 10.2 (cure)
INDIRECT: 10.1 (default, hop 2), 12.3 (termination, hop 3)
CHAIN:    7.2 -> 8.1 -> 10.1 -> 12.3
```

### Counterfactual
```
Payment deadline (Clause 7.2)               30 days -> 15 days  [CHANGED]
Default can be declared on (payment + cure)  day 45  -> day 30  [CHANGED]
Interest accrual window                      30 days -> 15 days  [CHANGED]
Cure period (Clause 10.2)                   15 days -> 15 days  [unchanged]
Notice required before default (Clause 14.1)  7 days ->  7 days  [unchanged]
```

### Uncertainty engine — 8 doubt codes, each evidence-linked
`definition_collision`, `cross_ref_mismatch`, `unchanged_dependent_param`,
`notice_vs_cure`, `undefined_term`, `amendment_form`, `scope_of_change`,
`definition_drift`

### Legal-meaning grouping (MVP spec items 3/4/5)
The report groups impacted clauses' nodes into obligations / conditions /
consequences / deadlines — "the obligation to pay late charges" rather than
"clause 8.1 is affected".

### Conflict + cross-contract
- Contract B has a deliberate **definition drift**: Def 1.2 says 30 days,
  Clause 5.2 says 45. The detector finds it.
- Contract A vs B: payment period 30 vs 45, SLA 99 vs 99.5 — both flagged.

### HTTP API (`api_b.py`) — what C builds against
| endpoint | returns |
|---|---|
| `GET /graph/{id}` | every node + every typed edge |
| `GET /clauses/{id}` | clauses + type + references + numbers |
| `POST /impact` | **the headline endpoint** — all 12 MVP spec items |
| `GET /compare/{a}/{b}` | cross-contract comparison + portfolio anomaly |
| `GET /demo/{id}/suggest` | ready-made example change (one click) |

`server.py` mounts A's `app.py` + B's `api_b.py` **without editing either
person's file**. A can optionally add two lines to `app.py` instead.

## 7. HOW TO RUN

```powershell
git clone https://github.com/lavanya-sathasivam/revengg-demo.git
cd revengg-demo
py -3.11 -m pip install -r requirements.txt
py -3.11 seed.py                                   # builds demo.db
py -3.11 cli.py --list
py -3.11 cli.py --contract 1 --change 7.2:30:15    # headline demo (terminal)
py -3.11 -m uvicorn server:app --reload            # headline demo (browser)
open http://127.0.0.1:8000/docs                    # interactive API docs
py -3.11 -m unittest discover -s tests -v           # 39 tests, must all pass
```

### TWO DATABASES — on purpose
- `app.db` — **Person A's** upload flow. A's `test_split_clauses.py` reads
  contract 1 from it and asserts it is a real 24-section uploaded document.
- `demo.db` — **branch B's** synthetic contracts.

Both git-ignored, both rebuilt locally. **Never write to `app.db` from
branch B.** An *empty* `app.db` (0 bytes) is worse than no file: A's test
skips its real-document tests only when the file is ABSENT, so an empty one
makes them fail with `no such table: contracts`. This already happened once.
`cli.py` always points at `demo.db`.

## 8. GIT PROTOCOL — read `docs/branching.md` before pushing

- `main` is the integration branch. **Merge into it daily, never at the end.**
  Merging everything on the last day means Day 14 is conflict hell with no time
  to fix it.
- Always branch from `origin/main`, never from another person's branch.
- Morning loop: `git checkout main; git pull; git checkout <your branch>; git merge main`
- Never `git push --force`. Only `--force-with-lease`, on your own branch.
- `.gitattributes` exists with `* text=auto eol=lf` — **do not delete it.**
  The laptop has `core.autocrlf=true` globally; without that file, different
  laptops produce different bytes for the same file and git manufactures
  phantom conflicts.
- **The repo must stay PRIVATE until the provisional patent is filed.** No
  public GitHub, no demo video, no public report link, no paper upload.

## 9. WHAT REMAINS — the actual work

### Mine (Person B) — in priority order

**1. EVALUATION METRICS (~1 hour). DO THIS NEXT.**
`data/ground_truth.json` already holds the hand-annotated truth: 59 + 22
clauses, expected types, expected dependencies, expected impact chain,
expected doubts, expected cross-contract mismatches. Write a script that
computes against it:
- Extraction precision / recall / F1 (clause segmentation, clause type)
- Impact precision / recall on the direct + indirect lists
- Doubt detection recall
- Cross-contract mismatch precision

This is the "Results" chapter of the report and the difference between "the
demo works" and "the system is measurably correct". It is what an examiner
and a patent agent will ask about. Put it in `tools/evaluate.py` and emit a
markdown table. **Do this before any new features.**

**2. SLA % COUNTERFACTUAL (~40 min).** The counterfactual is implemented for
payment-period changes only. An SLA change (99% → 98%) is not modelled. Add a
branch: service-credit exposure changes. Keep it in the same
`counterfactual()` function with the same row format.

**3. EXERCISE THE LLM PATH (~20 min).** `llm.py` currently runs in `mock`
mode (`LLM_PROVIDER=mock`) and `ask_json` returns `{}`. Add a real key to
`.env` (`GEMINI_API_KEY` or `OPENAI_API_KEY`), set `LLM_PROVIDER`, and prove
the LLM can extract a `depends_on` edge for a clause the rule table misses.
Confirm `ask_json` still never raises and the demo still works with the key
removed. `llm.py` must remain provider-agnostic and must never crash a demo.

**4. INVENTION NOTE for the patent (~30 min).** Write
`docs/invention_note.md` — the ONE narrow mechanism worth claiming:
*typed dependency graph with typed legal relations, reverse-dependency impact
traversal with hop classification, and a rule-driven doubt generator that
emits evidence-linked uncertainties*. Keep it out of any public repo until
the provisional is filed. Include: the problem, the mechanism, what is
novel over Kira/Luminance/Harvey/CoCounsel, and a claim sketch.

### Not mine — but I need to chase these
- **A must rebase `a-backend` onto `main`** (3 commits behind):
  `git checkout a-backend; git fetch origin; git rebase origin/main; git push --force-with-lease origin a-backend`
- **C must merge `main` into `c-ui`** before writing more code:
  `git checkout c-ui; git fetch origin; git merge origin/main`
- **A must answer the 3 questions in `docs/handoff-b.md`**:
  Q1 fold the 3 graph columns into `db.init_db()`? Q2 should `/upload` use
  the fine-grained splitter? Q3 confirm the `demo.db` split.

## 10. THE 15 INNOVATIONS — how they map

DEEP (must actually work) — **1,2,3,4,5 are DONE on branch B**:
1. Explainable Legal Reasoning → partially B (doubt engine + evidence),
   the IRAC narrative is C's
2. Contract Relationship Modeling → **DONE**
3. Clause Dependency Analysis → **DONE**
4. Contract Change Impact Analysis → **DONE**
5. Uncertainty-Aware Reasoning + Provenance → **DONE**

Also working as thin features on B: counterfactual (4), causal chain (7),
alternative interpretation (8 — the 2-readings in the doubt engine),
conflict resolution (9), evolution (10 — the diff), freshness (11 — year
check), evidence-backed recommendations (12), portfolio anomaly (13).

C owns: legal retrieval (knowledge base), the IRAC reasoning template, the
screen, negotiation suggestions (14).

Report says all 15; code proves what it proves; the rest is labelled
"future work". That is normal and examiners accept it — but label honestly.

## 11. HONEST LIMITATIONS (state these, don't hide them)

- Counterfactual is payment-period only until item 2 above is done.
- The doubt engine is 8 rule families — a rule engine, not a general
  defeasible-reasoning system. Say so.
- The synthetic contracts are deliberately well-drafted. Real contracts are
  messier; the extractor would need work on scanned PDFs and broken numbering.
- The `/upload` route still uses A's top-level splitter, so an uploaded PDF
  yields section-level clauses, not the full `7.2` hierarchy (see Q2).
- No retrieval of case law yet — C's branch.
- This is NOT patentable as a system. Under India Patents Act s.3(k) a
  computer-programme-per-se / business-method framing fails. Only the narrow
  technical mechanism in item 4 is plausibly claimable. Any public disclosure
  before filing destroys novelty. **This is why the repo stays private.**

## 12. HOW TO WORK WITH ME

- Explain in plain language first, then the technical detail. I am learning
  Python/FastAPI/SQL/graphs/RAG while building — I need the "why" more than
  the "what".
- Be brutally realistic about time. Do not make the plan sound easier than it
  is.
- Do not introduce infrastructure complexity. If something is unnecessary,
  say so directly.
- When you change a file, tell me which file and why.
- Never commit or push without me asking. Never `git push --force`.
- If you find a bug, fix it and add a test. The suite must stay green:
  `py -3.11 -m unittest discover -s tests` → 39 tests, OK.

## 13. FIRST FIVE TASKS FOR THIS SESSION

1. `git clone` the repo (or use the existing clone at
   `D:\Desktop\revengg-demo`), `git checkout b-graph`, `git pull`.
2. Run `py -3.11 seed.py` and `py -3.11 -m unittest discover -s tests`.
   Confirm 39/39 green before you change anything.
3. Read `graph.py` (the relation table + BFS) and `impact.py` (the doubt
   engine). These are the heart of the branch.
4. **Build `tools/evaluate.py`** — the evaluation metrics against
   `data/ground_truth.json`. This is the highest-value remaining work.
5. Write `docs/invention_note.md` — the patent mechanism.

Do not start item 2 (SLA counterfactual) or item 3 (LLM key) until item 4 is
done and committed.

================================================================================
END OF HANDOFF
================================================================================

# Architecture walkthrough — how to explain this project

**Owner: Person B.** Written for the final review and for anyone learning the
stack with us. Every section is **plain language first, then the technical
detail**, because you will be asked *"how does it work?"* by people who do not
read code, and you will be asked *"why did you do it that way?"* by people who
do.

---

## 1. The whole thing in one paragraph

A contract is not a list of clauses, it is a set of **interlocking
obligations**. We read the contract once, turn it into a **typed graph** where
the links between clauses come from a table a lawyer can audit, and then we
answer questions about it by walking that graph. The rule that matters: *the
language model does almost nothing*. The structural part of the system has no
model in it at all — it is deterministic, reproducible offline, and every
claim it makes points back at a clause id and quotes the clause text.

The second half of the idea, and the part nobody else does: **the system also
tells you what it cannot determine.** Every answer is accompanied by explicit
*doubt records* — a question for a human, with the evidence that raised it.

### The pipeline

```
 PDF / text
    │
    ▼  extract.py (A) + clauses.py (B)      ── segment into 1, 1.1, 7.2 …
    │
    ▼  clauses.classify_type()               ── give each clause ONE of 18 types
    │
    ▼  graph._deterministic_nodes()          ── what does each clause CREATE?
    │                                          (obligation, deadline, condition, right …)
    ▼  graph.build_graph()                   ── typed edges from the relation table
    │
    ├──► impact.impact_of()                 ── reverse traversal: what breaks?
    ├──► impact.counterfactual()            ── what does it mean in days / rupees?
    ├──► impact.detect_doubts()             ── what can we NOT determine?
    │
    ▼  retrieval.py + reasoning.py (C)       ── which Indian authorities touch this?
    │
    ▼  api_b.py  →  templates/index.html     ── the screen
    ▼  cli.py                                  ── the terminal
    ▼  tools/evaluate.py                      ── the measurement
```

---

## 2. Stage by stage

### 2.1 Segmenting the contract into a hierarchy

**Plain language.** A contract is written as numbered sections (`7. PAYMENT`),
and each section contains numbered sub-clauses (`7.2 Payment Period`). Those
are different legal objects: a change to 7.2 is not a change to section 7. So
we keep both levels. We also keep the section *anchor* (the heading alone),
because it carries the section's subject.

**Technical.** Person A's `extract.split_clauses()` finds the top-level
sections and is PDF-aware. We **do not re-implement it** — `clauses.split_clauses()`
(`clauses.py:187`) calls it and then subdivides each section with
`_split_hierarchy()` (`clauses.py:142`), which matches `7.2` with
`SUB_CLAUSE_RE` (`clauses.py:97`) and `SCHEDULE A` with `SCHEDULE_RE`
(`clauses.py:99`).

**Why it matters for the team.** Because we build on A's splitter, when A
improves the PDF handling we inherit it for free. That is also why the two
branches never conflicted.

**What would break it.** A contract with broken numbering (`7.2.1`, `7a`,
clauses with no numbers at all). Our output would then be section-level only —
which is exactly what happens today for **uploaded** PDFs, because A's
`/upload` route still uses the top-level splitter (A's open question Q2 in
`docs/handoff-b.md`).

### 2.2 Typing each clause

**Plain language.** Every clause gets exactly one label out of a fixed list of
18 legal roles: `definition, payment, fee, interest, default, cure,
termination, notice, sla, confidentiality, indemnity, limitation,
force_majeure, governing_law, dispute, warranty, term, other`. The label is
what lets us say "a default clause depends on a fee clause" — a *legal*
statement, not a textual one.

**Technical.** `clauses.classify_type()` (`clauses.py:305`) applies three
rules in order:

1. **A quoted heading is a definition.** `"Payment Due Date" means …` → `definition`.
2. **The heading names the subject → trust it.** A clause headed
   *"Termination for Non-Payment"* is a termination clause even though its
   body says "default". Keywords are matched with **word boundaries**
   (`_keyword_re()`, `clauses.py:272`) so `term` never matches `termination`.
3. **Otherwise fall back to the body**, using a fixed priority order
   (`TYPE_PRIORITY`, `clauses.py:39`) — most specific type first
   (`fee, interest, default, cure, termination, sla, …`).

Two extra rules exist because the evaluation found them necessary: a heading
naming a subject the vocabulary has no type for (a **tax** clause) is `other`
rather than `payment`; and a clause that merely *mentions* the Service
Levels without stating one is not an `sla` clause (`_states_a_level()`,
`clauses.py:291`).

**Why a fixed vocabulary and not a model.** Because it is *auditable*. A
lawyer can open `TYPE_KEYWORDS` and argue with the rule, and the same
contract always types the same way. A model would be neither.

### 2.3 Nodes: what does each clause *create*?

**Plain language.** A clause does not just exist, it **brings things into
existence**: a duty to pay, a deadline, a condition, a right, a termination
event. Those things are the nodes the lawyer actually reasons about — "the
Customer's obligation to pay late charges" rather than "clause 8.1 is
affected".

**Technical.** `graph._deterministic_nodes()` (`graph.py:145`) returns
`(kind, detail)` pairs per clause. Element nodes are named `7.2::payment`
(clause + `::` + kind) so they are distinguishable from clause nodes.

**Node precision is a correctness requirement, not polish.** Every node is
rendered as a sentence in the output, so a wrong node is a *false statement
about the contract*. Three bugs the evaluation caught:

- Clause 8.3 (*"… are not a penalty"*) is in the fee family but imposes no
  charge, so it must create **no** charge obligation.
- Clause 9.2 (Service Credits) creates a **right** to credits, not an
  obligation to pay late charges.
- Clause 7.1 (Invoicing) imposes an invoicing duty, and Clause 7.3 lets the
  Customer *withhold* the disputed part — so the generic "pay the amount
  stated in each undisputed Invoice" template was false for both.

The same bug appeared in the deadline node: Clause 7.2 was given *"notify the
Supplier of a dispute within 30 days"* because the substring test
`"disput" in text` also matches "**undisputed** Invoice". Fixed with
`mentions_dispute()` (`clauses.py:405`).

### 2.4 Edges: the typed relation table — *the core of the project*

**Plain language.** To know what a change breaks, we must know which clause
*depends on* which. That is a **legal** question: a default clause depends on
the late-charges clause because the default is triggered by late payment.
We write those relationships down as a small table of **clause-type pairs**
that a lawyer can read, argue with, and edit.

**Technical.** `DEPENDENCY_RULES` (`graph.py:46`) is 12 pairs:

```python
("default","fee"), ("default","payment"), ("default","interest"),
("default","cure"), ("cure","default"), ("cure","payment"),
("termination","default"), ("termination","cure"), ("termination","payment"),
("fee","payment"), ("interest","payment"), ("sla","payment"),
```

An edge is emitted **only** when two conditions both hold:

1. the referring clause contains an **explicit cross-reference** to the
   referenced clause (`clauses.find_references()`, `clauses.py:357` — a
   regex for `Clause 7.2`), **and**
2. the pair of clause types is in the table.

If the pair is *not* in the table, the cross-reference is stored as a
`references` edge instead. That distinction is load-bearing (see §3.3).

**The direction rule.** `depends_on` is stored **dependent-first**:
`10.1 depends_on 8.1` is stored as the edge `(10.1 → 8.1)`. Therefore the
impact of a clause X is found by walking `depends_on` **backwards** — the
clauses that depend on X are the ones a change to X hurts. This is the single
easiest thing in the system to get backwards, and it is why the traversal code
looks inverted at first glance.

**One pair runs the other way.** Clause 9.1 says *"Failure to achieve the
Service Levels entitles the Customer to the service credits set out in Clause
9.2"* — the reference runs 9.1 → 9.2, but the legal dependency runs the other
way: **the credits depend on the service level**. Those pairs live in
`INVERSE_DEPENDENCY_RULES` (`graph.py:83`) and the edge is emitted reversed.
So the direction is a *decision the table makes*, not an accident of where the
words sit.

**Why not embeddings / similarity.** Because in this corpus the pairs that
matter share almost no words. Clause 10.1 (*"If the Customer fails to pay …
the late payment under Clause 8.1 continues after the expiry of the Cure
Period … the Customer shall be in default"*) and Clause 8.1 (*"… shall pay
late charges at the rate of 1.5% per month"*) have essentially zero lexical
overlap. No retrieval function will connect them. The typed table connects
them every time, and tells you which row fired.

### 2.5 Traversal: reverse BFS with hop labels

**Plain language.** Start at the clause you changed and walk backwards
through the dependency links. What you reach in one step is *directly*
affected; what you reach further out is *indirectly* affected. The number of
steps (the "hop") is kept, because "directly affected" and "affected through
a chain" are different things to a lawyer.

**Technical.** Three small functions:

- `graph.impact_adjacency()` (`graph.py:360`) turns the edge list into
  "who is hurt if I change X" — for `depends_on` it inverts, for `causes` it
  does not, and for `references` it does **nothing** (a reference is an
  interpretation target, not damage).
- `graph.find_impact()` (`graph.py:378`) is a breadth-first search with a
  `max_hops=3` bound, returning `[{sec_no, hops}]`.
- `graph.chain_text()` (`graph.py:402`) walks the same adjacency to produce
  the readable chain `7.2 → 8.1 → 10.1 → 12.3`.

**Why breadth-first and not depth-first.** BFS gives the *shortest* distance,
so a clause that is both directly and indirectly affected is classified as
**direct** — the stronger, more useful claim. DFS would find it later and
call it indirect.

**Why hops are kept rather than thrown away.** Because they gate the doubt
rules (§2.7): `unchanged_dependent_param` fires only for clauses the
traversal actually reached, and `notice_vs_cure` fires only when the
traversal reached a termination clause. The traversal output is not merely
displayed — it *selects which rules run*.

### 2.6 Counterfactual: the arithmetic, recomputed

**Plain language.** If the payment period becomes 15 days, what does that
actually mean? The default can now bite on day 30 instead of day 45 — because
the cure period is still 15 days. And note what did **not** move: the cure
period, and the 7-day notice window. A naive "here is your new deadline"
answer loses exactly that.

**Technical.** `impact.counterfactual()` (`impact.py:371`) dispatches on the
clause type:

- `payment` → `_payment_counterfactual()` (`impact.py:204`): the payment
  deadline, the default trigger (old + cure), the interest window, and the
  **unchanged** cure and notice rows flagged as unchanged.
- `sla` → `_sla_counterfactual()` (`impact.py:272`): the credit threshold as
  drafted (unchanged → flagged), the threshold if conformed, the rate per
  point, the first credit earned (INR 22,500 at 98% availability → at 97%),
  the cap (INR 1,12,500), and the Charges base.
- anything else → **no rows**, plus a note saying the arithmetic is not
  modelled. (This was a bug: it used to print "Payment deadline (Clause
  17.2) 30 days → 2 days".)

**No model is involved.** The numbers come from regex extraction
(`clauses.find_numbers()`, `find_within_days()`, `find_amounts()`) and
arithmetic. The SLA branch parses the service-credit schedule
(`clauses.parse_service_credit_schedule()`, `clauses.py:444`) and **returns
`None` if it cannot read it** — it fails safe rather than guessing a number.

### 2.7 Doubts: what the system refuses to determine

**Plain language.** The most important output. A machine that answers
everything confidently is a machine you cannot check. So the system emits
*doubt records*: a code, a severity, a question for a human, the evidence
(clause ids and quoted text) that raised it, and the review point.

**Technical.** `impact.detect_doubts()` (`impact.py:417`) applies nine rule
families. The two that show the design best:

- **`unchanged_dependent_param`** (medium): for a clause *the traversal
  reached*, look at its numbers; if a figure is neither the old nor the new
  value, the system will not infer whether it should move. This is the
  "cure period is still 15 days — intended?" doubt.
- **`notice_vs_cure`** (medium): if the traversal reached a termination
  clause, and the notice clause says a notice is deemed served N days after
  despatch, then shortening the payment period has quietly shrunk the window
  for serving it.

Each doubt carries `evidence: [{id, type, why, text}]` where `text` is the
actual clause text — so a reviewer can check the claim without reopening the
contract. `impact._evidence()` (`impact.py:78`) builds them.

**A bug worth telling.** `notice_vs_cure` **never fired** until the
evaluation harness found it: the rule read the heading-only section anchor
"14. NOTICES" (which has no day count) instead of Clause 14.1, found no
number, and skipped the doubt *silently*. Fixed by choosing the notice clause
that actually states a period.

### 2.8 Retrieval and reasoning (Person C's half)

**Plain language.** For a clause, find the Indian legal authorities that touch
it, and explain it in a lawyer-friendly way — while refusing to say anything
it cannot support.

**Technical.** `retrieval.search_authorities()` (`retrieval.py:165`) is
**deterministic keyword matching**, not embeddings:

- the query is normalised (lowercased, whitespace collapsed, trailing
  sentence punctuation stripped from tokens that contain no digits);
- stopwords and *generic legal words* ("contract", "agreement", "section")
  are dropped as scoring tokens — a query of only those words returns nothing;
- a score is the number of distinct query tokens backed by evidence: a
  curated keyword of that authority, or a numeric token appearing in the
  authority's citation (`56` matching "Section 56");
- multi-word keywords must appear as a complete phrase.

`reasoning.explain_with_retrieval()` (`reasoning.py:394`) then builds the
explanation: summary, key points, relevant authorities, matched evidence,
uncertainties, a reasoning trace, and a disclaimer.

**Two facts you should know before you present it:**

1. Retrieval was **inert** until the integration: it reached **2 of 59**
   clauses. Two causes — the corpus did not cover the topics the demo
   contracts raise, and the last word of a clause could never match a keyword
   (`"penalty."` is not the token `"penalty"`). Now **29 of 59**.
2. **Clause 16.2 (Arbitration) still retrieves nothing**, because C's own
   test asserts that nothing matches `"arbitration"`. Adding an arbitration
   authority is C's call, not ours.

### 2.9 The API, the CLI and the screen

**Plain language.** The same engine, three ways to reach it: JSON over HTTP,
a terminal printout, and a web page.

**Technical.**

- `api_b.py` is a **separate FastAPI router** with five endpoints
  (`/graph/{id}`, `/clauses/{id}`, `POST /impact`, `/compare/{a}/{b}`,
  `/demo/{id}/suggest`). It is a separate file precisely so that nobody has
  to edit anybody else's file to expose it — the rule that kept the merges
  conflict-free.
- `server.py` points the whole process at `demo.db` **before** importing
  A's `app.py`, then mounts B's router onto it. One process, both routers.
- `cli.py` prints the same report in a readable form.
- `templates/index.html` renders `POST /impact` in the browser; it reads its
  state from the URL (`?contract=1&clause=7.2&old=30&new=15`), so any
  analysis is a shareable link.
- `templates/explain.html` is C's clause explainer, preserved as its own
  page at `/explain`.

**The seam — `adapter.py`.** Branch B never imports `db` except for
`get_conn()` and `get_db_path()`; all data access goes through `adapter.py`.
When A changes the schema, only `adapter.py` changes. That is why the rebase
onto A's branch took minutes instead of days, and why `tests/test_integration.py`
asserts that no other branch-B module imports `db` directly.

### 2.10 The evaluation: how we know it is right

**Plain language.** "The demo works" is not evidence. "The engine reproduces
every hand-annotated item" is. So we annotated the truth by hand and scored
the engine against it.

**Technical.** `tools/evaluate.py` scores ten metric families against
`data/ground_truth.json` (59 + 22 clauses hand-annotated with types, numbers,
cross-references, dependency edges, impact sets for 9 change scenarios, the
counterfactual arithmetic and the required doubt codes):

segmentation · clause types · numbers · dependency edges · reference edges ·
impact + chains · counterfactual · doubts · definition drift · cross-contract
mismatch · element-node number grounding.

Three properties make the numbers worth something:

1. **The annotations were written before running the system** (by reading the
   contract texts), so they are not copies of the system's output.
2. **The scorer is itself tested** — including a **negative control**
   (`test_scorer_reports_a_wrong_answer`): the harness is verified to report
   a wrong answer when the ground truth is wrong, so a perfect score cannot
   come from a broken scorer.
3. **The report states its own limits**: these are *self-consistency* numbers
   on two synthetic contracts written by the same team that wrote the rules.
   They are a regression guard, **not** a claim about real contracts.

### 2.11 Where the LLM fits

**Plain language.** Exactly one job: to fill gaps the relation table leaves.
If a clause states a cross-reference but the type pair is not in the table,
the model is asked directly which clauses this one relies on.

**Technical.** `graph.build_graph()` (`graph.py:223`) asks only when
`rule_dep_made` is False, i.e. only for the gaps. `llm.ask_json()`
(`llm.py:104`) **never raises** — on any failure it returns `{}` and the
caller falls back to the rule-based path. The provider is resolved at call
time (`llm.provider_name()`, `llm.py:39`), so `.env` changes take effect
without a restart and the LLM path can be tested without a key.

**Proven live** on 2026-10-20: asked about Clause 3.3 (Change Control →
"in accordance with Clause 17.1"), whose type pair `(other, other)` is not in
the table, Gemini returned `depends_on: ["17.1"]` plus a condition node — a
real dependency the typed table structurally could not produce, and legally
the right one. `tools/llm_probe.py` reproduces it.

**The important architectural point:** the structural path contains **no
model at all**. That is why the demo needs no key, why the output is
byte-for-byte reproducible, and why it can be regression-tested against a
hand-annotated truth set.

---

## 3. The five decisions a reviewer will probe

### 3.1 "Why not embeddings / similarity?"

*Because the relation is legal, not textual.* Demonstrate with 10.1 and 8.1:
they share almost no words, and no similarity function will ever connect
them — but the *typed* table connects them every time, reproducibly, and
tells you which row fired. A lawyer can open a 12-row table and disagree
with it; they cannot audit a 300-dimensional embedding.

### 3.2 "Which direction does the dependency go?"

*Dependent-first, and impact walks backwards.* Show the arrow: `10.1
depends_on 8.1` is stored as `(10.1 → 8.1)`, so the impact of 8.1 is found
by walking that edge in reverse — because 10.1 is what breaks. Then show the
inverse rule for 9.1/9.2, where the reference runs one way and the dependency
the other, and say that the *table* decides the direction.

### 3.3 "Why is a cross-reference to a definition not an impact?"

*Because a definition is an interpretation target, not a dependency.* If
Clause 7.2 changed, does Definition 2.4 automatically move? The system
cannot know, so it refuses to guess: it emits a **doub** ("does this
amendment also move Definition 2.4?") and leaves the definition out of the
impact set. This is the difference between a system that models legal
reasoning and one that models text adjacency.

### 3.4 "Isn't the doubt engine just a rule engine?"

*Yes — and that is deliberate.* Nine rule families, not a general
defeasible-reasoning system, and the report says so. The point is not
sophistication, it is **accountability**: every doubt names the clause that
raised it, so a lawyer can check it in seconds. A model that "notices
subtlety" cannot do that.

### 3.5 "What is the honest weakness?"

- The rule table is **12 rows**. Real drafting needs more.
- The **direction table** is the fragile part — one pair already needed the
  inverse rule.
- Clauses typed `other` create **no node**, so a duty stated outside the
  typed families (Clause 3.1's "reasonable skill and care") is invisible.
- The counterfactual is modelled for **payment and SLA changes only**.
- Rupee amounts are parsed only where the service-credit counterfactual needs
  them.
- The evaluation is **self-consistency on synthetic contracts**.

---

## 4. Vocabulary cheat sheet

Say these correctly and it will sound like you built it.

| term | what it means here |
|---|---|
| **graph** | clauses and the legal things they create, plus typed links between them |
| **node** | one vertex: a clause (`7.2`) or a thing a clause creates (`7.2::payment`) |
| **edge** | a typed link: `depends_on`, `references`, `creates`, `subject_to`, `causes` |
| **traversal** | walking the edges to find what is connected to what |
| **BFS** | breadth-first search: explores in rings, so the first time it reaches a clause is by the *shortest* path |
| **hop** | the number of edges on that shortest path — our direct/indirect measure |
| **backwards traversal** | walking `depends_on` against its stored direction, because the impact of X is who *depends on* X |
| **deterministic** | same input → same output, every time, on any machine, with no model |
| **embedding** | a vector representation of text, used for similarity search — deliberately **not** used for the relations here |
| **precision** | of the things the system claims, how many are right |
| **recall** | of the things that matter, how many the system found |
| **F1** | the harmonic mean of the two — the single number to quote |
| **micro-average** | pool all the counts and compute once |
| **macro-average** | average the per-class scores, so rare classes count equally |
| **negative control** | a test that proves the measuring instrument can actually fail |
| **ground truth** | a human-made set of correct answers, used to score the system |
| **seam** | a deliberate boundary so two people's code can change independently |
| **fail safe** | when a rule cannot be applied, say so rather than guess |

---

## 5. The honest limits, in one paragraph

Use this if someone asks how good it really is. The engine reproduces every
hand-annotated item on two synthetic contracts, which is a regression guard
and a demonstration of coverage — **not** a claim about real contracts, where
the numbers would fall. The relation table is 12 rows and the doubt engine is
9 rules; both are rule engines, not learned models. The direction convention
is the part most likely to break on a real document, and it is exactly where
the one inverse rule had to be added. Clauses typed `other` produce no node.
The counterfactual covers payment and SLA changes only. The retrieval corpus
has 13 authorities and one clause (arbitration) deliberately retrieves
nothing. And the structural path has no model in it at all — which is a
feature for reproducibility and a limitation for open-domain coverage.

---

## 6. Showing each piece

| piece | command | what to say |
|---|---|---|
| the pipeline | `py -3.11 cli.py --contract 1 --graph` | "this is the structure the answer comes from" |
| the headline | `py -3.11 cli.py --contract 1 --change 7.2:30:15` | "change 7.2 to 15 days — here's what breaks, in days, and what we refuse to conclude" |
| the screen | `http://127.0.0.1:8000/?contract=1&clause=7.2&old=30&new=15` | "the same engine in a browser, and every claim is clickable back to its clause" |
| the SLA case | `py -3.11 cli.py --contract 1 --change 9.1:99:98` | "the value of a credit doesn't move, but the band in which you earn one does" |
| the measurement | `py -3.11 tools/evaluate.py` | "we didn't just build it, we measured it against a hand-annotated truth set" |
| the LLM path | `py -3.11 tools/llm_probe.py --contract 1 --clause 3.3` | "the model only fills gaps the table leaves — here it found a dependency the table structurally could not" |
| the tests | `py -3.11 -m unittest discover -s tests` | "112 tests, including a negative control on the scorer itself" |

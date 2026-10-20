# Reverse-engineering log — what we tore down, what we found, what we added

**Owner: Person B.** This is the methodology document for a project whose
method is *reverse engineering*. It answers three questions in the order a
reviewer asks them:

1. **What did you analyse?** — the products and the parts of them we took apart.
2. **What did you find?** — the capability map, and the gap we found in it.
3. **Where did you go beyond it?** — the one differentiator we added, and why.

---

## 1. What we analysed

Four commercial systems that a lawyer would actually use to read a contract,
chosen because they cover the four distinct approaches in the market:

| system | approach it represents | what we took apart |
|---|---|---|
| **Kira Systems** | contract lifecycle management: extraction, obligation tracking, metadata, risk flags | how it turns a PDF into obligations with dates and statuses, and how it presents "what is outstanding" |
| **Luminance** | playbook-driven review: deviation of a draft from a preferred clause library, AI clause analysis | how it decides a clause "deviates", and how it presents the deviation |
| **Harvey** | the general-purpose legal assistant over a document set: retrieval, drafting, workflows | how it answers a legal question from a set of documents, and how it cites |
| **CoCounsel** (Thomson Reuters + OpenAI) | the assistant embedded in the review workflow: document review, drafting, cited answers | how it answers with citations and where the citations point |

We read their **public product material** — feature pages, documentation and
the workflow descriptions each vendor publishes — and reconstructed the
*capability* of each: what artefact it produces, at what granularity, and on
what basis.

**The honesty limit of this method, stated up front.** We could not read their
source code, their prompts, their models or their scoring functions. So
every claim we make about them is a claim about **published behaviour**, not
about internals. Where the public material does not describe something, this
document says *"not publicly described"* — which means *not in the public
material*, **not** *absent from the product*. The comparison in §2 and in
`docs/invention_note.md` §5 is a **feature-level** comparison, and it is not
a freedom-to-operate opinion. The patent agent verifies it independently.

---

## 2. What we found

We reconstructed each product's output model and lined them up against the
question the project exists to answer.

| capability | this system | Kira | Luminance | Harvey | CoCounsel |
|---|---|---|---|---|---|
| clause segmentation & classification | deterministic, fixed 18-type vocabulary | AI extraction of clause-level metadata | AI clause analysis | document parsing / extraction | document understanding, extraction |
| obligations, deadlines, conditions as first-class objects | **yes** — element nodes, typed | obligation management is a core product theme | not publicly described as a typed obligation graph | not publicly described | not publicly described |
| **the relation between clauses** | **typed relation table over clause types, explicit cross-references only** | not publicly described | not publicly described | not publicly described | not publicly described |
| **change-impact analysis** | **reverse-dependency traversal, direct/indirect by hop, causal chain** | lifecycle / version comparison | deviation detection against a playbook | not publicly described as a contract-graph traversal | not publicly described |
| numbers recomputed after a change | **deterministic day arithmetic with unchanged-parameter flagging** | not publicly described | not publicly described | not publicly described | not publicly described |
| **uncertainty output** | **evidence-linked doubt records: code, severity, question, clause ids, quoted text, review point** | risk flags / obligation status | deviation and playbook alerts | not publicly described as a structured uncertainty record | cited answers link to source text |
| **what the system refuses to conclude** | **a first-class output artefact** | not publicly described | not publicly described | not publicly described | not publicly described |
| model required for the structural path | **no** — fully deterministic, offline | AI-based features | AI-based features | LLM-based | LLM-based |
| deterministic reproducibility of the structural output | **yes, byte-for-byte** | not publicly described | not publicly described | not publicly described | not publicly described |

### 2.1 The gap we found

Read across the row marked **"the relation between clauses"**, and one row
stops standing out: **nothing in the market publicly describes a contract as
a set of typed relations between clauses.**

What the market does instead:

- **Kira** tracks obligations and their status — *each obligation in
  isolation*, with dates and states.
- **Luminance** compares a clause against a playbook — *one clause against a
  preferred version*, in isolation.
- **Harvey and CoCounsel** retrieve and generate over documents — *text
  similarity and language*, with citations.

All four are powerful, and all four answer the question *"what does this
clause say?"* or *"which clauses are like this one?"*. **None of them, as
described, answers *"which clauses depend on this one, and what happens to
them if it changes?"*** — because that question requires the relation between
clauses to be modelled explicitly, and the market's public descriptions show
no such model.

### 2.2 Why similarity cannot answer it — the demonstration

We did not take this on trust; we tested it on our own corpus. The two
clauses that matter most in the demo contract share almost no vocabulary:

> **Clause 8.1** — *"If the Customer fails to pay any undisputed Invoice by the
> Payment Due Date determined in accordance with Clause 7.2, the Customer shall
> pay late charges at the rate of 1.5% per month on the overdue amount…"*
>
> **Clause 10.1** — *"If the Customer fails to pay any undisputed Invoice and
> the late payment under Clause 8.1 continues after the expiry of the Cure
> Period specified in Clause 10.2, the Customer shall be in default."*

The legal relation is explicit and unavoidable: **the default is triggered by
the late payment**, so 10.1 depends on 8.1. Lexically, the overlap is
"late payment", "fails to pay", "undisputed Invoice" — and the clause that
*matters* to 10.1 ("Clause 8.1") is a two-word cross-reference, not a
similarity.

A similarity or embedding search over this corpus will not reliably connect
them, because they are connected by **legal causation**, not by vocabulary.
A typed relation table connects them every time, reproducibly, and tells you
which row fired.

---

## 3. What we added

The differentiator is one mechanism, and it has three parts.

### 3.1 The typed relation graph

Instead of retrieving clauses that *look* related, we **compile the contract
into a structure whose relations come from a table a lawyer can audit**:
a closed vocabulary of 18 clause types, and 12 rows of type pairs that mean
"a clause of this type depends on a clause of that type"
(`graph.py::DEPENDENCY_RULES`). The table is small on purpose — it is meant
to be read, argued with and edited by a lawyer without touching code.

A consequence worth naming: because the relation table is the *legal*
knowledge, the system's structure is **reproducible and contestable**. A
lawyer can say "no — a default clause does not depend on a cure clause in
our paper" and change one line.

### 3.2 Reverse-dependency traversal with hop classification

Because `depends_on` is stored dependent-first, the impact of a clause is
found by walking that relation **backwards** — the clauses that depend on it
are the ones a change hurts. A breadth-first walk keeps the hop distance, so
the answer distinguishes *directly affected* from *affected through a chain*,
and the chain itself is reported (`7.2 → 8.1 → 10.1 → 12.3`).

This is the piece that answers the question the market does not: *"what else
is affected, and how directly?"*

### 3.3 A doubt generator that says what it cannot determine

The second thing we added is not a feature but a **discipline**: the system
emits, as a first-class output artefact, the questions it *cannot* answer.

- *Clause 7.2 says 15 days but Definition 2.4 still says 30 days — on one
  reading the contract is now self-contradictory.*
- *Clause 8.1 is affected but still states 1.5% — the system will not infer
  whether that figure should move.*
- *Termination for non-payment needs a notice deemed served 7 days after
  despatch — the shortened payment period has quietly shrunk that window.*

Each doubt carries an identifier, a severity, the question, the clause ids
and the **quoted clause text** that raised it, and a review point. The
doubt rules are conditioned on the traversal result: a doubt about an
unchanged parameter fires only for clauses the traversal actually reached.

The market's systems present findings — obligations, deviations, risk flags,
cited answers. **Ours also presents its own limits.** That is the difference
between a tool that informs a lawyer and one that can be checked by one.

### 3.4 And one property the market's tools do not have

The structural path contains **no language model at all**. Extraction of the
hierarchy, clause typing, node creation, the relation table, the traversal,
the arithmetic and the doubt rules are all deterministic and finite. So:

- the demo needs **no API key** and runs **offline**;
- the structural output is **byte-for-byte reproducible**, which is what makes
  a measured evaluation against a hand-annotated truth set possible at all;
- and the LLM is confined to the one job it is good at — filling a *specific*
  gap in the relation table, which we demonstrated live (asked about Clause
  3.3, the model produced a `depends_on` edge the typed table structurally
  could not produce, and legally the right one).

---

## 4. What the reverse engineering produced besides the design

Three things that are easy to miss, because they are findings rather than
features:

1. **A testable formulation of the differentiator.** "A typed relation table"
  is a claim you can check in twelve lines of code. "Better AI" is not. That
   difference is what made the evaluation possible.
2. **A measure of our own reliability.** Building the comparison forced us to
   ask what a *correct* answer looked like — which is exactly how
   `data/ground_truth.json` came to exist, and why the engine now has
   measured agreement with a hand annotation rather than an assertion that it
   works.
3. **A patent position.** The teardown showed that the interesting mechanism
   is narrow, so the claim is narrow: one mechanism, not a system. That
   analysis is in `docs/invention_note.md`.

---

## 5. How to present this in the review

| the question | the answer | where the evidence is |
|---|---|---|
| "What did you reverse-engineer?" | Four systems, four approaches: lifecycle management, playbook review, the general legal assistant, and the embedded assistant | §1 |
| "What did you find?" | The market models clauses in isolation, or retrieves by similarity; **none publicly describes a typed relation between clauses** | §2, and the table in `docs/invention_note.md` §5 |
| "Isn't that just a difference you found in marketing copy?" | We tested it on our own corpus: the two clauses that matter most share almost no vocabulary, because the relation is legal causation, not similarity | §2.2 |
| "So what did you build?" | A typed relation table + reverse traversal with hop classification + a doubt generator that states what cannot be determined | §3 |
| "Is it better?" | It is *narrower*: it answers one question the others do not, deterministically and auditably, and it never answers what it cannot support | §3.3, §3.4 |
| "How do you know?" | Measured against a hand-annotated ground truth, with a negative control on the scorer itself | `docs/evaluation_report.md` |

**Say the limit before you are asked:** our comparison is against *published
behaviour*, not internals; "not publicly described" does not mean "absent".
And we did not build a system to beat these products — we built one narrow
mechanism they do not have, and we measured it.

# INVENTION NOTE — typed dependency graph with evidence-linked uncertainty for contract change analysis

**CONFIDENTIAL — PRE-FILING. DO NOT PUBLISH.**
This note, the repository it lives in, the demo video, the evaluation report
and the source code are all unpublished as of the date below. Under Indian
practice an earlier public disclosure can destroy novelty. Nothing here may
be posted, shared outside the three-person team, or shown outside the team
until the provisional application has been filed. See §9.

| | |
|---|---|
| Prepared by | Person B (branch `b-graph`), for review by the patent agent |
| Date of this version | 2026-10-20 |
| Branch / commit | `b-graph` @ `85e1e69` + the evaluation work on top |
| Jurisdiction | India (Patents Act, 1973, as amended in 2020) |
| Field | Natural language processing applied to legal documents; computer-implemented contract analysis |
| What this is | A technical disclosure for the agent. It is NOT a specification, NOT a legal opinion, and NOT a freedom-to-operate analysis. |

---

## 1. The problem, stated the way a lawyer states it

A commercial services agreement is not a bag of text. It is a set of
**interlocking obligations**, and the clause that states a duty is rarely the
clause that decides the consequence.

The concrete case this project was built for:

> *Clause 7.2 of an Indian supplier agreement says the Customer shall pay each
> undisputed Invoice within 30 days. The Supplier proposes to amend it to 15
> days. What else does that change? What does it actually mean in days? Which
> clauses support or contradict the answer? And what still has to be decided
> by a human?*

A lawyer answering that question has to do four things at once:

1. **Find the obligations that depend on the amended clause.** In the worked
   example the answer is Clause 8.1 (late charges at 1.5% per month),
   Clause 8.2 (interest at 12% per annum) and Clause 10.2 (the cure period) —
   directly; and Clause 10.1 (the Customer is in default) and Clause 12.3
   (the Supplier may terminate) — through a chain.
2. **Recompute the arithmetic.** If the payment period becomes 15 days, the
   cure period is still 15 days, so the Customer can now be in default on
   **day 30** instead of day 45. The cure period itself did not change, and
   the notice period is still 7 days — so the Supplier's window to serve
   notice has quietly shrunk.
3. **Say what is uncertain.** Does the amendment move Definition 2.4
   ("Payment Due Date") too, or do we now have a self-contradictory
   agreement? Is Clause 10.2 still 15 days on purpose? Was the notice window
   really meant to shrink?
4. **Do all of this in a way that can be audited.** A lawyer must be able to
   see *why* the system says Clause 8.1 is affected.

The existing commercial tools answer parts of this. What they do not — as far
as their public material describes — is answer it with a **deterministic,
auditable structure** and **explicitly state the limits of the determination**.

### 1.1 Why the obvious approach does not work

The obvious implementation is retrieval: embed every clause, embed the
question, return the nearest clauses. It fails on this corpus, and it fails
in a way that is easy to demonstrate:

- Clause 10.1 (Default) and Clause 8.1 (Late Charges) are connected because
  the default is *triggered by* late payment under the late-charge clause.
  Their vocabulary overlap is close to zero. No similarity function connects
  them.
- Clause 8.1 is connected to Clause 7.2 (Payment Period) because the late
  charge runs **from the Payment Due Date**. Again: no shared vocabulary.
- Clause 12.3 (Termination for Non-Payment) is connected to Clause 10.1
  because the termination right is *conditioned on* the default.

These are **legal** relations, not textual ones. They are also perfectly
regular: they can be written down as a table. That observation is the
inventive core of this project.

---

## 2. The mechanism

Three cooperating parts, all deterministic. The language model is used only
to fill gaps the rule table leaves, and the demonstration works with no API
key at all — a property that is itself part of the technical story (§6.3).

### 2.1 Part 1 — a typed contract graph

The contract is compiled into three artefact types.

**Clause nodes.** One node per clause, including the section anchors. The
contract is segmented into a hierarchy (`1`, `1.1`, `1.2`, …, `7.2`, …) and
each clause is assigned exactly one **clause type** from a fixed vocabulary of
18 values:

```
definition, payment, fee, interest, default, cure, termination, notice, sla,
confidentiality, indemnity, limitation, force_majeure, governing_law,
dispute, warranty, term, other
```

Assignment is rule-based and therefore reproducible and auditable: a heading
that names the subject of the clause wins over the body text (`Termination
for Non-Payment` is a termination clause even though its body says
"default"); keyword matching is applied with word boundaries and a fixed
priority order; a fixed vocabulary has no "tax" type, so a tax clause is
`other`; and a clause that merely *mentions* a service level without stating
one is not typed `sla`.

**Element nodes.** The legal *things* a clause creates — an obligation, a
condition, a right, a deadline, a payment duty, a termination event, a
definition. A clause does not create a node merely by being typed: the node
is created only if the clause actually imposes the thing. Clause 8.3
("… are reasonable estimates of the cost of late payment and are not a
penalty") sits in the fee family but creates **no** charge obligation, because
it imposes no charge. Clause 9.2 (Service Credits) creates a **right** to
credits, not an obligation to pay late charges. Node precision matters here:
every node is rendered as a sentence in the lawyer-facing output, so a
spurious node is a false statement about the contract.

**Typed edges.** Five relations, with a frozen direction rule:

| relation | direction | meaning |
|---|---|---|
| `creates` | clause → element | 7.2 creates a payment duty |
| `depends_on` | **dependent → dependency** | 10.1 depends on 8.1 |
| `subject_to` | element → element | the payment duty is subject to the 30-day deadline |
| `causes` | cause → effect | reserved |
| `references` | referrer → referenced | 7.2 references 2.4 |

`depends_on` is stored **dependent-first**. The consequence, stated once
because it is the easiest thing in the whole system to get backwards: the
impact of a clause X is found by walking `depends_on` **backwards** — the
clauses that depend on X are the ones a change to X hurts.

### 2.2 Part 2 — where the dependency edges come from (the typed relation table)

This is the part that replaces retrieval.

A dependency edge is emitted **only** when two conditions both hold:

1. the referring clause contains an **explicit cross-reference** to the
   referenced clause (`"… in accordance with Clause 7.2"`), and
2. the **pair of clause types** `(type of referring clause, type of
   referenced clause)` appears in a **typed relation table**:

```python
DEPENDENCY_RULES = [
    ("default", "fee"),      ("default", "payment"), ("default", "interest"),
    ("default", "cure"),     ("cure", "default"),    ("cure", "payment"),
    ("termination", "default"), ("termination", "cure"), ("termination", "payment"),
    ("fee", "payment"),      ("interest", "payment"), ("sla", "payment"),
]
```

Each reference is judged on its own, so a default clause that mentions both
the late-fee clause and the payment clause depends on **both** — which is
correct: the late payment is what actually triggers the default.

If the type pair is *not* in the table, the cross-reference is recorded as a
`references` edge instead of a `depends_on` edge. This distinction is
load-bearing: a **definition** referenced by an operative clause is an
interpretation target, not a dependency. So `7.2 references 2.4` produces a
*doubts* entry (see 2.4), never an *impact* entry. Making that distinction
explicit — rather than letting a similarity score decide — is the point.

**One type pair runs the other way round.** The table above assumes the
*referring* clause is the dependent: a default clause that mentions the
late-fee clause depends on it. That is wrong for one pair in the working
corpus. Clause 9.1 says *"Failure to achieve the Service Levels entitles the
Customer to the service credits set out in Clause 9.2"* — the reference runs
9.1 → 9.2, but the legal dependency runs the other way: **the service credits
depend on the service level**. Those pairs are listed in a second, small
table and the edge is emitted reversed, so `depends_on` remains dependent-first
and the impact of the SLA is the credit clause. The point is that the
direction is a *decision the table makes*, not an accident of the text
direction — and that the system can say which decision it made.

**Technical effect.** The resulting graph is:

- **deterministic** — the same contract always yields the same edges, on any
  machine, with no model, no training data and no network;
- **auditable** — for every edge, the table row that produced it can be
  printed;
- **bounded** — an edge exists only where the document itself states a
  cross-reference, so the graph cannot grow combinatorially with clause count
  the way an embedding-similarity graph can.

**The relation table is the legal-knowledge artefact.** It is compact (12
rows), human-readable, and editable by a lawyer without touching code. It is
also the point at which domain knowledge enters the system, and it is the
part a reviewer can challenge on legal grounds rather than on statistical
grounds.

### 2.3 Part 3 — reverse traversal with hop classification

Given a changed clause, the system performs a breadth-first traversal over
the **reverse** of the `depends_on` relation, retaining the hop distance of
each reached clause:

```
DIRECT   (hop 1): 8.1 late charges, 8.2 interest, 10.2 cure period
INDIRECT (hop 2): 10.1 default
INDIRECT (hop 3): 12.3 termination for non-payment
CHAIN:   7.2 → 8.1 → 10.1 → 12.3
```

The hop count is retained per clause and is not merely a traversal artefact:
it is carried into the output as the direct/indirect classification, it
bounds the traversal (currently 3 hops), and it is an **input to the doubt
rules** (§2.4, rules D2 and D3 — a doubt about an unchanged parameter is
raised only for clauses the traversal actually reached, and a doubt about
the notice window is raised only when the traversal reached a termination
clause).

**Technical effect.** The impact set is a specific, bounded subset of the
contract, computed by an algorithm with no model in the loop, and every
member of the set carries (a) the hop distance that produced it, (b) the
chain from the changed clause, and (c) a link back to the clause text. The
output is reproducible byte-for-byte across runs.

### 2.4 Part 4 — the rule-driven doubt generator with evidence linkage

The system does not emit a confident narrative. It emits **records of what
it cannot determine**, each one linked to the evidence that raised it. The
demonstrated implementation has nine doubt families:

| code | severity | what it detects |
|---|---|---|
| `definition_collision` | high | a definition separately states a period that the changed clause also states — the system cannot tell whether amending one moves the other |
| `cross_ref_mismatch` | high | after the change, the clause and the definition it points to now state different periods: on one reading the contract contradicts itself |
| `unchanged_dependent_param` | medium | a clause *in the impact chain* still states a figure the change does not move; the system will not infer whether it should move |
| `notice_vs_cure` | medium | a termination right in the impact chain depends on a notice that is deemed served N days after despatch, so the window for serving it has shrunk |
| `undefined_term` | low | a quoted capitalised term used in the changed clause that no definition clause defines |
| `amendment_form` | medium | the contract requires amendments to be in writing and signed; an agreement by email may not take effect |
| `scope_of_change` | low | other clauses of the same family were not recalculated and may also be affected |
| `definition_drift` | high | a definition and the clause that uses it already state different periods (intra-contract conflict) |
| `sla_schedule_mismatch` | high | a service level was amended but the schedule that provides the credits still measures against the old threshold, so the amendment has no effect on the remedy as drafted |

Each record has the shape:

```json
{
  "code": "cross_ref_mismatch",
  "severity": "high",
  "question": "Clause 7.2 now says 15 days but Definition 2.4 still says 30 days. "
              "On one reading the contract is self-contradictory.",
  "evidence": [
    {"id": "7.2", "type": "payment", "why": "new value",
     "text": "Payment Period. The Customer shall pay ... within 30 (thirty) days ..."},
    {"id": "2.4", "type": "definition", "why": "old value still stated",
     "text": "\"Payment Due Date\" means, in respect of any Invoice, the date falling 30 (thirty) days ..."}
  ],
  "review_point": "Decide whether Definition 2.4 is amended at the same time."
}
```

The `evidence[].text` field quotes the actual clause text, so a reviewer can
check the claim without re-opening the contract.

**The coupling that matters.** These rules do not run over the document
independently of the graph. `unchanged_dependent_param` fires only for
clauses the reverse traversal reached; `notice_vs_cure` fires only when the
traversal reached a termination clause; `definition_collision` fires only on
`references` edges that point at a definition. The uncertainty output is
therefore *localised by the graph structure*, and each doubt carries the
clause identifier that anchors it.

### 2.5 Supporting part — deterministic counterfactual arithmetic

Downstream time periods are recomputed by rule from the extracted numbers,
never by a model. For a payment-period change:

| metric | old | new |
|---|---|---|
| Payment deadline (Clause 7.2) | 30 days | 15 days |
| Default can be declared on (payment + cure) | day 45 | day 30 |
| Interest accrual window | 30 days | 15 days |
| Cure period (Clause 10.2) | 15 days | 15 days — **unchanged, flagged** |
| Notice required before default (Clause 14.1) | 7 days | 7 days — **unchanged, flagged** |

The last two rows matter. The system distinguishes a parameter that *changed*
from a parameter that *did not change and may need to*. That distinction is
precisely what a naive "here is your new deadline" answer loses, and it is
one of the entries in the doubt output.

The same machinery handles a **percentage** change, where the interesting
arithmetic is not a day count but a *band*. Lowering the service level in
Clause 9.1 from 99% to 98% does not change what a service credit is worth —
it changes the availability band in which the customer earns one at all:

| metric | old | new |
|---|---|---|
| Service level availability (Clause 9.1) | 99% | 98% |
| Service credit threshold (Schedule B, as drafted) | 99% | 99% — **unchanged, flagged** |
| Service credit threshold (Schedule B, if conformed) | 99% | 98% |
| Service credit per full point below the threshold | 5% of monthly Charges | 5% — **unchanged** |
| First service credit earned | INR 22,500 at 98% availability | INR 22,500 at 97% availability |
| Maximum monthly service credit (cap) | INR 1,12,500 | INR 1,12,500 — **unchanged** |
| Monthly Charges base (Schedule A) | INR 4,50,000 | INR 4,50,000 — **unchanged** |

Two things a reviewer should notice. First, as drafted the amendment has *no
effect at all* on the credits, because the schedule still measures against
99% — which is precisely what the `sla_schedule_mismatch` doubt says. Second,
the maximum exposure does not move (the cap is unchanged) but it takes longer
to reach: with the schedule conformed, the customer earns nothing at all
between 98% and 99% availability. A change that looks like "1 percentage
point" is a shift of the whole remedy band.

Change types with no modelled arithmetic say so. The system does not emit
payment rows for, say, a change to the confidentiality clause.

---

## 3. A complete worked embodiment (for the drawings)

Contract: a synthetic Indian supplier services agreement (Schedule A and B
attached, Indian Rupee charges, 99% availability, 30-day payment, 15-day
cure, 7-day notice, Bengaluru / Arbitration and Conciliation Act 1996).

Input: *amend Clause 7.2 — Payment Period — from 30 days to 15 days.*

Output of the method:

```
CHANGED CLAUSE: 7.2 (payment)   30 -> 15

DIRECT IMPACT
  - Clause 8.1    - Clause 8.2    - Clause 10.2
INDIRECT IMPACT
  - Clause 10.1 (hop 2)   - Clause 12.3 (hop 3)
CAUSAL CHAIN
  7.2  ->  8.1  ->  10.1  ->  12.3

WHAT IS AFFECTED, IN LEGAL TERMS
  Obligations:   Clause 8.1  pay late charges on any overdue Invoice
                Clause 8.2  pay interest on any amount paid late
  Conditions:   Clause 10.1 Customer remains in default until the failure is cured
  Deadlines:    Clause 10.2 Cure Period of 15 days after notice
  Consequences: Clause 12.3 terminate the Agreement on the stated ground

OLD vs NEW
  Payment deadline (Clause 7.2)                30 days -> 15 days   [CHANGED]
  Default can be declared on (payment + cure)  day 45  -> day 30     [CHANGED]
  Interest accrual window                      30 days -> 15 days    [CHANGED]
  Cure period (Clause 10.2)                   15 days -> 15 days    [unchanged]
  Notice required before default (Clause 14.1)  7 days -> 7 days    [unchanged]

UNCERTAINTY (what we are NOT sure about)
  [definition_collision / high]   Clause 7.2 changes the payment period to 15 days,
                                   but Definition 2.4 separately defines the term and
                                   states 30 days ...
  [cross_ref_mismatch / high]      Clause 7.2 now says 15 days but Definition 2.4
                                   still says 30 days ...
  [unchanged_dependent_param / medium]  Clause 8.1 is affected by this change but
                                   still states 1.5 % ...
  [notice_vs_cure / medium]        Termination for non-payment (Clause 12.3) requires
                                   notice under Clause 14.1, deemed served 7 days after
                                   despatch ...
  [amendment_form / medium]        Clause 17.1 requires amendments to be in writing
                                   and signed ...
  [scope_of_change / low]          The system was asked about Clause 7.2 only ...

LAWYER REVIEW POINTS
  - Confirm the interaction between Clause 7.2 and Definition 2.4.
  - Decide whether Definition 2.4 is amended at the same time.
  - Confirm whether the 1.5 % in Clause 8.1 should change.
  - Check the notice can be served in time under Clause 14.1.
  - Check the proposed change is executed in the form Clause 17.1 requires.
  - Confirm whether Clause 5.1, Clause 5.2, Clause 7.1 need conforming changes.
```

Every line is derived by rule. The method is fully specified by the type
vocabulary, the relation table, the traversal and the doubt rules — the
language model is not in the path of any of it.

---

## 4. What is claimed as novel

Stated narrowly, because a broad claim dies.

**N1 — Dependency edges derived from a typed relation table applied to
explicit cross-references.**
Not: "extract relationships from a contract" (extraction is old), and not
"rank clauses by similarity" (that is the prior-art-friendly route). The
claimed arrangement is: a *fixed type vocabulary* + a *table of legal type
pairs* + the *requirement that the document itself states the cross-reference*
+ the consequence that a pair absent from the table is recorded as a
non-impacting reference instead of a dependency. The table is the
legal-knowledge artefact; the graph is its output. The table also carries the
*direction* decision for each pair, so that a pair in which the referring
clause is not the dependent is expressed rather than assumed.

**N2 — Impact traversal over the reverse of a dependent-first stored relation,
retaining hop classification, with the hop count carried into the output and
used as a condition on subsequent rule evaluation.**
Not: "traverse a graph" (BFS is old). The claimed arrangement is the
*direction convention plus the retained hop distance plus the use of that
distance to select which downstream rules are applied to which clauses* —
i.e. the traversal output is not merely displayed, it *gates* the uncertainty
computation.

**N3 — A rule-driven generator of evidence-linked uncertainty records,
conditioned on the typed graph and the recomputed counterfactual values, each
record carrying an uncertainty identifier, a severity, a human-resolvable
question, at least one evidence link identifying a clause, and the quoted
clause text.**
Not: "the system outputs confidence scores" (soft, and often unpinned to
text), and not: "the system flags risky clauses" (risk flags are commercial
prior art). The claimed arrangement is: the *uncertainty is expressed as a
question to a human*, it is *anchored to specific clause identifiers with
quoted text*, and it is *generated by rules over the structure and the
arithmetic rather than by a model*.

**N4 — (dependent) — Counterfactual recomputation in which downstream
parameters that did not change are explicitly flagged as unchanged rather
than omitted.**
Not "compute the new deadline" (arithmetic is old). The claimed arrangement
is the *explicit unchanged-versus-changed distinction surfaced as a review
item*, which is what turns a date calculator into a risk surface.

**The combination is the invention.** Individually, N1–N4 each have
recognisable precedents (rule-based extraction, graph traversal, confidence
scoring, date arithmetic). The agent should draft to the combination, and
should expect the examiner to attack it under s.3(d) (mere aggregation) —
§6.2 sets out the strongest available answer.

---

## 5. Comparison with the commercial tools

**Basis and limits of this comparison.** This is a *feature-level* comparison
drawn from each vendor's public product material available in October 2026.
It is **not** a freedom-to-operate opinion, and it is **not** a statement
about any vendor's internal implementation, which is not public. It must be
independently verified by the agent before anything is asserted in a
specification. Where a cell says "not publicly described", that means *not
described in public material* — not *absent*.

| capability | this system | Kira Systems | Luminance | Harvey | CoCounsel |
|---|---|---|---|---|---|
| clause segmentation & classification | deterministic, fixed 18-type vocabulary, auditable | AI extraction of clause-level metadata | AI clause analysis | document parsing / extraction for review | document understanding, extraction for review and drafting |
| obligations, deadlines, conditions as first-class objects | yes — element nodes, typed | obligation management is a core product theme | not publicly described as a typed obligation graph | not publicly described | not publicly described |
| relation between clauses | **typed relation table over clause types**, explicit cross-references only | not publicly described | not publicly described | not publicly described | not publicly described |
| change-impact analysis | **reverse-dependency traversal, direct/indirect by hop, causal chain** | lifecycle / version comparison; not publicly described as a dependency traversal | deviation detection against a playbook | not publicly described as a contract-graph traversal | not publicly described |
| numbers recomputed after a change | deterministic day arithmetic with unchanged-parameter flagging | not publicly described | not publicly described | not publicly described | not publicly described |
| uncertainty output | **evidence-linked doubt records: code, severity, question, clause ids, quoted text, review point** | risk flags / obligation status | deviation and playbook alerts | not publicly described as a structured uncertainty record | cited answers link to source text |
| what the system refuses to conclude | first-class output artefact | not publicly described | not publicly described | not publicly described | not publicly described |
| model required for the structural path | **no** — fully deterministic, offline | AI-based features | AI-based features | LLM-based | LLM-based |
| deterministic reproducibility of the structural output | yes, byte-for-byte | not publicly described | not publicly described | not publicly described | not publicly described |

**The strongest single answer to "what is different from Kira / Harvey?"**
is this sentence:

> *Those systems help a lawyer read and compare contract text. This system
> compiles the contract into a typed legal structure whose relations come
> from a table a lawyer can audit, traverses that structure to find the
> clauses a proposed change actually hits, and then states — with the clause
> text attached — the questions it cannot answer.*

The second strongest is the auditability property: every claim in the output
is traceable to (i) a clause identifier, (ii) a rule row, and (iii) quoted
text. The third is that the structural analysis contains **no model at all**,
so the deterministic part of the output can be certified and regression-tested
(§8).

---

## 6. Patentability assessment (engineering view, not legal advice)

### 6.1 The obvious risk

Under the Patents Act 1973 as amended in 2020, §3(k) excludes "programmes
for computers", and §3(m) excludes methods of doing business. A draft that
says "a method of analysing a contract and reporting legal risk" is
vulnerable to both: it reads as a method of performing legal analysis (a
business activity) implemented on a computer, and as a computer program per
se. §3(d) (mere aggregation of known elements) is the third front.

This project was **never** conceived as a patentable *system*. It is a
university project. The honest position is that only a narrow mechanism is
even arguable, and that the agent's job is to find the narrow framing, or to
tell us there is none.

### 6.2 The framings that have a chance

**F1 — the typed-relation-table compilation.**
Claim the *technique* for compiling a document into a typed structure using a
closed type vocabulary and a table of type pairs, with the technical
characteristics being: (i) no trained model and no inference is used to
produce the structure; (ii) the structure is fully determined by the
document plus a bounded, human-editable table; (iii) the structure is
reproducible and bounded in size. The technical effect to argue: the
compilation produces a machine-usable representation that a subsequent
deterministic traversal can consume, and it does so without any
general-purpose model.

**F2 — the reverse traversal with retained hop distance used to gate rule
evaluation.**
This is the strongest of the four from a §3(k) point of view, because it
describes a *data-structure operation* (a directed traversal with a retained
distance label) whose output conditions which subsequent processing steps
apply to which data items. That is closer to how §3(k) is examined than
"analysing legal text" ever is.

**F3 — the uncertainty-record artefact.**
Claim the *record format and its generation*: a machine-readable record that
carries (a) an uncertainty identifier from a closed set, (b) a severity, (c) a
human-resolvable question, (d) at least one evidence link resolving to a
clause identifier in the same document together with the quoted clause text,
and (e) a review point; generated by rules conditioned on the graph traversal
result and on the recomputed counterfactual values. Argue the technical
effect: the artefact is a *bounded, machine-processable, self-verifying
index into the source document* — every claim in it can be checked against
the document by a program, which a free-text narrative cannot be.

**F4 — the s.3(k) Explanation.**
The 2020 amendment added an Explanation to §3(k) under which a computer
programme is not excluded where the claimed invention has a technical effect
or involves a technical intervention in, or on, a person or an animal or a
plant, or where the claim is directed to a method involving a specific
machine. F3 is the candidate most likely to satisfy "technical effect" and
F1 the candidate most likely to satisfy "specific machine / no ordinary use"
framing. The agent should test both.

**What the agent should be told plainly:** the risk under §3(d) is real. N1,
N2, N3 and N4 each resemble known things. The answer to "more than the sum" is
the *coupling*: the typed relation table produces a dependency set; the
reverse traversal produces a hop-labelled set; the doubt rules are
**conditioned on that hop-labelled set**; and the uncertainty records point
back into the same typed structure. Claim the arrangement, not the parts.

### 6.3 An argument that is genuinely strong for this case

The structural path contains **no language model at all**. The type
vocabulary, the relation table, the traversal, the arithmetic and the doubt
rules are all closed, deterministic and finite. The system can be run
offline, on a local machine, with no training set and no network, and its
structural output is byte-for-byte reproducible and regression-testable
against a hand-annotated ground truth (see §8).

That is not a marketing point, it is an examinable technical property, and it
distinguishes the mechanism from the general LLM-over-documents category that
dominates the field. It also means a claim can be restricted to the
deterministic path and will not read as "a machine learning model applied to
a contract".

### 6.4 What is NOT claimable

- The system as a whole, or the idea of "AI-assisted contract review".
- The LLM gap-filling path (a clause-by-clause extraction prompt whose output
  fills relations the table missed). This is ordinary prompting and it is
  also the part with the least engineering content.
- "Legal retrieval" over a case-law or authority corpus — ordinary
  information retrieval with keyword scoring, and it belongs to a different
  branch of the project.
- The reasoning narrative that turns the structured output into prose. That is
  presentation of an already-determined result.
- Anything about the synthetic contract corpus itself; the contracts are
  training documents and are not part of the invention.

---

## 7. Claim sketch (for the agent to draft over — not a specification)

**Claim 1 — method.**
A computer-implemented method of determining the legal effect of a proposed
amendment to a contract represented as a plurality of clauses, comprising:

- (a) segmenting the contract into clauses and assigning to each clause a
  clause type from a closed vocabulary of clause types;
- (b) for each clause, determining one or more legal-element nodes
  representing the obligations, conditions, rights, deadlines, payment
  duties and termination events created by that clause, and generating
  `creates` edges from the clause node to each element node;
- (c) identifying, from an explicit cross-reference within the text of a
  first clause to a second clause, and from a stored relation table mapping
  ordered pairs of clause types to a dependency relation and to a direction
  of that dependency, a dependency edge between the first and second clause,
  the dependency edge being stored in dependent-first order, and storing a
  cross-reference that does not satisfy the relation table as a reference edge
  instead;
- (d) in response to an identification of a changed clause, traversing the
  dependency edges in reverse from the changed clause and retaining, for each
  reached clause, a hop distance from the changed clause, thereby obtaining a
  bounded set of impacted clauses classified as direct or indirect;
- (e) extracting numeric parameters from the impacted clauses and
  recomputing, by deterministic arithmetic, a counterfactual value for each
  of a plurality of downstream periods, and identifying downstream parameters
  that are unchanged by the amendment;
- (f) applying a fixed set of doubt rules, each doubt rule being conditioned
  on at least one of the typed graph, the extracted numeric parameters and the
  recomputed counterfactual values, and generating from the applied doubt
  rules a plurality of doubt records, each doubt record comprising an
  uncertainty identifier, a severity, a question for resolution by a human, a
  review point, and at least one evidence link identifying a clause of the
  contract and including the text of that clause;
- (g) outputting the impacted clauses, the counterfactual values and the
  doubt records, the output being independent of any language model.

**Claim 2 — apparatus.** The method of claim 1 implemented as: a segmenting
module; a clause-typing module operating on a closed vocabulary and a fixed
priority order; a node-generating module; a relation-table module storing
ordered pairs of clause types; a dependency-generating module; a traversing
module configured to traverse in reverse and to retain hop distances; an
arithmetic module; a doubt-rule module; and an output module.

**Claim 3 — computer-readable medium.** Instructions that, when executed on a
computing device, cause the device to carry out the method of claim 1.

**Possible dependent claims:** the closed vocabulary of 18 clause types as
listed; the relation table as listed; the nine doubt codes as listed; the
distinction that a cross-reference to a definition clause is stored as a
reference edge and is therefore excluded from the impacted set and handled by
a doubt rule instead; the requirement that an element node is created only
where the clause imposes the corresponding obligation, condition, right or
deadline.

### 7.1 Suggested drawings

Six figures, all derivable from the mechanism, for the agent to draw:

| fig | title | what it shows |
|---|---|---|
| **1** | Compilation of a contract into a typed graph | flow: contract text → clause segmentation → clause typing (closed vocabulary) → element nodes → typed edges → graph; annotate the branch where an explicit cross-reference meets the relation table |
| **2** | The typed relation table and the edge direction | the 12 type pairs; the dependent-first storage convention; the separate inverse-pair list, with 9.1/9.2 as the worked example of a pair whose direction is decided rather than assumed |
| **3** | Reverse-dependency traversal with hop classification | the graph of the worked embodiment, arcs labelled with hop distance; the direct / indirect split shaded |
| **4** | Counterfactual recomputation and the changed/unchanged distinction | the 7.2 payment case as a table, with the two *unchanged* rows highlighted and the day arithmetic shown (30 + 15 = 45 → 15 + 15 = 30) |
| **5** | Doubt generation and evidence linkage | a flow from a traversal result + extracted numbers + recomputed values into doubt rules, and out into a doubt record with code, severity, question, evidence links (clause id + quoted text) and review point |
| **6** | Worked embodiment end to end | the 7.2 → 15-day output in the form it is presented to a lawyer, annotated with the four sections that correspond to claims 1(c)–1(f) |

Nothing in the drawings needs a model to be present: the claimed mechanism is
entirely deterministic, and that is the point the drawings should make
visually.

---

## 8. Evidence available for the examiner / agent

All of this is in the private repository and can be shown to the agent under
NDA before filing, and to the examiner afterwards.

| artefact | what it shows |
|---|---|
| `docs/evaluation_report.md` | measured agreement with a hand-annotated ground truth: clause segmentation, clause typing, number extraction, dependency edges, reference edges, impact sets and chains, counterfactual arithmetic, doubt detection, definition-drift detection, cross-contract comparison |
| `data/ground_truth.json` | the annotated truth set, with the provenance of every annotation recorded in the file |
| `tools/evaluate.py` | the scorer, including a negative control (the scorer is itself tested to prove it reports a wrong answer) |
| `tests/` | 92 automated tests, green, covering the rules individually and end-to-end |
| `graph.py` | the relation table and the traversal, in the exact form claimed |
| `impact.py` | the doubt rules and the counterfactual arithmetic |
| `clauses.py` | the closed vocabulary, the typing rules and the word-boundary matcher |
| the CLI / HTTP demo | the complete worked embodiment of §3 |

Note for the agent: the evaluation measures **internal consistency against
hand annotations on two synthetic contracts**, not accuracy on real
contracts. That is stated in the report itself. It is a regression guard and
a demonstration of coverage, not a claim of external validity.

---

## 9. Filing discipline and disclosure log

### 9.1 Rules for the team (until the provisional is filed)

1. The repository stays **private**. No public GitHub, no public gist, no
   public Hugging Face / Zenodo / Papers-with-code entry.
2. No demo video, screen recording, screenshot set or live link that shows
   the mechanism, publicly or semi-publicly (a private link to a limited
   audience is still a disclosure if the audience is not bound by
   confidentiality).
3. No paper, poster, thesis chapter, blog post, conference talk, workshop
   or course assignment that describes the typed relation table, the reverse
   traversal with hop classification, or the evidence-linked doubt generator.
   A generic talk about "we built a contract analysis tool for a university
   project" is lower risk; a talk with the mechanism on a slide is a
   disclosure. If in doubt, ask first.
4. No disclosure to a vendor, client, recruiter or interviewer without
   checking with the agent first. A demo to a prospective employer is a
   disclosure.
5. Nothing may be added to a public issue tracker, a public wiki, or a
   public AI-tool marketplace page.
6. Keep `docs/invention_note.md` inside the private repository only.

### 9.2 Disclosure log

Every disclosure gets a row here, before it happens. An empty log is the goal.

| date | what was disclosed | to whom | confidentiality in place? | consequence |
|---|---|---|---|---|
| — | (none yet) | — | — | — |

### 9.3 Sequence and rough steps (the agent to confirm)

1. Freeze this note and the evaluation report; finalise the drawings
   (flowcharts of the compilation, the traversal and the doubt generation).
2. File the **provisional application** (India: Form 1 with Forms 2, 3, 5, 8
   and 9 as applicable; the agent confirms the current form set and fees).
3. Record the filing date and the priority date here.
4. Only after filing, consider making the repository public. If publication
   is intended, publish the code and a technical description, not this note.
5. If any foreign filing is contemplated, note that an Indian provisional
   must be filed within 12 months of the earliest foreign filing to preserve
   the priority date (Paris Convention) — check this with the agent before
   any foreign filing.

### 9.4 Open questions for the agent

1. Is there a claimable mechanism here at all, or should we file a
   defensive/low-cost provisional and describe the rest as an academic
   contribution?
2. Which single framing has the best §3(k) survival: the compilation
   technique (F1), the traversal-with-gating (F2), or the uncertainty-record
   artefact (F3)? Our own view is F2 and F3, but the agent may see it
   differently.
3. Can the relation table be described as part of the claim, or must the
   claim be written in terms of the *general* mechanism (any closed type
   vocabulary + any type-pair table) with the specific table as an example? We
   prefer the general form, because the specific table is an obvious
   prior-art target for a third party.
4. **Inventorship.** Under Indian practice the inventors are the natural
   persons who contributed to the inventive step. All three of us worked on
   the mechanism, but not equally on every part. This has to be settled
   honestly *now*, before filing, because an inventorship dispute can
   compromise priority. Please advise what evidence you need from each of
   us. Suggest we agree a written account of who designed the relation
   table, who designed the traversal, who designed the doubt rules, and who
   built the evaluation, and keep it.
5. Is the "no language model in the structural path" property best claimed
   explicitly (as a limitation of the claim), or should the claim be silent
   and the property kept for the specification's technical-effect argument?
6. What is the minimum we must keep out of any public artefact? We can
   describe the demo's *output* (a lawyer-facing impact report) publicly
   while keeping the mechanism private, if that helps — please confirm.

---

## 10. What this note is not

- It is not a claim that the system is novel in the field of contract
  analytics. It is not. It is a claim about one narrow arrangement.
- It is not legal advice, and no part of it should be repeated to a vendor,
  an employer or a press without the agent's clearance.
- It is not a freedom-to-operate opinion, and the comparison in §5 is a
  feature-level reading of public marketing material, not a technical
  comparison of implementations.
- It does not claim accuracy on real contracts. The measured evidence is
  against hand annotations on two synthetic documents, and the known gaps
  (rupee-amount extraction, the unmodelled SLA-to-service-credit dependency,
  the rule-based nature of the doubt engine) are listed in
  `docs/evaluation_report.md` and must be reproduced in any description.

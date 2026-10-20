# DEMO RUNBOOK — final review

**Owner: Person B.** Print this or keep it in a second window. Everything below
has been run from a clean `git clone` on a fresh database — it works.

---

## 0. Ten minutes before you start

```powershell
py -3.11 seed.py                              # rebuild demo.db from scratch
py -3.11 -m unittest discover -s tests         # expect: Ran 92 tests ... OK
```

Open **two** terminals: terminal A for the CLI, terminal B for the server.

If the server port is busy: `py -3.11 -m uvicorn server:app --reload --port 8001`
and use `http://127.0.0.1:8001/docs`.

---

## 1. The six-step demo

| # | command | what it shows | one line to say |
|---|---------|---------------|-----------------|
| 1 | `py -3.11 seed.py` | one command builds the DB, loads two synthetic Indian commercial contracts and **compiles each into a typed graph** | *"One command. No Docker, no server, no API key. 59 clauses, 95 nodes, 63 typed edges for Contract A."* |
| 2 | `py -3.11 cli.py --contract 1 --change 7.2:30:15` | **the headline**: change the payment period 30 → 15 days | *"Clause 7.2 says pay within 30 days. Change it to 15. Here is what that touches, what it means in days, what we are not sure about, and the clause text behind every claim."* |
| 3 | `py -3.11 cli.py --contract 1 --graph` | the relationship model itself | *"This is the structure the answer comes from — not retrieved text, a typed graph with a direction rule."* |
| 4 | `py -3.11 -m uvicorn server:app --reload` → `http://127.0.0.1:8000/docs` | the HTTP API | *"Same engine over HTTP. POST /impact returns all twelve spec items. This is the surface the screen is built on."* |
| 5 | `py -3.11 tools/evaluate.py` | **the measurements** | *"We did not just build it, we measured it, against a hand-annotated ground truth."* |
| 6 | `py -3.11 cli.py --contract 1 --change 9.1:99:98` | the SLA counterfactual | *"The same machinery on a percentage change: the value of a service credit does not move, but the band in which you earn one does."* |

**If you only get three minutes: steps 2, 5, 2 again.** The headline, the
evidence that it is right, the headline again while they look at the doubts.

---

## 2. The 30-second pitch

> *A contract is not a bag of clauses, it is a set of interlocking obligations.
> We compile a contract into a typed structure — obligations, conditions,
> deadlines, remedies — where the links between clauses come from a table a
> lawyer can audit, not from a similarity score. Then we ask it: if this
> clause changes, what breaks, what does that mean in days, and — the part
> nobody else does — **what can we not determine?** Every answer points at a
> clause id and quotes the clause text.*

---

## 3. Questions you will get, and the answers

**"What is different about this than Kira / Luminance / Harvey / CoCounsel?"**
> *Those systems help a lawyer read and compare contract text. We compile the
> contract into a typed legal structure whose relations come from a table a
> lawyer can audit, traverse that structure to find the clauses a proposed
> change actually hits, and then state — with the clause text attached — the
> questions we cannot answer.*

The three concrete differences: (1) relations come from a **typed relation
table over clause types**, not from text similarity — Clause 10.1 (default)
and Clause 8.1 (late charges) share almost no words, and no retrieval system
will ever connect them; (2) the impact is a **deterministic traversal with
hop classification**, so "directly affected" and "affected through a chain"
are computed, not guessed; (3) the output includes **evidence-linked
uncertainty records** — the system is built to refuse to conclude, and says
which clause made it hesitate.

**"How do you know it is right?"**
> *"Against a hand-annotated ground truth: every clause type, every number,
> every dependency edge, every cross-reference, nine change scenarios, the
> counterfactual arithmetic and the doubt codes. `tools/evaluate.py` scores
> it; `docs/evaluation_report.md` is the output."*

**Then say the honest part yourself, before they ask:**
> *"These are self-consistency numbers on two synthetic contracts, written by
> the same team that wrote the rules. They are a regression guard, not a
> claim about real contracts. On a real, messier document these numbers will
> fall, and the report lists exactly where we expect them to fall."*

**"Isn't this just a rule engine / keyword matching?"**
> *The rules are the point. "Is a late-fee clause a dependency of a default
> clause?" is a legal question, not a similarity question, so we encode it as
> a typed relation table — 12 rows, human-readable, editable by a lawyer
> without touching code. The model is used only to fill gaps the table
> leaves, and only where a cross-reference exists but no rule fires. We
> proved that live: asked about Clause 3.3, whose type pair is not in the
> table, the model produced a dependency the table structurally could not.*

**"What if the contract is badly drafted?"**
> *It degrades in specific ways, and we have measured them rather than
> guessed. Rupee amounts are parsed only where the service-credit
> counterfactual needs them. A clause that references nothing gets no edges —
> so a bad reference means we miss an impact, we do not invent one. Broken
> numbering would need extractor work, which is A's branch. The honest
> summary is in the limitations section of the report.*

**"Does it need a GPU, Docker, an API key?"**
> *No. One file database, one command, no containers. The core demo needs no
> API key at all — the structural path has no model in it, which is also why
> the output is byte-for-byte reproducible.*

**"What is the weakest part?"**
> *The doubt engine is nine rule families, not a general defeasible-reasoning
> system. And clauses typed "other" create no node, so a duty stated outside
> the typed families — Clause 3.1's "reasonable skill and care" — is not
> surfaced as an obligation. Both are in the report.*

**"What would you do next?"**
> *Rupee amounts in the general number parser; nodes for "other"-typed
> clauses; and the inverse dependency rule currently covers one type pair,
> so it wants a proper rule for the general case.*

**"Is it patentable?"**
> *"As a system, no — a computer-programme-per-se or business-method
> framing fails under section 3(k). Only one narrow mechanism is plausibly
> claimable, and we have a written note for the patent agent. That is why the
> repository stays private until the provisional is filed."*

**"What did each of you do?"**
Know this cold — see §6.

---

## 4. If something breaks

| symptom | cause | fix |
|---|---|---|
| `no such table: contracts` in A's tests | a stray **empty** `app.db` (0 bytes) | `del app.db` — it must be *absent*, not empty |
| `demo.db` missing / no contracts | not seeded | `py -3.11 seed.py` |
| `[Errno 10048] address already in use` | an old server is running | `--port 8001`, or kill the old uvicorn |
| uvicorn shows two workers | `--reload` plus an existing process | kill it, run without `--reload` |
| the LLM probe says "no provider is configured" | expected — the demo does not need one | leave it; it is the intended default |
| `ModuleNotFoundError: google.generativeai` | only needed for the optional live LLM path | `py -3.11 -m pip install google-genai` |

---

## 5. The one thing to be careful about: disclosure

The patent note (`docs/invention_note.md`) is **pre-filing and confidential**.
Until the provisional application is filed:

- **Do not show `docs/invention_note.md` on screen** and do not read from it.
- **Do not publish anything**: no repo, no video, no slides, no report upload,
  no public link, no LinkedIn post, no paper.
- If you are asked to share a demo video or the report after the review, say
  it can be shared privately with the repo still private.
- If anything is shown to anyone outside the three of us — a mentor from a
  company, a recording that gets uploaded, a published report — it goes in
  the disclosure log at the top of §9.2 of the invention note, the same day.
- A final review inside the classroom, with no recording and no publication,
  is not a public disclosure. A review that is recorded and shared widely is.
  **If you are not sure which one tomorrow's is, treat it as the risky kind
  and do not hand out the mechanism.**

---

## 6. Ownership — know this cold

| person | owns | one line |
|---|---|---|
| **A** | `db.py`, `extract.py`, `app.py`, the upload flow, the database | *"the backend: PDF to clauses into SQLite"* |
| **B** | `clauses.py`, `adapter.py`, `graph.py`, `impact.py`, `api_b.py`, `llm.py`, the evaluation | *"the relationship model and the impact engine — and the measurement of it"* |
| **C** | `retrieval.py`, `reasoning.py`, `templates/`, `data/authorities.json`, the screen | *"the legal retrieval, the IRAC narrative, and the screen"* |

**The seam, if someone asks how we avoided merge hell:** `adapter.py` is the
only file that touches A's `db.py`, so when A changes the schema only
`adapter.py` changes. And `server.py` mounts A's `app.py` and B's router
without editing either file.

---

## 7. The numbers, in case they are quoted back at you

- 81 clauses hand-annotated (59 in Contract A, 22 in Contract B)
- 10 metric families; every annotated item reproduced exactly
- 7 change scenarios for impact + chains; 9 in total with the SLA change
- 9 doubt codes, every doubt carrying clause ids and quoted text
- 92 automated tests, green from a clean clone
- 4 real defects found by the evaluation harness and fixed
- 2 synthetic contracts, no real company contract used

**Say this with the caveat attached:** self-consistency against hand
annotations on synthetic documents — a regression guard, not external
validity.

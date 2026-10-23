# Decisions and Open Questions — Contract Intelligence

**Status:** Draft architecture decision record for review. This document does not authorize implementation or production deployment.

**Scope:** Phase 1 planning for `revengg-demo`.

**Basis:** The approved Phase 1 architecture decisions, the Phase 0 source-level audit, and the repository documentation reviewed during planning. No application code, database, test suite, server, or runtime behavior was inspected or executed for this document. Statements about existing behavior are therefore descriptions of documentation and audit evidence, not independent verification.

---

## 1. What we are building, and for whom

We are building a **contract intelligence application for lawyers and law firms**. It is intended to help a lawyer understand a contract as a structure of linked obligations, identify inconsistencies and dependencies, and reason about the consequences of a proposed change.

The product is not intended to be a generic chatbot or a clause-summarisation tool. Its core value is:

- evidence-grounded answers linked to the original contract text;
- a deterministic contract graph and dependency traversal;
- explicit uncertainty and limitations;
- change-impact analysis that distinguishes a real consequence from a reference, dependency, or wording difference;
- separate treatment of synthetic demo material and confidential client documents.

The initial release is aimed at legal professionals working with text-selectable PDFs. It is not a replacement for legal judgement, and it must not present unverified authorities or unsupported conclusions as verified fact.

---

## 2. Agreed architecture

### 2.1 Modular monolith

We will keep one deployable application with clear internal module boundaries rather than splitting into microservices. The application contains:

- a browser-facing UI;
- a canonical HTTP API;
- authentication and firm membership handling;
- server-side authorization and tenant context;
- domain services for contracts, ingestion, graph construction, calculations, conflicts, and explanations;
- repositories that require tenant context;
- background workers for extraction, graph builds, analysis, deletion, and retention;
- PostgreSQL for production metadata;
- private object storage for original documents and derived artifacts;
- append-only audit logging;
- an optional, disabled-by-default external AI gateway.

The existing deterministic graph, impact, and conflict engines should be preserved behind service boundaries where they are sound. They are not to be replaced by an LLM. The LLM is not part of the deterministic reasoning path.

### 2.2 Production and local storage

- **Production:** PostgreSQL with a shared schema, `firm_id` tenant isolation, composite constraints, and row-level security (RLS).
- **Local development:** SQLite may be used for development and tests.
- **Demo:** SQLite may be used for a read-only synthetic demo dataset.
- SQLite is never production multi-tenant storage.

### 2.3 One application entry point

A single canonical application factory and route registry should define the API. Existing entry points such as `app.py` and `server.py` should become thin bootstraps or be reconciled, not maintain separate route sets. This prevents authentication, authorization, or behaviour from differing depending on how the application was started.

The existing route divergence and the reported forced use of `demo.db` are Phase 0 audit findings. They must be verified before changes are made.

### 2.4 Trust boundaries

- The browser is untrusted; hiding a UI control is not authorization.
- The API establishes authentication and tenant context.
- Repositories and the database enforce tenant scope as defence in depth.
- Workers are trusted only for the specific signed job they receive; they must re-establish authorization.
- Object storage is private; documents are served through authorized, short-lived access.
- External AI is a separate trust boundary and the only approved path for sending contract content outside the application.

---

## 3. Firms, users, roles, and authorization

### 3.1 Firm isolation

A **firm** is the tenant. Contracts, documents, versions, clauses, graph builds, analyses, audit events, and firm settings belong to a firm. A user may belong to more than one firm through memberships.

The server derives the active firm from the authenticated session and a valid membership. A client-supplied `firm_id` is never authoritative. The firm switcher only chooses among memberships the server has already authorized.

### 3.2 Roles

Initial roles are:

- **Firm administrator** — manages members, retention, legal holds, audit access, and AI configuration.
- **Lawyer** — creates contracts, uploads documents, runs analyses, and reads explanations.
- **Paralegal** — uploads documents and supports analysis, without administrative control.
- **Reviewer** — reviews findings and explanations, without upload, deletion, or administrative rights.
- **Viewer** — read-only access to authorized contracts and analyses.

The detailed permission matrix is in the Phase 1 architecture and security specification. Any change to roles, deletion, legal holds, audit access, retention, or AI configuration is an elevated operation requiring explicit authorization and audit.

### 3.3 Future matter-level access

The first release uses firm-level roles. The schema and authorization design must allow a later contract-level or matter-level access grant, so that a firm can restrict a sensitive contract to selected users or ethical walls. A firm role alone must not become a permanent assumption in the data model.

### 3.4 Safe denial behaviour

- A resource outside the user’s tenant is treated as not found (`404`), with no metadata leakage.
- A known same-tenant resource without permission returns `403`.
- Denied attempts are audited and rate-limited.
- Unauthorized access must never return partial data.

---

## 4. Demo data, uploaded contracts, and legacy records

### 4.1 Three distinct categories

1. **Synthetic demo data** — system-owned, read-only, clearly labelled, and never mixed with client documents.
2. **User-uploaded contracts** — owned by a firm, with provenance and access controls.
3. **Legacy unclassified data** — records whose original source and ownership cannot be established.

### 4.2 No silent mixing or reclassification

- Demo data is a separate dataset or system tenant and is read-only.
- Uploads cannot write into the demo dataset.
- Legacy records are marked `legacy_unclassified` and quarantined pending human review.
- Quarantined records are not silently assigned to the demo dataset or to a firm.
- Reclassification requires an identified reviewer, evidence, a reason, and an audit record.
- Explicit comparison between demo and user data may be added later, with permissions and clear labels.

---

## 5. Document ingestion, clauses, graph, conflicts, and explanations

### 5.1 Ingestion

- Initial support is limited to **text-selectable PDFs**.
- Scanned PDFs, encrypted PDFs, malformed PDFs, empty files, unsupported types, and oversized files are rejected with clear, actionable errors.
- OCR is out of scope for the initial release.
- A partially extracted PDF may proceed only with visible warnings and limited analysis.
- Extraction failure must never be reported as a successful import.
- The original file, its hash, storage reference, uploader, source kind, extraction version, warnings, and lifecycle state are preserved.
- The existing uploaded-PDF path is documented as producing section-level clauses rather than the full sub-clause hierarchy; that is a known limitation to address as proposed work, not a completed capability.

### 5.2 Contract versions and clauses

- A contract has immutable versions.
- A version references one source document and one extraction artifact.
- Clauses preserve raw text, hierarchy, page numbers and character offsets, splitting version, and extraction confidence or warnings.
- Later edits create new versions rather than mutating a reviewed version.

### 5.3 Graph construction

- The deterministic graph is preserved.
- A graph build is version-scoped and built from one immutable contract version and one clause-splitting version.
- Builds are staged, validated, and published atomically.
- Only a published graph is queryable.
- A failed build leaves the prior published graph intact and reports a failure; a partial graph must never be treated as complete.
- Graph nodes and edges must not cross tenant, contract, or version boundaries.
- The existing graph algorithm is not redesigned in this phase.

### 5.4 Conflict and change analysis

The product supports three scopes over time:

1. conflicts and inconsistencies within one contract;
2. changes and impacts across contract versions;
3. potential conflicts between explicitly selected contracts.

Cross-contract comparison is designed for but deferred. Every selected contract must be authorized.

The analysis must distinguish:

- `conflict` and `inconsistency`;
- `dependency`;
- `cross_reference`;
- `textual_difference`;
- `review_warning`;
- `unsupported`.

Only genuine conflicts or inconsistencies may be presented as conflicts, and they require source evidence. A textual difference is not automatically a legal consequence.

Every requested analysis returns an explicit status: `supported`, `partially_supported`, or `unsupported`. The current deterministic engine is documented as mainly covering payment-period and SLA counterfactuals; other categories must be reported as partial or unsupported until rules and tests exist.

### 5.5 Clause explanation

The explanation must include:

- the complete original clause text, not a truncated substitute;
- page or character provenance where available;
- plain-language explanation;
- rights, obligations, conditions, exceptions, and deadlines where supported;
- linked clauses and graph context;
- evidence and uncertainty;
- a clear distinction between source text, extracted facts, deterministic findings, and any AI-generated interpretation.

---

## 6. Calculations and stale inputs

The calculation design is deterministic and evidence-bound.

- **Server-authoritative values:** the server reads the current clause value; a client cannot assert the old value.
- **Version binding:** every analysis is tied to a specific contract version and clause.
- **Stale-input rejection:** a proposed change includes an expected-value hash or revision. A mismatch returns a conflict and requires review.
- **Typed values:** decimals, ISO currencies, explicit percentages, duration units, and defined calendar conventions. No binary floating point for legal amounts.
- **No unsupported conversion:** currency conversion requires an identified rate source and date. Without it, the result is partial or unsupported.
- **Dependency resolution:** definitions, schedules, and cross-references must be resolved or the result is downgraded.
- **Evidence and assumptions:** each derived value carries its source span, rule version, assumptions, dependencies, and calculation trace.
- **Fingerprints:** analysis results carry an input fingerprint covering tenant, contract, version, clause hashes, graph build, proposed change, rules, and engine version.
- **Invalidation:** changed inputs supersede prior results; stale results must not be displayed as current.
- **Human review:** ambiguous values, missing calendars, unresolved references, and cross-contract effects require review.
- **AI exclusion:** AI is never an authority for calculation.

Calculation conventions still requiring approval include jurisdictions, business-day calendars, month-end behaviour, rounding, and whether currency conversion is in scope.

---

## 7. AI policy

- AI is **disabled by default**.
- External AI may be enabled only after the firm, provider, privacy, consent, evidence-validation, and data-handling requirements are approved.
- AI is optional. Deterministic ingestion, graph construction, calculations, conflicts, and explanations must work without it.
- AI output must be labelled and must never overwrite source text, extracted facts, deterministic findings, or verified authorities.
- AI claims require citations to authorized source spans. Unsupported claims are withheld or marked unsupported.
- Contract text is untrusted content and must not be treated as instructions. Prompt-injection handling is mandatory.
- No cross-tenant retrieval, shared cache, or shared embeddings.
- Only the minimum necessary clause context may be sent, never a whole-corpus transfer by default.
- If AI is disabled, unavailable, policy-blocked, or returns unsupported claims, the deterministic explanation remains available with a clear notice.
- No AI provider or vendor has been selected or approved.

---

## 8. Retention, legal holds, deletion, audit, and backups

- Retention is configurable at firm level, subject to legal and contractual requirements.
- Legal holds override ordinary deletion.
- Deletion is authorized, audited, and executed as a controlled workflow across metadata, objects, derived artifacts, search indexes, caches, and AI request data.
- An audit tombstone remains without sensitive clause text.
- Access to documents, exports, analyses, membership changes, deletion, legal holds, and AI requests is auditable.
- Audit records are append-only and access-restricted.
- Backups are encrypted and restored with tenant isolation.
- Deleted data must not be reintroduced by a routine backup restore; the exact backup-deletion timeline remains an open decision.
- Recovery objectives, restore ownership, and incident-response responsibilities remain open.

---

## 9. Existing functionality versus proposed work

### 9.1 Existing functionality and documentation

The repository documentation describes:

- a deterministic graph of clause nodes and typed edges;
- clause splitting and an 18-type clause vocabulary;
- a typed relation table and reverse-dependency traversal;
- impact analysis with direct/indirect hops and causal chains;
- uncertainty/doubt records with clause evidence;
- payment-period and SLA counterfactual arithmetic;
- legal-authority retrieval and an explanation layer;
- an optional LLM helper used only to fill graph gaps;
- FastAPI routes and a terminal interface;
- synthetic contracts and an evaluation harness with hand-annotated ground truth.

These are described as existing capabilities. They have not been re-verified in this planning step.

### 9.2 Proposed work

The Phase 1 work includes:

- canonical app factory and route registry;
- authentication, firm memberships, roles, and server-side authorization;
- PostgreSQL tenancy and RLS;
- provenance, immutable versions, and legacy quarantine;
- PDF failure states, malware controls, and partial-extraction warnings;
- staged graph construction and atomic publication;
- typed calculation values, stale-input rejection, and evidence contracts;
- three separate user-facing workspaces;
- conflict taxonomy and version comparison;
- retention, legal holds, deletion, audit, and backup controls;
- optional AI gateway and approval gates;
- security and legal-reasoning release verification.

### 9.3 Documentation and test-claim caveat

The repository documentation contains conflicting test-count claims. The README reports 90/90 tests; the architecture walkthrough reports 112 tests. The evaluation report records a generated run from 2026-10-20, but that is not evidence that the current repository passes today. No tests were run for this document. The discrepancy must be reconciled during repository verification before any release claim.

The Phase 0 audit also reports a forced `demo.db`, possible route divergence, unvalidated old impact values, numeric/unit risks, and incremental graph state. These are audit findings, not runtime-confirmed facts.

---

## 10. Agreed decisions

| ID | Decision | Status |
|---|---|---|
| D1 | Build a modular monolith, not a microservice rewrite. | Agreed |
| D2 | PostgreSQL shared schema with `firm_id` and RLS in production; SQLite only for local development and read-only demo. | Agreed |
| D3 | Users may belong to multiple firms; firm switcher; firm-level roles now; matter-level access designed for later. | Agreed |
| D4 | Legacy unknown-provenance records are quarantined as `legacy_unclassified` and reviewed before reclassification. | Agreed |
| D5 | Support configurable retention, audited deletion, legal holds, and documented backup-deletion timelines. | Agreed |
| D6 | Text-selectable PDFs only initially; reject scanned, encrypted, and malformed PDFs; partial extraction only with warnings and limited analysis. | Agreed |
| D7 | Use decimal arithmetic, explicit units and currencies, and defined calendar conventions. | Agreed |
| D8 | Distinguish conflicts, inconsistencies, dependencies, cross-references, textual differences, and review warnings; evidence required for conflict claims. | Agreed |
| D9 | Design for cross-contract comparison but defer delivery. | Agreed |
| D10 | AI disabled by default; external AI only after provider, privacy, consent, evidence, and data-handling approval. | Agreed |
| D11 | Unverified legal authorities are never presented as verified; legal-review ownership is required. | Agreed |
| D12 | Maintain README and architecture documentation; test claims require actual execution evidence. | Agreed |

---

## 11. Open questions

No named approvers or vendors have been recorded. The owner column identifies the role that must decide; the actual person or committee must be assigned before the blocking stage.

| ID | Open question | Why it matters | Decision owner | Blocks |
|---|---|---|---|---|
| OQ-01 | Which hosting environment, data region, PostgreSQL service, object storage, queue, and key-management service will be used? | Determines encryption, residency, backup, network, and operational controls. | Hosting/platform lead with security | Production deployment and Stage 2 |
| OQ-02 | Which identity provider, session mechanism, MFA policy, and optional SSO arrangements apply? | Authentication is a prerequisite for firm membership and authorization. | Security lead with product | Stage 2 |
| OQ-03 | What are the default and maximum retention periods, legal-hold authority, deletion SLA, and backup-deletion timeline? | Directly affects legal compliance and irreversible deletion. | Legal counsel with security | Stages 2 and 9 |
| OQ-04 | What PDF size/page limits, malware-scanning approach, encrypted-PDF policy, and partial-extraction acceptance rules apply? | Determines safe ingestion and user-facing failure behaviour. | Product with security | Stage 3 |
| OQ-05 | Which jurisdictions, business-day calendars, month-end conventions, rounding rules, and currency-conversion policy apply? | Prevents incorrect deadlines and financial calculations. | Product with legal | Stage 6 |
| OQ-06 | Are matter-level access restrictions or ethical walls required in the first release? | Affects authorization schema and contract-level access checks. | Legal with product | Stage 2 schema |
| OQ-07 | What are the incident-response roles, breach-notification process, RPO/RTO, and restore ownership? | Required for security operations and recovery claims. | Security with hosting/legal | Stage 9 and release |
| OQ-08 | Is external AI permitted at all? If so, which provider category, privacy terms, region, consent model, redaction, and data-retention terms are acceptable? | AI egress is a high-impact confidentiality decision. | Legal, security, product | Stage 10 |
| OQ-09 | Who owns legal-authority verification, and what is the publication/quarantine policy? | Prevents unverified authorities from being presented as verified. | Legal counsel | Release |
| OQ-10 | When should cross-contract comparison be released, and what explicit-selection and permission rules apply? | Avoids overclaiming and cross-tenant exposure. | Product with legal/security | Stage 8 |
| OQ-11 | Who reviews and classifies `legacy_unclassified` records, and what evidence is required? | Prevents silent data reassignment. | Product with legal/records owner | Stage 2 data migration |
| OQ-12 | Which documentation and test claims are canonical, and what is the authorized test baseline? | Resolves the 90/90 versus 112-test discrepancy and release evidence. | Engineering lead | Stage 1 and release |
| OQ-13 | What audit-retention period, access-review cadence, and export policy apply? | Audit evidence and privacy obligations depend on it. | Security with legal | Stage 9 |
| OQ-14 | What legal disclaimers and user acknowledgments are required? | The product must not present itself as legal advice. | Legal counsel | Release |
| OQ-15 | What is the approach for migrating existing local data into PostgreSQL without losing or misclassifying records? | Prevents data loss and demo/client mixing. | Engineering with product/legal | Stages 1–2 |

---

## 12. What this document does not decide

This document does not select:

- a cloud provider, hosting region, database vendor, identity provider, object-storage vendor, queue, malware scanner, monitoring vendor, or AI provider;
- retention periods, backup-deletion timelines, RPO/RTO, or breach-notification deadlines;
- jurisdiction-specific calculation rules;
- whether external AI is permitted;
- whether matter-level access is required at launch;
- legal disclaimers or client-consent language;
- a production release date.

Those remain approval items, not implicit decisions.

---

## 13. Change control

- This decision record should be updated when an open question is approved.
- Approval must name the decision owner and date.
- Implementation must not treat an open question as resolved by convenience or by a local prototype.
- Any change to the agreed decisions should record the reason and affected stages.
- No production claim may rely on local behaviour, documentation assertions, or unexecuted tests.

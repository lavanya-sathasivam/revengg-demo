# Implementation Checklist — Contract Intelligence

**Status:** Planning baseline. This is not an implementation authorization and does not claim that any test, migration, deployment, or security control has been executed.

**Priority definitions**

- **P0 — release blocker:** required before any production or client-data use.
- **P1 — core functionality:** required for the initial product, but may follow P0 foundations.
- **P2 — later enhancement:** explicitly deferred or dependent on a separate policy/product decision.

**Environment labels**

- **Local/CI:** can be developed and verified without production infrastructure.
- **Deployment decision:** requires an approved hosting, identity, storage, queue, or key-management choice.
- **Policy/legal decision:** requires an approved retention, privacy, AI, or legal-content decision.
- **Production gate:** requires completed security, legal, and operational evidence.

---

## Existing functionality versus proposed work

### Described as existing in repository documentation

The repository documentation describes a deterministic contract pipeline with clause splitting, an 18-type clause vocabulary, typed graph edges, reverse-dependency traversal, impact reports, doubt records, payment/SLA counterfactuals, legal-authority retrieval, an explanation layer, FastAPI routes, a terminal interface, synthetic contracts, and an evaluation harness.

These are **documented capabilities, not re-verified in this planning step**. The Phase 0 audit also reports forced use of `demo.db`, possible route divergence, unvalidated old values, numeric/unit risks, and incremental graph state. Those are audit findings, not runtime-confirmed facts.

### Proposed work

- Tenant-aware PostgreSQL schema, RLS, authentication, memberships, and authorization.
- Canonical application factory and route registry.
- Provenance, immutable versions, legacy quarantine, and demo separation.
- PDF failure states, malware/resource controls, and partial-extraction warnings.
- Staged graph construction and atomic publication.
- Typed calculation values, stale-input rejection, and evidence contracts.
- Three separate workspaces.
- Conflict taxonomy, version comparison, and deferred cross-contract comparison.
- Retention, legal holds, deletion, audit, backup, and recovery.
- Optional AI gateway and approval gates.
- Security and legal-reasoning release verification.

### Baseline caveat

No tests have been run for this checklist. The README reports 90/90 tests; the architecture walkthrough reports 112 tests; the evaluation report records a generated run. These claims conflict and must be reconciled with an authorized execution baseline. Existing deterministic graph and reasoning logic should be preserved where sound, not replaced by an LLM.

---

## Stage 1 — Repository and test-baseline verification

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| BL-01 | P0 | **Read-only repository inventory.** Map entry points, route sets, database files, storage paths, modules, data models, and documented capabilities. Expected outcome: a current-state inventory with unknown items explicitly labelled. | None | Inventory names every entry point, route, DB file, test suite, and relevant module; discrepancies and unknowns are listed; no code changes made. | Identifies where confidential data and authorization boundaries are missing; avoids accidental disclosure during inventory. | Local/CI |
| BL-02 | P0 | **Executable test and documentation baseline.** In an authorized phase, execute the existing test suites and record exact commands, environment, date, and results. Reconcile README, architecture walkthrough, and evaluation-report claims. | BL-01 | A dated test report exists; every pass/fail/error is recorded; documentation claims match evidence or are marked unverified; no unexecuted claim is presented as passing. | Prevents false assurance about tenant isolation, graph correctness, or legal reasoning. | Local/CI |
| BL-03 | P0 | **Preservation map for deterministic engines.** Identify the graph, impact, conflict, retrieval, and reasoning components that must survive behind service boundaries. Expected outcome: an interface map and “do not replace” list. | BL-01 | Graph, impact, conflict, and explanation engines each have a named boundary, inputs, outputs, and owners; no algorithm rewrite is proposed. | Reduces risk of replacing deterministic, auditable logic with opaque AI; clarifies data access. | Local/CI |

---

## Stage 2 — Security foundations

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| SF-01 | P0 | **Canonical application factory and route registry.** Make `app.py` and `server.py` thin bootstraps or reconcile them into one registry. Expected outcome: one authoritative route set and middleware order. | BL-01 | Both entry points expose an identical route manifest; live HTTP route-parity tests must pass when executed; auth, 404, and 405 behaviour match; no duplicate route definitions. | Prevents auth or behaviour bypass through an alternate entry point. | Local/CI |
| SF-02 | P0 | **Authentication and secure sessions.** Add login/logout, session rotation, CSRF protection, MFA for elevated roles, and optional SSO design. Expected outcome: verified user identity without authorizing firm data. | SF-01; OQ-02 | Unauthenticated requests cannot access protected resources; session theft/CSRF tests must pass; MFA and revocation policy is documented; no production identity provider is invented. | High: account takeover exposes all firm data. Requires approved identity provider and session policy. | Local/CI with test provider; production requires deployment decision |
| SF-03 | P0 | **Firms, memberships, and roles.** Implement user-to-firm memberships, firm switcher, role assignments, last-admin protection, and audited membership changes. Expected outcome: server-derived active firm and role. | SF-02 | A user sees only authorized firms; role matrix is enforced server-side; self-elevation, removal of the last administrator, and cross-firm membership manipulation are denied and audited. | Prevents privilege escalation and unauthorized firm access. | Local/CI; production requires policy decision |
| SF-04 | P0 | **Tenant-scoped repositories and PostgreSQL RLS.** Require `firm_id` context on every tenant-owned query; add composite constraints, least-privilege database roles, and RLS. Expected outcome: tenant isolation is enforced at application and database layers. | SF-03; OQ-01 | Direct database tests under tenant roles cannot read or write another firm’s rows; repository calls without tenant context fail closed; no application superuser; RLS coverage is documented. | Critical: RLS bypass or missing filters causes cross-firm disclosure. | Deployment decision |
| SF-05 | P0 | **Demo and uploaded-data separation.** Make the synthetic demo dataset read-only; route uploads only to firm datasets; quarantine legacy records as `legacy_unclassified`. Expected outcome: no silent mixing. | SF-04; OQ-11 | Upload tests leave demo row counts and hashes unchanged; unknown-provenance records are quarantined; no record is reclassified without reviewer, reason, and audit. | High: prevents client data entering the demo and prevents unknown records being mislabelled. | Local/CI; migration requires policy decision |
| SF-06 | P0 | **Authorization and audit security harness.** Build two-firm fixtures and test nested resources, IDOR, export, search, and job-context paths. Expected outcome: repeatable security regression suite. | SF-03; SF-04 | Cross-tenant and insufficient-role tests fail closed; every denial is audited; the harness runs in CI when authorized. | Provides evidence for tenant isolation claims; itself must not contain real client data. | Local/CI |

---

## Stage 3 — Secure document ingestion and provenance

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| IN-01 | P0 | **Upload intake and provenance.** Record original filename, MIME type, size, SHA-256, uploader, source kind, storage reference, and lifecycle state. Store originals privately. Expected outcome: every document is traceable. | SF-04; OQ-01; OQ-04 | Every document has all provenance fields; storage keys are tenant-prefixed and not client-controlled; duplicate uploads are detected or linked; access requires authorization. | High: original documents contain confidential and privileged material; storage must be private. | Local/CI with local storage adapter; production requires deployment decision |
| IN-02 | P0 | **PDF validation and extraction state machine.** Accept text-selectable PDFs; reject scanned, encrypted, malformed, empty, oversized, and unsupported files with stable error codes. Partial extraction proceeds only with visible warnings. Expected outcome: no false successful imports. | IN-01 | Fixtures for each failure case produce the correct state and actionable message; extraction failure never yields `ready`; valid text PDFs proceed; partial extraction records warnings and limits analysis. | Malformed or malicious files can exhaust resources or exploit parsers. Parser must be sandboxed. | Local/CI; parser/provider choice requires deployment decision |
| IN-03 | P0 | **Malware and resource-abuse controls.** Add size/page limits, MIME/magic checks, malware scanning, parser timeouts, memory limits, and no worker network egress. Expected outcome: hostile PDFs are contained. | IN-02; OQ-04 | Oversized, zip-bomb-like, malformed, and network-seeking fixtures are rejected or contained; worker timeouts are recorded; no external fetch occurs. | Critical availability and supply-chain boundary. | Deployment decision |
| IN-04 | P0 | **Ingestion retry, audit, and error taxonomy.** Make retries idempotent, record attempts, preserve prior state, and expose actionable errors. Expected outcome: failed jobs are recoverable without duplicate clauses or false success. | IN-02 | Retrying the same document does not duplicate derived artifacts; every state transition is audited; errors are stable and user-facing; no partial import is marked complete. | Audit evidence must not contain full clause text; failures must not leak stack traces. | Local/CI |

---

## Stage 4 — Versioned contracts and clause extraction

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| CV-01 | P0 | **Immutable contract versions.** Separate contract metadata from version snapshots; link each version to one source document and one extracted-text artifact. Expected outcome: analysis can bind to an exact version. | SF-04; IN-01 | Versions are immutable after publication; current-version pointer is valid and tenant-scoped; unique version numbering is enforced; edits create a new version. | Prevents analysis of an unintended document; supports audit and deletion. | Local/CI |
| CV-02 | P0 | **Clause extraction and source offsets.** Preserve raw text, hierarchy, page numbers, character offsets, splitting version, and confidence/warnings. Preserve the existing splitter where sound. Expected outcome: every clause is traceable to source. | CV-01 | Every clause has version, raw text, hierarchy, and offsets; section and sub-clause numbering are preserved where supported; extraction warnings are recorded. | Confidential text must remain tenant-scoped; offsets support evidence review. | Local/CI |
| CV-03 | P0 | **Extraction uncertainty and review.** Flag ambiguous, merged, split, or unclassified text; never silently discard unassigned content. Expected outcome: users can see what was not confidently parsed. | CV-02 | Unclassified text has a visible review state; ambiguous splits are flagged; no source text is silently dropped; review decisions are audited. | Helps prevent unsupported legal conclusions and preserves evidence. | Local/CI |
| CV-04 | P1 | **Improve uploaded-PDF clause hierarchy.** Address the documented limitation that uploaded PDFs may yield section-level rather than full sub-clause hierarchy. Expected outcome: hierarchy is created where numbering permits; fallback is explicit. | CV-02; IN-02 | Uploaded text-selectable PDF with recognizable numbering produces sub-clauses; unsupported numbering yields a clear warning and section-level fallback. | Incorrect hierarchy can misidentify the changed clause; warnings must be visible. | Local/CI |

---

## Stage 5 — Atomic graph construction and evidence-backed reasoning

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| GR-01 | P0 | **Staged graph builds and atomic publication.** Build a graph from one immutable version and one clause-splitting version; publish only after validation. Expected outcome: no partial graph is queryable. | CV-02; SF-04 | Injected build failure leaves the prior published graph available; no read returns a building or failed graph; publication is atomic. | Prevents false completeness and unsupported legal conclusions. | Local/CI |
| GR-02 | P0 | **Graph validation, recovery, and idempotency.** Validate references, counts, schema, and tenant/version isolation; make rebuilds deterministic. Expected outcome: repeatable and recoverable builds. | GR-01 | Validation rejects dangling or cross-tenant edges; retry does not duplicate nodes/edges; identical input yields a matching build hash; failed builds preserve diagnostics. | Cross-tenant or dangling edges would be a serious integrity and disclosure defect. | Local/CI |
| GR-03 | P1 | **Graph-to-evidence links.** Ensure every node and edge references an authorized clause or source span. Expected outcome: explanation and impact findings can show their graph path and source. | GR-01 | Every graph element has a resolvable clause/version reference; API responses include evidence identifiers; no orphan element is presented as supported. | Evidence must not expose another tenant’s clause. | Local/CI |
| GR-04 | P1 | **Published-graph query boundary.** Expose graph reads only through authorized, published-only service APIs. Expected outcome: clients cannot query staging state. | GR-02; SF-06 | Unauthorized and unpublished reads are denied; graph responses are tenant-scoped; API tests cover nested IDs. | Direct graph access could bypass contract permissions. | Local/CI |

---

## Stage 6 — Safe calculations and change-impact analysis

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| CA-01 | P0 | **Typed value model and parsers.** Implement decimal money, ISO currencies, percentages, duration units, dates, and calendar conventions. Expected outcome: no binary floating point for legal amounts. | CV-01; OQ-05 | Decimal, currency, percentage, duration, business-day, and month-end tests must pass when executed; ambiguous values return partial/unsupported; no float arithmetic. | Incorrect amounts or deadlines can cause legal and financial harm. | Local/CI; conventions require policy/legal decision |
| CA-02 | P0 | **Server-authoritative clause matching and stale-input rejection.** Require the server to read the current clause value; bind analysis to expected-value fingerprints. Expected outcome: old or mismatched values cannot be analysed silently. | CA-01 | A mismatched expected value returns a conflict; changed clause, version, or graph invalidates the prior result; identical requests are idempotent. | Prevents analysis of the wrong clause or stale legal position. | Local/CI |
| CA-03 | P0 | **Impact status and evidence contract.** Every analysis returns `supported`, `partially_supported`, or `unsupported`, with evidence, assumptions, dependencies, rule version, and calculation trace. Expected outcome: no overclaiming. | CA-02; GR-03 | Every response has a status and evidence; unsupported findings are not rendered as conflicts; assumptions are visible; currency/unit ambiguity downgrades the result. | Transparency is a legal-safety control, not just UX. | Local/CI |
| CA-04 | P1 | **Controlled coverage expansion.** Preserve existing payment/SLA deterministic calculations; add other categories only with explicit rules and tests. Expected outcome: coverage grows without advertising unsupported analysis. | CA-03 | Each added category has a rule specification, status mapping, and failure tests; categories without rules remain unsupported/partial. | Avoids fabricated consequences for liability, renewal, definitions, or obligations. | Local/CI |

---

## Stage 7 — Three user-facing pages

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| UI-01 | P1 | **Shared application shell.** Add authenticated navigation, firm switcher, contract context, processing status, evidence drawer, permission-aware actions, and error states. Expected outcome: one consistent authorized workspace. | SF-03; SF-01 | Navigation preserves active firm/contract context; unauthorized actions are disabled and server-denied; processing and error states are visible. | UI hiding is not authorization; every action still requires server checks. | Local/CI |
| UI-02 | P1 | **Contract Intelligence page.** Support upload, metadata overview, provenance, processing status, and clause hierarchy. Expected outcome: users can see what was ingested and how confident the extraction is. | IN-01; IN-02; CV-02; UI-01 | Upload exists only here; provenance, warnings, and version status are visible; no impact analysis is initiated from this page by default. | Displays confidential metadata and original text; access must be permission-checked. | Local/CI |
| UI-03 | P1 | **Conflict & Change Impact page.** Present within-contract analysis, proposed changes, version comparisons, evidence, status, and limitations. Expected outcome: impact work is separated from upload. | CA-03; UI-01 | No upload control appears; every finding shows status and evidence; stale analysis is marked and not presented as current; unsupported categories are explicit. | Users may act on legal conclusions; evidence and uncertainty must be prominent. | Local/CI |
| UI-04 | P1 | **Clause Explanation page.** Show complete original clause text, offsets, plain-language explanation, rights/obligations/conditions/exceptions/deadlines, linked clauses, evidence, and uncertainty. Expected outcome: explanations are verifiable against source. | GR-03; UI-01 | The full clause text is available, not a truncated summary; evidence links resolve; AI-generated content is labelled if present. | Prevents source/interpretation confusion; protects privileged text. | Local/CI |

---

## Stage 8 — Conflict taxonomy, version comparisons, and cross-contract comparison

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| CF-01 | P1 | **Conflict taxonomy and evidence.** Implement distinct conflict, inconsistency, dependency, cross-reference, textual-difference, review-warning, and unsupported categories. Expected outcome: no catch-all “conflict” label. | CA-03 | Conflicts require source evidence; references and dependencies are not labelled conflicts; unsupported is visible; human review can challenge findings. | Mislabeling references as conflicts can mislead legal advice. | Local/CI |
| CF-02 | P1 | **Version comparison.** Align clauses across immutable versions, show textual and substantive differences, and attach impact status/evidence. Expected outcome: changes are traceable across versions. | CV-01; CA-03 | Diff identifies changed, added, removed, and unmatched clauses; impact findings carry status/evidence; permissions are checked on both versions. | Prevents comparing an unauthorized or unintended version. | Local/CI |
| CF-03 | P2 | **Cross-contract comparison — deferred.** Design explicit selection, authorization, and conflict/reference separation. Expected outcome: capability is designed but not advertised as delivered. | CF-02; SF-03 | Only explicitly selected authorized contracts are used; no cross-contract graph edges; conflicts and references are separated; feature remains disabled until approved. | High risk of cross-firm disclosure if selection or authorization is wrong. | Policy/legal decision; production gate |

---

## Stage 9 — Retention, legal holds, deletion, audit, backups, and recovery

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| DR-01 | P0 | **Retention, legal hold, and deletion workflow.** Support firm retention settings, legal holds, authorized deletion, soft delete, hard-delete execution, and audit tombstones. Expected outcome: deletion is controlled and auditable. | CV-01; SF-03; OQ-03 | Legal hold blocks deletion; authorized deletion removes metadata, objects, derived artifacts, indexes, and caches; tombstone remains; unauthorized deletion is denied. | Irreversible data loss or evidence destruction is a major legal risk. | Policy/legal decision |
| DR-02 | P0 | **Audit integrity and access review.** Capture authentication, authorization denials, document access, exports, membership changes, analysis runs, deletion, legal holds, and AI requests. Expected outcome: append-only, reviewable evidence. | SF-03; SF-06 | Audit records are append-only and access-restricted; tamper attempts are detectable; no sensitive clause text is logged; access-review process is defined. | Audit logs can themselves leak data if over-collected; access must be restricted. | Local/CI; production requires deployment decision |
| DR-03 | P0 | **Backup, restore, and recovery.** Define tenant-scoped backup and restore, deletion propagation, encryption, and recovery objectives. Expected outcome: restoration does not mix tenants or resurrect deleted data. | DR-01; OQ-01; OQ-07 | Restore tests show tenant isolation and no reintroduction of deleted records beyond policy; RPO/RTO and restore ownership are approved; restore is audited. | Backup restoration is a common cross-tenant and deletion-bypass failure point. | Deployment decision |

---

## Stage 10 — Optional AI gateway and approval gates

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| AI-01 | P2 | **AI gateway policy and consent.** Implement disabled-by-default gateway, firm/contract policy, user permission, consent record, kill switch, and deterministic fallback. Expected outcome: no AI call without policy approval. | UI-04; OQ-08 | AI disabled produces no external request; enabled calls require recorded policy/consent; fallback works when unavailable; no provider is selected by this task. | High confidentiality and regulatory risk; external processing must be explicitly approved. | Policy/legal decision |
| AI-02 | P2 | **Provider approval and egress control.** Define provider allowlist, DPA/privacy terms, data region, retention/no-training terms, and network egress restrictions. Expected outcome: only approved providers and minimum data are used. | AI-01; OQ-01; OQ-08 | Egress attempts outside the allowlist fail; provider approval is documented; only minimum necessary context is sent; no cross-tenant retrieval or shared cache. | Unauthorized egress can breach privilege and confidentiality. | Policy/legal/security decision |
| AI-03 | P2 | **Evidence validation and prompt-injection testing.** Validate citations against authorized source spans, label AI outputs, and red-team embedded instructions in contract text. Expected outcome: unsupported AI claims are withheld. | AI-02; CF-01 | Each AI claim has a verified source span or is marked unsupported; prompt-injection tests cannot retrieve cross-tenant data or bypass policy; AI output is labelled. | Prevents hallucinated legal claims and prompt-driven data exfiltration. | Local/CI for tests; production requires policy decision |

---

## Stage 11 — Security verification, legal-content review, and release readiness

| ID | Priority | Work item and scope | Depends on | Acceptance criteria | Security/privacy implications | Environment |
|---|---|---|---|---|---|---|
| RV-01 | P0 | **Security verification.** Execute the threat-register tests: tenant isolation, IDOR, RLS, job context, signed URLs, upload abuse, secrets, deletion, restore, concurrency, and AI egress. Expected outcome: evidence for each release gate. | All P0 items | No open P0 security finding; test evidence is dated and reproducible; failures are tracked, not waived silently. | Required before any client data or production use. | Production gate |
| RV-02 | P0 | **Legal-reasoning evaluation.** Build/execute a human-reviewed golden corpus covering conflicts, references, dependencies, version changes, explanations, and unsupported cases. Expected outcome: measured evidence, not claims. | CF-01; CF-02; UI-04 | Every conflict finding has verified source evidence; unsupported cases are correctly labelled; reviewers agree on taxonomy; metrics/thresholds are recorded. | Legal conclusions can cause harm if unsupported or mislabelled. | Local/CI plus legal review |
| RV-03 | P0 | **Documentation and release evidence.** Reconcile README, architecture docs, evaluation reports, and this checklist with actual results. Expected outcome: no unverified capability or test claim. | BL-02; RV-01; RV-02 | Every test/capability claim cites executed evidence or is marked unverified; known limitations are documented; release notes distinguish existing from proposed functionality. | Prevents misleading security, accuracy, or compliance claims. | Production gate |
| RV-04 | P0 | **Production readiness decision.** Confirm hosting, identity, retention, legal hold, backups, incident response, AI policy, and authority review. Expected outcome: explicit go/no-go. | RV-01; RV-02; RV-03; all open decisions | All §13 decisions are approved and recorded; no unresolved P0 blocker remains; launch scope is explicitly limited. | Final accountability gate for confidentiality, privilege, and legal risk. | Production gate |

---

## Files created or proposed for update

**Created in this planning step:**

- `docs/DECISIONS_AND_OPEN_QUESTIONS.md`
- `docs/IMPLEMENTATION_CHECKLIST.md`

Both target documents did not previously exist, so no existing content was overwritten.

**Proposed for later reconciliation, not changed here:**

- `README.md` — test-count and capability claims conflict with the architecture walkthrough and must be reconciled with an authorized test baseline.
- `docs/architecture_walkthrough.md` — reports a different test count and should be aligned with the canonical verification report.
- `docs/evaluation_report.md` — is a generated historical report; it should state its date and scope clearly and not be treated as a current test result.

---

## Top five implementation priorities

1. **Establish a trusted baseline.** Complete the repository inventory, execute the existing tests only when authorized, reconcile conflicting documentation, and map the deterministic engines that must be preserved.
2. **Make authorization and tenancy real.** Canonical app factory and route registry, authentication, firm memberships, server-side authorization, PostgreSQL RLS, and demo/upload separation.
3. **Make ingestion and provenance safe.** Private storage, immutable originals, provenance hashes, PDF rejection states, warnings, malware/resource controls, and legacy quarantine.
4. **Protect reasoning integrity.** Immutable versions, staged graph publication, server-authoritative clause matching, typed calculations, stale-input rejection, and explicit supported/partial/unsupported statuses.
5. **Build evidence-first user workflows.** Separate the three pages, expose full clause text and evidence, and only then add version comparison, conflict taxonomy, retention/audit operations, and optionally AI.

---

## Decisions that must be resolved before production deployment

- Hosting environment, data region, PostgreSQL, object storage, queue, and key-management service.
- Identity provider, session policy, MFA, and optional SSO.
- Retention periods, legal-hold authority, deletion SLA, and backup-deletion timeline.
- PDF size/page limits, malware scanning, encrypted-PDF policy, and partial-extraction acceptance.
- Calculation jurisdictions, calendars, rounding, and currency-conversion policy.
- Matter-level access restrictions or ethical walls.
- Incident response, breach notification, RPO/RTO, and restore ownership.
- Whether external AI is permitted, and if so, provider/privacy/consent/redaction terms.
- Legal-authority verification owner and publication policy.
- Cross-contract comparison release timing and selection rules.
- Legacy record classification reviewer and evidence standard.
- Canonical test/capability documentation and release evidence.
- Legal disclaimers and user acknowledgments.
- Migration approach from existing local data to production without data loss or misclassification.

No production readiness should be claimed until these decisions are approved and the P0 release gates have executed evidence.

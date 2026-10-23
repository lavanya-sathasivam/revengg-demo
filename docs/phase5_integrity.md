# Phase 5 Database Integrity, Provenance, and Audit

**Status:** Additive SQLite implementation for local/test use. This is **not** a production multi-tenant database, retention system, backup system, or legal-hold implementation.

**Scope:** The schema and service changes in `db.py`, `adapter.py`, `graph.py`, `api_a.py`, `auth.py`, `application.py`, `seed.py`, and `audit.py`.

**Approval note:** The additive schema and audit design have not been recorded as an approved architecture decision. The legal retention, legal-hold, backup-deletion, and audit-retention decisions remain open in `docs/DECISIONS_AND_OPEN_QUESTIONS.md` (OQ-03, OQ-07, OQ-13). This document describes implemented behaviour, not approval of those policies.

---

## 1. What this phase adds

The deterministic graph, impact, and reasoning engines are unchanged. Phase 5 adds evidence and lifecycle integrity around them:

- immutable contract-version records for new intake;
- source provenance: file hash, size, MIME type, path, extraction status, splitter version, source kind, and provenance class;
- a materialised current clause set plus an immutable `clause_history` copy of replaced clauses;
- atomic graph publication with a `graph_builds` record for every attempted build;
- failed builds that leave the last published graph intact;
- an append-only `audit_events` trail for security and lifecycle actions;
- read-only compatibility with pre-Phase-5 databases, without silently migrating or reclassifying them;
- foreign-key restrictions that prevent direct deletion of a contract with versions or clauses.

All changes are additive. `demo.db` is never migrated by this work or by demo-mode startup.

---

## 2. Data model

### 2.1 Contract identity and version

`contracts` remains the stable identity. New columns:

| Column | Meaning |
|---|---|
| `firm_id` | Owning firm, or `NULL` for the synthetic demo scope. |
| `current_version_id` | The version currently represented by the materialised clause set. |
| `current_graph_build_id` | The last successfully published graph build. |
| `provenance_status` | `verified`, `synthetic`, `unverified`, or `legacy_unclassified`. |

`contract_versions` stores one immutable snapshot per ingestion or explicit re-import:

- contract and tenant;
- version number, unique per contract;
- source filename, path, SHA-256, size, and MIME type;
- extracted-text SHA-256;
- extraction status and splitter version;
- provenance class and source kind (`upload`, `synthetic`, `programmatic`, or `legacy`);
- lifecycle status (`draft`, `extracted`, `published`, `superseded`, or `failed`);
- publication timestamp and creator.

A version is published when its clause set is saved. Replacing a published set creates a new version and marks the previous one `superseded`.

**Limitation:** published-version immutability is enforced by the repository/service path (`db.save_clauses` and `db._validate_explicit_version`). SQLite does not currently contain an update trigger that prevents a database owner from editing version columns directly. The append-only audit triggers have the same database-owner caveat.

### 2.2 Current clauses and history

`clauses` remains the materialised current set so existing graph, impact, and evidence readers do not change. It gains `version_id` and `clause_sha256`.

Before a current set is replaced, its rows are copied to `clause_history` with:

- the original clause id, contract, version, section number, text, metadata, and hash;
- archive timestamp and reason (`version_replaced` or `legacy_replaced`).

Replacing the same draft version does not create a history row. A published version is never overwritten through `save_clauses`; an explicit attempt raises `ValueError`.

### 2.3 Graph builds

`graph_builds` records:

- contract and version;
- build/algorithm version;
- status (`building`, `published`, or `failed`);
- start/finish timestamps;
- node and edge counts;
- a stable content hash that excludes SQLite row ids;
- error type and a bounded error message for failures.

`nodes.build_id` and `edges.build_id` identify the published build. The previous published graph remains queryable until a replacement transaction commits.

### 2.4 Audit events

`audit_events` is separate from the legacy `traces` activity log. It records:

- timestamp, firm, actor user, actor type, action, and outcome;
- target type/id and correlation id;
- sanitised JSON metadata.

Triggers reject `UPDATE` and `DELETE` on `audit_events`. `seed.reset_demo_tables` deliberately does not clear audit rows or reset their sequence.

Current audit actions include:

- `firm.created`, `user.created`, `membership.upserted`;
- `session.created`, `session.revoked`;
- `auth.denied`, `authorization.denied`, `firm.switch_denied`, `firm.switched`;
- `upload.denied`, `contract.access_denied`;
- `contract.created`, `contract.extraction_failed`;
- `clauses.persisted`, `clauses.persist_failed`;
- `graph.build_published`, `graph.build_failed`, `contract.graph_status_failed`.

The sanitizer redacts sensitive key names (text/content/token/secret/password/authorization/cookie/API key), token-like values, and truncates long strings. Audit metadata is not a copy of the contract.

**Limitations:** there is no audit viewer, export, retention job, access-review workflow, or role that can read audit events through the application. SQLite triggers are not tamper-proof against a database owner. Those decisions and controls remain open (OQ-13).

---

## 3. Transaction boundaries

The following operations are single SQLite transactions:

1. **Contract intake:** contract row, first version, optional audit event, and trace. On failure, none are left behind.
2. **Clause persistence:** version selection/creation, history archive, current-clause replacement, version publication/supersession, current-version pointer, and audit event.
3. **Graph publication:** `graph_builds` insert, old graph removal, new node/edge insertion, published build update, current-build pointer, and audit event.
4. **Graph failure recording:** failed-build diagnostics and audit event. The prior graph is not touched.
5. **Auth provisioning:** firm/user/membership/session mutation plus its audit event.

`adapter.replace_graph` assembles and validates the complete graph in memory before opening its write transaction. A validation error creates no build row and no partial graph. A SQLite error during insertion rolls back the delete, the build row, and all node/edge inserts; the previous graph remains.

`adapter.record_graph_failure` is a separate transaction because the original graph transaction has already rolled back. A failure to record diagnostics is logged and never replaces the original exception.

---

## 4. Provenance classes

| Class | Used for |
|---|---|
| `verified` | An uploaded source was hashed and its extraction/version recorded. |
| `synthetic` | Seeded demo contracts with known synthetic source. |
| `unverified` | Programmatic or test writes without a verified source. |
| `legacy_unclassified` | Rows that predate the integrity schema, or rows whose source cannot be established. |

Legacy rows are not assigned a firm, a version, or a guessed source. They remain readable in demo mode and are reported explicitly as `legacy_unclassified`. Reclassification requires a human decision and an audit record; it is not automatic (OQ-11/D4).

Uploaded contracts use the server-derived active firm. A version whose firm would differ from its contract's firm is rejected.

---

## 5. Read-only legacy compatibility

When the application runs in demo mode, the database is opened read-only with SQLite `mode=ro` and `query_only`. Read paths never run DDL. If old columns or tables are absent:

- missing `contracts.firm_id` permits demo reads only; firm-scoped reads fail closed;
- missing `current_version_id`/`provenance_status` return explicit defaults;
- missing version/history/build tables return empty results;
- missing `nodes.contract_id` makes node reads fail closed with `[]`;
- missing `edges.contract_id` is handled by scoping both endpoint nodes;
- `demo.db` bytes are not modified.

Running `db.init_db()` against a **writable** legacy database performs only additive migrations (`ALTER TABLE ... ADD COLUMN`, `CREATE TABLE IF NOT EXISTS`, indexes, triggers). It preserves existing rows and leaves them `legacy_unclassified`.

---

## 6. Migration implications

### From a pre-Phase-5 SQLite database

1. Stop the application and make an approved backup copy outside the repository (do not modify `demo.db`).
2. Point the writable upload/test configuration at the copy.
3. Run `py -3.11 -c "import adapter; adapter.init_db()"` or let the upload-mode lifespan run it.
4. Verify column/table additions and that old contracts remain `legacy_unclassified` with no version.
5. Do not bulk-backfill versions or assign firms without an approved, audited reclassification process.

There is no destructive down migration. Rolling back application code against an already-migrated database is not supported as a migration strategy; restore the pre-migration backup if the schema change must be undone.

### Existing upload rows

Phase 4 upload rows may have `firm_id IS NULL` and no version. They remain invisible to firm users and are reported as legacy/unclassified. They must not be silently attached to a firm or to the demo dataset.

### Production

This is SQLite-only and does not implement PostgreSQL, RLS, object storage, backup deletion, or retention. Those remain separate decisions (D2, OQ-01, OQ-03, OQ-07, OQ-13).

---

## 7. Retention, legal hold, deletion, and backups

**Not implemented in Phase 5.** No retention period, legal-hold authority, deletion SLA, backup-deletion timeline, or audit-retention period has been approved. This phase deliberately does not choose one.

What currently exists:

- foreign keys use `ON DELETE RESTRICT` for contracts, versions, clauses, and graph builds, so a direct `DELETE FROM contracts` cannot silently discard a contract that has versions or clauses;
- audit rows are append-only at the trigger level;
- failed extraction still records a version and audit event rather than reporting success.

What is missing and must be decided before any deletion feature:

- user-facing soft delete, hard delete, and audit tombstone;
- legal-hold capture, authority, and override rules;
- deletion across original files, derived artifacts, caches, indexes, and future object storage;
- deletion/retention execution on a schedule and its audit evidence;
- backup encryption, restore ownership, RPO/RTO, and backup-deletion propagation;
- audit access roles, export policy, retention, and legal discovery;
- reviewer identity and evidence requirements for legacy reclassification.

Until those decisions are approved, do not claim that deletion, legal hold, retention, or backup controls are implemented. Do not use direct SQL deletion as a production workflow.

---

## 8. Tests

The Phase 5 suite is `tests/test_phase5_integrity.py`. It covers:

- read-only legacy reads with no DDL, explicit legacy provenance, and fail-closed graph scoping;
- additive writable migration preserving rows;
- immutable published-version handling, clause history, and firm/contract version boundaries;
- provenance and no duplicated source text;
- atomic graph publication, forced mid-transaction failure, rollback, last-known-good graph preservation, and failed-build diagnostics;
- graph hash stability and graph-version ownership;
- audit sanitisation, truncation, append-only UPDATE/DELETE rejection;
- upload source hashes, MIME, size, splitter version, version publication, and lifecycle audit events;
- anonymous, role, and foreign-contract denial auditing;
- demo reset preserving audit history and sequence;
- foreign-key-restricted direct contract deletion.

Run:

```powershell
py -3.11 -m unittest discover -s tests -p "test_phase5_integrity.py" -v
py -3.11 -m unittest discover -s tests -v
git diff --check
```

The suite uses temporary databases and upload directories. It does not open `demo.db`, `app.db`, or real uploads.

---

## 9. Remaining risks and open decisions

- Approve or reject the additive schema design and audit scope as an architecture decision.
- Approve retention periods, legal-hold authority, deletion SLA, backup-deletion timeline, and audit retention/access (OQ-03, OQ-07, OQ-13).
- Add a database-level immutability trigger for published version columns if the service-level rule is insufficient.
- Add audit read/export authorization and tamper-evidence beyond SQLite triggers.
- Decide the legacy review and reclassification process (OQ-11).
- Implement PostgreSQL, RLS, encrypted backups, and object lifecycle only under a separate approved design.
- Keep the deterministic graph and impact engines free of LLM authority; graph build changes are persistence-only.

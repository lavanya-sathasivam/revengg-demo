# Phase 3 Reliable PDF Ingestion

**Status:** Implemented for local/test use. Phase 3 builds on the existing
Phase 5 versioning, Phase 6 upload validation, and the deterministic clause,
graph, impact, and reasoning engines. It does **not** replace them.

**Scope:** `ingest.py`, `normalize.py`, `extract.py`, `clauses.py`, `db.py`,
`api_a.py`, `api_b.py`, `templates/overview.html`,
`templates/explain.html`, and `tests/test_phase3_ingestion.py`.

**Decision record:** Duplicate suppression is scoped per firm by the SHA-256 of
the uploaded bytes. Failed extraction is not retryable because rejected bytes
are deliberately not retained. See `docs/phase6_upload_hardening.md` for the
upload limits and failure codes that still apply.

---

## 1. What this phase adds

Before Phase 3, an accepted PDF produced contract text, clauses, and a graph,
but the pipeline could not answer three operational questions: which page a
clause came from, whether a failed run could safely be retried, and whether the
same document had already been ingested. Phase 3 adds:

- page-aware extraction with exact character offsets into the stored text;
- conservative normalization stored beside, never instead of, the source text;
- sub-clause segmentation with explicit parent links;
- deterministic clause classification, including an `obligations` type;
- per-stage processing records and safe, targeted retries;
- duplicate suppression per firm;
- source page/offset evidence in the clause API and both frontend views.

All database changes are additive. Existing tables, APIs, demo data, and
uploads are left in place; no parallel clause table or alternate schema is
introduced.

---

## 2. Pipeline sequence

The upload route calls `ingest.py` for every step after staging:

```
validate upload (Phase 6)
  -> duplicate guard (SHA-256, per firm)
  -> extract   (extract.extract_pages)
  -> normalize (normalize.normalize_text, stored beside source)
  -> segment   (clauses.split_clauses with offsets and pages)
  -> classify  (clauses.classify_type per clause)
  -> persist   (db.save_clauses + db.link_clause_hierarchy)
  -> graph     (graph.build_graph)
```

| Stage | Where | What it guarantees |
|---|---|---|
| Duplicate | `ingest.find_duplicate_contract` | Same bytes in the same firm resolve to the existing contract; no second version or clause set is created. |
| Extract | `extract.extract_pages` | Text plus one record per page: `page_number`, `text`, `char_start`, `char_end`. Offsets address the returned text exactly, including after the document-level whitespace strip. |
| Normalize | `normalize.normalize_text` | Line endings, form feeds, in-line space runs, trailing whitespace, and page-break blank lines are cleaned. Numbering, headings, wording, and paragraph boundaries are not rewritten. |
| Segment | `clauses.split_clauses(..., preserve_text=True, include_offsets=True, pages=...)` | Numbered top-level sections and dotted sub-clauses, in source order, with `char_start`/`char_end`, `page_number`, and `metadata["parent_section"]`. Inconsistent or missing numbering returns `[]`; sections are never invented. |
| Classify | `clauses.classify_type` | Deterministic type from the heading first, then from specific body cues. Unmatched clauses stay `other`; uncertainty is preserved. |
| Persist | `db.save_clauses`, `db.link_clause_hierarchy` | Clauses are stored for one explicit `version_id`; `parent_id` is rebuilt from `parent_section`, and an unmatched parent remains `NULL`. |
| Graph | `graph.build_graph` | The existing typed graph is rebuilt from the stored clause set. A graph failure does not delete the clauses or a previous published graph. |

The pipeline is synchronous: the upload response is not returned until every
stage has completed or failed.

---

## 3. Provenance guarantees

Every uploaded clause row carries:

| Field | Meaning |
|---|---|
| `version_id` | The contract version whose clause set this row belongs to. |
| `clause_sha256` | SHA-256 of the stored clause text. |
| `page_number` | 1-based source page containing the clause start. |
| `char_start`, `char_end` | Half-open character range in `contracts.raw_text`. |
| `normalized_text` | Conservatively normalized form of the clause text. |
| `parent_id` | Clause id of the numbered parent, or `NULL`. |

The round-trip invariant is checked by the Phase 3 tests:

```python
raw_text[char_start:char_end] == clause_text
```

For a top-level section that contains sub-clauses, the parent's `char_end` is
narrowed to the parent's own text (up to the first sub-clause) so the range
never claims the children's text. Sub-clause ranges remain exact slices of the
source.

Page numbers are derived from extraction-time page ranges. When a clause starts
exactly on a page boundary, it is attributed to the page containing the offset;
the mapping is deterministic and never guessed from headings.

`api_b.py` exposes these fields from `GET /clauses/{contract_id}`:
`section_number`, `page_number`, `char_start`, `char_end`, `parent_id`,
`parent_section`, and `normalized_text`. The existing `clause_id`, `text`,
`type`, `version_id`, `clause_sha256`, `references`, and `numbers` fields remain
unchanged.

---

## 4. Schema additions

`db.init_db()` extends existing tables with guarded `ALTER TABLE` statements and
creates one new table. No existing column or table is dropped or renamed.

### 4.1 `clauses` columns

| Column | Type | Notes |
|---|---|---|
| `page_number` | `INTEGER` | Source page, `NULL` for legacy or non-paginated saves. |
| `char_start` | `INTEGER` | Start offset in `contracts.raw_text`. |
| `char_end` | `INTEGER` | End offset (exclusive). |
| `parent_id` | `INTEGER` | Self-reference to the parent clause, `NULL` when none. |
| `normalized_text` | `TEXT` | Normalized copy of the clause text. |

An index on `clauses(parent_id)` supports hierarchy reads. The columns are
populated from explicit clause items, or from clause `metadata`, during
`db.save_clauses()`.

### 4.2 `processing_log`

```sql
CREATE TABLE processing_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    version_id INTEGER,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_no INTEGER NOT NULL DEFAULT 1,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    error_type TEXT,
    error_message TEXT,
    correlation_id TEXT
);
```

Indexed by `(contract_id, stage)` and `status`. Stages are `extract`,
`segment`, `classify`, `persist`, and `graph`; statuses are `started`,
`succeeded`, and `failed`. The current synchronous pipeline writes one row per
completed attempt (with both timestamps); `started` is reserved for a future
asynchronous worker. The table contains operational metadata only — never
contract text, clause text, or secrets.

`db.list_processing_log(contract_id)` returns the history oldest-first.
`db.next_processing_attempt(contract_id, stage)` supplies monotonically
increasing attempt numbers for retries.

---

## 5. Duplicate semantics

The duplicate guard runs after staging but before extraction:

- The key is the uploaded file's SHA-256, matched against
  `contract_versions.source_sha256`.
- The lookup is scoped to the active firm. The same bytes uploaded by another
  firm create a separate contract in that firm.
- A match deletes the newly staged temporary file, records an `upload.duplicate`
  audit event, and returns HTTP 200 with:

```json
{
  "contract_id": 12,
  "duplicate": true,
  "status": "extracted",
  "analysis_status": "ready"
}
```

No new contract, version, clause set, graph, or processing-log rows are
created. A byte-identical re-upload can therefore never inflate the portfolio
or duplicate clauses.

The check is byte identity, not semantic identity: a re-saved or re-scanned PDF
with different bytes is a new document. This is deliberate and conservative.

---

## 6. Processing states and retries

`contracts.processing_status` gains these partial-failure meanings:

| Status | Meaning |
|---|---|
| `extracted` | Text, clauses, and graph are stored; analysis is available if clauses were detected. |
| `extraction_failed` | The PDF was rejected before any text was stored. Source bytes are not retained. |
| `clause_persistence_failed` | Text and version exist, but segmentation, classification, or clause persistence failed. |
| `graph_build_failed` | Text and clauses exist, but graph construction failed. |

The upload response reports `clause_split_status` (`ok` or
`no_clauses_detected`) and `analysis_status` (`ready` or `unavailable`). A
document with zero detected clauses is still stored with its text; it is never
reported as fully analyzed.

### 6.1 `GET /contracts/{contract_id}/processing`

Returns the contract's processing status, whether retry is allowed, and the
stage history:

```json
{
  "contract_id": 12,
  "processing_status": "clause_persistence_failed",
  "retryable": true,
  "stages": [
    {"stage": "extract", "status": "succeeded", "attempt_no": 1, "...": "..."},
    {"stage": "persist", "status": "failed", "attempt_no": 1, "error_type": "RuntimeError", "...": "..."}
  ]
}
```

The route uses the same firm-scoped authorization as every other contract
read. It never returns raw or clause text.

### 6.2 `POST /contracts/{contract_id}/retry`

Requires an upload-capable role (administrator, lawyer, or paralegal) in the
owning firm.

| Current status | Behaviour |
|---|---|
| `clause_persistence_failed` | Creates a new draft version, re-segments and re-classifies the stored `raw_text`, persists the replacement set atomically, rebuilds the graph, and returns the contract to `extracted`. The previous clause set is archived to `clause_history`; the live set is replaced, not duplicated. |
| `graph_build_failed` | Rebuilds only the graph. Clauses and versions are untouched. |
| `extraction_failed` | `409`. The rejected bytes were not retained; the user must re-upload the source PDF. |
| Any other status | `409`. Nothing is changed. |

Each retry writes a new numbered attempt for the stage it runs
(`attempt_no` 2, 3, ...), records success or failure, and writes a
`contract.retry_succeeded` audit event on success. A failed retry leaves the
contract in a retryable failed state rather than claiming success.

When the retained source PDF still exists, a clause retry re-reads it to restore
page numbers. If the file is missing, the retry continues from the stored text
and leaves page numbers unset; it never fabricates a page mapping.

---

## 7. Normalization and classification policy

Normalization is intentionally limited to artifacts that cannot change legal
meaning:

- CRLF/CR line endings become `\n`;
- form feeds and vertical tabs are removed;
- runs of spaces/tabs inside a line collapse to one space;
- trailing whitespace is removed;
- blank-line runs created by page breaks are collapsed.

The raw extracted text remains the authoritative stored document. The
normalized form is stored in `clauses.normalized_text` and clause metadata. It
is not used to compute source offsets.

Clause types come from the heading first, then from specific body keyword cues.
The vocabulary now includes `liability` for clauses that assume or allocate
liability without being a limitation/cap clause, and `obligations` for clauses
whose heading names obligations, duties, or responsibilities. `obligations` is
heading-only on purpose: the verb “shall” appears in nearly every operative
clause and would otherwise over-classify the document. Clauses that match no
type remain `other`.

---

## 8. Frontend

`templates/overview.html`:

- the contract drawer shows an **Ingestion stages** panel with a status badge
  per stage and the latest attempt number;
- partial failures show a **Retry failed stage** button, inline progress, and
  success/error messages;
- an `extraction_failed` contract explains that re-upload is required and does
  not offer a retry button;
- upload feedback distinguishes duplicate documents, no-clause documents, and
  fully analyzed documents.

`templates/explain.html`:

- clause tree rows show the source page (`p. N`) when available;
- the viewer meta ribbon shows stored source evidence
  (`Source: p. 2 · chars 120–450 · parent 7`);
- exact highlighting uses the stored `char_start`/`char_end` when that range
  still addresses the clause text, and falls back to text search only when it
  does not.

Both pages degrade gracefully for legacy clauses that have no page or offset
evidence.

---

## 9. Tests

New file: `tests/test_phase3_ingestion.py` (11 tests), covering:

- page boundaries and exact offsets from `extract.extract_pages`;
- the historical `extract.extract_text(path)` string API;
- round-trip clause provenance and page attribution;
- parent/child links for `7 -> 7.2, 7.3`;
- required classification categories, including `obligations` and
  `liability`;
- conservative normalization with unchanged word order;
- per-stage processing-log success records and the processing endpoint;
- duplicate suppression within a firm and separate documents across firms;
- cross-firm read isolation;
- retry of clause persistence without duplicate live clauses or versions;
- retry of graph construction without touching clauses;
- non-retryable extraction failure.

Verification run:

```
py -3.11 -m unittest discover -s tests -v
Ran 316 tests ... OK (skipped=1)
```

Top-level suite (includes the real-document compatibility checks):

```
py -3.11 -m unittest test_api test_db_clauses test_reasoning test_retrieval test_split_clauses -v
Ran 104 tests ... OK (skipped=4)
```

The intentional `RuntimeError` traces in test output come from mocked failure
paths and are expected.

---

## 10. Known limitations and remaining work

- **Synchronous pipeline.** Upload processing runs inside the request. There is
  no background queue or worker, so long PDFs still hold the request open.
- **Extraction retries require re-upload.** Rejected bytes are removed by the
  Phase 6 policy and cannot be retried from storage.
- **Page mapping on retry is conditional.** It needs the retained source file;
  otherwise stored text is reused without page numbers.
- **Heuristic classification.** Types are deterministic keyword/heading rules,
  not a legal model. Ambiguous clauses deliberately stay `other`.
- **One-level parent matching.** `parent_id` is rebuilt by exact
  `parent_section` match. A missing or inconsistent parent stays `NULL`; the
  pipeline does not invent a hierarchy.
- **Duplicate identity is byte identity.** Semantically identical re-exports
  with different bytes are separate contracts.
- **No OCR.** Image-only PDFs still fail extraction as in Phase 6.
- **Processing log is operational, not immutable.** Durable lifecycle and
  security events remain in the append-only `audit_events` trail.
- **No production claims.** The upload limits, cooperative deadline, and
  schema remain local/test controls, not production quotas, malware protection,
  or a multi-tenant database.

---

## 11. Files changed

| File | Change |
|---|---|
| `ingest.py` | New orchestrator: duplicate guard, extraction, segmentation, classification, persistence, graph stages, with injectable seams for existing patch points. |
| `normalize.py` | New conservative text normalization helpers. |
| `extract.py` | Added `extract_pages()` and `extract_text(..., return_pages=False)`; exact page offsets after document stripping. |
| `clauses.py` | Offset/page-aware segmentation, parent metadata, `obligations` type, `liability` classification, exact parent ranges. |
| `db.py` | Additive clause provenance columns, `processing_log`, clause persistence, hierarchy linking, processing helpers. |
| `api_a.py` | Upload pipeline wired to ingest stages, processing endpoint, retry endpoint, stage logging, partial-failure statuses. |
| `api_b.py` | Clause API exposes page/offset/parent/normalized provenance. |
| `templates/overview.html` | Ingestion-stage panel, retry UI, duplicate/no-clause upload feedback. |
| `templates/explain.html` | Page badges, source-evidence ribbon, offset-based highlighting. |
| `test_api.py` | Graph isolation test uses distinct bytes because identical uploads are now de-duplicated. |
| `tests/test_phase3_ingestion.py` | New Phase 3 coverage. |
| `docs/phase3_ingestion.md` | This document. |

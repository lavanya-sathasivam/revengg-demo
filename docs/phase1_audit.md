# Phase 1 Audit, Stabilization, and Technical Handover

**Date:** 2026-11-02  
**Scope:** Read-only Phase 1 audit of the existing Contract Intelligence
application, minimal bug fixes, regression verification, and documentation.  
**Out of scope:** company folders/workspaces, additional mock contracts,
custom-upload redesign, semantic/hybrid retrieval, graph-visualization
redesign, and broad frontend work. Those are Phase 2 candidates only.

---

## 1. Result in one paragraph

The existing application is a working modular monolith: a FastAPI app with a
read-only synthetic demo mode and an authenticated, firm-scoped upload mode; a
deterministic clause graph and impact engine; keyword-only legal-authority
retrieval; and three browser workspaces. The Phase 1 audit found no failing
backend behaviour. It found five user-facing defects in the templates and one
documentation drift problem. The defects were fixed without changing API
payloads, database rules, retrieval rules, dependency rules, or test
expectations. The full suite now reports **276 tests run, 276 passed, 0 failed,
0 errors, 0 skipped**.

No API key or contract content is reproduced in this report.

---

## 2. Architecture

### 2.1 Runtime shape

```
Browser
  │  HTML shells + JSON fetches
  ▼
application.create_app()  ── canonical factory, route registry, middleware
  │
  ├── api_auth.py   /auth/me, /auth/firm, /auth/logout
  ├── api_a.py      /upload, contract reads, clause explanation
  ├── api_b.py      /graph, /clauses, /impact, /compare, /demo/.../suggest
  └── api_chat.py   /chat, /chat/status
        │
        ▼
  adapter.py  ── the data seam used by the engine
        │
        ▼
  db.py  ── SQLite schema, migrations, reads/writes, audit triggers
```

`app.py` and `server.py` are thin compatibility bootstraps over
`application.create_app()`. The factory mounts each router exactly once. This
avoids the historical risk of `app.py` and `server.py` exposing different route
sets.

### 2.2 Two modes

| Mode | Data scope | Authentication | Writes |
|---|---|---|---|
| `demo` | Synthetic rows with `firm_id IS NULL` | Anonymous demo principal | Disabled (read-only DB; upload returns 403) |
| `upload` | One active firm, derived server-side from the session | Session cookie; anonymous requests are rejected | Upload, extraction, clauses, graph builds |

Demo mode is the default. Upload mode requires an explicit database path
(`REVENGG_UPLOAD_DB_PATH` or `create_app(database_path=...)`) and never falls
back to `demo.db`.

### 2.3 Request lifecycle

1. `application.create_app()` loads `AppSettings`, builds the database
   configuration, and registers routes.
2. The HTTP middleware binds the per-request database configuration.
3. `auth.authenticate_request()` resolves a `Principal`. In upload mode,
   non-public paths without an authenticated principal receive HTTP 401 before
   a handler runs.
4. Routes call `auth.authorized_contract()` / `authorized_contracts()`. A
   foreign or nonexistent contract is reported as 404, deliberately
   indistinguishable to avoid cross-firm probing.
5. The deterministic engine reads through `adapter.py` and returns the report.
6. Impact requests bind the expected clause hash and reject stale values or a
   stale/missing graph with a typed error code.

### 2.4 Module map

| Module | Responsibility |
|---|---|
| `application.py` | Canonical app factory, middleware, lifespan, page routes, router mounting, typed HTTP error envelope |
| `app.py`, `server.py` | Thin bootstraps / historical import paths |
| `app_config.py` | Demo vs upload settings, upload limits, session settings, fail-closed configuration |
| `auth.py` | Principal/session model, firm membership, authorization helpers, public-path policy |
| `api_auth.py` | `/auth/me`, `/auth/firm`, `/auth/logout` |
| `audit.py` | Append-only audit event writer and metadata sanitization |
| `db.py` | SQLite schema/migrations, contract/clause/version persistence, graph-build storage, audit triggers |
| `adapter.py` | The engine's DB seam; clause ordering; graph binding/publication/status |
| `extract.py` | PDF validation and text extraction; top-level section splitting |
| `clauses.py` | Sub-clause splitting, 18-type classification, references, numbers, calendar/amount helpers |
| `graph.py` | Deterministic nodes and typed edges, build hashing, graph persistence, BFS traversal |
| `impact.py` | Counterfactual arithmetic, doubts, conflicts, impact report, review-point ranking, cross-contract comparison |
| `retrieval.py` | Deterministic keyword retrieval over curated legal authorities |
| `reasoning.py` | Explanation payload (summary, key points, authorities, evidence, uncertainties, disclaimer) |
| `llm.py` | Optional LLM helper used only to fill graph-rule gaps; never raises |
| `api_a.py` | Upload and contract/clause-read/explanation routes |
| `api_b.py` | Graph, clause, impact, compare, and demo-suggestion routes |
| `api_chat.py` | Gemini chat with bounded contract context and prompt-injection defenses |
| `seed.py`, `cli.py` | Demo seeding and terminal interface |
| `templates/` | Overview, impact workspace, clause explainer, and shared chat UI |
| `tests/` | Unit, integration, security-scope, and hardening regression suites |

### 2.5 HTTP surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Contract portfolio overview |
| GET | `/impact-analysis` | Change-impact workspace |
| GET | `/explain` | Clause explainer |
| GET | `/health` | Health check |
| GET | `/contracts` | Visible contract list (no raw text) |
| GET | `/contracts/{id}` | Contract metadata and extracted text |
| GET | `/contracts/{id}/clauses` | Legacy raw clause rows |
| GET | `/contracts/{id}/clauses/{section}/explanation` | Explanation by contract/section |
| GET | `/clauses/{id}` | Canonical clause payload with type, references, numbers, hash |
| GET | `/graph/{id}` | Published graph nodes and edges |
| POST | `/impact` | Impact analysis |
| GET | `/compare/{a}/{b}` | Cross-contract comparison |
| GET | `/demo/{id}/suggest` | Suggested demo change |
| POST | `/upload` | Firm-scoped PDF upload (upload mode only) |
| GET | `/auth/me` | Current principal and firm context |
| POST | `/auth/firm` | Switch among authorized firms |
| POST | `/auth/logout` | Revoke session |
| POST | `/chat` | Gemini chat with optional contract/clause context |
| GET | `/chat/status` | Whether chat is configured |

`/clauses/{id}` is the canonical shape used by the current browser pages.
`/contracts/{id}/clauses` remains a legacy raw-row route.

### 2.6 Data model

Core tables in the local SQLite schema:

| Table | Purpose |
|---|---|
| `contracts` | Contract metadata, raw text, current version, current published graph, firm and provenance |
| `contract_versions` | Immutable version records with source hash, extraction status, and provenance |
| `clauses` | Current clause rows with section number, text, metadata, version, SHA-256 |
| `clause_history` | Archived clause rows for version replacement |
| `graph_builds` | Staged/published build records, counts, hash, failure information |
| `nodes`, `edges` | Published graph content, scoped by contract and build |
| `firms`, `firm_memberships`, `users`, `auth_sessions` | Tenant, membership, identity, and session state |
| `audit_events` | Append-only audit trail with sanitized metadata |
| `authorities` | Curated legal-authority records used by keyword retrieval |
| `traces` | Legacy operation trace rows |

The demo database currently contains two synthetic contracts, 81 clauses,
131 graph nodes and 81 graph edges. Both contracts have published graph builds
and current bindings.

### 2.7 Deterministic pipeline

```
PDF/text
  → extract.py / clauses.py       segment into sections and sub-clauses
  → clauses.classify_type()       one of 18 auditable types
  → graph.py                      clause/element nodes and typed edges
  → impact.impact_of()            reverse dependency traversal
  → impact.counterfactual()       payment / SLA arithmetic
  → impact.detect_doubts()        explicit uncertainty records
  → impact.clash_check()          definition drift / conflicts
  → retrieval.py                  keyword retrieval over authorities
  → reasoning.py                  evidence-grounded explanation
  → templates / API / CLI
```

The structural path is deterministic and requires no model. The optional LLM
is used only when the typed relation table leaves a gap. Chat is a separate
assistant surface and is not used to produce the deterministic report.

---

## 3. Findings and fixes

### 3.1 Fixed defects

| # | File | Symptom | Root cause | Fix |
|---|---|---|---|---|
| 1 | `templates/index.html` | The impact page did not show "Key Review Points for Counsel"; the UI contract test failed because `ranked_review_points` was absent from the page. | An uncommitted working-tree edit removed both the CSS block and the render block. The removed renderer also read `item.point`, but the backend emits `review_point`, so restoring it unchanged would have displayed `[object Object]`. | Restored the styles and renderer, using `review_point` first, with a safe legacy `review_points` fallback and escaped output. |
| 2 | `templates/index.html` | After entering an invalid value, the "Analyze change" button stayed disabled and read "Analyzing...". Changing contract/clause during an in-flight request could also leave it stuck. | The button was disabled and the request version was advanced before numeric validation; validation failures returned early without restoring either. `clearAnalysis()` also advanced the version for a selection change without re-enabling the button, so the old request's `finally` handler correctly refused to touch it. | Validate first, then start the request. The disable/label change and request-version increment now happen only when a request will actually be sent. `clearAnalysis()` restores the button when a selection change invalidates an in-flight request; the `finally` handler is also defensive. |
| 3 | `templates/index.html` | Conflict cards displayed `[object Object]` (or an unhelpful fallback). | The renderer read `c.description || c.question || c`, but `impact.clash_check()` emits `message`. | Render `message` first, then `description`/`question`, with a safe text fallback. Added the conflict's `review_point` as a "Counsel Action" line. |
| 4 | `templates/overview.html` | In firm mode, an anonymous visit showed "Server Offline" instead of "Auth Required". | `/auth/me` is protected by the middleware in upload mode and returns 401 for an anonymous request; `initAuthContext()` treated every non-2xx response as a server outage. | Treat 401 as the expected signed-out state and fall through to the existing "Auth Required" rendering. True non-2xx/network failures still show "Server Offline". |
| 5 | `templates/overview.html` | "Analysis Ready" counted every stored contract, even ones with no published graph. The contract drawer also showed version/graph "1" when neither existed. | The metric used `allContracts.length`; the drawer used `|| "1"` placeholders. | Count contracts with a `current_graph_build_id` (with a `graph_build_id` fallback), and show `Unavailable` / `Not built` instead of inventing IDs. |

The uncommitted `templates/index.html` edit was treated as user work: the
feature was restored rather than discarded, and the underlying field-name bug
was corrected.

### 3.2 Documentation corrections

`README.md` and `docs/architecture_walkthrough.md` still described the
pre-integration branch state (no web UI, `b-graph`, 90/90 or 112 tests, an
`app.db` upload database, and the old page URLs). They now state the current
`main` baseline, the three workspaces, the canonical app factory, the explicit
upload-database configuration, and the current test count. Historical branch
ownership is retained but marked as historical.

### 3.3 What was deliberately not changed

- API payload shapes, status codes, and error codes.
- Database schema, migration rules, audit triggers, and graph publication rules.
- Keyword retrieval scoring and corpus.
- Dependency rules and graph direction.
- Clause classification vocabulary.
- Chat prompt and context-assembly behavior.
- Test expectations.

---

## 4. Verification

### 4.1 Full test suite

Command:

```powershell
py -3.11 -m unittest discover -s tests -v
```

Result:

- **276 tests run**
- **276 passed**
- **0 failed**
- **0 errors**
- **0 skipped**
- Duration: **99.261 s** (final run after the button-state race fix)
- Final unittest status: **OK**

The run included Person A's original test files, the integration tests, the
security-scope tests, and the Phase 5/6/7 hardening suites. The live Gemini
test passed because a locally configured key was present; without a key the
test skips by design, and provider quota/rate limiting also causes a skip.

### 4.2 Targeted and static checks

| Check | Result |
|---|---|
| `py -3.11 -m unittest discover -s tests -p "test_integration.py" -v` | 26 tests, OK |
| `py -3.11 -m py_compile` on all application Python modules | Passed |
| Node syntax parse of all inline scripts in the three templates | Passed |
| Page smoke test: `/`, `/impact-analysis`, `/explain`, `/docs`, `/openapi.json` | HTTP 200 |
| API smoke test: health, auth context, chat status, contracts, clauses, explanation, graph, comparison, demo suggestion | Expected responses |
| Negative checks: unknown contract/clause, stale old value, SLA > 100, missing fields, demo upload | Expected 404/400/422/403 |

### 4.3 Warnings observed

`tests/test_integration.py` emits `ResourceWarning: unclosed file` while
reading source files with bare `open(...).read()`. These are test-harness
warnings only; they do not affect application behavior and were not changed to
avoid altering test expectations.

---

## 5. Remaining issues and risks

1. **Gemini is an external dependency.** The live chat path depends on key
   availability, network access, and provider quota. It is not part of the
   deterministic report, and no production AI-provider approval exists. Local
   `.env` values must never be logged or committed.
2. **Explain-page legacy fallback is not shape-normalized.** The primary
   `/clauses/{id}` route is used normally. If it fails, `explain.html` falls
   back to `/contracts/{id}/clauses`, which returns raw database rows
   (`clause_text`, nested `metadata`) rather than the canonical UI shape, and
   the fallback does not check its own HTTP status. This path is not exercised
   by the current app flow but should be cleaned up or removed in Phase 2.
3. **Uploads remain section-level.** The upload route still uses the top-level
   splitter, so uploaded PDFs do not receive the full sub-clause hierarchy used
   by the synthetic demo contracts.
4. **Retrieval is keyword-only.** There are no embeddings, vector search, or
   hybrid ranking. The deterministic keyword path is intentional and tested.
5. **Local SQLite only.** There is no PostgreSQL/RLS, background worker, object
   storage, retention workflow, legal hold, deletion workflow, or production
   malware/OCR control.
6. **No identity provider/login flow.** Sessions are provisioned locally; the
   firm-switcher UI is not a full workspace-management experience.
7. **Documentation history.** Older docs still contain branch-era narrative;
   this Phase 1 report and the current-state notes in `README.md` /
   `docs/architecture_walkthrough.md` are the current references.

---

## 6. Modules likely to change in Phase 2

| Phase 2 capability | Modules / assets |
|---|---|
| Company folders and workspaces | `auth.py`, `api_auth.py`, `application.py`, `templates/overview.html`, `templates/index.html`; existing `firms` / `firm_memberships` schema |
| Additional mock contracts | `seed.py`, `data/contracts/*.txt`, `data/ground_truth.json`, evaluation tests |
| Custom upload flow | `api_a.py` upload handler, `extract.py`, `clauses.py`, `graph.py`, `adapter.py`, `templates/overview.html`; versioned graph rebuild |
| Semantic / hybrid retrieval | `retrieval.py`, `data/authorities.json`, `reasoning.py`, retrieval/integration tests |
| Graph visualization redesign | `templates/explain.html`, `templates/index.html`, `/graph/{id}` payload consumers |
| Major frontend redesign | `templates/overview.html`, `templates/index.html`, `templates/explain.html` |
| Production tenancy and audit | `db.py`, `adapter.py`, `auth.py`, `audit.py`; PostgreSQL/RLS and append-only storage design |
| Background extraction / graph jobs | New worker boundary around `extract.py`, `graph.py`, `adapter.py`; status/progress API |
| Retention, legal hold, deletion | `db.py`, `audit.py`, new service/API surface |
| AI gateway / policy controls | `api_chat.py`, `llm.py`, configuration and audit |

Per the Phase 1 constraint, none of these were implemented.

---

## 7. Reproduction

```powershell
py -3.11 -m pip install -r requirements.txt
py -3.11 seed.py
py -3.11 -m unittest discover -s tests -v
py -3.11 -m uvicorn server:app --reload
```

Then open:

- `http://127.0.0.1:8000/` — portfolio overview
- `http://127.0.0.1:8000/impact-analysis` — impact workspace
- `http://127.0.0.1:8000/explain` — clause explainer
- `http://127.0.0.1:8000/docs` — OpenAPI documentation

---

## 8. Files changed in this Phase 1 pass

| File | Reason |
|---|---|
| `templates/index.html` | Restored ranked review points with the correct `review_point` field; fixed the stuck Analyze button on validation failure; fixed conflict rendering to use `message`; added conflict review-point rendering |
| `templates/overview.html` | Treated 401 `/auth/me` as "Auth Required"; corrected "Analysis Ready" to count published graphs; replaced invented version/graph fallback IDs |
| `README.md` | Updated branch/status/test claims, browser URLs, app-factory description, database configuration, and Git instructions for the integrated tree |
| `docs/architecture_walkthrough.md` | Added a current-state note, corrected workspace URLs, and updated the test baseline |
| `docs/phase1_audit.md` | This Phase 1 audit, architecture, verification, residual-issues, and Phase 2 change-map report |

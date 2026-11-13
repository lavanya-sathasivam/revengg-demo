# Authentication and Firm-Level Authorization

**Status:** Local password login and server-side authorization are implemented.
This is **not** a production authentication deployment and does not select an
identity provider.

## 1. What is implemented

- Server-side opaque sessions. The browser receives a random token; the database stores only its SHA-256 hash.
- `HttpOnly`, `SameSite=Lax`, `Secure`-configurable session cookie.
- Users, PBKDF2 password hashes, firms, firm memberships, roles, and sessions
  in additive SQLite tables.
- A nullable `contracts.firm_id`: `NULL` is the synthetic demo scope; an integer is a firm-owned contract.
- Server-derived active firm on every request. A client-supplied firm id is only a switch request and is checked against membership.
- Authentication enforced by the application middleware for every non-public route in upload mode, not per-handler.
- Contract reads, clause reads, graph, impact, explanation, suggestion, comparison, and upload all use the active firm.
- Foreign and unknown contract ids both return `404`; a member without the required role gets `403`.
- Demo mode remains anonymous and read-only, and only sees `contracts.firm_id IS NULL`.
- Security-relevant actions and denials are written to an append-only `audit_events` table. This covers firm/user/membership/session lifecycle, authentication and authorization denials, firm-switch denials, uploads, clause persistence, and graph publication/failure. Audit metadata is sanitized and must not contain contract text, clause text, session tokens, or secrets.

There is deliberately no MFA, SSO, or production identity provider
integration. OQ-02 in `docs/DECISIONS_AND_OPEN_QUESTIONS.md` remains open.

## 2. Roles

| Role | Read / analyze | Upload |
|---|---|---|
| `administrator` | yes | yes |
| `lawyer` | yes | yes |
| `paralegal` | yes | yes |
| `reviewer` | yes | no |
| `viewer` | yes | no |

Role is re-read from `firm_memberships` on each request. It is not trusted from a cookie, header, query parameter, or request body.

## 3. Modes

| Mode | Authentication | Data visible | Upload |
|---|---|---|---|
| `demo` | none | `firm_id IS NULL` only | rejected with `403` |
| `upload` | valid session required | active firm only | role-gated |

The demo database and an upload database must remain separate. `load_settings()` refuses to use the demo database as the upload database.

## 4. Environment configuration

The normal local startup is writable and uses dedicated files:

```powershell
py -3.11 -m uvicorn server:app --reload
```

This defaults to `local-uploads.db` and `local-uploads\`, never `demo.db`.
Demo mode is explicit with `REVENGG_MODE=demo`.

Optional mode configuration:

```text
REVENGG_MODE=demo|upload
REVENGG_UPLOAD_DB_PATH=D:\path\to\local-uploads.db
REVENGG_UPLOAD_DIR=D:\path\to\uploads
```

New session configuration:

```text
# Local HTTP default is false. Set true when serving over HTTPS.
REVENGG_SESSION_COOKIE_SECURE=false

# Default 28800 (8 hours).
REVENGG_SESSION_TTL_SECONDS=28800
```

Upload-processing limits (upload mode only):

```text
# Local/test defaults, not approved production policy (OQ-04).
# REVENGG_UPLOAD_MAX_BYTES=10485760
# REVENGG_UPLOAD_MAX_PAGES=200
# REVENGG_UPLOAD_MAX_EXTRACTED_CHARS=1000000
# REVENGG_UPLOAD_MAX_PROCESSING_SECONDS=30
```

Passwords are never stored; only PBKDF2-HMAC-SHA256 hashes with per-user
random salts are persisted. Session tokens are random bearer values, stored
only as SHA-256 hashes, and revoked on logout or expiry.

## 5. Local setup

1. Start the app; the default paths are already separate from `demo.db`.
2. Run the one-time administrator setup. The password is prompted without
   echoing:

```powershell
py -3.11 tools/provision_auth.py `
    --db .\local-uploads.db `
    --firm "Demo Law Firm" `
    --subject admin@demo.local `
    --display-name "Demo Lawyer" `
    --role administrator
```

3. Open `http://127.0.0.1:8000/`, sign in, and use the Contract Portfolio &
   Intake workspace. Normal users then only need the login page; no session
   token or cookie provisioning is required.

Ordinary users may register at `/register`, but registration does not grant
administrator privileges or firm access. An administrator adds them to the
firm through the existing firm-membership flow. A multi-firm user can switch
only among memberships already granted by the server.

## 6. Endpoint behavior

Public in upload mode:

- `GET /health`
- `GET /`, `/impact-analysis`, `/explain`, `/ask`, `/graph`, `/members`
  (HTML shells only; upload-mode navigation redirects anonymous users to
  `/login`)
- `/docs`, `/redoc`, `/openapi.json`

Protected in upload mode:

- `POST /upload`
- `GET /contracts`
- `GET /contracts/{contract_id}`
- `GET /contracts/{contract_id}/clauses`
- `GET /contracts/{contract_id}/clauses/{section}/explanation`
- `GET /graph/{contract_id}`
- `GET /clauses/{contract_id}`
- `POST /impact`
- `GET /compare/{contract_a}/{contract_b}`
- `GET /demo/{contract_id}/suggest`
- `GET /auth/me`
- `POST /auth/firm`
- `POST /auth/logout`

Authentication and authorization semantics:

- No valid session → `401 Authentication required`.
- Valid session without an active firm → `403 No active firm is selected`.
- Foreign contract or clause → `404 Contract not found` (same as nonexistent, no metadata leak).
- Insufficient upload role → `403`.
- Switching to a firm without membership → `403`, and the previous active firm remains in force.
- Upload extension/empty/oversized-body rejection → `400`/`413`, audit only, no contract row and no stored bytes.
- Defined PDF content/parse rejection (non-PDF bytes, malformed/repaired, encrypted, no text, page/text limit, timeout) → `422`/`413`, a failed contract version with hash/size/name but no stored bytes, and a failure audit event. The client receives a stable, sanitized message.
- Unexpected parser or storage failure → `500` with a generic message; no parser internals, paths or document text are returned.

## 7. Security assumptions

- The application is served over HTTPS in any non-local environment; `REVENGG_SESSION_COOKIE_SECURE=true`.
- The browser is untrusted. UI hiding is not authorization; the server checks every request.
- Cookie-authenticated state changes rely on `SameSite=Lax` in this phase. A separate CSRF token is not implemented; do not add permissive CORS or cross-site embedding.
- SQLite is local/test storage, not production multi-tenant storage. There is no PostgreSQL or row-level security in this phase.
- Filesystem access to the SQLite database or upload directory is outside the HTTP authorization boundary.
- Uploaded PDFs are size/page/text bounded and content-checked, but there is no parser sandbox, malware scanner, antivirus integration, hard pre-emptive timeout, or hard memory quota. The processing deadline is cooperative only.
- No rate limiting, account lockout, MFA, SSO, or session rotation on privilege change is implemented yet.
- The audit trail is append-only at the SQLite trigger level, but there is no audit viewer, export endpoint, retention job, access-review workflow, or protection against a database owner who can drop triggers. See `docs/phase5_integrity.md`.
- No production identity provider is selected. Do not treat local provisioning as production login.
- Session tokens are bearer credentials. Anyone who obtains one can use it until expiry or logout.
- In upload mode unauthenticated workspace navigation redirects to `/login`;
  protected API calls still return `401`. The login response establishes the
  HttpOnly, SameSite session cookie.
- A legacy database that predates `contracts.firm_id` is still readable in demo mode for compatibility; in that case demo reads are not filtered by firm. Never point demo mode at an upload or production database.

## 8. Remaining risks / open decisions

- Choose and approve an identity provider, MFA policy, SSO model, and production session policy (OQ-02).
- Approve a production CSRF strategy if cookie-authenticated state changes remain browser-facing.
- Approve PostgreSQL and RLS before claiming database-enforced tenant isolation.
- Approve the exact audit access roles, export controls, audit-retention period, and access-review cadence (OQ-13). Restricted audit access and exports are approved policy, but no in-app viewer/export role enforcement exists yet. Whether stronger tamper evidence is required remains open (OQ-16).
- Decide whether matter-level/ethical-wall permissions are required at launch (OQ-06).
- Approve production PDF size/page/text limits, malware-scanning approach, encrypted-PDF policy, and partial-extraction rules (OQ-04). The local/test defaults are not production policy.
- Add session rotation, absolute/idle timeouts, device/session management, and revocation tooling.
- Decide legacy-record reviewer authorization and evidence requirements; human review with recorded evidence is approved, and uncertain rows remain unclassified (OQ-11). Rows with `firm_id IS NULL` are never visible to firm members.

## 9. Tests

Focused authorization tests:

```powershell
py -3.11 -m unittest discover -s tests -p "test_auth_authorization.py" -v
py -3.11 -m unittest discover -s tests -p "test_phase6_upload_hardening.py" -v
```

Existing relevant suites:

```powershell
py -3.11 -m unittest test_api -v
py -3.11 -m unittest discover -s tests -p "test_app_factory.py" -v
py -3.11 -m unittest discover -s tests -p "test_security_scope.py" -v
py -3.11 -m unittest discover -s tests -v
git diff --check
```

The authorization suite covers anonymous access, cross-firm contract/clause/graph/impact/comparison/suggestion/explanation attacks, role restrictions, firm switching without membership, membership removal, expired/revoked sessions, and demo/firm separation. The Phase 6 suite covers content-based PDF validation, size/page/text/time limits, failed-version provenance, sanitized errors, temp cleanup, storage naming, and last-known-good graph preservation. Neither suite proves production multi-tenant isolation because PostgreSQL/RLS, the identity provider, and production session policy are out of scope.

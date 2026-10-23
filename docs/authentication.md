# Authentication and Firm-Level Authorization

**Status:** Phase 4 foundation implemented for local/test use. This is **not** a production authentication deployment and does not select an identity provider.

## 1. What is implemented

- Server-side opaque sessions. The browser receives a random token; the database stores only its SHA-256 hash.
- `HttpOnly`, `SameSite=Lax`, `Secure`-configurable session cookie.
- Users, firms, firm memberships, roles, and sessions in additive SQLite tables.
- A nullable `contracts.firm_id`: `NULL` is the synthetic demo scope; an integer is a firm-owned contract.
- Server-derived active firm on every request. A client-supplied firm id is only a switch request and is checked against membership.
- Authentication enforced by the application middleware for every non-public route in upload mode, not per-handler.
- Contract reads, clause reads, graph, impact, explanation, suggestion, comparison, and upload all use the active firm.
- Foreign and unknown contract ids both return `404`; a member without the required role gets `403`.
- Demo mode remains anonymous and read-only, and only sees `contracts.firm_id IS NULL`.

There is deliberately **no login endpoint, password table, password hash, MFA, SSO, or production identity provider integration** yet. OQ-02 in `docs/DECISIONS_AND_OPEN_QUESTIONS.md` remains open.

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

Existing mode configuration:

```text
REVENGG_MODE=demo|upload
REVENGG_UPLOAD_DB_PATH=D:\path\to\local-uploads.db
REVENGG_UPLOAD_DIR=D:\path\to\uploads
```

New session configuration:

```text
# Default true. Keep true behind HTTPS. Set false only for local HTTP testing.
REVENGG_SESSION_COOKIE_SECURE=true

# Default 28800 (8 hours).
REVENGG_SESSION_TTL_SECONDS=28800
```

No session signing secret is required because tokens are random and stored server-side as hashes. Do not add an authentication secret to `.env.example` or source control.

## 5. Local setup

1. Choose an explicit upload database path outside `demo.db`.
2. Start the app in upload mode.
3. Provision a synthetic firm, user, membership, and session with the local tool:

```powershell
py -3.11 tools/provision_auth.py `
    --db D:\path\to\local-uploads.db `
    --firm "Demo Law Firm" `
    --subject local:demo-lawyer `
    --display-name "Demo Lawyer" `
    --role lawyer `
    --session
```

The tool prints a `session_token` once. Set it as the `revengg_session` cookie in a local HTTP client or browser developer tools. Treat the token as a secret; it is not committed and no fixed credential is stored in the repository.

For a multi-firm local user, run the tool for each firm with the same `--subject` and the desired `--role`. A session with multiple memberships starts without an active firm; call `POST /auth/firm` to select one.

## 6. Endpoint behavior

Public in upload mode:

- `GET /health`
- `GET /`, `/impact-analysis`, `/explain` (HTML shells only)
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

## 7. Security assumptions

- The application is served over HTTPS in any non-local environment; `REVENGG_SESSION_COOKIE_SECURE=true`.
- The browser is untrusted. UI hiding is not authorization; the server checks every request.
- Cookie-authenticated state changes rely on `SameSite=Lax` in this phase. A separate CSRF token is not implemented; do not add permissive CORS or cross-site embedding.
- SQLite is local/test storage, not production multi-tenant storage. There is no PostgreSQL or row-level security in this phase.
- Filesystem access to the SQLite database or upload directory is outside the HTTP authorization boundary.
- No audit log, rate limiting, account lockout, MFA, SSO, or session rotation on privilege change is implemented yet.
- No production identity provider is selected. Do not treat local provisioning as production login.
- Session tokens are bearer credentials. Anyone who obtains one can use it until expiry or logout.
- In upload mode the HTML workspaces are public shells but have no browser login flow yet. Their API calls return `401` until a session cookie is provisioned out of band or an approved IdP integration is added.
- A legacy database that predates `contracts.firm_id` is still readable in demo mode for compatibility; in that case demo reads are not filtered by firm. Never point demo mode at an upload or production database.

## 8. Remaining risks / open decisions

- Choose and approve an identity provider, MFA policy, SSO model, and production session policy (OQ-02).
- Approve a production CSRF strategy if cookie-authenticated state changes remain browser-facing.
- Approve PostgreSQL and RLS before claiming database-enforced tenant isolation.
- Add audit logging for authentication, authorization denials, membership changes, and uploads.
- Decide whether matter-level/ethical-wall permissions are required at launch (OQ-06).
- Add session rotation, absolute/idle timeouts, device/session management, and revocation tooling.
- Decide how legacy unclassified upload rows should be quarantined/reclassified; rows with `firm_id IS NULL` are never visible to firm members.

## 9. Tests

Focused authorization tests:

```powershell
py -3.11 -m unittest discover -s tests -p "test_auth_authorization.py" -v
```

Existing relevant suites:

```powershell
py -3.11 -m unittest test_api -v
py -3.11 -m unittest discover -s tests -p "test_app_factory.py" -v
py -3.11 -m unittest discover -s tests -p "test_security_scope.py" -v
py -3.11 -m unittest discover -s tests -v
git diff --check
```

The authorization suite covers anonymous access, cross-firm contract/clause/graph/impact/comparison/suggestion/explanation attacks, role restrictions, firm switching without membership, membership removal, expired/revoked sessions, and demo/firm separation. It does not prove production multi-tenant isolation because PostgreSQL/RLS, the identity provider, and production session policy are out of scope.

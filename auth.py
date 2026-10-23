"""Authentication and firm-level authorization foundation.

This module deliberately does not choose or implement a production identity
provider.  It provides the server-side session and membership boundary that an
approved IdP (or an explicit local provisioning step) can use:

* opaque session tokens are stored only as SHA-256 hashes;
* the active firm is stored on the server-side session;
* membership and role are re-read from the database on every request;
* a client-supplied firm id is only a switch request, never proof of access;
* no passwords or password hashes exist in this design.

The demo mode bypasses authentication entirely, but it is read-only and only
sees synthetic contracts whose ``firm_id`` is NULL.  Upload mode requires a
valid session and always scopes data to the session's active firm.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response

import db

logger = logging.getLogger("app.auth")

SESSION_COOKIE_NAME = "revengg_session"
DEFAULT_SESSION_TTL_SECONDS = 8 * 60 * 60

ROLE_ADMINISTRATOR = "administrator"
ROLE_LAWYER = "lawyer"
ROLE_PARALEGAL = "paralegal"
ROLE_REVIEWER = "reviewer"
ROLE_VIEWER = "viewer"

VALID_ROLES = frozenset(
    {
        ROLE_ADMINISTRATOR,
        ROLE_LAWYER,
        ROLE_PARALEGAL,
        ROLE_REVIEWER,
        ROLE_VIEWER,
    }
)

# Every member may read and run analyses.  Only these roles may upload.
UPLOAD_ROLES = frozenset({ROLE_ADMINISTRATOR, ROLE_LAWYER, ROLE_PARALEGAL})

PUBLIC_PATHS = frozenset(
    {
        "/",
        "/impact-analysis",
        "/explain",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    }
)


@dataclass(frozen=True)
class Principal:
    """The authenticated (or demo) context for one request."""

    authenticated: bool
    demo: bool
    user_id: int | None = None
    subject: str | None = None
    display_name: str | None = None
    firm_id: int | None = None
    firm_name: str | None = None
    role: str | None = None
    session_id: int | None = None

    @property
    def can_upload(self) -> bool:
        return (
            self.authenticated
            and not self.demo
            and self.firm_id is not None
            and self.role in UPLOAD_ROLES
        )


ANONYMOUS = Principal(authenticated=False, demo=False)
DEMO_PRINCIPAL = Principal(authenticated=False, demo=True)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _require_role(role: str) -> str:
    if role not in VALID_ROLES:
        raise ValueError(
            "Unknown role %r; expected one of: %s"
            % (role, ", ".join(sorted(VALID_ROLES)))
        )
    return role


def _record_audit(
    action,
    outcome="success",
    *,
    principal=None,
    firm_id=None,
    actor_user_id=None,
    actor_type="system",
    target_type=None,
    target_id=None,
    correlation_id=None,
    metadata=None,
    conn=None,
    commit=True,
    swallow=False,
):
    """Write an audit event without making demo mode writable.

    Demo mode opens a read-only database.  A skipped audit write there is not
    a silent security failure: the demo contains only synthetic read-only data
    and has no authenticated actor.  In upload mode failures are either
    propagated (default) or logged (``swallow`` on denial paths).
    """

    if principal is not None:
        if principal.demo:
            return None
        if principal.authenticated:
            actor_user_id = principal.user_id
            actor_type = "user"
            if firm_id is None:
                firm_id = principal.firm_id
    config = db.get_database_config()
    if config is not None and config.read_only:
        return None

    import audit

    return audit.record_event(
        action,
        outcome,
        firm_id=firm_id,
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        target_type=target_type,
        target_id=target_id,
        correlation_id=correlation_id,
        metadata=metadata,
        conn=conn,
        commit=commit,
        swallow=swallow,
    )


def is_public_path(path: str) -> bool:
    """Return True for routes that never expose firm data.

    The HTML workspaces are public shells.  They fetch protected JSON routes,
    which remain authenticated.  API documentation exposes no contract data.
    """

    return path in PUBLIC_PATHS or path.startswith("/docs/")


# ---------------------------------------------------------------------------
# Provisioning helpers (local/test use; no public signup endpoint)
# ---------------------------------------------------------------------------
def create_firm(name: str) -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Firm name is required.")
    conn = db.get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO firms (name, created_at) VALUES (?, ?)",
            (name, _iso(_now())),
        )
        firm_id = cur.lastrowid
        _record_audit(
            "firm.created",
            firm_id=firm_id,
            target_type="firm",
            target_id=firm_id,
            metadata={"name": name},
            conn=conn,
            commit=False,
        )
        conn.commit()
        return firm_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def find_firm(name: str):
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT firm_id, name, created_at FROM firms WHERE name = ?",
            ((name or "").strip(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_user(subject: str, display_name: str | None = None) -> int:
    subject = (subject or "").strip()
    if not subject:
        raise ValueError("User subject is required.")
    display_name = (display_name or subject).strip() or subject

    conn = db.get_conn()
    try:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE subject = ?", (subject,)
        ).fetchone()
        if existing is not None:
            return existing["user_id"]
        cur = conn.execute(
            """INSERT INTO users (subject, display_name, created_at, disabled)
               VALUES (?, ?, ?, 0)""",
            (subject, display_name, _iso(_now())),
        )
        user_id = cur.lastrowid
        _record_audit(
            "user.created",
            target_type="user",
            target_id=user_id,
            metadata={"user_id": user_id},
            conn=conn,
            commit=False,
        )
        conn.commit()
        return user_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_membership(firm_id: int, user_id: int, role: str) -> None:
    """Create or update a membership (provisioning/admin operation)."""

    _require_role(role)
    conn = db.get_conn()
    try:
        conn.execute(
            """INSERT INTO firm_memberships
                   (firm_id, user_id, role, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(firm_id, user_id)
               DO UPDATE SET role = excluded.role""",
            (firm_id, user_id, role, _iso(_now())),
        )
        _record_audit(
            "membership.upserted",
            firm_id=firm_id,
            target_type="membership",
            target_id="%s:%s" % (firm_id, user_id),
            metadata={"firm_id": firm_id, "user_id": user_id, "role": role},
            conn=conn,
            commit=False,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_membership(user_id: int, firm_id: int):
    conn = db.get_conn()
    try:
        row = conn.execute(
            """SELECT m.firm_id, f.name AS firm_name, m.role, m.created_at
               FROM firm_memberships m
               JOIN firms f ON f.firm_id = m.firm_id
               WHERE m.user_id = ? AND m.firm_id = ?""",
            (user_id, firm_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_memberships(user_id: int) -> list[dict]:
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT m.firm_id, f.name AS firm_name, m.role, m.created_at
               FROM firm_memberships m
               JOIN firms f ON f.firm_id = m.firm_id
               WHERE m.user_id = ?
               ORDER BY f.name, m.firm_id""",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def create_session(
    user_id: int,
    active_firm_id: int | None = None,
    ttl_seconds: int | None = None,
) -> str:
    """Create a server-side session and return the opaque token once.

    If no active firm is supplied, exactly one membership is selected
    automatically.  Multiple memberships require an explicit switch after the
    session is established, so the server never has to guess.
    """

    ttl = DEFAULT_SESSION_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    if ttl <= 0:
        raise ValueError("Session lifetime must be positive.")

    conn = db.get_conn()
    try:
        user = conn.execute(
            "SELECT user_id, disabled FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if user is None or user["disabled"]:
            raise ValueError("User does not exist or is disabled.")

        memberships = conn.execute(
            "SELECT firm_id FROM firm_memberships WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        membership_ids = {row["firm_id"] for row in memberships}

        if active_firm_id is None and len(membership_ids) == 1:
            active_firm_id = next(iter(membership_ids))
        if active_firm_id is not None and active_firm_id not in membership_ids:
            raise ValueError("The user is not a member of the selected firm.")

        token = secrets.token_urlsafe(32)
        now = _now()
        cur = conn.execute(
            """INSERT INTO auth_sessions
                   (token_hash, user_id, active_firm_id, created_at,
                    expires_at, revoked_at)
               VALUES (?, ?, ?, ?, ?, NULL)""",
            (
                _token_hash(token),
                user_id,
                active_firm_id,
                _iso(now),
                _iso(now + timedelta(seconds=ttl)),
            ),
        )
        session_id = cur.lastrowid
        _record_audit(
            "session.created",
            firm_id=active_firm_id,
            actor_user_id=user_id,
            actor_type="user",
            target_type="session",
            target_id=session_id,
            metadata={
                "session_id": session_id,
                "user_id": user_id,
                "active_firm_id": active_firm_id,
            },
            conn=conn,
            commit=False,
        )
        conn.commit()
        return token
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _principal_from_row(row) -> Principal:
    return Principal(
        authenticated=True,
        demo=False,
        user_id=row["user_id"],
        subject=row["subject"],
        display_name=row["display_name"],
        firm_id=row["active_firm_id"],
        firm_name=row["firm_name"],
        role=row["role"],
        session_id=row["session_id"],
    )


def _session_row(session_id: int):
    conn = db.get_conn()
    try:
        return conn.execute(
            """SELECT s.session_id, s.user_id, s.active_firm_id, s.expires_at,
                      s.revoked_at, u.subject, u.display_name, u.disabled,
                      f.name AS firm_name, m.role
               FROM auth_sessions s
               JOIN users u ON u.user_id = s.user_id
               LEFT JOIN firms f ON f.firm_id = s.active_firm_id
               LEFT JOIN firm_memberships m
                      ON m.firm_id = s.active_firm_id
                     AND m.user_id = s.user_id
               WHERE s.session_id = ?""",
            (session_id,),
        ).fetchone()
    finally:
        conn.close()


def _valid_session_principal(row) -> Principal | None:
    if row is None or row["revoked_at"] is not None or row["disabled"]:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except (TypeError, ValueError):
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= _now():
        return None

    # A deleted/invalid membership must not leave an authorized firm attached
    # to an old session.  Clear it and let /auth/me offer the remaining firms.
    if row["active_firm_id"] is not None and row["role"] not in VALID_ROLES:
        conn = db.get_conn()
        try:
            conn.execute(
                "UPDATE auth_sessions SET active_firm_id = NULL WHERE session_id = ?",
                (row["session_id"],),
            )
            conn.commit()
        finally:
            conn.close()
        row = dict(row)
        row["active_firm_id"] = None
        row["firm_name"] = None
        row["role"] = None

    return _principal_from_row(row)


def get_session_principal(token: str | None) -> Principal | None:
    """Resolve an opaque session token, or return None if it is not valid."""

    if not token:
        return None
    conn = db.get_conn()
    try:
        row = conn.execute(
            """SELECT s.session_id, s.user_id, s.active_firm_id, s.expires_at,
                      s.revoked_at, u.subject, u.display_name, u.disabled,
                      f.name AS firm_name, m.role
               FROM auth_sessions s
               JOIN users u ON u.user_id = s.user_id
               LEFT JOIN firms f ON f.firm_id = s.active_firm_id
               LEFT JOIN firm_memberships m
                      ON m.firm_id = s.active_firm_id
                     AND m.user_id = s.user_id
               WHERE s.token_hash = ?""",
            (_token_hash(token),),
        ).fetchone()
    finally:
        conn.close()
    return _valid_session_principal(row)


def authenticate_request(request: Request, settings) -> Principal:
    """Resolve the request's principal without making an authorization claim."""

    if getattr(settings, "mode", "") == "demo":
        return DEMO_PRINCIPAL
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return get_session_principal(token) or ANONYMOUS


def principal_from_request(request: Request | None) -> Principal:
    """Return the middleware-resolved principal.

    ``None`` means a handler was invoked directly by Python code (tests or the
    CLI), outside the HTTP trust boundary.  Such calls see only demo-scoped
    synthetic data.  HTTP requests always pass through the application
    middleware, which sets ``request.state.principal``.
    """

    if request is None:
        return DEMO_PRINCIPAL
    principal = getattr(request.state, "principal", None)
    if isinstance(principal, Principal):
        return principal
    settings = getattr(getattr(request, "app", None), "state", None)
    settings = getattr(settings, "settings", None)
    if getattr(settings, "mode", "") == "demo":
        return DEMO_PRINCIPAL
    return ANONYMOUS


def require_authenticated(principal: Principal) -> Principal:
    if principal.demo:
        return principal
    if not principal.authenticated:
        _record_audit(
            "auth.denied",
            "denied",
            actor_type="anonymous",
            target_type="route",
            metadata={"reason": "not_authenticated"},
            swallow=True,
        )
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Session"},
        )
    return principal


def require_active_firm(principal: Principal) -> int | None:
    """Return the authorized firm id, or None for the read-only demo scope."""

    if principal.demo:
        return None
    require_authenticated(principal)
    if principal.firm_id is None:
        _record_audit(
            "authorization.denied",
            "denied",
            principal=principal,
            target_type="firm",
            metadata={"reason": "no_active_firm"},
            swallow=True,
        )
        raise HTTPException(
            status_code=403,
            detail="No active firm is selected for this session.",
        )
    return principal.firm_id


def require_upload_permission(principal: Principal) -> int:
    if principal.demo:
        raise HTTPException(
            status_code=403,
            detail="Uploads are disabled in read-only demo mode.",
        )
    require_authenticated(principal)
    firm_id = require_active_firm(principal)
    if principal.role not in UPLOAD_ROLES:
        _record_audit(
            "upload.denied",
            "denied",
            principal=principal,
            target_type="firm",
            target_id=firm_id,
            metadata={
                "reason": "insufficient_role",
                "role": principal.role,
            },
            swallow=True,
        )
        raise HTTPException(
            status_code=403,
            detail="Your firm role does not permit contract uploads.",
        )
    return firm_id


def authorized_contract(contract_id: int, principal: Principal) -> dict:
    """Load a contract only when it belongs to the principal's active firm.

    A foreign contract and a nonexistent contract are indistinguishable (404)
    so an attacker cannot probe another firm's contract ids.
    """

    if principal.demo:
        contract = db.get_contract_for_firm(contract_id, None)
        firm_id = None
    else:
        firm_id = require_active_firm(principal)
        contract = db.get_contract_for_firm(contract_id, firm_id)
    if contract is None:
        _record_audit(
            "contract.access_denied",
            "denied",
            principal=principal,
            firm_id=firm_id,
            target_type="contract",
            target_id=contract_id,
            metadata={"reason": "not_found_or_foreign"},
            swallow=True,
        )
        raise HTTPException(status_code=404, detail="Contract not found")
    return contract


def authorized_contracts(principal: Principal) -> list[dict]:
    if principal.demo:
        return db.list_contracts_for_firm(None)
    firm_id = require_active_firm(principal)
    return db.list_contracts_for_firm(firm_id)


def context_payload(principal: Principal) -> dict:
    memberships = list_memberships(principal.user_id) if principal.authenticated else []
    return {
        "authenticated": principal.authenticated,
        "demo": principal.demo,
        "user": (
            {
                "user_id": principal.user_id,
                "subject": principal.subject,
                "display_name": principal.display_name,
            }
            if principal.authenticated
            else None
        ),
        "active_firm": (
            {
                "firm_id": principal.firm_id,
                "name": principal.firm_name,
                "role": principal.role,
            }
            if principal.authenticated and principal.firm_id is not None
            else None
        ),
        "memberships": memberships,
        "permissions": {
            "can_read": principal.demo or principal.authenticated,
            "can_upload": principal.can_upload,
        },
    }


def switch_active_firm(principal: Principal, firm_id: int) -> Principal:
    """Change the session's active firm only after checking membership."""

    if principal.demo:
        raise HTTPException(
            status_code=403,
            detail="Firm switching is not available in read-only demo mode.",
        )
    require_authenticated(principal)
    membership = get_membership(principal.user_id, firm_id)
    if membership is None:
        _record_audit(
            "firm.switch_denied",
            "denied",
            principal=principal,
            target_type="firm",
            target_id=firm_id,
            metadata={"reason": "not_a_member"},
            swallow=True,
        )
        raise HTTPException(
            status_code=403,
            detail="You do not have access to that firm.",
        )

    conn = db.get_conn()
    try:
        cur = conn.execute(
            """UPDATE auth_sessions
               SET active_firm_id = ?
               WHERE session_id = ? AND user_id = ? AND revoked_at IS NULL""",
            (firm_id, principal.session_id, principal.user_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            _record_audit(
                "firm.switch_denied",
                "denied",
                principal=principal,
                target_type="firm",
                target_id=firm_id,
                metadata={"reason": "session_no_longer_valid"},
                swallow=True,
            )
            raise HTTPException(status_code=401, detail="Session is no longer valid.")
        _record_audit(
            "firm.switched",
            principal=principal,
            firm_id=firm_id,
            target_type="firm",
            target_id=firm_id,
            metadata={
                "previous_firm_id": principal.firm_id,
                "active_firm_id": firm_id,
            },
            conn=conn,
            commit=False,
        )
        conn.commit()
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    row = _session_row(principal.session_id)
    updated = _valid_session_principal(row)
    if updated is None:
        raise HTTPException(status_code=401, detail="Session is no longer valid.")
    return updated


def logout(principal: Principal) -> None:
    if principal.demo or not principal.authenticated:
        return
    conn = db.get_conn()
    try:
        cur = conn.execute(
            """UPDATE auth_sessions SET revoked_at = ?
               WHERE session_id = ? AND revoked_at IS NULL""",
            (_iso(_now()), principal.session_id),
        )
        if cur.rowcount == 1:
            _record_audit(
                "session.revoked",
                principal=principal,
                target_type="session",
                target_id=principal.session_id,
                metadata={"session_id": principal.session_id},
                conn=conn,
                commit=False,
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def set_session_cookie(response: Response, token: str, settings) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=int(getattr(settings, "session_ttl_seconds", DEFAULT_SESSION_TTL_SECONDS)),
        httponly=True,
        secure=bool(getattr(settings, "session_cookie_secure", True)),
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=bool(getattr(settings, "session_cookie_secure", True)),
        httponly=True,
        samesite="lax",
    )

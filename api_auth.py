"""Authentication routes: registration, login, firm context, and logout.

Registration and login use locally stored password hashes (PBKDF2-HMAC-SHA256).
A session is an opaque server-side token stored as a SHA-256 hash; the active
firm is held server-side on the session.  Firm switching and membership remain
server-checked: a client-supplied firm id is only a switch request, never proof
of access.

Demo mode bypasses authentication entirely and is read-only.  Upload mode
requires a valid session for all non-public routes.
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth

router = APIRouter(tags=["authentication"])


class RegisterRequest(BaseModel):
    subject: str
    display_name: str | None = None
    password: str


class LoginRequest(BaseModel):
    subject: str
    password: str


class FirmSwitchRequest(BaseModel):
    firm_id: int


@router.post("/auth/register")
def register(body: RegisterRequest, request: Request) -> dict:
    """Register a new user with a password.

    Open registration creates the account but no firm membership.  The new user
    can log in immediately but has no active firm (and therefore no contract
    access) until a firm is created or they are added as a member.
    """

    if not body.password or len(body.password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters.",
        )
    try:
        user_id = auth.register_user(
            body.subject, body.display_name, body.password
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "ok": True,
        "user": {
            "user_id": user_id,
            "subject": body.subject,
            "display_name": body.display_name,
        },
        "message": "Account created. Log in to create or join a firm.",
    }


@router.post("/auth/login")
def login(body: LoginRequest, request: Request) -> dict:
    """Verify credentials, create a session, and set the session cookie.

    When the user has exactly one firm membership it is selected automatically.
    With multiple memberships the session starts without an active firm and the
    client must call ``POST /auth/firm`` to choose one.
    """

    settings = getattr(getattr(request, "app", None), "state", None)
    settings = getattr(settings, "settings", None)
    user = auth.authenticate_user(body.subject, body.password)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid subject or password.",
            headers={"WWW-Authenticate": "Session"},
        )
    token = auth.create_session(user["user_id"], ttl_seconds=getattr(
        settings, "session_ttl_seconds", auth.DEFAULT_SESSION_TTL_SECONDS
    ))
    principal = auth.get_session_principal(token)
    response = JSONResponse(auth.context_payload(principal))
    auth.set_session_cookie(response, token, request.app.state.settings)
    return response


@router.get("/auth/me")
def current_context(request: Request) -> dict:
    principal = auth.principal_from_request(request)
    return auth.context_payload(principal)


@router.post("/auth/firm")
def switch_firm(body: FirmSwitchRequest, request: Request) -> dict:
    principal = auth.principal_from_request(request)
    updated = auth.switch_active_firm(principal, body.firm_id)
    return auth.context_payload(updated)


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    principal = auth.principal_from_request(request)
    auth.logout(principal)
    response = JSONResponse({"ok": True})
    auth.clear_session_cookie(response, request.app.state.settings)
    return response

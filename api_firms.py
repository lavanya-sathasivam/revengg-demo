"""Firm workspace and member-management routes.

Firms are the tenant boundary.  Every contract is scoped to exactly one firm
(via ``contracts.firm_id``), and every session holds exactly one active firm.
These routes let an authenticated user create a firm, list their memberships,
and (as a firm administrator) add, re-role, and remove members.

All authorization is enforced on the server: a caller can only act on firms
they are a member of, and membership/role is re-read from the database on
every request.
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
import db

router = APIRouter(tags=["firms"])


class FirmCreateRequest(BaseModel):
    name: str


class MemberAddRequest(BaseModel):
    subject: str
    display_name: str | None = None
    role: str = auth.ROLE_VIEWER


class MemberRoleRequest(BaseModel):
    role: str


def _require_firm_admin(principal, firm_id: int):
    """Return the caller's membership, requiring administrator role on the firm."""

    membership = auth.get_membership(principal.user_id, firm_id)
    if membership is None:
        auth._record_audit(
            "firm.access_denied",
            "denied",
            principal=principal,
            firm_id=firm_id,
            target_type="firm",
            target_id=firm_id,
            metadata={"reason": "not_a_member"},
            swallow=True,
        )
        raise HTTPException(
            status_code=403,
            detail="You are not a member of that firm.",
        )
    if membership["role"] != auth.ROLE_ADMINISTRATOR:
        auth._record_audit(
            "firm.admin_required",
            "denied",
            principal=principal,
            firm_id=firm_id,
            target_type="firm",
            target_id=firm_id,
            metadata={"reason": "insufficient_role", "role": principal.role},
            swallow=True,
        )
        raise HTTPException(
            status_code=403,
            detail="Firm administrator role is required.",
        )
    return membership


def _firm_payload(firm_id: int) -> dict:
    conn = db.get_conn()
    try:
        row = conn.execute(
            "SELECT firm_id, name, created_at FROM firms WHERE firm_id = ?",
            (firm_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@router.post("/firms")
def create_firm(body: FirmCreateRequest, request: Request) -> dict:
    """Create a firm and make the caller its administrator."""

    principal = auth.require_authenticated(
        auth.principal_from_request(request)
    )
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Firm name is required.")
    try:
        firm_id = auth.create_firm(name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    auth.add_membership(firm_id, principal.user_id, auth.ROLE_ADMINISTRATOR)
    # Set the new firm as the session's active firm.
    try:
        principal = auth.switch_active_firm(principal, firm_id)
    except HTTPException:
        # If the session is somehow no longer valid, the firm still exists;
        # report success and let the client re-establish a session.
        pass
    return {
        "ok": True,
        "firm": _firm_payload(firm_id),
        "role": auth.ROLE_ADMINISTRATOR,
        "message": "Firm created. You are its administrator.",
    }


@router.get("/firms")
def list_firms(request: Request) -> list[dict]:
    """List the firms the caller is a member of, with their role in each."""

    principal = auth.require_authenticated(
        auth.principal_from_request(request)
    )
    memberships = auth.list_memberships(principal.user_id)
    return [
        {
            "firm_id": m["firm_id"],
            "name": m["firm_name"],
            "role": m["role"],
            "created_at": m["created_at"],
            "is_active": m["firm_id"] == principal.firm_id,
        }
        for m in memberships
    ]


@router.get("/firms/{firm_id}/members")
def list_members(firm_id: int, request: Request) -> list[dict]:
    """List the members of one firm (caller must be a member)."""

    principal = auth.require_authenticated(
        auth.principal_from_request(request)
    )
    auth.require_active_firm(principal)  # must have an active firm to browse
    membership = auth.get_membership(principal.user_id, firm_id)
    if membership is None:
        raise HTTPException(
            status_code=403,
            detail="You are not a member of that firm.",
        )
    conn = db.get_conn()
    try:
        rows = conn.execute(
            """SELECT u.user_id, u.subject, u.display_name, u.disabled,
                      m.role, m.created_at
                 FROM firm_memberships m
                 JOIN users u ON u.user_id = m.user_id
                 WHERE m.firm_id = ?
                 ORDER BY u.display_name, u.subject""",
            (firm_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


@router.post("/firms/{firm_id}/members")
def add_member(firm_id: int, body: MemberAddRequest, request: Request) -> dict:
    """Add (or re-role) a member of the firm.  Administrator only."""

    principal = auth.require_authenticated(
        auth.principal_from_request(request)
    )
    _require_firm_admin(principal, firm_id)
    if body.role not in auth.VALID_ROLES:
        raise HTTPException(
            status_code=400,
            detail="Invalid role: %s" % body.role,
        )
    subject = (body.subject or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="User subject is required.")
    # Look up or provision the user by subject.  Local credentials are set
    # separately (registration / password reset); here we only establish the
    # membership boundary.
    user_id = auth.create_user(subject, body.display_name)
    auth.add_membership(firm_id, user_id, body.role)
    return {
        "ok": True,
        "user_id": user_id,
        "subject": subject,
        "role": body.role,
        "message": "Member added to the firm.",
    }


@router.patch("/firms/{firm_id}/members/{user_id}")
def change_member_role(
    firm_id: int, user_id: int, body: MemberRoleRequest, request: Request
) -> dict:
    """Change a member's role.  Administrator only; cannot demote oneself."""

    principal = auth.require_authenticated(
        auth.principal_from_request(request)
    )
    _require_firm_admin(principal, firm_id)
    if body.role not in auth.VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role: %s" % body.role)
    if user_id == principal.user_id:
        raise HTTPException(
            status_code=400,
            detail="You cannot change your own role.",
        )
    membership = auth.get_membership(user_id, firm_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="User is not a member of this firm.")
    auth.add_membership(firm_id, user_id, body.role)
    return {
        "ok": True,
        "user_id": user_id,
        "role": body.role,
        "message": "Member role updated.",
    }


@router.delete("/firms/{firm_id}/members/{user_id}")
def remove_member(firm_id: int, user_id: int, request: Request) -> dict:
    """Remove a member from the firm.  Administrator only; cannot remove oneself."""

    principal = auth.require_authenticated(
        auth.principal_from_request(request)
    )
    _require_firm_admin(principal, firm_id)
    if user_id == principal.user_id:
        raise HTTPException(
            status_code=400,
            detail="You cannot remove yourself from the firm.",
        )
    membership = auth.get_membership(user_id, firm_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="User is not a member of this firm.")
    conn = db.get_conn()
    try:
        cur = conn.execute(
            """DELETE FROM firm_memberships
               WHERE firm_id = ? AND user_id = ?""",
            (firm_id, user_id),
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise HTTPException(status_code=404, detail="User is not a member of this firm.")
        auth._record_audit(
            "membership.removed",
            principal=principal,
            firm_id=firm_id,
            target_type="membership",
            target_id="%s:%s" % (firm_id, user_id),
            metadata={"firm_id": firm_id, "user_id": user_id},
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
    return {"ok": True, "message": "Member removed from the firm."}

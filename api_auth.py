"""Authentication context and firm-switch routes.

There is intentionally no login endpoint in this phase: the identity provider
and login flow are an approved open decision.  A session is provisioned by the
future IdP integration (or by the explicit local provisioning tool), and these
routes let the client inspect its authorized firms, switch among them, and log
out.  The active firm remains server-side; the request body only selects a firm
whose membership is checked here.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import auth

router = APIRouter(tags=["authentication"])


class FirmSwitchRequest(BaseModel):
    firm_id: int


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

"""Branch B's HTTP API - the impact analysis engine, exposed over HTTP.

Owner: Person B (branch b-graph).

WHY THIS IS A SEPARATE FILE
    Person A owns app.py and the upload/retrieval routes. Person C owns the
    screen. Neither of them should have to edit the other's file to make this
    work, and nobody should have to resolve a merge conflict over app.py.

    So branch B ships its OWN router. Person A adds exactly one line to
    app.py to mount it:

        from api_b import router
        app.include_router(router)

    Everything else stays untouched, and the merge stays a one-line merge.

ENDPOINTS
    GET  /graph/{contract_id}          the relationship model (nodes + edges)
    GET  /clauses/{contract_id}        clauses with type, references, numbers
    POST /impact                       THE headline endpoint
    GET  /compare/{a}/{b}              cross-contract comparison
    GET  /demo/{contract_id}/suggest   a ready-made example change to try

REQUEST SHAPE for POST /impact
    {
      "contract_id": 1,
      "clause_id": "7.2",
      "old_value": 30,
      "new_value": 15
    }

    clause_id is a section number scoped to contract_id. The server reads that
    clause from the database and rejects the request if old_value does not
    match the value stored in the requested contract; the client's old_value
    is an expectation, never the authoritative baseline.

    old_value and new_value are numbers in the unit implied by the clause:
    days for a payment period and percentage points for an SLA. Decimal values
    such as 99.5 are accepted. The API has no unit field and performs no
    conversion between days, months, years, or percentages; a modelled value
    stored in a different unit is rejected.

RESPONSE SHAPE (all 12 items of the MVP spec)
    changed_clause     what you changed
    direct             clauses hit directly
    indirect           clauses hit through a chain
    obligations        what the parties must DO, in legal terms
    conditions         what must be true / what triggers
    consequences       what happens if it goes wrong
    deadlines          when things are due
    counterfactual     the old-vs-new arithmetic
    doubts             what we are NOT sure about, with evidence
    conflicts          contradictions found inside the contract
    evidence           every claim traced to a clause id
    review_points      what the lawyer must decide

The engine never raises on a bad input: it returns an {"error": ...} body
with HTTP 400 so the demo cannot die mid-presentation.
"""

import math
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

import adapter
import auth
import clauses as clause_tools
import graph
import impact

router = APIRouter(tags=["impact-analysis (branch b)"])

_INVALID_NUMBER = "__invalid_number__"
_NON_FINITE_NUMBER = "__non_finite_number__"


class AnalysisError(HTTPException):
    """An HTTP error with a stable machine-readable code.

    ``detail`` remains a plain string for backward compatibility. The
    application factory registers a handler that adds ``error_code`` to the
    JSON body; direct callers still see a normal ``HTTPException``.
    """

    def __init__(self, status_code, detail, error_code):
        super().__init__(status_code=status_code, detail=detail)
        self.error_code = error_code


class ImpactRequest(BaseModel):
    contract_id: int
    clause_id: str = Field(..., examples=["7.2"])
    old_value: Decimal = Field(
        ..., examples=[30],
        description="Current value in the clause's implied unit: days for "
                    "payment, percentage points for SLA.",
    )
    new_value: Decimal = Field(
        ..., examples=[15],
        description="Proposed value in the same implied unit.",
    )
    expected_clause_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-fA-F]{64}$",
        description="Optional SHA-256 of the clause text the caller read. "
                    "A mismatch is a stale request and returns HTTP 409.",
    )

    @field_validator("old_value", "new_value", mode="before")
    @classmethod
    def _reject_non_finite(cls, value):
        """Keep NaN/infinity from breaking the 422 error response itself.

        Pydantic rejects non-finite Decimals, but FastAPI then tries to echo
        the original NaN/infinity in the validation detail, which Python's
        JSON encoder cannot serialise. Substituting an ordinary marker string
        makes the rejection serialisable while still failing validation.
        """
        if isinstance(value, bool):
            return _INVALID_NUMBER
        if isinstance(value, float):
            return value if math.isfinite(value) else _NON_FINITE_NUMBER
        if isinstance(value, Decimal):
            return value if value.is_finite() else _NON_FINITE_NUMBER
        if isinstance(value, (int, str)):
            return value
        return _INVALID_NUMBER


def _contract_or_404(contract_id, request=None):
    principal = auth.principal_from_request(request)
    return auth.authorized_contract(contract_id, principal)


@router.get("/graph/{contract_id}")
def get_graph(contract_id: int, request: Request = None):
    """The contract relationship model and the build binding it came from."""
    _contract_or_404(contract_id, request)
    binding = adapter.get_graph_binding(contract_id)
    model = graph.get_graph(contract_id)
    return {
        "contract_id": contract_id,
        "binding": binding,
        "analysis_ready": binding["analysis_ready"],
        "nodes": model["nodes"],
        "edges": model["edges"],
        "counts": {
            "nodes": len(model["nodes"]),
            "edges": len(model["edges"]),
        },
    }


@router.get("/clauses/{contract_id}")
def get_clauses(contract_id: int, request: Request = None):
    """Every clause with its structure, content hash, and version binding."""
    _contract_or_404(contract_id, request)
    binding = adapter.get_graph_binding(contract_id)
    rows = adapter.list_clauses(contract_id)
    out = []
    for row in rows:
        meta = adapter.clause_metadata(row)
        out.append({
            "clause_id": row["section_number"],
            "heading": meta.get("heading", ""),
            "type": meta.get("type", "other"),
            "level": meta.get("level", 1),
            "text": row["clause_text"],
            "version_id": row.get("version_id"),
            "clause_sha256": row.get("clause_sha256"),
            "references": clause_tools.find_references(row["clause_text"] or ""),
            "numbers": clause_tools.find_numbers(row["clause_text"] or ""),
        })
    return {
        "contract_id": contract_id,
        "contract_version_id": binding.get("current_version_id"),
        "binding": binding,
        "analysis_ready": binding["analysis_ready"],
        "clauses": out,
        "count": len(out),
    }


@router.post("/impact")
def analyze(req: ImpactRequest, request: Request = None):
    """The headline endpoint: what breaks if this clause changes."""
    contract = _contract_or_404(req.contract_id, request)
    if contract.get("processing_status") == "graph_build_failed":
        raise AnalysisError(
            409,
            "Impact analysis is unavailable because graph construction "
            "failed for contract %s." % req.contract_id,
            "graph_build_failed",
        )
    report = impact.analyze_change(
        req.contract_id,
        req.clause_id,
        req.old_value,
        req.new_value,
        expected_clause_sha256=req.expected_clause_sha256,
    )
    if "error" in report:
        raise AnalysisError(
            report.get("http_status", 400),
            report["error"],
            report.get("error_code", "analysis_error"),
        )
    return report


@router.get("/compare/{contract_a}/{contract_b}")
def compare(contract_a: int, contract_b: int, request: Request = None):
    """Compare two contracts on the numbers that matter.

    Both contracts must be visible in the request's active scope.  A foreign
    contract makes the whole request a 404; no partial comparison is returned.
    """
    _contract_or_404(contract_a, request)
    _contract_or_404(contract_b, request)
    result = impact.compare_contracts(contract_a, contract_b)
    result["anomaly"] = impact.portfolio_anomaly([contract_a, contract_b])
    return result


@router.get("/demo/{contract_id}/suggest")
def suggest(contract_id: int, request: Request = None):
    """A ready-made example change, so the demo is one click and not a typo."""
    _contract_or_404(contract_id, request)
    index = impact._clause_index(contract_id)
    for sec, clause in index.items():
        if clause.get("type") != "payment":
            continue
        within = clause_tools.find_within_days(clause.get("text", ""))
        if within:
            return {
                "contract_id": contract_id,
                "clause_id": sec,
                "old_value": within,
                "new_value": max(within // 2, 1),
                "label": "Shorten the payment period in Clause %s from %d days to %d days"
                         % (sec, within, max(within // 2, 1)),
                "request": {
                    "contract_id": contract_id,
                    "clause_id": sec,
                    "old_value": within,
                    "new_value": max(within // 2, 1),
                },
            }
    return {"error": "No payment clause with a day count was found."}

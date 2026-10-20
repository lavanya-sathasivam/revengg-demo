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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import adapter
import clauses as clause_tools
import graph
import impact

router = APIRouter(tags=["impact-analysis (branch b)"])


class ImpactRequest(BaseModel):
    contract_id: int
    clause_id: str = Field(..., examples=["7.2"])
    old_value: int = Field(..., examples=[30])
    new_value: int = Field(..., examples=[15])


def _contract_or_404(contract_id):
    contract = adapter.get_contract(contract_id)
    if contract is None:
        raise HTTPException(status_code=404,
                            detail="Contract %s not found." % contract_id)
    return contract


@router.get("/graph/{contract_id}")
def get_graph(contract_id: int):
    """The contract relationship model: every node and every typed edge."""
    _contract_or_404(contract_id)
    model = graph.get_graph(contract_id)
    return {
        "contract_id": contract_id,
        "nodes": model["nodes"],
        "edges": model["edges"],
        "counts": {
            "nodes": len(model["nodes"]),
            "edges": len(model["edges"]),
        },
    }


@router.get("/clauses/{contract_id}")
def get_clauses(contract_id: int):
    """Every clause with the structure branch B extracted from it."""
    _contract_or_404(contract_id)
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
            "references": clause_tools.find_references(row["clause_text"] or ""),
            "numbers": clause_tools.find_numbers(row["clause_text"] or ""),
        })
    return {"contract_id": contract_id, "clauses": out, "count": len(out)}


@router.post("/impact")
def analyze(req: ImpactRequest):
    """The headline endpoint: what breaks if this clause changes."""
    contract = _contract_or_404(req.contract_id)
    if contract.get("processing_status") == "graph_build_failed":
        raise HTTPException(
            status_code=409,
            detail=(
                "Impact analysis is unavailable because graph construction "
                f"failed for contract {req.contract_id}."
            ),
        )
    report = impact.analyze_change(req.contract_id, req.clause_id,
                                    req.old_value, req.new_value)
    if "error" in report:
        raise HTTPException(status_code=400, detail=report["error"])
    return report


@router.get("/compare/{contract_a}/{contract_b}")
def compare(contract_a: int, contract_b: int):
    """Compare two contracts on the numbers that matter."""
    _contract_or_404(contract_a)
    _contract_or_404(contract_b)
    result = impact.compare_contracts(contract_a, contract_b)
    result["anomaly"] = impact.portfolio_anomaly([contract_a, contract_b])
    return result


@router.get("/demo/{contract_id}/suggest")
def suggest(contract_id: int):
    """A ready-made example change, so the demo is one click and not a typo."""
    _contract_or_404(contract_id)
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

"""Phase 4 HTTP API: retrieval, incremental indexing and grounded QA."""

from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

import auth
import contract_search
import embeddings
import qa
import retrieval_index

router = APIRouter(tags=["retrieval and grounded QA"])

MAX_QUESTION_LENGTH = 4000


class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=MAX_QUESTION_LENGTH,
        description="Natural-language question about one contract or a collection.",
    )
    contract_id: Optional[int] = Field(
        default=None,
        description="One contract in focus. Omit to search every authorized contract.",
    )
    contract_ids: Optional[List[int]] = Field(
        default=None,
        description="Authorized contract collection to search.",
    )
    top_k: int = Field(default=8, ge=1, le=contract_search.MAX_TOP_K)

    @field_validator("question")
    @classmethod
    def _reject_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question must not be empty or whitespace-only.")
        return value.strip()

    @field_validator("contract_ids")
    @classmethod
    def _reject_empty_collection(cls, value):
        if value is not None and not value:
            raise ValueError("contract_ids must contain at least one contract.")
        return value


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=MAX_QUESTION_LENGTH)
    contract_id: Optional[int] = None
    contract_ids: Optional[List[int]] = None
    top_k: int = Field(default=8, ge=1, le=contract_search.MAX_TOP_K)

    @field_validator("query")
    @classmethod
    def _reject_blank_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Query must not be empty or whitespace-only.")
        return value.strip()

    @field_validator("contract_ids")
    @classmethod
    def _reject_empty_collection(cls, value):
        if value is not None and not value:
            raise ValueError("contract_ids must contain at least one contract.")
        return value


def _resolve_scope(request: Request, contract_id=None, contract_ids=None):
    """Authorize and return contract ids; foreign ids fail closed with 404."""

    principal = auth.principal_from_request(request)
    if contract_id is not None and contract_ids is not None:
        raise HTTPException(
            status_code=422,
            detail="Pass either contract_id or contract_ids, not both.",
        )
    if contract_id is not None:
        contract = auth.authorized_contract(int(contract_id), principal)
        return [int(contract["contract_id"])]
    if contract_ids is not None:
        unique = []
        seen = set()
        for value in contract_ids:
            contract = auth.authorized_contract(int(value), principal)
            cid = int(contract["contract_id"])
            if cid not in seen:
                seen.add(cid)
                unique.append(cid)
        return unique
    return [
        int(contract["contract_id"])
        for contract in auth.authorized_contracts(principal)
    ]


def _index_payload(status: dict) -> dict:
    return status


@router.get("/qa/status")
def qa_status(request: Request = None):
    """Report the retrieval model and whether an optional LLM rewrite is on."""

    import llm

    return {
        "embedding_model": embeddings.model_metadata(),
        "retrieval": {
            "hybrid": True,
            "semantic": True,
            "keyword": True,
            "reranking": True,
            "default_top_k": contract_search.DEFAULT_TOP_K,
            "max_top_k": contract_search.MAX_TOP_K,
        },
        "answer_generation": {
            "mode": "extractive-grounded",
            "optional_llm_rewrite": qa._llm_enabled(),
            "llm_provider_configured": bool(llm.provider_available()),
        },
    }


@router.post("/qa/ask")
def ask(payload: AskRequest, request: Request = None):
    """Answer a natural-language question from authorized clause evidence."""

    contract_ids = _resolve_scope(
        request, payload.contract_id, payload.contract_ids
    )
    try:
        return qa.answer_question(
            payload.question, contract_ids, top_k=payload.top_k
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/retrieval/search")
def search(payload: SearchRequest, request: Request = None):
    """Return ranked evidence without generating a narrative answer."""

    contract_ids = _resolve_scope(
        request, payload.contract_id, payload.contract_ids
    )
    try:
        return contract_search.search(
            payload.query, contract_ids, top_k=payload.top_k
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/retrieval/index/{contract_id}")
def get_index_status(contract_id: int, request: Request = None):
    """Report index readiness for one authorized contract."""

    principal = auth.principal_from_request(request)
    auth.authorized_contract(contract_id, principal)
    return _index_payload(retrieval_index.embedding_status(contract_id))


@router.post("/retrieval/index/{contract_id}")
def build_index(contract_id: int, request: Request = None):
    """Build or incrementally update one contract's clause embeddings."""

    principal = auth.principal_from_request(request)
    auth.authorized_contract(contract_id, principal)
    auth.require_upload_permission(principal)
    try:
        result = retrieval_index.index_contract(contract_id)
    except retrieval_index.IndexNotWritable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "index": result,
        "status": retrieval_index.embedding_status(contract_id),
    }


@router.get("/retrieval/status")
def retrieval_status(request: Request = None):
    """Index readiness across the active firm or demo scope."""

    principal = auth.principal_from_request(request)
    contracts = auth.authorized_contracts(principal)
    return {
        "model": embeddings.model_metadata(),
        "contracts": [
            retrieval_index.embedding_status(contract["contract_id"])
            for contract in contracts
        ],
    }

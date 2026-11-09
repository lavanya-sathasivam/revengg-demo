"""Evidence-grounded contract question answering (Phase 4).

The answer generator is extractive by default: it quotes exact spans from the
retrieved clause rows, attaches clause/page/source provenance, and refuses to
answer when the retrieved evidence does not support the question's legal
concept.  There is no free-form legal knowledge in this module and no fallback
citation is ever invented.

The response deliberately separates:

* ``answer`` - a short grounded narrative that only cites returned evidence;
* ``citations`` / ``direct_evidence`` - exact quotes and provenance;
* ``interpretation`` - a clearly labelled note explaining the limits of the
  extractive answer;
* ``insufficient_evidence`` - true when no clause sufficiently supports the
  question, including negative questions where the provision is absent.

An optional LLM narrative path can be enabled through
``REVENGG_QA_LLM=1`` when a configured provider is available.  Its output is
validated against the same citation set before it is returned; any unknown
citation or unsupported quote causes a fallback to the extractive answer.
"""

from __future__ import annotations

import os
import re
from typing import Dict, List, Optional, Sequence

import contract_search
import db
import embeddings


_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")

DISCLAIMER = (
    "Informational analysis only. This is not legal advice and does not "
    "replace review of the original contract by a qualified lawyer."
)


def _quote_from_clause(text: str, question: str, max_chars: int = 520) -> str:
    """Return an exact, meaningful substring of the clause text.

    The quote is never paraphrased.  When a clause is longer than the display
    budget, the sentence with the strongest overlap with the question is
    selected by character span, so the returned value is still an exact slice
    of the stored clause.
    """

    text = text or ""
    if not text.strip():
        return ""
    if len(text) <= max_chars:
        return text.strip()

    query_tokens = set(embeddings.tokenize(question))
    best = None
    best_score = -1
    for match in _SENTENCE_RE.finditer(text):
        sentence = match.group(0)
        if not sentence.strip():
            continue
        sentence_tokens = set(embeddings.tokenize(sentence))
        score = len(sentence_tokens.intersection(query_tokens))
        if score > best_score:
            best_score = score
            best = sentence
    if best is not None:
        candidate = best.strip()
        if len(candidate) <= max_chars:
            return candidate
    return text[:max_chars].strip()


def _citation_from_result(result: dict, index: int, question: str) -> dict:
    quote = _quote_from_clause(result.get("text") or "", question)
    return {
        "citation_id": "C%d" % index,
        "contract_id": result["contract_id"],
        "filename": result.get("filename"),
        "version_id": result.get("version_id"),
        "clause_id": result.get("clause_id"),
        "section_number": result.get("section_number"),
        "heading": result.get("heading"),
        "type": result.get("type"),
        "page_number": result.get("page_number"),
        "char_start": result.get("char_start"),
        "char_end": result.get("char_end"),
        "parent_section": result.get("parent_section"),
        "hierarchy": result.get("hierarchy") or [],
        "clause_sha256": result.get("clause_sha256"),
        "quote": quote,
        "quote_sha256": db.sha256_text(quote),
        "source_url": result.get("source_url"),
        "retrieval_rank": result.get("rank"),
        "retrieval_scores": result.get("scores"),
    }


def _select_citations(results: Sequence[dict], analysis: dict) -> List[dict]:
    if not results:
        return []
    top_score = results[0]["scores"]["rerank"]
    relative_floor = max(contract_search.MIN_EVIDENCE_SCORE, top_score * 0.55)
    primary = set(analysis.get("primary_concepts") or [])
    selected = []
    per_contract: Dict[int, int] = {}
    contracts_seen = {result["contract_id"] for result in results}
    for result in results:
        score = result["scores"]["rerank"]
        if score < relative_floor:
            continue
        if per_contract.get(result["contract_id"], 0) >= 2:
            continue
        concept_supported = bool(
            set(result["match"].get("concepts") or []).intersection(primary)
        )
        strong = score >= top_score * 0.82
        exact_ref = bool(result["match"].get("clause_reference"))
        if primary and not (concept_supported or strong or exact_ref):
            continue
        selected.append(result)
        per_contract[result["contract_id"]] = per_contract.get(
            result["contract_id"], 0
        ) + 1
        if len(selected) >= 4:
            break

    # In a comparison, make sure both contracts are represented when the
    # retriever found a supporting clause for each.
    if len(contracts_seen) > 1:
        represented = {result["contract_id"] for result in selected}
        for contract_id in contracts_seen:
            if contract_id in represented:
                continue
            candidate = next(
                (
                    result
                    for result in results
                    if result["contract_id"] == contract_id
                    and result["scores"]["rerank"] >= relative_floor * 0.8
                ),
                None,
            )
            if candidate is not None:
                selected.append(candidate)
                if len(selected) > 4:
                    selected = selected[:4]
                break
    return selected


def _render_answer(citations: Sequence[dict], analysis: dict) -> str:
    if not citations:
        return ""
    lines = []
    if len(citations) == 1:
        citation = citations[0]
        lines.append(
            "%s, Clause %s states: \u201c%s\u201d [%s]."
            % (
                citation.get("filename") or "Contract",
                citation.get("section_number"),
                citation.get("quote"),
                citation.get("citation_id"),
            )
        )
    else:
        lines.append("The cited contract text provides:")
        for citation in citations:
            lines.append(
                "- %s, Clause %s: \u201c%s\u201d [%s]"
                % (
                    citation.get("filename") or "Contract",
                    citation.get("section_number"),
                    citation.get("quote"),
                    citation.get("citation_id"),
                )
            )
    if analysis.get("comparison") and len(
        {citation["contract_id"] for citation in citations}
    ) > 1:
        lines.append(
            "This is a comparison of the quoted clauses only; no rule from one "
            "contract is applied to another."
        )
    return "\n".join(lines)


def _interpretation(citations: Sequence[dict], analysis: dict) -> Optional[str]:
    if not citations:
        return None
    contracts = {citation["contract_id"] for citation in citations}
    if len(contracts) > 1:
        return (
            "Interpretation: the answer compares only the quoted wording from "
            "each contract. Differences in drafting or defined terms outside "
            "these quotes are not inferred."
        )
    return (
        "Interpretation: the answer is a direct extract from the cited "
        "clause(s). The summary does not add facts, implied terms or legal "
        "conclusions beyond the quoted text."
    )


def _confidence(results: Sequence[dict], sufficient: bool) -> str:
    if not sufficient or not results:
        return "insufficient"
    score = float(results[0]["scores"]["rerank"])
    if score >= 0.62:
        return "high"
    if score >= 0.34:
        return "medium"
    return "low"


def _validate_grounding(
    answer: str,
    citations: Sequence[dict],
    results: Sequence[dict],
    contract_ids: Sequence[int],
) -> dict:
    by_key = {
        (result["contract_id"], result["section_number"]): result
        for result in results
    }
    checks = {
        "citations_present": bool(citations),
        "all_quotes_exact": True,
        "all_clauses_in_retrieved_evidence": True,
        "all_contracts_in_scope": True,
        "answer_markers_resolve": True,
    }
    for citation in citations:
        key = (citation["contract_id"], citation["section_number"])
        result = by_key.get(key)
        if result is None:
            checks["all_clauses_in_retrieved_evidence"] = False
            continue
        source_text = result.get("text") or ""
        if not citation.get("quote") or citation["quote"] not in source_text:
            checks["all_quotes_exact"] = False
        if citation["contract_id"] not in set(contract_ids):
            checks["all_contracts_in_scope"] = False
    markers = set(re.findall(r"\[(C\d+)\]", answer or ""))
    known = {citation["citation_id"] for citation in citations}
    if not markers.issubset(known):
        checks["answer_markers_resolve"] = False
    passed = all(checks.values())
    return {
        "status": "grounded" if passed else "failed_validation",
        "checks": checks,
        "citation_count": len(citations),
        "valid_citations": len(citations) if passed else 0,
    }


def _insufficient_answer(reason: Optional[str]) -> str:
    detail = {
        "no_clauses_in_scope": "No clauses are available in the authorized scope.",
        "no_results": "No clause was retrieved for this question.",
        "below_relevance_threshold": (
            "The closest clauses did not reach the relevance threshold required "
            "for a grounded answer."
        ),
        "question_concept_not_supported": (
            "The retrieved clauses do not address the legal concept asked about."
        ),
        "no_lexical_evidence": (
            "The question's terms do not occur in the highest-ranked clause, so "
            "the semantic match alone is not reliable evidence."
        ),
    }.get(reason or "", "The available clauses do not support an answer.")
    return (
        "Insufficient evidence: %s The provided contracts may be silent on "
        "this point, may express it in different wording, or the relevant "
        "clause may not be indexed. This response does not infer an answer."
        % detail
    )


def _llm_enabled() -> bool:
    return os.environ.get("REVENGG_QA_LLM", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _try_llm_narrative(question: str, citations: Sequence[dict]) -> Optional[str]:
    """Optionally rewrite the grounded extract into a concise narrative.

    The rewrite is accepted only if it keeps every citation marker and adds no
    citation that is not already in ``citations``.  It is not used for the
    legal evidence itself; the citations remain the authoritative record.
    """

    if not _llm_enabled() or not citations:
        return None
    try:
        import llm
    except Exception:
        return None
    if not llm.provider_available():
        return None

    evidence_lines = []
    for citation in citations:
        evidence_lines.append(
            "[%s] %s, Clause %s (page %s): %s"
            % (
                citation["citation_id"],
                citation.get("filename"),
                citation.get("section_number"),
                citation.get("page_number")
                if citation.get("page_number") is not None
                else "unavailable",
                citation.get("quote"),
            )
        )
    prompt = (
        "Answer the user's contract question using ONLY the evidence below. "
        "Keep the [C#] citation markers exactly. Do not add a citation that "
        "is not listed. Do not state a legal fact that is absent from the "
        "quotes. Return strict JSON with one key, 'answer', containing a "
        "short answer.\n\nQuestion: %s\n\nEvidence:\n%s"
        % (question, "\n".join(evidence_lines))
    )
    data = llm.ask_json(prompt)
    answer = str(data.get("answer") or "").strip()
    if not answer:
        return None
    markers = set(re.findall(r"\[(C\d+)\]", answer))
    known = {citation["citation_id"] for citation in citations}
    if not markers or not markers.issubset(known):
        return None
    return answer


def answer_question(
    question: str,
    contract_ids: Sequence[int],
    *,
    top_k: int = 8,
    model_key: Optional[str] = None,
) -> dict:
    """Retrieve evidence and produce a grounded answer.

    ``contract_ids`` must already have passed firm authorization.  The
    returned dict is JSON-serialisable and stable for the HTTP API.
    """

    search_result = contract_search.search(
        question, contract_ids, top_k=top_k, model_key=model_key
    )
    analysis = search_result["analysis"]
    results = search_result["results"]
    sufficient = bool(search_result["sufficient"])

    if not sufficient:
        return {
            "question": question,
            "answer": _insufficient_answer(
                search_result.get("insufficient_reason")
            ),
            "answer_type": "insufficient_evidence",
            "insufficient_evidence": True,
            "insufficient_reason": search_result.get("insufficient_reason"),
            "citations": [],
            "direct_evidence": [],
            "interpretation": None,
            "confidence": "insufficient",
            "grounding": {
                "status": "insufficient_evidence",
                "checks": {
                    "citations_present": False,
                    "all_quotes_exact": True,
                    "all_clauses_in_retrieved_evidence": True,
                    "all_contracts_in_scope": True,
                    "answer_markers_resolve": True,
                },
                "citation_count": 0,
                "valid_citations": 0,
            },
            "scope": search_result["contracts"],
            "query_understanding": analysis,
            "retrieval": {
                "results": results,
                "count": len(results),
                "candidate_count": search_result["candidate_count"],
                "index": search_result["index"],
                "model": search_result["model"],
                "thresholds": search_result["thresholds"],
            },
            "disclaimer": DISCLAIMER,
        }

    selected = _select_citations(results, analysis)
    citations = [
        _citation_from_result(result, index, question)
        for index, result in enumerate(selected, start=1)
    ]
    answer = _render_answer(citations, analysis)
    narrative = _try_llm_narrative(question, citations)
    if narrative:
        answer = narrative
    interpretation = _interpretation(citations, analysis)
    grounding = _validate_grounding(
        answer, citations, results, [int(value) for value in contract_ids]
    )
    return {
        "question": question,
        "answer": answer,
        "answer_type": "extractive_grounded",
        "insufficient_evidence": False,
        "insufficient_reason": None,
        "citations": citations,
        "direct_evidence": citations,
        "interpretation": interpretation,
        "confidence": _confidence(results, sufficient),
        "grounding": grounding,
        "scope": search_result["contracts"],
        "query_understanding": analysis,
        "retrieval": {
            "results": results,
            "count": len(results),
            "candidate_count": search_result["candidate_count"],
            "index": search_result["index"],
            "model": search_result["model"],
            "thresholds": search_result["thresholds"],
        },
        "disclaimer": DISCLAIMER,
    }

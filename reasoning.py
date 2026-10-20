from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

import retrieval


def _normalize_text_safe(text: Any) -> str:
    """Normalize text using retrieval.normalize_text if available, else fallback."""
    try:
        if hasattr(retrieval, "normalize_text"):
            res = retrieval.normalize_text(text)
            if isinstance(res, str):
                return res
    except Exception:
        pass
    # Fallback normalization
    if text is None:
        return ""
    s = str(text)
    # collapse whitespace
    import re

    s2 = re.sub(r"\s+", " ", s).strip().lower()
    return s2


def _deep_copy(obj: Any) -> Any:
    """Deep copy to avoid mutating inputs/outputs."""
    try:
        return copy.deepcopy(obj)
    except Exception:
        # Fallback shallow copy where deep fails
        if isinstance(obj, dict):
            return dict(obj)
        if isinstance(obj, list):
            return list(obj)
        return obj


def _safe_get(d: Any, key: str, default: Any) -> Any:
    if isinstance(d, dict):
        return d.get(key, default)
    return default


def _build_source_details(authority: Dict[str, Any], include_sources: bool) -> Optional[Dict[str, Any]]:
    if not include_sources:
        return None
    source_details: Dict[str, Any] = {}
    source_details["source_name"] = authority.get("source_name") if authority.get("source_name") is not None else ""
    source_details["source_url"] = authority.get("source_url") if "source_url" in authority else None
    # ensure None preserved
    if "source_url" not in authority:
        source_details["source_url"] = None
    else:
        source_details["source_url"] = authority.get("source_url")
    source_details["source_granularity"] = authority.get("source_granularity") if authority.get("source_granularity") is not None else "unknown"
    source_details["source_record_url"] = authority.get("source_record_url") if "source_record_url" in authority else None
    if "source_record_url" not in authority:
        source_details["source_record_url"] = None
    else:
        source_details["source_record_url"] = authority.get("source_record_url")
    source_details["additional_source_name"] = authority.get("additional_source_name") if "additional_source_name" in authority else None
    if "additional_source_name" not in authority:
        source_details["additional_source_name"] = None
    else:
        source_details["additional_source_name"] = authority.get("additional_source_name")
    source_details["additional_source_url"] = authority.get("additional_source_url") if "additional_source_url" in authority else None
    if "additional_source_url" not in authority:
        source_details["additional_source_url"] = None
    else:
        source_details["additional_source_url"] = authority.get("additional_source_url")
    vstatus = authority.get("verification_status")
    if vstatus is None:
        vstatus = "needs_verification"
    source_details["verification_status"] = vstatus
    return source_details


def _build_matched_evidence(authority: Dict[str, Any], clause_text: str) -> Optional[Dict[str, Any]]:
    matched_keywords = authority.get("matched_keywords")
    if not isinstance(matched_keywords, list) or len(matched_keywords) == 0:
        return None
    # copy actual matched keywords
    matched_terms = []
    for mk in matched_keywords:
        if mk is None:
            continue
        matched_terms.append(mk)
    if len(matched_terms) == 0:
        return None
    # normalized snippet from input text, at most 200 chars
    norm_snip = _normalize_text_safe(clause_text)
    if len(norm_snip) > 200:
        normalized_snippet = norm_snip[:200]
    else:
        normalized_snippet = norm_snip
    # if snippet empty, try to build from authority summary cautiously
    if not normalized_snippet:
        summ = authority.get("summary")
        if isinstance(summ, str) and summ.strip():
            ns = _normalize_text_safe(summ)
            normalized_snippet = ns[:200] if len(ns) > 200 else ns
    evidence = {
        "authority_id": authority.get("id"),
        "evidence_type": "keyword_match",
        "matched_terms": matched_terms,
        "field_matched": None,
        "normalized_snippet": normalized_snippet,
    }
    return evidence


def explain_clause(
    clause_text: str,
    retrieved_authorities: Optional[List[Dict[str, Any]]] = None,
    min_score: int = 1,
    include_sources: bool = True,
) -> Dict[str, Any]:
    """
    Generate structured, template-based explanation for a clause/text.
    """
    disclaimer = "This explanation is for informational/demo purposes only and does not constitute legal advice. This is not legal advice."

    # Handle empty/whitespace
    norm_input = _normalize_text_safe(clause_text)
    if not norm_input:
        input_obj = {
            "type": "clause",
            "text": clause_text if isinstance(clause_text, str) else ("" if clause_text is None else str(clause_text)),
            "normalized_preview": "",
        }
        result = {
            "input": input_obj,
            "summary": "No clause text provided.",
            "key_points": [
                "Empty input text detected.",
                "No authorities retrieved or explained.",
            ],
            "relevant_authorities": [],
            "matched_evidence": [],
            "possible_legal_relevance": [],
            "uncertainties_and_missing_information": [
                "Empty input text provided; cannot identify clause/topic from input.",
                "Keyword matching alone does not determine validity, enforceability, breach, liability, or any legal outcome.",
                "Verification status: no authorities retrieved to assess.",
                "Insufficient information to infer possible legal relevance.",
            ],
            "reasoning_trace": [
                "Step 1: Detect empty/whitespace-only input -> return empty-results structure.",
            ],
            "disclaimer": disclaimer,
            "meta": {
                "min_score": min_score,
                "include_sources": include_sources,
                "retrieved_count": 0,
                "used_internal_retrieval": False,
                "retrieval_error": False,
            },
        }
        return result

    # Determine authorities source
    used_internal_retrieval = False
    retrieval_error_flag = False
    auth_list: List[Dict[str, Any]] = []

    if retrieved_authorities is None:
        # Try to use explain_with_retrieval logic by calling it would be recursive if not careful;
        # but better to call retrieval directly here to avoid recursion
        try:
            res_ret = retrieval.search_authorities(clause_text, authorities=None, min_score=min_score)
            if isinstance(res_ret, list):
                auth_list = res_ret
            else:
                auth_list = []
            used_internal_retrieval = True
        except Exception:
            auth_list = []
            retrieval_error_flag = True
            used_internal_retrieval = True
    else:
        # Use supplied authorities as-is; do not filter by min_score
        if isinstance(retrieved_authorities, list):
            auth_list = retrieved_authorities
        else:
            auth_list = []
        used_internal_retrieval = False

    # Process authorities
    relevant_authorities: List[Dict[str, Any]] = []
    matched_evidence_list: List[Dict[str, Any]] = []

    for item in auth_list:
        if not isinstance(item, dict):
            continue  # ignore non-dict
        # deep copy
        auth_copy = _deep_copy(item)
        # ensure verification status default
        vstatus = auth_copy.get("verification_status")
        if vstatus is None:
            auth_copy["verification_status"] = "needs_verification"
            vstatus = "needs_verification"
        # flags
        auth_copy["is_retrieved"] = True
        if vstatus == "confirmed":
            auth_copy["is_verified"] = True
        else:
            auth_copy["is_verified"] = False
        # why_matched: describe actual retrieval evidence only. applicable_topics
        # are descriptive metadata and never caused the match.
        mk = auth_copy.get("matched_keywords")
        if isinstance(mk, list) and len(mk) > 0:
            mk_str = ", ".join(str(x) for x in mk)
            auth_copy["why_matched"] = f"Matched curated keyword(s): {mk_str}."
        else:
            auth_copy["why_matched"] = "Matched via numeric section citation."
        # source details
        auth_copy["source_details"] = _build_source_details(auth_copy, include_sources)
        relevant_authorities.append(auth_copy)
        # evidence
        ev = _build_matched_evidence(auth_copy, clause_text)
        if ev is not None:
            matched_evidence_list.append(ev)

    # Build output
    input_obj = {
        "type": "clause",
        "text": clause_text if isinstance(clause_text, str) else str(clause_text),
        "normalized_preview": norm_input[:200] if len(norm_input) > 200 else norm_input,
    }

    if retrieval_error_flag:
        result = {
            "input": input_obj,
            "summary": "Retrieval failed; explanation cannot be generated from retrieved authorities.",
            "key_points": [
                "Retrieval call raised an exception.",
                "No authorities were fabricated.",
                "Keyword matching not performed successfully.",
            ],
            "relevant_authorities": [],
            "matched_evidence": [],
            "possible_legal_relevance": [],
            "uncertainties_and_missing_information": [
                "Retrieval failed and no authorities were retrieved; no conclusion can be drawn from that failure.",
                "Keyword matching alone does not determine validity, enforceability, breach, liability, or any legal outcome.",
                "Verification status: unknown due to retrieval failure.",
            ],
            "reasoning_trace": [
                "Step 1: Normalize input text.",
                "Step 2: Attempt internal retrieval via retrieval.search_authorities() -> exception.",
                "Step 3: Return structured error result without fabricating authorities.",
            ],
            "disclaimer": disclaimer,
            "meta": {
                "min_score": min_score,
                "include_sources": include_sources,
                "retrieved_count": 0,
                "used_internal_retrieval": True,
                "retrieval_error": True,
            },
        }
        return result

    if len(relevant_authorities) == 0:
        result = {
            "input": input_obj,
            "summary": "No matching authorities found in the dataset for the provided text.",
            "key_points": [
                "Input normalized; query tokens checked against dataset.",
                "No authorities met retrieval criteria.",
            ],
            "relevant_authorities": [],
            "matched_evidence": [],
            "possible_legal_relevance": [],
            "uncertainties_and_missing_information": [
                "No authorities retrieved by keyword matching; keywords may not match the existing dataset.",
                "Keyword matching alone does not determine validity, enforceability, breach, liability, or any legal outcome.",
                "Verification status: no authorities retrieved to assess.",
                "The dataset is limited to 5 Indian contract law authorities for demo purposes; other relevant authorities may exist.",
            ],
            "reasoning_trace": [
                "Step 1: Normalize input text.",
                "Step 2: Prepare query/context for retrieval.",
                "Step 3: Retrieve authorities (keyword-based) -> 0 results.",
                "Step 4: No authorities to explain; return empty authorities with uncertainties.",
                "Step 5: Include disclaimer.",
            ],
            "disclaimer": disclaimer,
            "meta": {
                "min_score": min_score,
                "include_sources": include_sources,
                "retrieved_count": 0,
                "used_internal_retrieval": used_internal_retrieval,
                "retrieval_error": False,
            },
        }
        return result

    # Non-empty results
    # Build a neutral summary from actual match evidence only. applicable_topics
    # are descriptive metadata and do not establish that a clause relates to them.
    matched_terms_all = []
    for ra in relevant_authorities:
        mk_list = ra.get("matched_keywords")
        if isinstance(mk_list, list):
            for term in mk_list:
                if term is not None:
                    term_str = str(term)
                    if term_str not in matched_terms_all:
                        matched_terms_all.append(term_str)
    if matched_terms_all:
        summary_text = (
            "Clause/text excerpt matches curated keyword evidence: "
            + ", ".join(matched_terms_all[:5])
            + ". This is not a legal conclusion."
        )
    else:
        summary_text = (
            "Clause/text excerpt matches a numeric section citation in the "
            "reference dataset. This is not a legal conclusion."
        )

    # key points
    verified_count = sum(1 for ra in relevant_authorities if ra.get("is_verified") is True)
    key_points = [
        "Input normalized (case-insensitive, whitespace collapsed).",
        "Matching uses curated keywords and numeric section citations; summaries and topics are not scored.",
        f"{len(relevant_authorities)} authority records retrieved.",
        f"Verification status: {verified_count} verified, {len(relevant_authorities) - verified_count} not verified (needs_verification or missing).",
    ]

    # possible legal relevance
    possible_legal_relevance = []
    for ra in relevant_authorities[:3]:
        atype = ra.get("authority_type") or ""
        if "statute" in str(atype):
            possible_legal_relevance.append("May relate to statutory provisions based solely on keyword matching; this is not a determination of legal outcome.")
        elif "judgment" in str(atype):
            possible_legal_relevance.append("May relate to judicial principles based solely on keyword matching; this is not a determination of legal outcome.")
        else:
            possible_legal_relevance.append("May relate to relevant legal topics based solely on keyword matching; this is not a determination of legal outcome.")
    if len(possible_legal_relevance) == 0:
        possible_legal_relevance.append("Possible relevance to topics for human review based solely on keyword matching; this is not a determination.")

    # uncertainties
    uncertainties = [
        "Keyword matching alone does not determine validity, enforceability, breach, liability, or any legal outcome.",
        "All retrieved references are not automatically verified; retrieved authorities are marked retrieved (is_retrieved=True).",
        "Source granularity varies (act_pdf_only / act_pdf_and_additional_government_reference / unknown); section-specific URLs may not be confirmed.",
        "Missing context: facts, parties, governing law specifics, timeline, jurisdictional nuances, and any exceptions/qualifications.",
        "The dataset is limited to 5 Indian contract law authorities for demo purposes; other relevant authorities may exist.",
        "No quotations or interpretive conclusions are provided; only descriptive metadata and match signals.",
    ]
    if verified_count == 0:
        uncertainties.append("None of the retrieved authorities are marked verified (verification_status != 'confirmed'); treat as retrieved references only.")

    # reasoning trace
    reasoning_trace = [
        "Step 1: Normalize input text using retrieval.normalize_text logic (case-insensitive, whitespace collapsed).",
        "Step 2: Prepare query/context from normalized input.",
        "Step 3: Retrieve candidate authorities via retrieval.search_authorities() when using internal retrieval (or use supplied retrieved_authorities).",
        "Step 4: Accept only dictionary entries; ignore non-dictionary items.",
        "Step 5: Deep-copy each accepted authority and preserve order; do not mutate inputs.",
        "Step 6: Set is_retrieved=True and is_verified = (verification_status == 'confirmed'); default verification_status to 'needs_verification' if missing.",
        "Step 7: Build deterministic why_matched from matched_keywords, using a numeric section-citation description when no keywords matched; construct matched_evidence from actual matched_keywords only.",
        "Step 8: Attach source_details (respecting include_sources) and generate cautious summaries/key points/uncertainties with disclaimer.",
    ]

    result = {
        "input": input_obj,
        "summary": summary_text,
        "key_points": key_points,
        "relevant_authorities": relevant_authorities,
        "matched_evidence": matched_evidence_list,
        "possible_legal_relevance": possible_legal_relevance,
        "uncertainties_and_missing_information": uncertainties,
        "reasoning_trace": reasoning_trace,
        "disclaimer": disclaimer,
        "meta": {
            "min_score": min_score,
            "include_sources": include_sources,
            "retrieved_count": len(relevant_authorities),
            "used_internal_retrieval": used_internal_retrieval,
            "retrieval_error": False,
        },
    }
    return result


def explain_with_retrieval(
    clause_text: str,
    min_score: int = 1,
    include_sources: bool = True,
) -> Dict[str, Any]:
    """
    Retrieve authorities using retrieval.search_authorities() and explain results.
    """
    # Try to call retrieval and pass results; if retrieval raises, return structured error
    try:
        retrieved = retrieval.search_authorities(clause_text, authorities=None, min_score=min_score)
    except Exception:
        # Return structured error result
        disclaimer = "This explanation is for informational/demo purposes only and does not constitute legal advice. This is not legal advice."
        norm_input = _normalize_text_safe(clause_text)
        input_obj = {
            "type": "clause",
            "text": clause_text if isinstance(clause_text, str) else str(clause_text),
            "normalized_preview": norm_input[:200] if len(norm_input) > 200 else norm_input,
        }
        if not norm_input:
            return {
                "input": input_obj,
                "summary": "No clause text provided.",
                "key_points": ["Empty input text detected.", "No authorities retrieved or explained."],
                "relevant_authorities": [],
                "matched_evidence": [],
                "possible_legal_relevance": [],
                "uncertainties_and_missing_information": [
                    "Empty input text provided; cannot identify clause/topic from input.",
                    "Keyword matching alone does not determine validity, enforceability, breach, liability, or any legal outcome.",
                    "Verification status: no authorities retrieved to assess.",
                    "Insufficient information to infer possible legal relevance.",
                ],
                "reasoning_trace": ["Step 1: Detect empty/whitespace-only input -> return empty-results structure."],
                "disclaimer": disclaimer,
                "meta": {"min_score": min_score, "include_sources": include_sources, "retrieved_count": 0, "used_internal_retrieval": True, "retrieval_error": False},
            }
        return {
            "input": input_obj,
            "summary": "Retrieval failed; explanation cannot be generated from retrieved authorities.",
            "key_points": ["Retrieval call raised an exception.", "No authorities were fabricated."],
            "relevant_authorities": [],
            "matched_evidence": [],
            "possible_legal_relevance": [],
            "uncertainties_and_missing_information": [
                "Retrieval failed and no authorities were retrieved; no conclusion can be drawn from that failure.",
                "Keyword matching alone does not determine validity, enforceability, breach, liability, or any legal outcome.",
            ],
            "reasoning_trace": [
                "Step 1: Normalize input text.",
                "Step 2: Attempt retrieval via retrieval.search_authorities() -> exception.",
                "Step 3: Return structured error result without fabricating authorities.",
            ],
            "disclaimer": disclaimer,
            "meta": {"min_score": min_score, "include_sources": include_sources, "retrieved_count": 0, "used_internal_retrieval": True, "retrieval_error": True},
        }
    # Pass retrieved results to explain_clause (preserve order)
    res = explain_clause(clause_text, retrieved_authorities=retrieved, min_score=min_score, include_sources=include_sources)
    try:
        if isinstance(res.get("meta"), dict):
            res["meta"]["used_internal_retrieval"] = True
    except Exception:
        pass
    return res

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional, Union


# Common English words that carry no retrieval signal. They are dropped before
# scoring so a summary containing "the" or "this" cannot add points.
STOPWORDS = frozenset({
    "a", "about", "an", "and", "any", "are", "as", "at", "be", "been",
    "but", "by", "can", "do", "does", "for", "from", "had", "has", "have",
    "he", "her", "his", "how", "if", "in", "into", "is", "it", "its",
    "may", "must", "my", "no", "not", "of", "on", "or", "our", "out",
    "shall", "she", "should", "so", "such", "than", "that", "the", "their",
    "them", "then", "there", "these", "they", "this", "those", "to", "up",
    "was", "we", "were", "what", "when", "where", "which", "who", "will",
    "with", "would", "you", "your",
})

# Generic legal words. On their own they describe almost every contract (or
# point at no particular provision), so they must never retrieve authorities
# by themselves. They are ignored for scoring even when a meaningful term is
# present, but the query still works. "section" is included so the bare word
# cannot match citation strings or keywords such as "section 56".
GENERIC_LEGAL_TERMS = frozenset({"contract", "agreement", "section"})


def _get_authorities_path(path: Union[Path, str, None] = None) -> Path:
    """Return the path to data/authorities.json.

    The default path is resolved relative to this file (retrieval.py), not the
    current working directory. A custom path can be passed for testing.

    Args:
        path: Optional custom path to authorities JSON.

    Returns:
        Path to the authorities JSON file.
    """
    if path is not None:
        return Path(path)
    # Resolve relative to this file
    return Path(__file__).resolve().parent / "data" / "authorities.json"


def _compile_word_pattern(token: str) -> re.Pattern:
    """Compile a case-insensitive regex pattern for matching a single token.

    - Use word boundaries for alphabetic tokens to favor whole-word matches.
    - For tokens containing digits/punctuation (e.g. "2(h)"), use a flexible
      substring pattern to sensibly match citations like "Section 2(h)".

    Args:
        token: Normalized query token.

    Returns:
        Compiled regex pattern.
    """
    if not token:
        # Should not happen as empty tokens are filtered
        return re.compile(r"(?!)", re.IGNORECASE)

    # If token contains non-word chars (digits/punctuation), use substring match
    if re.search(r"\W", token):
        # Escape and match as substring (case-insensitive)
        escaped = re.escape(token)
        return re.compile(escaped, re.IGNORECASE)

    # Alphabetic token: prefer whole-word boundary
    escaped = re.escape(token)
    return re.compile(rf"\b{escaped}\b", re.IGNORECASE)


def normalize_text(text: Union[str, None]) -> str:
    """Normalize text for matching.

    - Convert to lowercase
    - Collapse repeated whitespace
    - Strip leading/trailing whitespace

    Args:
        text: Input text to normalize (may be None).

    Returns:
        Normalized string.
    """
    if text is None:
        return ""
    # Collapse repeated whitespace (spaces, tabs, newlines)
    normalized = re.sub(r"\s+", " ", text).lower().strip()
    return normalized


def load_authorities(path: Union[Path, str, None] = None) -> List[dict]:
    """Load authorities list from JSON file.

    Handles missing files, invalid JSON, and unexpected top-level structure
    gracefully by returning an empty list. Does not fabricate records.

    Args:
        path: Optional custom path to authorities JSON.

    Returns:
        List of authority dicts. Returns empty list on error.
    """
    try:
        authorities_path = _get_authorities_path(path)
        if not authorities_path.exists():
            return []
        with open(authorities_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return []

    # Expect top-level dict with 'authorities' key
    if not isinstance(data, dict):
        return []
    authorities = data.get("authorities")
    if not isinstance(authorities, list):
        return []

    # Return copies; filter out non-dict entries defensively
    result: List[dict] = []
    for item in authorities:
        if isinstance(item, dict):
            result.append(item)
    return result


def _get_keywords_list(authority: dict) -> List[str]:
    """Get authority keywords as list of strings.

    Args:
        authority: Authority dict.

    Returns:
        List of keyword strings (may be empty).
    """
    keywords = authority.get("keywords", [])
    if not isinstance(keywords, list):
        return []
    result = []
    for kw in keywords:
        if kw is not None:
            result.append(str(kw))
    return result


def search_authorities(
    query: str,
    authorities: Optional[Union[List[dict], None]] = None,
    min_score: int = 1,
) -> List[dict]:
    """Search authorities using deterministic keyword matching.

    Matching is case-insensitive with whitespace normalization. Common English
    stopwords (such as "the", "this", "is", "and", "of", "to") are dropped
    before scoring, so they can never add points. Generic legal words
    ("contract", "agreement") and the bare word "section" are ignored for
    scoring as well: a query made up only of those words returns no results,
    while they are still accepted in queries that contain a meaningful term.

    For individual tokens, whole-word matching is preferred for alphabetic
    tokens; for tokens containing punctuation/digits (e.g. "2(h)"), substring
    matching is used. Multiword keywords must occur as a complete phrase in the
    normalized query, so "consent" does not match "free consent".

    Score: number of distinct meaningful query tokens backed by evidence.
    Evidence is either a match against a curated keyword, or a match against
    the authority citation for a token that contains at least one digit
    (e.g. "56" from "Section 56", or "2(h)"). Summaries, non-numeric citation
    words such as "section", and applicable_topics never score on their own.
    Each query token contributes at most one point per authority, even if it
    matches multiple keywords.

    Ties: results are sorted by relevance_score descending, preserving the
    original input order of authorities for ties.

    Edge cases (return empty list): empty query, stopword-only or generic-only
    query, no matches, missing files, invalid JSON, or invalid top-level data.
    Missing optional fields are handled without crashing. No fabricated
    records are returned.

    Args:
        query: Search query string.
        authorities: Optional preloaded list of authorities. If None, loads
            from data/authorities.json (relative to this file).
        min_score: Minimum relevance score to include (default 1).

    Returns:
        List of dicts, each containing original authority fields plus
        'relevance_score', 'matched_keywords', and 'query_tokens'.
    """
    # Normalize query
    norm_query = normalize_text(query)
    if not norm_query:
        return []

    # Split into query tokens and drop common English stopwords.
    query_tokens = [t for t in norm_query.split() if t and t not in STOPWORDS]
    if not query_tokens:
        return []

    # Generic legal words alone ("contract", "agreement", "section") are not
    # evidence of relevance, so a generic-only query returns nothing. When
    # meaningful terms are present, the generic words stay in query_tokens for
    # transparency but do not contribute to the score.
    scoring_tokens = [t for t in query_tokens if t not in GENERIC_LEGAL_TERMS]
    if not scoring_tokens:
        return []

    # Load authorities if not provided
    if authorities is None:
        auth_list = load_authorities()
    else:
        # Filter to dicts and make shallow copies
        auth_list = []
        for item in authorities:
            if isinstance(item, dict):
                auth_list.append(item)

    # If no authorities, return empty
    if not auth_list:
        return []

    results: List[dict] = []

    for idx, authority in enumerate(auth_list):
        keywords_list = _get_keywords_list(authority)

        # Track which query tokens matched this authority
        matched_token_set = set()
        # Track which authority keywords matched any query token
        matched_keywords_set = set()

        # Compile patterns per scoring token (stopwords and generic terms
        # have already been removed).
        token_patterns = []
        for t in scoring_tokens:
            token_patterns.append(_compile_word_pattern(t))

        # 1) Curated keywords are the only source of scoring evidence.
        for kw in keywords_list:
            norm_kw = normalize_text(kw)
            if not norm_kw:
                continue
            # Multiword keywords must occur as a complete phrase. This stops a
            # single token such as "consent" from matching "free consent".
            if len(norm_kw.split()) > 1 and norm_kw not in norm_query:
                continue
            for t, pat in zip(scoring_tokens, token_patterns):
                if pat.search(norm_kw):
                    matched_token_set.add(t)
                    matched_keywords_set.add(kw)  # preserve original keyword text

        # 2) Citation references count only when the token is a section number
        # (contains a digit), e.g. "56" from "Section 56" or "2(h)". Summary
        # text, non-numeric citation words ("section") and applicable_topics
        # never score on their own.
        citation_text = normalize_text(authority.get("citation", ""))
        if citation_text:
            for t, pat in zip(scoring_tokens, token_patterns):
                if any(ch.isdigit() for ch in t) and pat.search(citation_text):
                    matched_token_set.add(t)

        # Score is number of distinct query tokens matched
        relevance_score = len(matched_token_set)

        # Apply min_score filter
        if relevance_score < min_score:
            continue

        # Build result copy with metadata
        result_item = dict(authority)  # shallow copy of original fields
        result_item["relevance_score"] = relevance_score
        # Preserve matched keywords order as they appear in authority keywords
        matched_keywords_list = []
        seen_mk = set()
        for kw in keywords_list:
            if kw in matched_keywords_set and kw not in seen_mk:
                matched_keywords_list.append(kw)
                seen_mk.add(kw)
        # If not found in order, append remaining
        for kw in matched_keywords_set:
            if kw not in seen_mk:
                matched_keywords_list.append(kw)
                seen_mk.add(kw)
        result_item["matched_keywords"] = matched_keywords_list
        result_item["query_tokens"] = query_tokens
        result_item["_orig_index"] = idx  # for stable tie-breaking
        results.append(result_item)

    # Sort: relevance_score desc, then original index asc for ties
    sorted_results = sorted(
        results,
        key=lambda r: (-r["relevance_score"], r.get("_orig_index", 0)),
    )

    # Remove internal index
    for r in sorted_results:
        r.pop("_orig_index", None)

    return sorted_results

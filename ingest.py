"""Phase 3 document ingestion pipeline.

Orchestrates the reliable conversion of an uploaded PDF contract into
structured, traceable legal information:

    duplicate check -> extract -> normalize -> segment -> classify ->
    persist -> link hierarchy -> graph build

Every stage is recorded in ``processing_log`` so a failed run can be
retried from the last completed stage, and no stage is ever reported as
successful unless it actually completed.

This module is intentionally thin: it calls the existing, tested modules
(``extract``, ``normalize``, ``clauses``, ``db``, ``graph``) and never
re-implements their logic.
"""

import clauses as clause_tools
import db
import extract
import graph
import normalize


def find_duplicate_contract(firm_id, source_sha256):
    """Return the contract_id of an already-ingested document with the same
    SHA-256 for the same firm, or None.

    This is the duplicate-processing guard: re-uploading the same bytes does
    not create a second contract, version, or clause set.
    """
    conn = db.get_conn()
    try:
        row = conn.execute(
            """SELECT c.contract_id, c.processing_status
               FROM contracts c
               JOIN contract_versions v ON v.contract_id = c.contract_id
               WHERE c.firm_id = ? AND v.source_sha256 = ?
               ORDER BY v.version_number DESC
               LIMIT 1""",
            (firm_id, source_sha256),
        ).fetchone()
        if row is None:
            return None
        return dict(row)
    finally:
        conn.close()


def duplicate_result(contract):
    """Build the success response for a duplicate upload."""
    return {
        "message": "This document was already ingested; no duplicate created.",
        "contract_id": contract["contract_id"],
        "duplicate": True,
        "status": contract["processing_status"],
        "analysis_status": "ready"
        if contract["processing_status"] == "extracted"
        else "unavailable",
    }


def extract_document(staged, max_pages, max_extracted_chars,
                     max_processing_seconds, extractor=None):
    """Stage 1: extract text and per-page positions from the staged PDF.

    Returns (text, pages, page_count).  Raises extract.PDFValidationError
    for defined rejections, which the upload route converts to HTTP errors.

    ``extractor`` is an optional seam (defaults to
    :func:`extract.extract_pages`) so the upload route can keep its
    historically patchable extraction entry point while still receiving page
    information.  It must return the same dict shape as ``extract_pages``.
    """
    reader = extractor or extract.extract_pages
    # Accept either a staged-upload object or a plain filesystem path (used
    # when a retry re-reads the retained source file of a stored version).
    path = getattr(staged, "temp_path", staged)
    result = reader(
        path,
        max_pages=max_pages,
        max_extracted_chars=max_extracted_chars,
        max_processing_seconds=max_processing_seconds,
    )
    return result["text"], result["pages"], result["page_count"]


def segment_document(text, pages):
    """Stage 2: split into clauses with offsets, page numbers, hierarchy.

    Uses preserve_text=True so the full source slice (including the section
    heading) is retained, and include_offsets=True so every clause is
    traceable to its source character range and page.
    """
    return clause_tools.split_clauses(
        text,
        preserve_text=True,
        include_offsets=True,
        pages=pages,
    )


def classify_clauses(clauses):
    """Stage 3: classify every clause without forcing an incorrect type.

    Clauses that match no category keep type ``other``; uncertainty is
    preserved, never fabricated.
    """
    classified = []
    for clause in clauses:
        metadata = dict(clause.get("metadata") or {})
        metadata["type"] = clause_tools.classify_type(
            clause.get("clause_text") or "",
            metadata.get("heading", ""),
        )
        metadata["normalized"] = normalize.normalize_text(
            clause.get("clause_text") or ""
        )
        clause["metadata"] = metadata
        clause["normalized_text"] = metadata["normalized"]
        classified.append(clause)
    return classified


def persist_clauses(contract_id, version_id, clauses, correlation_id,
                    audit_event=None, saver=None):
    """Stage 4: store clauses with page/offset/hierarchy and link parents.

    ``audit_event`` lets the caller supply firm/actor attribution.  When it is
    omitted a minimal system event is recorded so a clause write can never be
    silent.  ``saver`` defaults to :func:`db.save_clauses`; the upload route
    passes its own import so existing integrations that patch
    ``api_a.save_clauses`` keep intercepting persistence.
    """
    event = audit_event or {
        "action": "clauses.persisted",
        "outcome": "success",
        "target_type": "contract",
        "target_id": contract_id,
        "correlation_id": correlation_id,
        "metadata": {
            "clause_count": len(clauses),
            "processing_status": "extracted",
        },
    }
    save = saver or db.save_clauses
    saved = save(
        contract_id,
        clauses,
        version_id=version_id,
        audit_event=event,
    )
    db.link_clause_hierarchy(contract_id)
    return saved


def build_contract_graph(contract_id, audit_context):
    """Stage 5: build and publish the typed relationship graph.

    Graph construction lives in ``graph.build_graph``; the adapter exposes the
    read side and schema seam, not the builder.
    """
    return graph.build_graph(
        contract_id,
        use_llm=False,
        audit_context=audit_context,
    )

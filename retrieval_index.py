"""Incremental SQLite embedding index for contract clauses (Phase 4).

Design
------
Embeddings are stored in SQLite.  A row is identified by
``(contract_id, version_id, section_number, model_key)`` and carries the exact
``clause_sha256`` plus an ``input_sha256`` of the canonical embedding text.
The retrieval query joins an embedding to the live clause only when the
contract, version, section, clause hash and model all match, so an old vector
can never be returned for a new version.

Vectors are retained after a clause set is replaced.  That is deliberate: an
unchanged clause in a new version reuses its old vector instead of being
re-embedded.  Version isolation is enforced at query time, not by deleting
history.  A per-contract index run records how many clauses were embedded,
reused, updated or removed, which makes incremental behaviour auditable.

Read-only demo databases are never migrated.  Their vectors are either read
from an index that was built when the database was writable (``seed.py``) or
computed into a process-local cache by :func:`load_current_embeddings`.
"""

from __future__ import annotations

import math
import os
import struct
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import db
import embeddings


class IndexNotWritable(RuntimeError):
    """Raised when an index cannot be persisted because the database is read-only."""


_EMBEDDING_TABLES = ("embedding_models", "clause_embeddings", "retrieval_index_runs")

# Process-local cache for read-only demo databases.  The key includes the
# database path and the exact model/input identity, so one process serving
# multiple test databases can never mix vectors.
_MEMORY_CACHE: Dict[Tuple[str, str, int, int, str, str], Tuple[float, ...]] = {}


def _tables_exist(conn, table_names: Sequence[str]) -> bool:
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    return all(name in names for name in table_names)


def _is_writable() -> bool:
    config = db.get_database_config()
    if config is not None:
        return not config.read_only
    path = Path(db.get_db_path())
    if not path.exists():
        return False
    conn = db.get_conn()
    try:
        row = conn.execute("PRAGMA query_only").fetchone()
        return not bool(row and row[0])
    finally:
        conn.close()


def _require_tables(conn, *, create_if_possible: bool = True) -> bool:
    if _tables_exist(conn, _EMBEDDING_TABLES):
        return True
    if not create_if_possible or not _is_writable():
        return False
    # db.init_db() is the canonical additive schema migration.  Close this
    # read connection first so the migration gets its own transaction.
    if create_if_possible:
        conn.close()
        db.init_db()
        return True
    return False


def ensure_embedding_schema() -> bool:
    """Create the additive Phase 4 tables on a writable database.

    Returns False when the database is read-only or the tables are unavailable.
    No contract or clause data is modified.
    """

    conn = db.get_conn()
    try:
        if _tables_exist(conn, _EMBEDDING_TABLES):
            return True
        if not _is_writable():
            return False
        conn.close()
        db.init_db()
        return True
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _pack_vector(vector: Sequence[float]) -> bytes:
    values = [float(value) for value in vector]
    for value in values:
        if not math.isfinite(value):
            raise ValueError("Embedding contains a non-finite value.")
    return struct.pack("<%df" % len(values), *values)


def _unpack_vector(blob: bytes, dimension: int) -> List[float]:
    if blob is None:
        return []
    expected = int(dimension) * 4
    if len(blob) != expected:
        return []
    return list(struct.unpack("<%df" % int(dimension), blob))


def _clause_input_identity(clause: dict, model_key: str) -> Tuple[str, str, str]:
    """Return (embedding_text, input_sha256, clause_sha256)."""

    metadata = clause.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    section = str(clause.get("section_number") or "")
    heading = str(metadata.get("heading") or "")
    clause_type = str(metadata.get("type") or "")
    text = str(clause.get("clause_text") or "")
    embedding_text = embeddings.build_clause_embedding_text(
        section_number=section,
        heading=heading,
        clause_type=clause_type,
        text=text,
    )
    clause_hash = clause.get("clause_sha256") or db.sha256_text(text)
    input_hash = db.sha256_text(
        "%s|%s|%s|%s|%s"
        % (model_key, embeddings.MODEL_VERSION, section, heading, embedding_text)
    )
    return embedding_text, input_hash, clause_hash


def _register_model(conn, model_key: str) -> dict:
    metadata = embeddings.model_metadata(model_key)
    conn.execute(
        """INSERT OR IGNORE INTO embedding_models
               (model_key, provider, model_name, model_version, dimension,
                distance_metric, config_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            metadata["model_key"],
            metadata["provider"],
            metadata["model_name"],
            str(metadata["model_version"]),
            int(metadata["dimension"]),
            metadata["distance_metric"],
            None,
            db.utc_now_iso(),
        ),
    )
    row = conn.execute(
        "SELECT * FROM embedding_models WHERE model_key = ?",
        (metadata["model_key"],),
    ).fetchone()
    if row is None:
        raise RuntimeError("Embedding model metadata could not be stored.")
    if int(row["dimension"]) != int(metadata["dimension"]):
        raise RuntimeError(
            "Stored embedding model %r has dimension %s, expected %s."
            % (
                metadata["model_key"],
                row["dimension"],
                metadata["dimension"],
            )
        )
    return dict(row)


def _record_run(
    conn,
    *,
    contract_id: int,
    version_id: Optional[int],
    model_key: str,
    mode: str,
    status: str,
    started_at: str,
    finished_at: Optional[str],
    clause_count: int = 0,
    embedded_count: int = 0,
    reused_count: int = 0,
    updated_count: int = 0,
    removed_count: int = 0,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO retrieval_index_runs
               (contract_id, version_id, model_key, mode, status, started_at,
                finished_at, clause_count, embedded_count, reused_count,
                updated_count, removed_count, error_type, error_message)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            contract_id,
            version_id,
            model_key,
            mode,
            status,
            started_at,
            finished_at,
            int(clause_count),
            int(embedded_count),
            int(reused_count),
            int(updated_count),
            int(removed_count),
            error_type,
            error_message,
        ),
    )
    return int(cur.lastrowid)


def _current_clauses(contract_id: int) -> Tuple[Optional[int], List[dict]]:
    version_id = db.get_current_version_id(contract_id)
    clauses = db.list_clauses(contract_id)
    if version_id is not None:
        clauses = [
            clause
            for clause in clauses
            if clause.get("version_id") in (None, version_id)
        ]
        # An explicit version filter is stricter: a row carrying another
        # version is not part of the current materialised set.
        clauses = [
            clause
            for clause in clauses
            if clause.get("version_id") == version_id
        ]
    return version_id, clauses


def index_contract(
    contract_id: int,
    *,
    model_key: Optional[str] = None,
    force: bool = False,
    embedder=None,
) -> dict:
    """Embed the current clause set of one contract incrementally.

    ``force`` re-embeds every clause even when an identical vector exists.  It
    is intended for model debugging, not normal operation.
    """

    selected = (model_key or embeddings.configured_model_key()).strip()
    started_at = db.utc_now_iso()
    started_clock = time.perf_counter()
    version_id: Optional[int] = None
    stats = {
        "contract_id": contract_id,
        "version_id": None,
        "model_key": selected,
        "mode": "full" if force else "incremental",
        "status": "failed",
        "clause_count": 0,
        "embedded_count": 0,
        "reused_count": 0,
        "updated_count": 0,
        "removed_count": 0,
        "run_id": None,
        "storage": "sqlite",
        "duration_ms": 0.0,
    }

    conn = db.get_conn()
    try:
        if not _require_tables(conn):
            raise IndexNotWritable(
                "The embedding index is unavailable because the database is "
                "read-only and no persisted index exists."
            )
        # _require_tables may have closed and re-opened the database through
        # db.init_db(); obtain a fresh connection for the actual work.
        if conn is None or not _tables_exist(conn, _EMBEDDING_TABLES):
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass

    conn = db.get_conn()
    try:
        if not _tables_exist(conn, _EMBEDDING_TABLES):
            raise IndexNotWritable("Embedding tables are not available.")
        _register_model(conn, selected)

        version_id, clauses = _current_clauses(contract_id)
        stats["version_id"] = version_id
        stats["clause_count"] = len(clauses)

        current_rows = conn.execute(
            """SELECT embedding_id, section_number, clause_id, clause_sha256,
                      input_sha256, dimension, vector
               FROM clause_embeddings
               WHERE contract_id = ? AND model_key = ?
                 AND version_id IS ?""",
            (contract_id, selected, version_id),
        ).fetchall()
        current_by_section: Dict[str, dict] = {
            str(row["section_number"]): dict(row) for row in current_rows
        }

        # Any historical vector for the same section and identical input can
        # be reused.  This is what makes a new version cheap: unchanged clauses
        # keep their old vector even though SQLite assigns new clause ids.
        reuse_cache: Dict[Tuple[str, str], Tuple[float, ...]] = {}
        for row in conn.execute(
            """SELECT section_number, input_sha256, dimension, vector
               FROM clause_embeddings
               WHERE contract_id = ? AND model_key = ?
               ORDER BY indexed_at DESC, embedding_id DESC""",
            (contract_id, selected),
        ).fetchall():
            key = (str(row["section_number"]), str(row["input_sha256"]))
            if key not in reuse_cache:
                vector = _unpack_vector(row["vector"], row["dimension"])
                if vector:
                    reuse_cache[key] = tuple(vector)

        identities = []
        for clause in clauses:
            embedding_text, input_hash, clause_hash = _clause_input_identity(
                clause, selected
            )
            identities.append(
                {
                    "clause": clause,
                    "text": embedding_text,
                    "input_sha256": input_hash,
                    "clause_sha256": clause_hash,
                    "section": str(clause.get("section_number") or ""),
                }
            )

        to_embed: List[dict] = []
        plans: List[dict] = []
        for item in identities:
            section = item["section"]
            existing = current_by_section.get(section)
            if (
                not force
                and existing is not None
                and str(existing["input_sha256"]) == item["input_sha256"]
            ):
                vector = _unpack_vector(existing["vector"], existing["dimension"])
                if vector:
                    if existing.get("clause_id") != item["clause"].get("clause_id"):
                        conn.execute(
                            """UPDATE clause_embeddings SET clause_id = ?
                               WHERE embedding_id = ?""",
                            (item["clause"].get("clause_id"), existing["embedding_id"]),
                        )
                    stats["reused_count"] += 1
                    continue
            if not force:
                cached = reuse_cache.get((section, item["input_sha256"]))
                if cached is not None:
                    plans.append({**item, "vector": list(cached), "reused": True})
                    stats["reused_count"] += 1
                    continue
            to_embed.append(item)
            plans.append({**item, "vector": None, "reused": False})

        if to_embed:
            if embedder is None:
                vectors = embeddings.embed_texts(
                    [item["text"] for item in to_embed], model_key=selected
                )
            else:
                vectors = embedder(
                    [item["text"] for item in to_embed], model_key=selected
                )
            if len(vectors) != len(to_embed):
                raise RuntimeError(
                    "Embedding backend returned %d vectors for %d inputs."
                    % (len(vectors), len(to_embed))
                )
            for item, vector in zip(to_embed, vectors):
                if len(vector) != int(embeddings.model_metadata(selected)["dimension"]):
                    raise RuntimeError(
                        "Embedding dimension mismatch for clause %s."
                        % item["section"]
                    )
                item["computed_vector"] = list(vector)

        embedded_by_section = {
            item["section"]: item.get("computed_vector") for item in to_embed
        }
        now = db.utc_now_iso()
        dimension = int(embeddings.model_metadata(selected)["dimension"])
        seen_sections = set()
        for item in plans:
            section = item["section"]
            seen_sections.add(section)
            vector = item.get("vector") or embedded_by_section.get(section)
            if vector is None:
                raise RuntimeError("No vector was produced for clause %s." % section)
            existing = current_by_section.get(section)
            if existing is None:
                conn.execute(
                    """INSERT INTO clause_embeddings
                           (contract_id, version_id, clause_id, section_number,
                            clause_sha256, input_sha256, model_key, dimension,
                            vector, indexed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        contract_id,
                        version_id,
                        item["clause"].get("clause_id"),
                        section,
                        item["clause_sha256"],
                        item["input_sha256"],
                        selected,
                        dimension,
                        _pack_vector(vector),
                        now,
                    ),
                )
            else:
                conn.execute(
                    """UPDATE clause_embeddings
                       SET clause_id = ?, clause_sha256 = ?, input_sha256 = ?,
                           dimension = ?, vector = ?, indexed_at = ?
                       WHERE embedding_id = ?""",
                    (
                        item["clause"].get("clause_id"),
                        item["clause_sha256"],
                        item["input_sha256"],
                        dimension,
                        _pack_vector(vector),
                        now,
                        existing["embedding_id"],
                    ),
                )
                if not item["reused"]:
                    stats["updated_count"] += 1

        # Remove rows for clauses that no longer exist in this exact version.
        stale_ids = []
        for section, row in current_by_section.items():
            if section not in seen_sections:
                stale_ids.append(row["embedding_id"])
        if stale_ids:
            marks = ",".join("?" * len(stale_ids))
            conn.execute(
                "DELETE FROM clause_embeddings WHERE embedding_id IN (%s)" % marks,
                stale_ids,
            )
            stats["removed_count"] = len(stale_ids)

        stats["embedded_count"] = len(to_embed)
        stats["status"] = "succeeded"
        finished_at = db.utc_now_iso()
        stats["run_id"] = _record_run(
            conn,
            contract_id=contract_id,
            version_id=version_id,
            model_key=selected,
            mode=stats["mode"],
            status="succeeded",
            started_at=started_at,
            finished_at=finished_at,
            clause_count=stats["clause_count"],
            embedded_count=stats["embedded_count"],
            reused_count=stats["reused_count"],
            updated_count=stats["updated_count"],
            removed_count=stats["removed_count"],
        )
        conn.commit()
        stats["finished_at"] = finished_at
        stats["duration_ms"] = round(
            (time.perf_counter() - started_clock) * 1000.0, 3
        )
        return stats
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            if _tables_exist(conn, _EMBEDDING_TABLES):
                stats["run_id"] = _record_run(
                    conn,
                    contract_id=contract_id,
                    version_id=version_id,
                    model_key=selected,
                    mode=stats["mode"],
                    status="failed",
                    started_at=started_at,
                    finished_at=db.utc_now_iso(),
                    clause_count=stats["clause_count"],
                    embedded_count=stats["embedded_count"],
                    reused_count=stats["reused_count"],
                    updated_count=stats["updated_count"],
                    removed_count=stats["removed_count"],
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:500],
                )
                conn.commit()
        except Exception:
            pass
        stats["status"] = "failed"
        stats["error_type"] = type(exc).__name__
        stats["error_message"] = str(exc)
        stats["duration_ms"] = round(
            (time.perf_counter() - started_clock) * 1000.0, 3
        )
        raise
    finally:
        conn.close()


def index_contracts(contract_ids: Iterable[int], **kwargs) -> List[dict]:
    results = []
    for contract_id in contract_ids:
        results.append(index_contract(int(contract_id), **kwargs))
    return results


def get_last_run(contract_id: int, model_key: Optional[str] = None) -> Optional[dict]:
    selected = (model_key or embeddings.configured_model_key()).strip()
    conn = db.get_conn()
    try:
        if not _tables_exist(conn, _EMBEDDING_TABLES):
            return None
        row = conn.execute(
            """SELECT * FROM retrieval_index_runs
               WHERE contract_id = ? AND model_key = ?
               ORDER BY run_id DESC LIMIT 1""",
            (contract_id, selected),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def embedding_status(
    contract_id: int,
    *,
    model_key: Optional[str] = None,
) -> dict:
    """Report index readiness without writing to the database."""

    selected = (model_key or embeddings.configured_model_key()).strip()
    metadata = embeddings.model_metadata(selected)
    conn = db.get_conn()
    try:
        tables = _tables_exist(conn, _EMBEDDING_TABLES)
        clauses = db.list_clauses(contract_id)
        version_id = db.get_current_version_id(contract_id)
        if version_id is not None:
            clauses = [
                clause
                for clause in clauses
                if clause.get("version_id") == version_id
            ]
        expected: Dict[str, str] = {}
        for clause in clauses:
            _text, input_hash, _clause_hash = _clause_input_identity(
                clause, selected
            )
            expected[str(clause.get("section_number") or "")] = input_hash

        indexed = 0
        if tables:
            rows = conn.execute(
                """SELECT section_number, input_sha256
                   FROM clause_embeddings
                   WHERE contract_id = ? AND model_key = ?
                     AND version_id IS ?""",
                (contract_id, selected, version_id),
            ).fetchall()
            stored = {
                str(row["section_number"]): str(row["input_sha256"])
                for row in rows
            }
            indexed = sum(
                1 for section, digest in expected.items()
                if stored.get(section) == digest
            )
        return {
            "contract_id": contract_id,
            "version_id": version_id,
            "model": metadata,
            "storage": "sqlite" if tables else "none",
            "read_only": not _is_writable(),
            "clause_count": len(expected),
            "indexed_count": indexed,
            "missing_count": max(0, len(expected) - indexed),
            "ready": bool(expected) and indexed == len(expected),
            "last_run": get_last_run(contract_id, model_key=selected)
            if tables
            else None,
        }
    finally:
        conn.close()


def load_current_embeddings(
    contract_id: int,
    clauses: Sequence[dict],
    *,
    model_key: Optional[str] = None,
    auto_index: bool = True,
) -> Tuple[Dict[str, List[float]], dict]:
    """Return ``section -> vector`` for the current version of one contract.

    The mapping is built from persisted rows only when the stored input hash
    still matches the live clause.  Missing rows are indexed when the database
    is writable.  On a read-only database they are computed into the
    process-local cache and reported as ``storage="memory"``.
    """

    selected = (model_key or embeddings.configured_model_key()).strip()
    metadata = embeddings.model_metadata(selected)
    version_id = db.get_current_version_id(contract_id)
    identities = {}
    for clause in clauses:
        text, input_hash, _clause_hash = _clause_input_identity(clause, selected)
        identities[str(clause.get("section_number") or "")] = {
            "text": text,
            "input_sha256": input_hash,
        }

    conn = db.get_conn()
    try:
        tables = _tables_exist(conn, _EMBEDDING_TABLES)
        stored: Dict[str, Tuple[str, List[float]]] = {}
        if tables:
            rows = conn.execute(
                """SELECT section_number, input_sha256, dimension, vector
                   FROM clause_embeddings
                   WHERE contract_id = ? AND model_key = ?
                     AND version_id IS ?""",
                (contract_id, selected, version_id),
            ).fetchall()
            for row in rows:
                vector = _unpack_vector(row["vector"], row["dimension"])
                if vector:
                    stored[str(row["section_number"])] = (
                        str(row["input_sha256"]),
                        vector,
                    )
    finally:
        conn.close()

    result: Dict[str, List[float]] = {}
    missing = []
    for section, identity in identities.items():
        hit = stored.get(section)
        if hit is not None and hit[0] == identity["input_sha256"]:
            result[section] = hit[1]
        else:
            missing.append(section)

    storage = "sqlite" if tables else "none"
    writable = _is_writable()
    if missing and auto_index and writable:
        try:
            index_contract(contract_id, model_key=selected)
            conn = db.get_conn()
            try:
                rows = conn.execute(
                    """SELECT section_number, input_sha256, dimension, vector
                       FROM clause_embeddings
                       WHERE contract_id = ? AND model_key = ?
                         AND version_id IS ?""",
                    (contract_id, selected, version_id),
                ).fetchall()
            finally:
                conn.close()
            stored = {}
            for row in rows:
                vector = _unpack_vector(row["vector"], row["dimension"])
                if vector:
                    stored[str(row["section_number"])] = (
                        str(row["input_sha256"]),
                        vector,
                    )
            missing = []
            for section, identity in identities.items():
                hit = stored.get(section)
                if hit is not None and hit[0] == identity["input_sha256"]:
                    result[section] = hit[1]
                else:
                    missing.append(section)
            storage = "sqlite"
        except Exception:
            # Auto-indexing is a convenience.  Retrieval must still work if a
            # write fails; fall through to the in-memory path and report it.
            storage = "sqlite+memory-fallback"

    if missing:
        memory_key_base = (
            str(db.get_db_path()),
            selected,
            int(contract_id),
            int(version_id or 0),
        )
        to_compute = []
        for section in missing:
            identity = identities[section]
            key = memory_key_base + (section, identity["input_sha256"])
            cached = _MEMORY_CACHE.get(key)
            if cached is not None:
                result[section] = list(cached)
            elif identity["text"].strip():
                to_compute.append((section, identity, key))
        if to_compute:
            vectors = embeddings.embed_texts(
                [item[1]["text"] for item in to_compute], model_key=selected
            )
            for (section, _identity, key), vector in zip(to_compute, vectors):
                normalised = tuple(float(value) for value in vector)
                _MEMORY_CACHE[key] = normalised
                result[section] = list(normalised)
        if storage == "none":
            storage = "memory"
        elif storage == "sqlite":
            storage = "sqlite+memory"
        elif storage == "sqlite+memory-fallback":
            storage = "memory-fallback"

    return result, {
        "storage": storage,
        "model": metadata,
        "version_id": version_id,
        "embedded_in_memory": len(missing),
    }


def clear_memory_cache() -> None:
    """Clear the process-local read-only embedding cache (tests/maintenance)."""

    _MEMORY_CACHE.clear()


def all_index_status() -> List[dict]:
    contracts = db.list_contracts()
    return [
        {
            "contract_id": contract["contract_id"],
            "filename": contract.get("filename"),
            "firm_id": contract.get("firm_id"),
            **embedding_status(contract["contract_id"]),
        }
        for contract in contracts
    ]

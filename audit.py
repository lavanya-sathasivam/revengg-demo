"""Durable audit events for security-relevant and document-lifecycle actions.

This is deliberately separate from ``traces``.  ``traces`` predates this module
and is an application activity log: it is convenient for debugging, but it has
no actor, tenant, outcome, or append-only protection.  ``audit_events`` is the
durable trail:

* append-only (database triggers reject UPDATE and DELETE);
* tenant- and actor-attributed where the caller knows them;
* structured, allow-listed metadata;
* never contains raw contract text, clause text, session tokens, or secrets.

The audit trail is not a replacement for an application log.  Operational
exceptions still go to the logger; audit events record that a lifecycle or
authorization action happened and what its outcome was.
"""

from __future__ import annotations

import json
import logging
import re

import db

logger = logging.getLogger("app.audit")

MAX_STRING_LENGTH = 500

# These key names are refused even if a caller accidentally passes them.  The
# audit trail must not become a second copy of the contract.
_SENSITIVE_KEY = re.compile(
    r"(raw[_-]?text|clause[_-]?text|text|content|body|token|secret|password|"
    r"password[_-]?hash|authorization|cookie|session[_-]?token|api[_-]?key)",
    re.IGNORECASE,
)

_SENSITIVE_VALUE = re.compile(
    r"(bearer\s+[a-z0-9._~+/=-]+|revengg_session\s*=|token\s*[:=]\s*\S+)",
    re.IGNORECASE,
)


def _clean_string(value: str) -> str:
    value = _SENSITIVE_VALUE.sub("[redacted]", value)
    if len(value) > MAX_STRING_LENGTH:
        return value[:MAX_STRING_LENGTH] + "...[truncated]"
    return value


def sanitize_metadata(value, key=None):
    """Return a JSON-safe copy with sensitive keys and token-like values removed.

    Callers pass short scalar facts (ids, counts, hashes, filenames, outcomes).
    The sanitizer is a defence in depth, not a licence to pass contract text.
    """

    if key is not None and _SENSITIVE_KEY.search(str(key)):
        return "[redacted]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _clean_string(value)
    if isinstance(value, dict):
        return {
            str(k): sanitize_metadata(v, str(k))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_metadata(item) for item in value]
    return _clean_string(str(value))


def record_event(
    action: str,
    outcome: str = "success",
    *,
    firm_id=None,
    actor_user_id=None,
    actor_type: str = "system",
    target_type=None,
    target_id=None,
    correlation_id=None,
    metadata=None,
    conn=None,
    commit: bool = True,
    swallow: bool = False,
):
    """Insert one append-only audit event.

    ``conn`` lets a caller include the audit row in the same transaction as the
    action it records.  When this module opens its own connection it commits by
    default.  ``swallow`` is for denial paths: an audit write failure must not
    turn a 403 into a 500, but it is still logged loudly.
    """

    own_connection = conn is None
    try:
        payload = json.dumps(
            sanitize_metadata(metadata or {}), sort_keys=True
        )
        if own_connection:
            conn = db.get_conn()
        cur = conn.execute(
            """INSERT INTO audit_events
                   (occurred_at, firm_id, actor_user_id, actor_type, action,
                    outcome, target_type, target_id, correlation_id,
                    metadata_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                db.utc_now_iso(),
                firm_id,
                actor_user_id,
                actor_type,
                action,
                outcome,
                target_type,
                None if target_id is None else str(target_id),
                correlation_id,
                payload,
            ),
        )
        event_id = cur.lastrowid
        if own_connection and commit:
            conn.commit()
        return event_id
    except Exception:
        if swallow:
            logger.exception(
                "Could not record audit event %s (%s)", action, outcome
            )
            return None
        raise
    finally:
        if own_connection and conn is not None:
            conn.close()

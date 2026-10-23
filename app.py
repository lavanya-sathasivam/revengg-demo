"""Thin launch entry point for the canonical Contract Intelligence app.

Configuration is explicit:
    demo    (default) read-only synthetic database, no uploads
    upload  requires REVENGG_UPLOAD_DB_PATH (or an injected path) and never
            falls back to demo.db

Run with either entry point; both expose the same route manifest:
    py -3.11 -m uvicorn app:app --reload
    py -3.11 -m uvicorn server:app --reload
"""

from app_config import BASE_DIR, load_settings
from application import (
    EXPLAIN_HTML,
    IMPACT_HTML,
    OVERVIEW_HTML,
    TEMPLATES_DIR,
    create_app,
)

# Historical template name: index.html is now the impact-analysis workspace.
INDEX_HTML = IMPACT_HTML

# Re-exported for existing callers and tests. The handlers themselves live in
# api_a.py so that the factory is the only place that registers routes.
from api_a import (  # noqa: E402
    CHUNK_SIZE,
    MAX_UPLOAD_SIZE,
    get_clause_explanation_route,
    get_contract_clauses_route,
    get_contract_route,
    health,
    list_contracts_route,
    mark_clause_failure,
    mark_graph_failure,
    sanitize_filename,
    save_clauses,
    save_contract_with_trace,
    save_upload,
    upload_contract,
    _normalize_section_number,
)

app = create_app(load_settings())
UPLOAD_DIR = app.state.settings.upload_dir

__all__ = [
    "app",
    "create_app",
    "BASE_DIR",
    "TEMPLATES_DIR",
    "INDEX_HTML",
    "IMPACT_HTML",
    "OVERVIEW_HTML",
    "EXPLAIN_HTML",
    "UPLOAD_DIR",
    "MAX_UPLOAD_SIZE",
    "CHUNK_SIZE",
    "health",
    "upload_contract",
    "list_contracts_route",
    "get_contract_route",
    "get_contract_clauses_route",
    "get_clause_explanation_route",
    "sanitize_filename",
    "save_upload",
    "save_contract_with_trace",
    "save_clauses",
    "mark_clause_failure",
    "mark_graph_failure",
    "_normalize_section_number",
]

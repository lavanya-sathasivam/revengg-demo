"""Demo server: Person A's API + Person B's impact engine, in one process.

Owner: Person B (branch b-graph).

WHY THIS FILE EXISTS
    The demo has to be shown in a browser, not just a terminal. But Person A
    owns app.py and Person C owns the screen, and nobody should have to edit
    another person's file to make the demo work.

    So this file MOUNTS both sides without editing either:

        Person A's app.py  ->  imported as-is, routes unchanged
        Person B's api_b   ->  mounted as a router

    Person A does not need to do anything. Later, if A prefers to have the
    routes inside app.py itself, the change is two lines in app.py
    (see docs/handoff-b.md) and this file can be deleted.

RUN
    py -3.11 seed.py                 # once, builds demo.db
    py -3.11 uvicorn server:app --reload
    open http://127.0.0.1:8000/docs  -> interactive API docs
    open http://127.0.0.1:8000/      -> the demo page

NOTE ON THE DATABASE
    Everything here points at demo.db (branch B's synthetic contracts).
    Person A's real uploaded contracts live in app.db and are untouched.
"""

import os

import db

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO_DB = os.path.join(HERE, "demo.db")

# Point the whole process at demo.db BEFORE importing app.py, so A's upload
# route and B's impact routes agree on which database they are talking to.
db.get_db_path = lambda: DEMO_DB

from app import app          # noqa: E402  (Person A's FastAPI app, unchanged)
from api_b import router     # noqa: E402  (Person B's impact-analysis routes)

app.include_router(router)

# A small landing page so `http://127.0.0.1:8000/` shows something useful
# instead of a 404. Person C will replace this with the real screen.
@app.get("/", include_in_schema=False)
def home():
    import json
    from fastapi.responses import HTMLResponse

    payload = {
        "title": "Contract Intelligence & Legal Reasoning Engine",
        "branch": "b-graph (impact analysis) + a-backend (upload)",
        "try_it": [
            "GET  /demo/1/suggest",
            "POST /impact  {\"contract_id\":1,\"clause_id\":\"7.2\","
            "\"old_value\":30,\"new_value\":15}",
            "GET  /graph/1",
            "GET  /clauses/1",
            "GET  /compare/1/2",
        ],
        "interactive_docs": "/docs",
    }
    return HTMLResponse(
        "<html><head><title>revengg-demo</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:760px;"
        "margin:40px auto;padding:0 20px;line-height:1.6}"
        "code{background:#f4f4f5;padding:2px 6px;border-radius:4px}"
        "li{margin:6px 0}</style></head><body>"
        "<h1>Contract Intelligence & Legal Reasoning Engine</h1>"
        "<p><strong>Branch b-graph</strong> (impact analysis) + "
        "<strong>a-backend</strong> (upload &amp; storage).</p>"
        "<h2>Try these endpoints</h2><ul>"
        + "".join("<li><code>%s</code></li>" % e for e in payload["try_it"])
        + "</ul>"
        "<p>Full interactive API docs: <a href='/docs'>/docs</a></p>"
        "</body></html>"
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

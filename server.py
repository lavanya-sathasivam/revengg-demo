"""Thin launch entry point for the canonical Contract Intelligence app.

Same factory and route manifest as app.py. The default is the read-only
synthetic demo database; upload mode requires an explicit database path:
    REVENGG_MODE=upload
    REVENGG_UPLOAD_DB_PATH=D:\\path\\to\\local-uploads.db

Run:
    py -3.11 seed.py                 # once, builds the synthetic demo.db
    py -3.11 -m uvicorn server:app --reload
    open http://127.0.0.1:8000/      # Contract Intelligence overview
"""

from app_config import load_settings
from application import create_app

app = create_app(load_settings())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)

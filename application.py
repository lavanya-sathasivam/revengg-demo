"""Canonical application factory and route-registration path.

Both launch entry points (app.py and server.py) call create_app() and expose
the returned FastAPI object as ``app``. No router is mounted anywhere else.
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.routing import iter_route_contexts

import api_a
import api_auth
import api_b
import auth
import db
from app_config import AppSettings, BASE_DIR, MODE_DEMO, load_settings

logger = logging.getLogger("app")

TEMPLATES_DIR = BASE_DIR / "templates"
OVERVIEW_HTML = TEMPLATES_DIR / "overview.html"
IMPACT_HTML = TEMPLATES_DIR / "index.html"
EXPLAIN_HTML = TEMPLATES_DIR / "explain.html"


def _database_config(settings: AppSettings) -> db.DatabaseConfig:
    return db.DatabaseConfig(
        path=str(settings.database_path),
        read_only=settings.database_read_only,
    )


def _lifespan(settings: AppSettings):
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Demo mode is deliberately read-only: never create, migrate, or seed
        # the synthetic database from application startup.
        if settings.uploads_enabled:
            token = db.set_database_config(_database_config(settings))
            try:
                db.init_db()
                if settings.upload_dir is not None:
                    Path(settings.upload_dir).mkdir(parents=True, exist_ok=True)
            finally:
                db.reset_database_config(token)
        yield

    return lifespan


def _serve(template: Path, label: str) -> FileResponse:
    if not template.is_file():
        raise HTTPException(status_code=404, detail=f"{label} template not found")
    return FileResponse(template, media_type="text/html")


def _include_router_once(app: FastAPI, router) -> None:
    """Include a router once and keep ``app.routes`` introspectable.

    FastAPI 0.143 stores an included router as a lazy wrapper. That is fine
    for request dispatch, but route-manifest checks need the actual routes.
    Older FastAPI versions already append them directly, so this is a no-op
    there. No prefix or include options are used here.
    """
    app.include_router(router)
    wrapper = app.router.routes[-1]
    if getattr(wrapper, "original_router", None) is not router:
        return
    include_context = getattr(wrapper, "include_context", None)
    if getattr(include_context, "prefix", ""):
        return
    app.router.routes[-1:] = list(router.routes)
    mark_changed = getattr(app.router, "_mark_routes_changed", None)
    if mark_changed is not None:
        mark_changed()


def route_manifest(app: FastAPI) -> list[tuple[str, tuple[str, ...]]]:
    """Return the canonical (path, methods) manifest for an application."""
    manifest = []
    for context in iter_route_contexts(app.routes):
        methods = context.methods
        if methods:
            manifest.append((context.path, tuple(sorted(methods))))
    return sorted(manifest)


def create_app(
    settings: AppSettings | None = None,
    *,
    mode=None,
    database_path=None,
    upload_dir=None,
) -> FastAPI:
    """Build the one canonical application.

    Callers may pass a complete AppSettings or explicit overrides. With no
    arguments, configuration comes from the environment and defaults to the
    read-only synthetic demo mode.
    """
    if settings is not None and any(
        value is not None for value in (mode, database_path, upload_dir)
    ):
        raise ValueError("Pass either settings or explicit overrides, not both.")

    if settings is None:
        settings = load_settings(
            mode=mode, database_path=database_path, upload_dir=upload_dir
        )

    database_config = _database_config(settings)
    app = FastAPI(
        title="Smart Contract Intelligence API",
        description="Backend API for contract upload, storage, and extraction.",
        version="0.1.0",
        lifespan=_lifespan(settings),
    )
    app.state.settings = settings
    app.state.config = settings
    app.state.database_config = database_config
    app.state.upload_dir = settings.upload_dir

    @app.middleware("http")
    async def request_scope(request, call_next):
        """Bind the database, then establish authentication and tenant context.

        The database must be bound first because session and membership lookups
        use the same per-application database.  The middleware, not an
        individual handler, enforces authentication so a newly added API route
        cannot accidentally become public.
        """

        token = db.set_database_config(database_config)
        try:
            principal = auth.authenticate_request(request, settings)
            request.state.principal = principal

            if (
                settings.mode != MODE_DEMO
                and not auth.is_public_path(request.url.path)
                and not principal.authenticated
            ):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Authentication required"},
                    headers={"WWW-Authenticate": "Session"},
                )

            return await call_next(request)
        finally:
            db.reset_database_config(token)

    # The overview is the explicit homepage. The two workspaces are separate
    # pages so changing a clause and explaining a clause stay distinct tasks.
    @app.get("/", include_in_schema=False)
    def overview_page() -> FileResponse:
        return _serve(OVERVIEW_HTML, "Overview")

    @app.get("/impact-analysis", include_in_schema=False)
    def impact_page() -> FileResponse:
        return _serve(IMPACT_HTML, "Impact analysis")

    @app.get("/explain", include_in_schema=False)
    def explain_page() -> FileResponse:
        return _serve(EXPLAIN_HTML, "Clause explanation")

    # Register each router exactly once, through this factory only.
    _include_router_once(app, api_auth.router)
    _include_router_once(app, api_a.router)
    _include_router_once(app, api_b.router)

    return app

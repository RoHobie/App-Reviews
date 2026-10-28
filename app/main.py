"""
Review Intelligence FastAPI Application Entrypoint.

Purpose:
    Initializes the FastAPI application, mounts API routers (runs, apps, reports),
    serves the static frontend dashboard, provides system health check, and manages
    runtime startup/shutdown lifecycles (including marking interrupted jobs on container restart).

Key Responsibilities:
    - Expose `/health` for container orchestration and LLM configuration discovery.
    - Serve static assets and UI from `/dashboard`.
    - Provide standardized JSON error formatting: `{"error": {"code": ..., "message": ..., "details": ...}}`.
    - Ensure single-worker concurrency safety for SQLite and in-memory background executor.
"""

from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from typing import Any, Dict

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import settings

# Configure standard logging format
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("review_intel.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifecycle.

    On startup:
      - Verify persistent directories exist.
      - Mark any lingering 'running' runs as 'interrupted' to prevent stuck states
        if the container was killed abruptly.
    On shutdown:
      - Cooperative resource cleanup.
    """
    data_dir = Path(os.getenv("DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Application starting up. Data directory: %s", data_dir.resolve())

    # Defer import to allow clean modularity
    try:
        import signal
        from app.core.db import init_db
        from app.core.runner import handle_sigterm_graceful, recover_interrupted_runs

        try:
            signal.signal(signal.SIGTERM, handle_sigterm_graceful)
        except Exception:
            pass  # Some environments or non-main threads ignore signal registration

        init_db()
        interrupted_count = recover_interrupted_runs()
        if interrupted_count > 0:
            logger.warning("Recovered %d interrupted runs from previous process run.", interrupted_count)
    except Exception as exc:
        logger.warning("Database or recovery initialization skipped or deferred: %s", exc)

    yield

    logger.info("Application shutting down.")


app = FastAPI(
    title="Review Intelligence MVP API",
    description="Prioritized, version-aware product feedback SaaS from App Store & Play Store reviews.",
    version="0.1.0",
    lifespan=lifespan,
)

from app.api.runs import router as runs_router
from app.api.apps import router as apps_router
from app.api.reports import router as reports_router
from app.core.errors import ReviewIntelError

# Mount API routers
app.include_router(runs_router)
app.include_router(apps_router)
app.include_router(reports_router)



@app.exception_handler(ReviewIntelError)
async def review_intel_error_handler(request: Request, exc: ReviewIntelError):
    """Standardized JSON response for all typed ReviewIntelError exceptions."""
    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.to_dict()},
    )



@app.get("/health", summary="Health check and configuration probe")
def health_check() -> Dict[str, Any]:
    """
    Returns system liveness and LLM configuration readiness.

    Returns:
        JSON object with status, llm_configured flag, and provider details.
        Note: If GEMINI_API_KEY is not set, the app still reports status='ok'
        with llm_configured=false so the UI dashboard can display a setup warning banner.
    """
    gemini_keys = settings.get_gemini_api_keys()
    llm_provider = settings.llm_provider.lower()
    llm_configured = bool(gemini_keys) or llm_provider == "fake"

    return {
        "status": "ok",
        "llm_configured": llm_configured,
        "provider": llm_provider,
        "version": "0.1.0",
        "git_sha": settings.git_sha,
    }


@app.get("/", include_in_schema=False)
def root_redirect():
    """Redirect root path to interactive dashboard."""
    return RedirectResponse(url="/dashboard")


# Mount static dashboard files
dashboard_path = Path(__file__).resolve().parent.parent / "dashboard"
if dashboard_path.exists():
    app.mount("/dashboard", StaticFiles(directory=str(dashboard_path), html=True), name="dashboard")

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from runrail import __version__
from runrail.api.routes_activity import router as activity_router
from runrail.api.routes_insights import router as insights_router
from runrail.api.routes_reports import router as reports_router
from runrail.api.routes_resources import router as resources_router
from runrail.api.routes_runcontrol import router as runcontrol_router
from runrail.api.routes_runs import router as runs_router
from runrail.api.routes_watchdog import router as watchdog_router
from runrail.api.routes_workflows import router as workflows_router
from runrail.api.routes_ws import router as ws_router
from runrail.api.ws import manager as ws_manager
from runrail.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    ws_manager.set_loop(asyncio.get_running_loop())
    init_db()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="RunRail", version=__version__, lifespan=lifespan)
    app.include_router(ws_router)
    app.include_router(resources_router); app.include_router(workflows_router); app.include_router(runs_router)
    # Every /api router must be registered before the SPA catch-all below, which
    # matches "/{path:path}" and would otherwise swallow their paths.
    app.include_router(runcontrol_router); app.include_router(watchdog_router)
    app.include_router(reports_router); app.include_router(insights_router)
    app.include_router(activity_router)

    @app.get("/api/health")
    def health(): return {"status": "ok", "service": "runrail"}

    static = Path(__file__).parents[1] / "web" / "static"
    assets = static / "assets"
    if assets.is_dir(): app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        # Unknown API paths must not fall through to the SPA index page.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not found")
        if path:
            requested = (static / path).resolve()
            # Confine file serving to the static directory (no path traversal).
            if requested.is_relative_to(static.resolve()) and requested.is_file():
                return FileResponse(requested)
        index = static / "index.html"
        if index.is_file(): return FileResponse(index)
        return {"message": "RunRail API is running; frontend assets are not built. "
                           "Build them with: cd frontend && npm install && npm run build"}
    return app


app = create_app()

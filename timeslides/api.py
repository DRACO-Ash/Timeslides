"""The HTTP surface. createApp(deps) style: build the app, do not listen.

Route notes that matter for the platform:

  * GET / is both the UI and the readiness probe target (the App Store probes
    port 8080, path /, and wants a 200). It is therefore served from local
    assets and never touches the UDL. A readiness probe that depends on an
    upstream turns someone else's outage into a restart loop of our own.
  * Long work never happens in a request. A render is queued and polled.
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import audit
from .audit import event
from .config import Settings, load_settings
from .errors import TimeslidesError, ValidationError
from .groups import GroupStore
from .jobs import DONE, JobRunner
from .models import DATA_MODES, STATE_SOURCE_KEYS
from .pipeline import (MAX_GROUPS_PER_RUN, MAX_WINDOW_DAYS, RunSpec, build_report,
                       demo_report, validate_modes, validate_sources, validate_window)
from .shell import render_shell


# --------------------------------------------------------------------------- #
#  Request bodies. Pydantic rejects at the trust boundary before anything runs.
# --------------------------------------------------------------------------- #
class GroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    sats: list = Field(min_length=1, max_length=200)
    reference: Optional[int] = None
    rev: Optional[int] = None


class RunBody(BaseModel):
    groupIds: list = Field(default_factory=list, max_length=MAX_GROUPS_PER_RUN)
    days: int = Field(default=7, ge=1, le=MAX_WINDOW_DAYS)
    start: Optional[dt.datetime] = None
    end: Optional[dt.datetime] = None
    modes: list = Field(default_factory=lambda: ["REAL"], max_length=len(DATA_MODES))
    sources: list = Field(default_factory=lambda: list(STATE_SOURCE_KEYS),
                          max_length=len(STATE_SOURCE_KEYS))
    invert: bool = False


def _naive_utc(when: dt.datetime) -> dt.datetime:
    if when.tzinfo is None:
        return when.replace(microsecond=0)
    return when.astimezone(dt.timezone.utc).replace(tzinfo=None, microsecond=0)


def _window(body: RunBody) -> tuple:
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    end = _naive_utc(body.end) if body.end else now
    start = _naive_utc(body.start) if body.start else end - dt.timedelta(days=body.days)
    return validate_window(start, end)


def create_app(settings: Settings = None, store=None, runner=None,
               client_factory=None) -> FastAPI:
    """Build the application. Every dependency is injectable, so the whole
    surface is testable in-process with no network and no volume."""
    settings = settings or load_settings()
    audit.configure(settings.log_level)
    store = store or GroupStore(settings.groups_file)
    app = FastAPI(title="Timeslides", docs_url=None, redoc_url=None,
                  openapi_url=None)

    def _client():
        if client_factory is not None:
            return client_factory()
        from .udl import UDLClient
        return UDLClient(settings)

    def _render(spec: RunSpec, progress):
        if settings.demo:
            return demo_report(spec)
        groups = [store.get(gid) for gid in spec.group_ids]
        return build_report(groups, _client(), spec, progress=progress)

    runner = runner or JobRunner(_render, settings.runs_path)
    _seed_groups(store)
    app.state.settings = settings
    app.state.store = store
    app.state.runner = runner

    _register_error_handler(app)
    _register_ui(app, settings, store)
    _register_groups(app, store)
    _register_catalogue(app, settings, _client)
    _register_runs(app, settings, store, runner)
    return app


def _seed_groups(store) -> None:
    """Carry the groups that were in credentials.ini into the store, once.

    A failure here is logged and tolerated rather than fatal. An unwritable
    volume should not stop the app serving; the operator will meet the same
    problem with a clear fsGroup message the first time they save a group, and
    a pod that refuses to start over a seeding failure is harder to diagnose
    than one that starts and says why the store is empty.
    """
    try:
        seeded = store.seed_if_empty()
    except TimeslidesError as exc:
        event("boot.seed_failed", error=type(exc).__name__, detail=str(exc))
        return
    if seeded:
        event("boot.seeded", groups=seeded)


# --------------------------------------------------------------------------- #
#  Errors: one place decides what a failure looks like to a caller
# --------------------------------------------------------------------------- #
def _register_error_handler(app: FastAPI) -> None:
    @app.exception_handler(TimeslidesError)
    async def handle(request: Request, exc: TimeslidesError):
        event("request.rejected", path=request.url.path,
              error=type(exc).__name__, detail=str(exc))
        return JSONResponse(status_code=exc.status,
                            content={"error": type(exc).__name__, "detail": str(exc)})


# --------------------------------------------------------------------------- #
#  UI and health
# --------------------------------------------------------------------------- #
def _register_ui(app: FastAPI, settings: Settings, store) -> None:
    @app.get("/", response_class=HTMLResponse)
    async def index():
        """The shell, and the platform's readiness target. No upstream calls."""
        return HTMLResponse(render_shell(settings.classification, settings.demo))

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "demo": settings.demo}


# --------------------------------------------------------------------------- #
#  Groups
# --------------------------------------------------------------------------- #
def _register_groups(app: FastAPI, store) -> None:
    @app.get("/api/groups")
    async def list_groups(includeArchived: bool = False):
        return store.load(include_archived=includeArchived)

    @app.post("/api/groups", status_code=201)
    async def create_group(body: GroupBody):
        return store.create(body.model_dump())

    @app.put("/api/groups/{group_id}")
    async def update_group(group_id: str, body: GroupBody):
        return store.update(group_id, body.model_dump(), expected_rev=body.rev)

    @app.delete("/api/groups/{group_id}")
    async def archive_group(group_id: str, rev: Optional[int] = None):
        return store.archive(group_id, expected_rev=rev)

    @app.post("/api/groups/{group_id}/restore")
    async def restore_group(group_id: str, rev: Optional[int] = None):
        return store.restore(group_id, expected_rev=rev)


# --------------------------------------------------------------------------- #
#  Catalogue: the picker's source of truth
# --------------------------------------------------------------------------- #
def _register_catalogue(app: FastAPI, settings: Settings, client_for) -> None:
    @app.get("/api/catalogue")
    async def search(q: str = Query("", max_length=120),
                     limit: int = Query(50, ge=1, le=200)):
        if settings.demo:
            return {"results": _demo_catalogue(q, limit), "demo": True}
        results = client_for().search_objects(q, limit=limit)
        event("catalogue.searched", query=q, results=len(results))
        return {"results": results, "demo": False}


def _demo_catalogue(query: str, limit: int) -> list:
    """A fixed catalogue for demo mode, drawn from the objects the demo data
    actually contains plus the groups seeded from the old ini file, so the
    picker is usable with no credentials."""
    catalogue = [
        (59884, "OBJECT G"), (67689, "PRC TEST SPACECRAFT 4"), (69673, "OBJECT H"),
        (99995, "PRC OBJECT 4D"),
        (40001, "CLUSTER LEAD"), (40002, "CLUSTER TRAIL"), (40003, "CLUSTER TENDER"),
        (62902, "COSMOS 2581"), (62903, "COSMOS 2582"), (62904, "COSMOS 2583"),
        (68754, "ICEYE-X40"), (68759, "ICEYE-X41"), (68762, "ICEYE-X42"),
        (68763, "ICEYE-X43"), (68764, "ICEYE-X44"),
        (59102, "SPIDER BABY 1"), (59103, "SPIDER BABY 2"),
    ]
    text = (query or "").strip().upper()
    rows = [dict(satNo=n, name=name, intlDes=None, country=None,
                 objectType="PAYLOAD", launchDate=None, decayDate=None)
            for n, name in catalogue
            if not text or text in name.upper() or text in str(n)]
    return rows[:limit]


# --------------------------------------------------------------------------- #
#  Runs
# --------------------------------------------------------------------------- #
def _register_runs(app: FastAPI, settings: Settings, store, runner) -> None:
    @app.post("/api/runs", status_code=202)
    async def start_run(body: RunBody):
        start, end = _window(body)
        groups = _resolve_groups(store, body.groupIds, settings.demo)
        spec = RunSpec(group_ids=tuple(g["id"] for g in groups),
                       start=start, end=end,
                       modes=validate_modes(body.modes),
                       sources=validate_sources(body.sources),
                       invert=body.invert,
                       classification=settings.classification)
        label = ", ".join(g["name"] for g in groups) or "demo"
        job, joined = runner.submit(spec, label)
        return {**job.as_dict(), "joined": joined}

    @app.get("/api/runs")
    async def list_runs():
        return {"runs": runner.list()}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        return runner.get(run_id).as_dict()

    @app.get("/api/runs/{run_id}/report")
    async def get_report(run_id: str, request: Request):
        """Serve the generated document.

        The report is several megabytes and immutable once written, so it is
        streamed from the volume rather than read into memory, and a revisit
        gets a 304. Starlette does not implement If-None-Match itself, so the
        conditional check is explicit; without it every reload of the Report
        tab pushed another four megabytes.
        """
        job = runner.get(run_id)
        if job.status != DONE or job.path is None or not job.path.exists():
            raise ValidationError(
                f"run {run_id} is {job.status}; the report is not ready yet")
        etag = f'"{run_id}"'
        headers = {
            "ETag": etag,
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": f'inline; filename="phase_offset_{run_id[:8]}.html"',
        }
        if _matches_etag(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return FileResponse(job.path, media_type="text/html", headers=headers)


def _matches_etag(header: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match: a comma-separated list, "*" matches anything,
    and a weak validator prefix is ignored for this comparison."""
    if not header:
        return False
    candidates = [c.strip() for c in header.split(",")]
    if "*" in candidates:
        return True
    return any(c.removeprefix("W/") == etag for c in candidates)


def _resolve_groups(store, group_ids, demo: bool) -> list:
    """Which groups a run covers: the ones asked for, or every live group."""
    if group_ids:
        groups = [store.get(gid) for gid in group_ids]
        archived = [g["name"] for g in groups if g.get("archived")]
        if archived:
            raise ValidationError(
                f"cannot render archived group(s): {', '.join(archived)}")
        return groups
    groups = store.active()
    if not groups and not demo:
        raise ValidationError(
            "no groups are defined. Build one in the Configure tab by selecting "
            "objects from the UDL catalogue.")
    return groups

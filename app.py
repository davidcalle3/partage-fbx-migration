"""Private SSH-tunnel-only migration UI. Run exactly one uvicorn worker."""
import logging
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from engine import Partage, State, Worker, manifest_from_selection
from freebox import Freebox

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
HOME = Path(__file__).resolve().parent
token = secrets.token_urlsafe(32)
runtime = {}


@asynccontextmanager
async def lifespan(app):
    state = State()
    freebox = Freebox()
    partage = Partage()
    worker = Worker(freebox, partage, state)
    worker.start()
    runtime.update(state=state, freebox=freebox, partage=partage, worker=worker)
    try:
        yield
    finally:
        worker.shutdown()
        runtime.clear()


app = FastAPI(title="Partage — migration Freebox", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'"
    return response


def guard(value):
    if not value or not secrets.compare_digest(value, token):
        raise HTTPException(403, "Action refusée")
    # Accept only same-origin JavaScript requests; no public host binding.


def fail(exc):
    if isinstance(exc, (ValueError, KeyError)):
        raise HTTPException(400, str(exc)) from None
    logging.exception("Erreur de l'outil de migration")
    raise HTTPException(502, "Freebox ou stockage indisponible") from None


class Selection(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=200)
    target: str


class QueueRequest(BaseModel):
    plan_id: str


class SavedItem(BaseModel):
    path: str = Field(min_length=1,max_length=8192)
    name: str = Field(min_length=1,max_length=180)
    type: str
    size: int | None = None


class SavedSelection(BaseModel):
    items: list[SavedItem] = Field(max_length=200)
    target: str


class JobAction(BaseModel):
    action: str


@app.get("/")
def index():
    return FileResponse(HOME / "static/index.html", media_type="text/html")


@app.get("/static/app.js")
def js():
    return FileResponse(HOME / "static/app.js", media_type="text/javascript")


@app.get("/static/style.css")
def css():
    return FileResponse(HOME / "static/style.css", media_type="text/css")


@app.get("/api/config")
def config():
    try:
        return {"csrf": token, "partitions": runtime["freebox"].partitions(),
                "destinations": runtime["partage"].destinations()}
    except Exception as exc:
        fail(exc)


@app.get("/api/list")
def ls(path: str = Query(min_length=1, max_length=8192)):
    try:
        return {"entries": runtime["freebox"].list_dir(path)}
    except Exception as exc:
        fail(exc)


@app.get("/api/selection")
def saved_selection():
    return runtime["state"].selection()


@app.post("/api/selection")
def save_selection(body: SavedSelection, x_migration_token: str | None = Header(None)):
    guard(x_migration_token)
    try:
        if body.target not in {row["id"] for row in runtime["partage"].destinations()}:
            raise ValueError("Destination inconnue")
        for item in body.items:
            runtime["freebox"].allowed(item.path)
        runtime["state"].save_selection([x.model_dump() for x in body.items], body.target)
        return {"ok":True}
    except Exception as exc:
        fail(exc)


@app.post("/api/preview")
def preview(body: Selection, x_migration_token: str | None = Header(None)):
    guard(x_migration_token)
    try:
        destination = next((x for x in runtime["partage"].destinations()
                            if x["id"] == body.target), None)
        if not destination:
            raise ValueError("Destination Partage introuvable")
        manifest = manifest_from_selection(runtime["freebox"], body.paths)
        if manifest["files"]:
            first = manifest["files"][0]
            runtime["freebox"].probe_download(first["source"], first["size"])
        pid = runtime["state"].plan(manifest, body.target)
        return {"plan_id":pid, "files":len(manifest["files"]),
                "folders":len(manifest["folders"]), "bytes":manifest["bytes"],
                "examples":["/".join(x["parts"]) for x in manifest["files"][:12]]}
    except Exception as exc:
        fail(exc)


@app.post("/api/queue")
def queue(body: QueueRequest, x_migration_token: str | None = Header(None)):
    guard(x_migration_token)
    try:
        manifest, target = runtime["state"].take_plan(body.plan_id)
        mapped, skipped = runtime["partage"].prepare(
            manifest, target, runtime["state"].known())
        runtime["state"].queue(mapped)
        # Selection is cleared only after durable queue creation.
        runtime["state"].save_selection([], target)
        return {"queued": len(mapped), "skipped": skipped}
    except Exception as exc:
        fail(exc)


@app.get("/api/jobs")
def jobs():
    return {"jobs": runtime["state"].jobs(), "stats": runtime["state"].stats()}


@app.post("/api/jobs/{job_id}/action")
def action(job_id: str, body: JobAction, x_migration_token: str | None = Header(None)):
    guard(x_migration_token)
    try:
        if body.action not in ("pause", "resume", "retry"):
            raise ValueError("Action inconnue")
        runtime["state"].action(job_id, body.action)
        return {"ok": True}
    except Exception as exc:
        fail(exc)


@app.get("/health")
def health():
    return JSONResponse({"ok": bool(runtime)}, status_code=200 if runtime else 503)

"""HTTP API (FastAPI).

Every endpoint is a plain `def`, so FastAPI runs it in a worker thread and
the event loop is never blocked. The old `/edit` and `/video` were `async
def` but called a *blocking* job waiter, which froze the whole server --
`/health` stopped answering for the entire duration of a job.
"""
from __future__ import annotations

import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response

from . import __version__
from .config import Settings
from .discovery import ModelDiscovery
from .jobs import JobError, JobQueue, JobTimeout, QueueFull
from .media import TempFiles, UnsupportedMedia, UploadTooLarge, decode_base64, sniff
from .runtime import BrowserRuntime
from .state import StateStore
from .ui import NotLoggedIn

log = logging.getLogger("flowbridge.api")

def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=getattr(logging, level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


class Context:
    """Everything the routes need, built once per app."""

    def __init__(self, settings: Settings):
        self.settings = settings
        settings.prepare_dirs()
        self.state = StateStore(settings.state_file)
        self.runtime = BrowserRuntime(settings, self.state)
        self.discovery = ModelDiscovery(settings, self.state)
        self.queue = JobQueue(settings, self.runtime, self.discovery)
        self.temp = TempFiles(settings.tmp_dir, settings.max_upload_bytes)


def _error_response(exc: Exception) -> HTTPException:
    if isinstance(exc, NotLoggedIn):
        return HTTPException(503, detail=str(exc))
    if isinstance(exc, JobTimeout):
        return HTTPException(504, detail=str(exc))
    if isinstance(exc, QueueFull):
        return HTTPException(429, detail=str(exc))
    if isinstance(exc, UploadTooLarge):
        return HTTPException(413, detail=str(exc))
    if isinstance(exc, UnsupportedMedia):
        return HTTPException(415, detail=str(exc))
    if isinstance(exc, JobError):
        message = str(exc)
        if "not_logged_in" in message:
            return HTTPException(503, detail=message)
        return HTTPException(502, detail=message)
    if isinstance(exc, ValueError):
        return HTTPException(400, detail=str(exc))
    log.exception("unhandled error")
    return HTTPException(500, detail=f"internal error: {exc}")


def create_app(settings: Settings | None = None, ctx: Context | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    ctx = ctx or Context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx.queue.start()
        ctx.temp.sweep()
        if settings.warm_on_start:
            # Warming up in the background means the first real request does
            # not pay the ~15-25s Chromium + Flow bootstrap.
            ctx.queue.submit_async("warmup", {}, timeout=180)
        log.info("flowbridge %s ready (home=%s, idle_release=%ss)",
                 __version__, settings.home, settings.idle_release_s)
        try:
            yield
        finally:
            ctx.queue.stop()

    app = FastAPI(title="flowbridge", version=__version__, lifespan=lifespan)

    def auth(x_api_key: str | None = Header(default=None),
             authorization: str | None = Header(default=None)) -> None:
        key = settings.api_key
        if not key:
            return
        presented = x_api_key or ""
        if not presented and authorization and authorization.lower().startswith("bearer "):
            presented = authorization[7:]
        if not hmac.compare_digest(presented, key):
            raise HTTPException(401, detail="invalid or missing API key")

    def run_job(kind: str, payload: dict, timeout: float):
        try:
            return ctx.queue.submit(kind, payload, timeout=timeout)
        except Exception as exc:
            raise _error_response(exc) from exc

    def read_upload(upload: UploadFile | None, b64: str | None, field: str) -> Path | None:
        data = b""
        if upload is not None:
            data = upload.file.read()
        elif b64:
            data = decode_base64(b64)
        if not data:
            return None
        return ctx.temp.write(data, prefix=field)

    # ------------------------------------------------------------------ health
    @app.get("/health")
    def health() -> dict:
        stats = ctx.queue.stats()
        state = ctx.state.read()
        return {
            "ok": True,
            "version": __version__,
            "browser": stats["browser"],
            "logged_in": state.get("logged_in"),
            "queue": stats["queue"],
            "running": stats["running"],
            "project_url": state.get("project_url"),
            "idle_s": stats["idle_s"],
            "models_cached": bool(ctx.discovery.cached()),
        }

    @app.get("/stats", dependencies=[Depends(auth)])
    def stats() -> dict:
        return {**ctx.queue.stats(), "settings": {
            "image_model": settings.image_model,
            "video_model": settings.video_model,
            "idle_release_s": settings.idle_release_s,
            "max_queue": settings.max_queue,
        }}

    # ------------------------------------------------------------------ models
    @app.get("/models", dependencies=[Depends(auth)])
    def models(refresh: bool = Query(default=False, description="force a fresh scrape")):
        if not refresh:
            hit = ctx.discovery.cached()
            if hit:
                return hit
        return run_job("models", {"refresh": refresh}, timeout=240)

    # ------------------------------------------------------------------ warmup
    @app.post("/warmup", dependencies=[Depends(auth)])
    def warmup() -> dict:
        data = run_job("warmup", {}, timeout=180)
        ctx.state.set("logged_in", bool(data.get("logged_in")))
        return data

    @app.post("/diagnose-model", dependencies=[Depends(auth)])
    def diagnose() -> dict:
        return run_job("diagnose", {}, timeout=180)

    # ------------------------------------------------------------------- image
    @app.post("/generate", dependencies=[Depends(auth)])
    def generate(payload: dict) -> Response:
        raw = payload.get("prompt")
        if raw is not None and not isinstance(raw, str):
            raise HTTPException(400, detail="prompt must be a string")
        prompt = (raw or "").strip()
        if not prompt:
            raise HTTPException(400, detail="prompt required")
        model = payload.get("model")
        started = time.time()
        data = run_job("image", {"prompt": prompt, "model": model},
                       timeout=settings.image_timeout + 60)
        mime, _ = sniff(data)
        headers = {
            "X-Flowbridge-Ms": str(int((time.time() - started) * 1000)),
            "X-Flowbridge-Model": model or settings.image_model,
        }
        return Response(content=data, media_type=mime or "image/jpeg", headers=headers)

    @app.post("/edit", dependencies=[Depends(auth)])
    def edit(image: UploadFile = File(default=None), prompt: str = Form(...),
             image_b64: str = Form(default=None), model: str = Form(default=None)) -> Response:
        prompt = (prompt or "").strip()
        if not prompt:
            raise HTTPException(400, detail="prompt required")
        tmp = None
        started = time.time()
        try:
            tmp = read_upload(image, image_b64, "in")
            if tmp is None:
                raise HTTPException(400, detail="image file or image_b64 is required")
            data = run_job("image", {"prompt": prompt, "image_path": str(tmp), "model": model},
                           timeout=settings.image_timeout + 60)
        except HTTPException:
            raise
        except Exception as exc:
            raise _error_response(exc) from exc
        finally:
            ctx.temp.drop(tmp)
        mime, _ = sniff(data)
        headers = {
            "X-Flowbridge-Ms": str(int((time.time() - started) * 1000)),
            "X-Flowbridge-Model": model or settings.image_model,
        }
        return Response(content=data, media_type=mime or "image/jpeg", headers=headers)

    # ------------------------------------------------------------------- video
    @app.post("/video", dependencies=[Depends(auth)])
    def video(prompt: str = Form(...), video: UploadFile = File(default=None),
              mode: str = Form("generate"), resolution: str = Form("360p"),
              model: str = Form(default=None)) -> Response:
        prompt = (prompt or "").strip()
        if not prompt:
            raise HTTPException(400, detail="prompt required")
        if mode not in ("generate", "edit"):
            raise HTTPException(400, detail="mode must be 'generate' or 'edit'")
        if resolution not in ("360p", "720p"):
            raise HTTPException(400, detail="resolution must be '360p' or '720p'")
        if mode == "edit" and video is None:
            raise HTTPException(400, detail="mode=edit needs a video file")
        tmp = None
        started = time.time()
        try:
            tmp = read_upload(video, None, "vin") if video is not None else None
            data = run_job("video", {
                "prompt": prompt, "video_path": str(tmp) if tmp else None,
                "mode": mode, "resolution": resolution, "model": model,
            }, timeout=settings.video_timeout + 60)
        except HTTPException:
            raise
        except Exception as exc:
            raise _error_response(exc) from exc
        finally:
            ctx.temp.drop(tmp)
        mime, _ = sniff(data)
        headers = {
            "X-Flowbridge-Ms": str(int((time.time() - started) * 1000)),
            "X-Flowbridge-Model": model or settings.video_model,
        }
        return Response(content=data, media_type=mime or "video/mp4", headers=headers)

    # -------------------------------------------------------------------- jobs
    @app.get("/jobs/{job_id}", dependencies=[Depends(auth)])
    def job_status(job_id: str) -> dict:
        job = ctx.queue.job(job_id)
        if not job:
            raise HTTPException(404, detail="unknown job")
        return job

    @app.get("/")
    def index() -> dict:
        return {"service": "flowbridge", "version": __version__,
                "endpoints": ["/health", "/models", "/edit", "/generate", "/video",
                              "/warmup", "/stats", "/jobs/{id}"]}

    app.state.ctx = ctx
    return app


app = create_app()

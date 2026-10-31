"""Job queue: one browser worker, event-driven results.

Differences from the old `_enqueue_job`:
  * the caller waits on a `threading.Event` instead of polling every 2s,
    so the HTTP response leaves the instant the job finishes (up to 2s of
    pure latency removed per request);
  * a job has a real `running` state, so the idle watcher can no longer
    close the browser in the middle of one;
  * finished jobs are pruned (the old `_jobs` dict grew forever);
  * the queue is bounded and rejects work instead of piling up;
  * cancellation: a client that timed out marks the job cancelled and the
    worker stops before submitting the prompt.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any

from .diag import run_model_diagnose
from .image_flow import run_image_job
from .video_flow import run_video_job

log = logging.getLogger("flowbridge.jobs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
ERROR = "error"
CANCELLED = "cancelled"


class JobError(RuntimeError):
    """Job failed; message is safe to return to the client."""


class QueueFull(RuntimeError):
    pass


class JobTimeout(RuntimeError):
    pass


@dataclass
class Job:
    id: str
    kind: str
    payload: dict
    timeout: float
    status: str = QUEUED
    result: Any = None
    error: str | None = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cancel_requested: bool = False
    done: threading.Event = field(default_factory=threading.Event)

    def public(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "error": self.error,
            "queued_s": round((self.started or time.time()) - self.created, 1),
            "ran_s": round((self.finished or time.time()) - self.started, 1)
            if self.started else None,
        }


class JobQueue:
    def __init__(self, settings, runtime, discovery):
        self.settings = settings
        self.runtime = runtime
        self.discovery = discovery
        self._queue: queue.Queue[Job | None] = queue.Queue()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._idle_checked = 0.0

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="flow-worker", daemon=True)
        self._thread.start()
        log.info("job worker started")

    def stop(self, timeout: float = 20.0) -> None:
        self._stopping.set()
        self._queue.put(None)  # sentinel: worker shuts the browser down
        if self._thread:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("job worker did not stop within %ss", timeout)
        self._thread = None

    # ----------------------------------------------------------------- submit
    def submit(self, kind: str, payload: dict, timeout: float) -> Any:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, payload=payload, timeout=timeout)
        with self._lock:
            pending = sum(1 for j in self._jobs.values() if j.status in (QUEUED, RUNNING))
            if pending >= self.settings.max_queue:
                raise QueueFull(
                    f"queue is full ({pending} pending); retry later")
            self._jobs[job.id] = job
        self._queue.put(job)
        if not job.done.wait(timeout):
            job.cancel_requested = True
            raise JobTimeout(f"Flow bridge timed out after {timeout:.0f}s")
        with self._lock:
            result = job.result
            job.result = None  # free the payload; the caller holds the only copy
            status, error = job.status, job.error
        if status == ERROR:
            raise JobError(error or "job failed")
        if status == CANCELLED:
            raise JobTimeout("job was cancelled")
        return result

    def submit_async(self, kind: str, payload: dict, timeout: float = 300) -> str:
        """Enqueue without waiting (used for the startup warm-up)."""
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, payload=payload, timeout=timeout)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job.id

    def stats(self) -> dict:
        with self._lock:
            queued = sum(1 for j in self._jobs.values() if j.status == QUEUED)
            running = [j.public() for j in self._jobs.values() if j.status == RUNNING]
        return {"queue": queued, "running": running, **self.runtime.stats()}

    def job(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.public() if job else None

    # ----------------------------------------------------------------- worker
    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                self._idle_tick()
                continue
            if job is None:
                break
            self._run(job)
        try:
            self.runtime.shutdown()
            log.info("browser closed, worker exiting")
        except Exception as exc:
            log.warning("browser shutdown failed: %s", exc)

    def _idle_tick(self) -> None:
        now = time.time()
        if now - self._idle_checked < 30:
            return
        self._idle_checked = now
        try:
            self._prune()
            self.runtime.maybe_release_idle(has_pending=self._pending())
        except Exception as exc:
            log.warning("idle tick failed: %s", exc)

    def _pending(self) -> bool:
        with self._lock:
            return any(j.status in (QUEUED, RUNNING) for j in self._jobs.values())

    def _prune(self) -> None:
        cutoff = time.time() - self.settings.keep_jobs_s
        with self._lock:
            for job_id in [j.id for j in self._jobs.values()
                           if j.finished and j.finished < cutoff]:
                self._jobs.pop(job_id, None)

    def _run(self, job: Job) -> None:
        job.status = RUNNING
        job.started = time.time()
        self.runtime.touch()
        log.info("job %s (%s) started", job.id, job.kind)
        try:
            job.result = self._dispatch(job)
            job.status = DONE
            log.info("job %s (%s) done in %.1fs", job.id, job.kind,
                     time.time() - job.started)
        except Exception as exc:
            job.error = str(exc) or exc.__class__.__name__
            job.status = CANCELLED if job.cancel_requested else ERROR
            log.error("job %s failed: %s\n%s", job.id, job.error, traceback.format_exc())
        finally:
            job.finished = time.time()
            self.runtime.touch()
            job.done.set()

    def _dispatch(self, job: Job) -> Any:
        payload = job.payload
        kind = job.kind
        if kind == "warmup":
            return self._warmup()
        if kind == "models":
            return self.discovery.get(self.runtime.ui(), refresh=payload.get("refresh", False))
        if kind == "diagnose":
            return run_model_diagnose(self.runtime.ui())
        ui = self.runtime.ui()
        if job.cancel_requested:
            raise JobTimeout("job was cancelled before it started")
        if kind == "video":
            return run_video_job(
                ui, payload["prompt"], video_path=payload.get("video_path"),
                mode=payload.get("mode", "generate"),
                resolution=payload.get("resolution", "360p"),
                model=payload.get("model"))
        return run_image_job(ui, payload["prompt"],
                             image_path=payload.get("image_path"),
                             model=payload.get("model"))

    def _warmup(self) -> dict:
        ui = self.runtime.ui()
        logged_in = ui.is_logged_in(navigate=True)
        try:
            self.runtime.state.set("logged_in", bool(logged_in))
        except Exception:
            pass
        result = {"logged_in": logged_in, "url": ui.d.url}
        if logged_in:
            try:
                ui.ensure_ready()
                result["project_url"] = ui.d.url
            except Exception as exc:
                result["warm_error"] = str(exc)
        return result

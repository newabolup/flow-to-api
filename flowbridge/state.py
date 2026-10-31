"""Small persistent state file (project URL, model cache).

The old bridge rewrote state.json with a non-atomic `write_text` from
several threads, so a crash mid-write left an empty file and the next job
created a brand new Flow project (a full ~20s detour). Writes here are
atomic (tmp file + os.replace) and guarded by a lock, and the model cache
survives restarts so a cold boot never needs a 30-60s scrape.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("flowbridge.state")


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None

    # ------------------------------------------------------------------ io
    def read(self) -> dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return dict(self._cache)
            data: dict[str, Any] = {}
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    data = {}
            except FileNotFoundError:
                data = {}
            except Exception as exc:
                log.warning("state file unreadable (%s); starting clean", exc)
                data = {}
            self._cache = data
            return dict(data)

    def write(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._cache = dict(data)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".json.tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)
            except Exception as exc:
                log.warning("state write failed: %s", exc)

    def update(self, **kwargs: Any) -> dict[str, Any]:
        data = self.read()
        data.update(kwargs)
        self.write(data)
        return data

    # -------------------------------------------------------------- helpers
    def get(self, key: str, default: Any = None) -> Any:
        return self.read().get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.update(**{key: value})

    # project URL ---------------------------------------------------------
    def project_url(self, lane: int = 0) -> str | None:
        data = self.read()
        lanes = data.get("project_urls") or {}
        if isinstance(lanes, dict) and lanes.get(str(lane)):
            return lanes[str(lane)]
        if lane == 0:
            return data.get("project_url")
        return None

    def set_project_url(self, url: str, lane: int = 0) -> None:
        data = self.read()
        lanes = data.get("project_urls")
        if not isinstance(lanes, dict):
            lanes = {}
        lanes[str(lane)] = url
        data["project_urls"] = lanes
        if lane == 0:
            data["project_url"] = url
        data["project_updated"] = int(time.time())
        self.write(data)

    # model cache ---------------------------------------------------------
    def cached_models(self, ttl_s: int) -> dict[str, Any] | None:
        data = self.read()
        models = data.get("models")
        updated = data.get("models_updated") or 0
        if not isinstance(models, dict):
            return None
        if ttl_s and time.time() - updated > ttl_s:
            return None
        out = dict(models)
        out["cached"] = True
        out["updated"] = updated
        return out

    def save_models(self, image_models: list[str], video_models: list[str]) -> None:
        self.update(
            models={"image_models": image_models, "video_models": video_models},
            models_updated=int(time.time()),
        )

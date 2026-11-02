"""Model discovery: read the model menus Flow actually offers this account.

Results are cached in memory AND in state.json, so a service restart no
longer pays a 30-60s scrape before the first request (the old bridge kept
the cache in RAM only).
"""
from __future__ import annotations

import logging
import re
import threading
import time

from .driver import css
from .ui import CHROME_LABELS, FlowUI, model_key

log = logging.getLogger("flowbridge.discovery")


class ModelDiscovery:
    def __init__(self, settings, state):
        self.settings = settings
        self.state = state
        self._lock = threading.Lock()
        self._ts = 0.0
        self._data: dict | None = None

    # ---------------------------------------------------------------- public
    def cached(self) -> dict | None:
        with self._lock:
            if self._data and time.time() - self._ts < self.settings.models_ttl_s:
                out = dict(self._data)
                out["cached"] = True
                return out
        stored = self.state.cached_models(self.settings.models_ttl_s)
        if stored:
            with self._lock:
                self._data = {"image_models": stored.get("image_models", [])}
                self._ts = stored.get("updated") or time.time()
            out = dict(stored)
            out["cached"] = True
            return out
        return None

    def get(self, ui: FlowUI, refresh: bool = False) -> dict:
        if not refresh:
            hit = self.cached()
            if hit:
                return hit
        data = self._scan(ui)
        with self._lock:
            self._data = {"image_models": data["image_models"]}
            self._ts = time.time()
        try:
            self.state.save_models(data["image_models"], [])
        except Exception:
            pass
        out = dict(data)
        out["cached"] = False
        return out

    # ----------------------------------------------------------------- scans
    def _scan(self, ui: FlowUI) -> dict:
        ui.ensure_ready()
        ui.close_panels()
        image_models: list[str] = []
        try:
            image_models = ui.open_image_model_options()
        except Exception as exc:
            log.warning("image-model discovery failed: %s", exc)
        ui.d.escape(100)
        ui.close_panels()
        if not image_models:
            body = ui.d.probe().get("body", "")
            match = re.search(r"Nano Banana[^\n]{0,40}", body)
            if match:
                image_models = [model_key(match.group(0).strip())]
                log.warning("image-model discovery: reporting the selected model only")
        log.info("models discovered: image=%s", image_models)
        return {"image_models": image_models}

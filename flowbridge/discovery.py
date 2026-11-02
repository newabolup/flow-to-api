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
                self._data = {"image_models": stored.get("image_models", []),
                              "video_models": stored.get("video_models", [])}
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
            self._data = {"image_models": data["image_models"],
                          "video_models": data["video_models"]}
            self._ts = time.time()
        try:
            self.state.save_models(data["image_models"], data["video_models"])
        except Exception:
            pass
        out = dict(data)
        out["cached"] = False
        return out

    # ----------------------------------------------------------------- scans
    def _scan(self, ui: FlowUI) -> dict:
        ui.ensure_ready()
        ui.close_panels()
        ui.set_agent(True)
        image_models: list[str] = []
        try:
            image_models = ui.open_image_model_options()
        except Exception as exc:
            log.warning("image-model discovery failed: %s", exc)
        ui.d.escape(100)
        agent_video: list[str] = []
        try:
            agent_video = self._agent_video_models(ui)
        except Exception as exc:
            log.warning("agent-panel video discovery failed: %s", exc)
        ui.close_panels()
        if not image_models:
            body = ui.d.probe().get("body", "")
            match = re.search(r"Nano Banana[^\n]{0,40}", body)
            if match:
                image_models = [model_key(match.group(0).strip())]
                log.warning("image-model discovery: reporting the selected model only")
        video_models: list[str] = []
        try:
            ui.set_agent(False)
            video_models = self._standard_video_models(ui)
        except Exception as exc:
            log.warning("video-model discovery failed: %s", exc)
        finally:
            ui.d.escape(100)
            try:
                ui.set_agent(True)
            except Exception:
                pass
        merged: list[str] = []
        for name in agent_video + video_models:
            if name not in merged:
                merged.append(name)
        if agent_video and video_models and set(agent_video) != set(video_models):
            log.info("video model lists differ (agent=%s standard=%s)", agent_video, video_models)
        log.info("models discovered: image=%s video=%s", image_models, merged)
        return {"image_models": image_models, "video_models": merged}

    def _agent_video_models(self, ui: FlowUI) -> list[str]:
        """The Agent panel has its own video-model dropdown (no toggle dance)."""
        spec = css('[aria-label="Video generation default model"]')
        if not ui.d.visible(spec):
            return []
        if not ui.d.click(spec, timeout=6):
            return []
        labels = [m for m in ui.d.probe().get("menu", []) if m not in CHROME_LABELS]
        ui.d.escape(100)
        return labels

    def _standard_video_models(self, ui: FlowUI) -> list[str]:
        if not ui.open_standard_settings():
            log.warning("video-model discovery: settings trigger not found")
            return []
        try:
            ui.select_toggle("Mode", "Video")
        except Exception as exc:
            log.warning("video-model discovery: Mode=Video failed: %s", exc)
        labels: list[str] = []
        if ui.d.click(css(".model-select-trigger-content"), timeout=6):
            probe = ui.d.probe()
            labels = [m for m in probe.get("menu", [])
                      if m not in CHROME_LABELS and "credit" not in m.lower()]
        if not labels:
            labels = ui.model_menu_labels()
        if not labels:
            trigger_text = ui.d.inner_text(css(".model-select-trigger-content"), timeout=2)
            if trigger_text and "\n" not in trigger_text and len(trigger_text) <= 80:
                labels = [trigger_text]
        ui.d.escape(100)
        log.info("standard video menu labels: %s", labels)
        return labels

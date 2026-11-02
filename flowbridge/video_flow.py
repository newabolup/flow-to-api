"""Video generate / edit job (standard composer, Agent OFF)."""
from __future__ import annotations

import logging

from .media import sniff
from .ui import FlowError, FlowUI

log = logging.getLogger("flowbridge.video")

# Measured on 2026-10-25: Omni 1.1 Flash 360p = 6 credits, 720p = 12.
OMNI_CREDITS = {"360p": 6, "720p": 12}


def run_video_job(ui: FlowUI, prompt: str, video_path: str | None = None,
                  mode: str = "generate", resolution: str = "360p",
                  model: str | None = None) -> bytes:
    settings = ui.settings
    model_name = (model or settings.video_model).strip()
    if resolution not in ("360p", "720p"):
        log.warning("invalid resolution %r; falling back to 360p", resolution)
        resolution = "360p"
    try:
        ui.ensure_ready()
        ui.close_panels()
        if ui.set_agent(False) is False:
            log.warning("could not confirm Agent OFF; video settings may not stick")
        if mode == "edit" and video_path:
            # Video-ingredient edits cost ~40 credits: skip the total check.
            ui.set_standard_video_settings(model=model_name, resolution=resolution,
                                           expect_credits=None,
                                           strict_model=bool(model))
            ui.set_video_type("Ingredients")
            ui.add_video_ingredient(video_path)
        else:
            credits = OMNI_CREDITS.get(resolution) if model_name == "Omni 1.1 Flash" else None
            ui.set_standard_video_settings(model=model_name, resolution=resolution,
                                           expect_credits=credits,
                                           strict_model=bool(model))
        seen = ui.settings_trigger_mode()
        if seen == "image":
            raise FlowError("composer still in Image mode (video settings did not stick)")
        if seen == "unknown":
            log.warning("composer mode unknown; proceeding")
        ui.submit(prompt)
        ui.wait_video_result(settings.video_result_timeout)
        data = ui.download_video()
    except Exception:
        ui.reset_session()
        ui.save_debug("vidfail")
        raise
    finally:
        ui.d.escape(0)
        try:
            ui.set_agent(True)  # image jobs need the Agent back ON
        except Exception:
            pass
    mime, _ = sniff(data)
    if mime.startswith("image/"):
        raise FlowError("Flow returned an image instead of a video (mode switch failed)")
    if not mime.startswith("video/"):
        raise FlowError("downloaded file is not a video (unexpected format)")
    return data

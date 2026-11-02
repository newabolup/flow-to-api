"""Image generate / edit job."""
from __future__ import annotations

import logging

from .media import sniff
from .ui import FlowError, FlowUI

log = logging.getLogger("flowbridge.image")


def run_image_job(ui: FlowUI, prompt: str, image_path: str | None = None,
                  image_paths: list[str] | None = None,
                  model: str | None = None) -> bytes:
    """Full job: ready -> agent ON -> model -> upload? -> prompt -> wait -> download."""
    settings = ui.settings
    paths = list(image_paths) if image_paths else ([image_path] if image_path else [])
    try:
        ui.ensure_ready()
        # A leftover side panel hides the composer; closing it is cheap when
        # nothing is open (one probe) and required otherwise.
        ui.close_panels()
        if ui.set_agent(True) is False:
            log.warning("agent toggle could not be confirmed ON; continuing")
        ui.ensure_image_model(model, strict=bool(model))
        if paths:
            ui.upload_images(paths)
            ui.add_ingredients(count=len(paths))
        # Freshness snapshot AFTER the upload (so the uploaded image is part
        # of the baseline) and before submitting.
        baseline = ui.card_srcs()
        ui.submit(prompt)
        new_srcs = ui.wait_result(settings.result_timeout, baseline)
        data = ui.download_image(new_srcs=new_srcs, is_edit=bool(paths))
    except Exception:
        ui.reset_session()
        ui.save_debug("jobfail")
        raise
    ui.d.escape(150)  # close the viewer cleanly so next job sees the composer
    mime, _ = sniff(data)
    if mime and not mime.startswith("image/"):
        raise FlowError(f"Flow returned {mime}, expected an image")
    return data

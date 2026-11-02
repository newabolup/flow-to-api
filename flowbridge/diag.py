"""Optional diagnostics (never on the job hot path).

The old bridge ran `_log_post_job_state` twice per job -- four
`is_visible()` probes with 200ms timeouts each -- and shipped two
hardcoded `/tmp/...` diagnostic endpoints. Diagnostics are now a single
opt-in endpoint and the per-job logging is behind FLOWBRIDGE_VERBOSE_DIAGNOSTICS.
"""
from __future__ import annotations

import logging
import time

from .driver import css

log = logging.getLogger("flowbridge.diag")


def run_model_diagnose(ui) -> dict:
    """Screenshot the composer, the settings popup and the reset state.
    Never submits anything."""
    shots: list[str] = []
    debug_dir = ui.settings.debug_dir
    debug_dir.mkdir(parents=True, exist_ok=True)

    def shot(name: str) -> None:
        ts = int(time.time())
        path = debug_dir / f"diag-{name}-{ts}.png"
        if ui.d.screenshot(str(path)):
            shots.append(str(path))

    try:
        ui.ensure_ready()
        ui.set_agent(True)
        ui.d.click(css('[aria-label="What do you want to create?"]'), timeout=5)
        shot("composer")
        if ui.open_agent_panel():
            shot("agent-panel")
        ui.close_panels()
        probe = ui.d.probe()
        return {
            "shots": shots,
            "url": probe.get("url"),
            "composer_visible": bool((probe.get("composer") or {}).get("visible")),
            "agent": probe.get("agent"),
            "cards": len(probe.get("cards") or []),
            "buttons": (probe.get("buttons") or [])[:25],
        }
    except Exception as exc:
        log.warning("diagnose failed: %s", exc)
        shot("fail")
        return {"shots": shots, "error": str(exc)}

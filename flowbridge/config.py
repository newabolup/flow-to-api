"""Configuration (env driven, one object passed down -- no globals)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

TRUTHY = {"1", "true", "yes", "on", "y"}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUTHY


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # paths ---------------------------------------------------------------
    home: Path = field(default_factory=lambda: Path(
        os.environ.get("FLOWBRIDGE_HOME") or (Path.home() / "flowbridge")))

    # models --------------------------------------------------------------
    image_model: str = field(default_factory=lambda: os.environ.get(
        "FLOW_IMAGE_MODEL", "Nano Banana 2 Lite"))
    video_model: str = field(default_factory=lambda: os.environ.get(
        "FLOW_VIDEO_MODEL", "Omni 1.1 Flash"))

    # queue ---------------------------------------------------------------
    max_queue: int = field(default_factory=lambda: _env_int("FLOWBRIDGE_MAX_QUEUE", 32))
    keep_jobs_s: int = field(default_factory=lambda: _env_int("FLOWBRIDGE_KEEP_JOBS_S", 900))

    # timeouts (seconds) --------------------------------------------------
    image_timeout: float = field(default_factory=lambda: _env_float("FLOWBRIDGE_IMAGE_TIMEOUT", 420))
    video_timeout: float = field(default_factory=lambda: _env_float("FLOWBRIDGE_VIDEO_TIMEOUT", 900))
    result_timeout: float = field(default_factory=lambda: _env_float("FLOWBRIDGE_RESULT_TIMEOUT", 300))
    video_result_timeout: float = field(
        default_factory=lambda: _env_float("FLOWBRIDGE_VIDEO_RESULT_TIMEOUT", 600))
    nav_timeout: float = field(default_factory=lambda: _env_float("FLOWBRIDGE_NAV_TIMEOUT", 25))
    click_timeout: float = field(default_factory=lambda: _env_float("FLOWBRIDGE_CLICK_TIMEOUT", 12))
    probe_poll_ms: int = field(default_factory=lambda: _env_int("FLOWBRIDGE_PROBE_POLL_MS", 250))

    # lifecycle -----------------------------------------------------------
    idle_release_s: float = field(default_factory=lambda: _env_float("FLOWBRIDGE_IDLE_RELEASE_S", 900))
    keep_warm_s: float = field(default_factory=lambda: _env_float("FLOWBRIDGE_KEEP_WARM_S", 240))
    warm_on_start: bool = field(default_factory=lambda: _env_bool("FLOWBRIDGE_WARM_ON_START", True))
    headless: bool | None = field(default_factory=lambda: (
        None if os.environ.get("FLOWBRIDGE_HEADLESS") is None
        else _env_bool("FLOWBRIDGE_HEADLESS", True)))

    # limits / security ---------------------------------------------------
    api_key: str | None = field(default_factory=lambda: os.environ.get("FLOWBRIDGE_API_KEY") or None)
    max_upload_mb: int = field(default_factory=lambda: _env_int("FLOWBRIDGE_MAX_UPLOAD_MB", 64))
    models_ttl_s: int = field(default_factory=lambda: _env_int("FLOWBRIDGE_MODELS_TTL_S", 3600))
    debug_artifacts: bool = field(default_factory=lambda: _env_bool("FLOWBRIDGE_DEBUG", True))
    verbose_diagnostics: bool = field(
        default_factory=lambda: _env_bool("FLOWBRIDGE_VERBOSE_DIAGNOSTICS", False))
    log_level: str = field(default_factory=lambda: os.environ.get("FLOWBRIDGE_LOG_LEVEL", "INFO"))

    # browser -------------------------------------------------------------
    viewport: tuple = (1280, 900)
    launch_args: tuple = (
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-extensions",
        "--mute-audio",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
    )

    # derived -------------------------------------------------------------
    @property
    def profile_dir(self) -> Path:
        return self.home / "profile"

    @property
    def state_file(self) -> Path:
        return self.home / "state.json"

    @property
    def debug_dir(self) -> Path:
        return self.home / "debug"

    @property
    def tmp_dir(self) -> Path:
        return self.home / "tmp"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    def prepare_dirs(self) -> None:
        for path in (self.home, self.debug_dir, self.tmp_dir):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> Settings:
        return cls()

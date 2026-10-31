"""Test doubles: a fake page driver and a fake runtime.

The point of these is that the whole job pipeline (queue -> flow -> HTTP)
can be exercised in-process, without Chromium and without a Google
account. `FakeDriver` implements exactly the surface `PageDriver` exposes
to the flow code, so a change in the flow layer shows up here.
"""
from __future__ import annotations

import copy
import tempfile
from pathlib import Path

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32

DEFAULT_BUTTONS = [
    "Add media", "Upload", "Add ingredients to the prompt box", "Add to prompt",
    "Start generation", "Download media", "Settings", "Save", "Agent",
    "New project", "Settings trigger", "Upload media", "More options",
]


def default_world() -> dict:
    return {
        "url": "https://flow.google.com/project/abc",
        "composer_visible": True,
        "composer_text": "",
        "panel_open": False,
        "agent": True,
        "popup_open": False,
        "sign_in_wall": False,
        "new_project": True,
        "body": "What do you want to create?\nNano Banana 2 Lite\nAgent settings\nMode",
        "cards": [{"src": "https://lh3/old-card", "w": 400, "y": 100}],
        "buttons": list(DEFAULT_BUTTONS),
        "menu": [],
        "toggles": {},
        "summary": ["Nano Banana 2 Lite x1"],
        "videos": 0,
        "credit_total": None,
        "prompt_chips": 0,
        "inner_texts": {},
        # behaviour knobs
        "finish_on_wait": True,
        "new_card_src": "https://lh3/new-card",
        "fail_on_wait": None,
        "download_bytes": JPEG,
        "video_download_bytes": MP4,
    }


class FakeDriver:
    """Drop-in stand-in for flowbridge.driver.PageDriver."""

    def __init__(self, world: dict | None = None):
        self.world = copy.deepcopy(world or default_world())
        self.calls: list[str] = []
        self.uploads: list[str] = []
        self.marked: str | None = None

    # ------------------------------------------------------------------ reads
    @property
    def url(self) -> str:
        return self.world["url"]

    def probe(self) -> dict:
        w = self.world
        return {
            "url": w["url"],
            "body": w["body"],
            "low": w["body"].lower(),
            "composer": {"present": True, "visible": w["composer_visible"],
                         "text": w["composer_text"]},
            "cards": copy.deepcopy(w["cards"]),
            "menu": list(w["menu"]),
            "buttons": [{"t": b, "al": b} for b in w["buttons"]],
            "agent": w["agent"],
            "toggles": copy.deepcopy(w["toggles"]),
            "download": self._has("Download media"),
            "settings_trigger": self._has("Settings trigger"),
            "popup_open": w["popup_open"],
            "panel_open": w["panel_open"],
            "sign_in_wall": w["sign_in_wall"],
            "new_project": w["new_project"],
            "thinking": False,
            "failed": bool(w["fail_on_wait"]),
            "credit_total": w["credit_total"],
            "progress": False,
            "summary": list(w["summary"]),
            "videos": w["videos"],
            "file_inputs": 0,
            "prompt_chips": w["prompt_chips"],
            "dialogs": 0,
        }

    def _has(self, name: str) -> bool:
        return any(name in b for b in self.world["buttons"])

    def wait_until(self, kind: str, arg=None, timeout: float = 30.0):
        self.calls.append(f"wait:{kind}:{arg}")
        w = self.world
        if kind == "composer":
            return w["composer_visible"]
        if kind == "button":
            return self._has(arg)
        if kind == "text":
            return str(arg).lower() in w["body"].lower()
        if kind == "upload_idle":
            return True
        if kind in ("result", "result_any"):
            if w["fail_on_wait"]:
                return "failed"
            if not w["finish_on_wait"]:
                return False
            base = arg or []
            src = w["new_card_src"]
            if src not in base and src not in [c["src"] for c in w["cards"]]:
                w["cards"].append({"src": src, "w": 400, "y": 200})
            if kind == "result_any":
                w["videos"] = 1
            return "done"
        return False

    def count(self, spec) -> int:
        return 1 if self._spec_exists(spec) else 0

    def visible(self, spec, timeout: float = 0) -> bool:
        return self._spec_exists(spec)

    def inner_text(self, spec, timeout: float = 2.0) -> str:
        return self.world["inner_texts"].get(repr(spec), "")

    def elements(self, spec) -> list:
        return []

    def first_visible(self, specs):
        for spec in specs:
            if self.visible(spec):
                return spec
        return None

    def _spec_exists(self, spec) -> bool:
        text = " ".join(str(part) for part in spec)
        for label in (".model-select-trigger-content", "[aria-label=\"Settings trigger\"]"):
            if label in text:
                return True
        haystack = " ".join(self.world["buttons"] + self.world["menu"])
        if any(name.lower() in text.lower() for name in haystack):
            return True
        # text()/label() specs also match visible body text, like Playwright
        return any(str(part).lower() in self.world["body"].lower()
                   for part in spec[1:] if isinstance(part, str))

    # ----------------------------------------------------------------- writes
    def click(self, spec, timeout=None, force_fallback: bool = True) -> bool:
        self.calls.append(f"click:{spec}")
        if not self._spec_exists(spec):
            return False
        text = " ".join(str(part) for part in spec)
        # A real click on the Agent control flips the toggle.
        if "Agent" in text and "instructions" not in text.lower():
            self.world["agent"] = not self.world["agent"]
        if "Settings trigger" in text:
            self.world["popup_open"] = True
        return True

    def click_first(self, specs, timeout=None):
        for spec in specs:
            if self.click(spec, timeout=timeout):
                return spec
        return None

    def click_within(self, group, spec, timeout=None) -> bool:
        self.calls.append(f"click_within:{group}:{spec}")
        return True

    def goto(self, url: str, timeout: float = 25.0) -> bool:
        self.calls.append(f"goto:{url}")
        self.world["url"] = url
        return True

    def sleep(self, seconds: float) -> None:
        self.calls.append(f"sleep:{seconds}")

    def clear_composer(self) -> bool:
        self.world["composer_text"] = ""
        return True

    def escape(self, wait_ms: int = 120) -> None:
        self.calls.append("escape")

    def press(self, key: str) -> None:
        self.calls.append(f"press:{key}")

    def type_text(self, value: str) -> None:
        self.calls.append("type_text")
        self.world["composer_text"] += value

    def fill(self, spec, value: str) -> bool:
        self.calls.append("fill")
        self.world["composer_text"] = value
        return True

    def upload_via(self, action, path: str, timeout: float = 20.0) -> bool:
        action()
        self.uploads.append(str(path))
        return True

    def download_after(self, action, timeout: float = 60.0):
        if action is not None:
            action()
        return self.world["download_bytes"]

    def mark_card(self, src: str) -> bool:
        self.marked = src
        return src in [c["src"] for c in self.world["cards"]]

    def click_marked_card(self, timeout: float = 8.0) -> bool:
        self.calls.append("click_marked_card")
        return self.marked is not None

    def screenshot(self, path: str) -> bool:
        return False

    def html(self) -> str:
        return "<html>fake</html>"


class FakeRuntime:
    """Stands in for BrowserRuntime so JobQueue can run headless."""

    def __init__(self, driver: FakeDriver, state, settings):
        self.driver = driver
        self.state = state
        self.settings = settings
        self.launch_count = 0
        self.touches = 0
        self.shut = False
        from flowbridge.ui import FlowUI
        self._ui = FlowUI(driver, settings, state)

    @property
    def running(self) -> bool:
        return True

    def ui(self):
        self.launch_count += 1
        return self._ui

    def touch(self) -> None:
        self.touches += 1

    def stats(self) -> dict:
        return {"browser": "running", "launches": 1, "idle_s": 0.0}

    def maybe_release_idle(self, has_pending: bool) -> bool:
        return False

    def shutdown(self) -> None:
        self.shut = True


class FakeDiscovery:
    def __init__(self, data=None):
        self.data = data or {"image_models": ["Nano Banana 2 Lite"],
                             "video_models": ["Omni 1.1 Flash"], "cached": False}

    def cached(self):
        return dict(self.data, cached=True)

    def get(self, ui, refresh: bool = False):
        return dict(self.data, cached=False)


class FakeContext:
    def __init__(self, settings, driver=None, discovery=None):
        from flowbridge.jobs import JobQueue
        from flowbridge.state import StateStore

        self.settings = settings
        self.state = StateStore(settings.state_file)
        self.driver = driver or FakeDriver()
        self.runtime = FakeRuntime(self.driver, self.state, settings)
        self.discovery = discovery or FakeDiscovery()
        self.queue = JobQueue(settings, self.runtime, self.discovery)
        from flowbridge.media import TempFiles
        self.temp = TempFiles(settings.tmp_dir, settings.max_upload_bytes)


def make_settings(tmp: Path, **overrides):
    from flowbridge.config import Settings

    settings = Settings(home=Path(tmp))
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.prepare_dirs()
    return settings


def temp_settings() -> tuple:
    tmp = tempfile.mkdtemp(prefix="flowbridge-test-")
    return make_settings(Path(tmp)), Path(tmp)

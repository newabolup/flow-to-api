"""Flow UI adapter: every hard-won selector and UI recipe lives here.

Ported 1:1 from the original bridge.py (the comments explain *why* each
selector exists -- they were learned from live failures), but rewritten on
top of `PageDriver`:

  * state decisions use one `probe()` instead of dozens of `.count()` /
    `.is_visible()` round trips;
  * waits are event driven (`wait_until`) instead of `wait_for_timeout`;
  * per-project session facts (agent toggle, selected model) are cached
    and re-verified from the probe instead of being re-derived each job.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from .driver import (
    PageDriver,
    css,
    label,
    placeholder,
    role,
    text,
)

log = logging.getLogger("flowbridge.ui")

COMPOSER_SPECS = (
    css('[aria-label="What do you want to create?"]'),
    label("What do you want to create?"),
    placeholder("What do you want to create?"),
    css("textarea"),
)
COMPOSER = COMPOSER_SPECS[0]

START_SPECS = (
    role("button", "Start generation"),
    css('button[aria-label="Start generation"]'),
    css("button[type=submit]"),
)
DOWNLOAD_SPECS = (role("button", "Download media"),)
SIZE_RE = re.compile(r"Original|1K|Download|Full|Large", re.I)

# Chrome labels that are never model names (used by the dropdown readers).
CHROME_LABELS = {
    "Image generation default", "Save", "Cancel", "Settings",
    "Agent settings", "Done", "Back", "Agent instructions",
    "What do you want to create?",
}


class FlowError(RuntimeError):
    """A step of the Flow recipe failed."""


class NotLoggedIn(FlowError):
    pass


def model_key(label_text: str) -> str:
    """Model label without a leading emoji/prefix: '🍌 Nano Banana 2 Lite'
    -> 'Nano Banana 2 Lite' (user-supplied names are matched on this)."""
    value = (label_text or "").strip()
    value = re.sub(r"^[^\w]+", "", value).strip()
    return value


def body_has_model(body: str, wanted_key: str) -> bool:
    """Line comparison, not substring: 'Nano Banana 2' must not match the
    line 'Nano Banana 2 Lite'."""
    return any(model_key(line) == wanted_key for line in (body or "").splitlines())


class FlowUI:
    """One Flow tab: navigation, settings, prompt, result, download."""

    def __init__(self, driver: PageDriver, settings, state, lane: int = 0):
        self.d = driver
        self.settings = settings
        self.state = state
        self.lane = lane
        # Per-project cache. Reset on any navigation / on failure.
        self._sess = {"agent": None, "model": None, "url": None, "settings": False}

    # ------------------------------------------------------------------ util
    @property
    def debug_dir(self) -> Path:
        return self.settings.debug_dir

    def reset_session(self) -> None:
        self._sess = {"agent": None, "model": None, "url": None, "settings": False}

    def probe(self) -> dict:
        return self.d.probe()

    def save_debug(self, tag: str) -> None:
        if not self.settings.debug_artifacts:
            return
        try:
            ts = int(time.time())
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self.d.screenshot(str(self.debug_dir / f"{tag}-{ts}.png"))
            (self.debug_dir / f"{tag}-{ts}.html").write_text(self.d.html(), encoding="utf-8")
            log.info("debug artifacts saved: %s-%s", tag, ts)
        except Exception as exc:
            log.warning("debug save failed: %s", exc)

    def wait_composer(self, timeout: float = 20.0) -> bool:
        return bool(self.d.wait_until("composer", timeout=timeout))

    def wait_button(self, name: str, timeout: float = 10.0) -> bool:
        return bool(self.d.wait_until("button", name, timeout=timeout))

    def wait_upload_idle(self, timeout: float = 25.0) -> bool:
        return bool(self.d.wait_until("upload_idle", timeout=timeout))

    def _require_click(self, specs, what: str, timeout: float | None = None) -> bool:
        used = self.d.click_first(specs, timeout=timeout)
        if used is None:
            log.warning("click target not found: %s", what)
            return False
        return True

    # ------------------------------------------------------------- lifecycle
    def is_logged_in(self, navigate: bool = False) -> bool:
        """Cheap login check. The composer / 'New project' button only render
        for an authenticated Flow session."""
        probe = self.d.probe()
        if probe.get("sign_in_wall") or "accounts.google.com" in (probe.get("url") or ""):
            return False
        if probe.get("composer", {}).get("visible") or probe.get("new_project"):
            return True
        if not navigate:
            return "flow.google.com" in (probe.get("url") or "")
        self.d.goto("https://flow.google.com", timeout=self.settings.nav_timeout)
        if self.wait_composer(self.settings.nav_timeout):
            return True
        probe = self.d.probe()
        return bool(probe.get("new_project")) and not probe.get("sign_in_wall")

    def ensure_ready(self) -> None:
        """Leave the tab on a project with a usable composer, logged in.

        Fast path (the common case for a warm bridge): the composer is
        already visible -> zero navigations, zero sleeps.
        """
        probe = self.d.probe()
        url = probe.get("url") or self.d.url
        if probe.get("composer", {}).get("visible"):
            if self._sess["url"] != url:
                # A navigation happened behind our back: cached facts are void.
                log.info("session cache reset (url changed)")
                self.reset_session()
                self._sess["url"] = url
            return
        if not self.is_logged_in(navigate=True):
            raise NotLoggedIn("not_logged_in: Flow session missing, owner must log in")
        self.open_project()

    def open_project(self) -> None:
        """Reuse the stored project, else create one and store it."""
        stored = self.state.project_url(self.lane)
        if stored:
            self.d.goto(stored, timeout=self.settings.nav_timeout)
            if self.wait_composer(self.settings.nav_timeout):
                self.reset_session()
                self._sess["url"] = self.d.url
                log.info("reused project %s", stored)
                return
            log.warning("stored project did not show the composer; creating a new one")
        self.d.goto("https://flow.google.com", timeout=self.settings.nav_timeout)
        self.wait_composer(self.settings.nav_timeout)
        self.dismiss_onboarding()
        if not self.d.click_first((role("button", "New project"), text("New project"))):
            self.save_debug("noproject")
            raise FlowError("could not click 'New project' (UI changed?)")
        if not self.wait_composer(30):
            self.save_debug("nocomposer")
            raise FlowError("new project did not render the prompt box")
        self.state.set_project_url(self.d.url, self.lane)
        self.reset_session()
        self._sess["url"] = self.d.url
        log.info("created project %s", self.d.url)
        self.ensure_settings()

    def dismiss_onboarding(self) -> None:
        for spec in (role("button", "Get started"), role("button", "Got it"),
                     text("Get started"), text("Got it")):
            if self.d.visible(spec):
                self.d.click(spec, timeout=3)
                break

    def ensure_settings(self) -> None:
        """New projects only, best effort: output count x1 (the image model is
        handled per job by ensure_image_model). Never fatal."""
        try:
            if not self.d.click(role("button", "Settings"), timeout=4):
                return
            self.d.wait_until("button", "Save", timeout=5)
            self.d.click_first((text("x1", True), css('button:has-text("x1")')), timeout=3)
            self.d.click_first((role("button", "Save"), role("button", "Done"),
                                text("Save", True)), timeout=5)
            log.info("project settings ensured (best effort)")
        except Exception as exc:
            log.warning("settings ensure skipped: %s", exc)

    # ----------------------------------------------------------- panel/agent
    def close_panels(self) -> bool:
        """Close a side panel (Agent settings / instructions) that replaces
        the composer bar. Escape alone does not close them; their Back/X
        does. Escape is still pressed first because an open dropdown eats
        the first click."""
        self.d.escape(150)
        probe = self.d.probe()
        if probe.get("composer", {}).get("visible") and not probe.get("panel_open"):
            return True
        close_specs = (
            role("button", "Back", True),
            css('button[aria-label="Back"]'),
            css('button[aria-label="Close"]'),
            role("button", "Close", True),
            # Material icon buttons expose the ligature as innerText.
            text("arrow_back", True),
            text("close", True),
        )
        for _ in range(5):
            if self.d.click_first(close_specs, timeout=5) is None:
                break
            self.d.escape(100)
            probe = self.d.probe()
            if probe.get("composer", {}).get("visible") and not probe.get("panel_open"):
                return True
        probe = self.d.probe()
        ok = probe.get("composer", {}).get("visible") or not probe.get("panel_open")
        if not ok:
            log.warning("agent panel still open after close attempts")
        return ok

    def agent_state(self) -> bool | None:
        probe = self.d.probe()
        value = probe.get("agent")
        return value if isinstance(value, bool) else None

    def set_agent(self, on: bool) -> bool:
        """Set the Agent toggle. `exact=True` on the accessible name is
        REQUIRED: name="Agent" substring-matches the 'Agent instructions'
        button, which opens a panel and hides the composer."""
        probe = self.d.probe()
        current = probe.get("agent") if isinstance(probe.get("agent"), bool) else None
        if current == on:
            self._sess["agent"] = on
            return True
        specs = (
            role("button", "Agent", True),
            role("switch", "Agent", True),
            css('[aria-label="Agent"]'),
            text("Agent", True),
        )
        clicked = False
        for _ in range(3):
            if self.d.click_first(specs, timeout=5) is not None:
                clicked = True
                break
            time.sleep(0.3)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            probe = self.d.probe()
            if probe.get("agent") == on:
                self._sess["agent"] = on
                return True
            time.sleep(self.settings.probe_poll_ms / 1000)
        log.warning("agent toggle unconfirmed (wanted=%s, clicked=%s)", on, clicked)
        return False

    # ------------------------------------------------------------ agent panel
    def open_agent_panel(self) -> bool:
        probe = self.d.probe()
        if "Agent settings" in probe.get("body", ""):
            return True
        for _ in range(3):
            if self.d.click(css('button:has-text("tune")'), timeout=6):
                if self.d.wait_until("text", "Agent settings", timeout=6):
                    return True
            time.sleep(0.3)
        log.warning("agent settings panel did not open (tune button missing?)")
        return False

    def open_image_model_options(self) -> list[str]:
        """Open the Agent panel + image-model dropdown, return option labels."""
        if not self.open_agent_panel():
            return []
        labels: list[str] = []
        # Trigger candidates in the order that worked live: the proven
        # 'Nano Banana 2.1' trigger first, then any visible 'Nano Banana' text.
        for spec in (text("🍌 Nano Banana 2.1", True), text("Nano Banana 2.1"),
                     text("Nano Banana")):
            if labels:
                break
            if not self.d.visible(spec):
                continue
            if not self.d.click(spec, timeout=6):
                continue
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                probe = self.d.probe()
                menu = [m for m in probe.get("menu", []) if m not in CHROME_LABELS]
                if menu:
                    labels = menu
                    break
                body_models = [ln.strip() for ln in probe.get("body", "").splitlines()
                               if "Nano Banana" in ln and len(ln.strip()) <= 80]
                if len(body_models) >= 2:
                    labels = body_models
                    break
                time.sleep(self.settings.probe_poll_ms / 1000)
        if not labels:
            log.warning("image-model dropdown did not expose options")
        return labels

    # -------------------------------------------------------- image model set
    def ensure_image_model(self, wanted: str | None = None, strict: bool = False) -> bool:
        """Make the project's image model `wanted`. Cached per project: a
        verified switch is not repeated, which removes the old ~10-19s
        settings dance from every job after the first."""
        wanted = (wanted or self.settings.image_model).strip()
        key = model_key(wanted)
        body = self.d.probe().get("body", "")
        if self._sess.get("model") == key and body_has_model(body, key):
            log.info("image-model: cache hit (%s)", wanted)
            return True
        try:
            if not self.open_agent_panel():
                if strict:
                    raise FlowError(f"could not open Agent settings to select {wanted!r}")
                log.warning("image-model switch skipped: no settings panel")
                return False
            body = self.d.probe().get("body", "")
            if body_has_model(body, key):
                self._sess["model"] = key
                log.info("image-model: %s already selected", wanted)
                return True
            options = self.open_image_model_options()
            if not options:
                if strict:
                    raise FlowError(f"image-model dropdown did not open for {wanted!r}")
                return False
            target = None
            for option in options:
                if option == wanted or model_key(option) == key:
                    target = option
                    break
            if target is None:
                for option in options:
                    if model_key(option).lower() == key.lower():
                        target = option
                        break
            if target is None:
                message = f"image model {wanted!r} not available in Flow; available: {options}"
                if strict:
                    raise FlowError(message)
                log.warning("image-model switch skipped: %s", message)
                return False
            clicked = self.d.click_first((text(target, True), text(key)), timeout=6) is not None
            if clicked:
                self.d.click_first((role("button", "Save"), role("button", "Done")), timeout=4)
                if self.d.wait_until("text", key, timeout=8):
                    self._sess["model"] = key
                log.info("image-model switch: %s (clicked=%s)", wanted, clicked)
            return clicked
        except FlowError:
            raise
        except Exception as exc:
            if strict:
                raise
            log.warning("image-model switch skipped: %s", exc)
            return False
        finally:
            try:
                self.close_panels()
            except Exception:
                pass

    # ------------------------------------------------------- standard settings
    def open_standard_settings(self) -> bool:
        """Open the composer's Settings popup (mode/resolution/model).

        Never click a button whose text is 'Video' outside the composer:
        the sidebar 'Videos' nav matches too and navigates away (that bug
        broke every video job on 2026-10-29)."""
        probe = self.d.probe()
        if probe.get("agent") is True:
            raise FlowError("Agent still ON; refusing standard settings")
        if probe.get("popup_open"):
            return True
        # The composer's submit-controls only render once the box is focused.
        self.d.click(COMPOSER, timeout=3)
        trigger = css('[aria-label="Settings trigger"]')
        # The composer re-renders after the Agent toggle: the prompt box shows
        # up before its submit-controls, sometimes ~10s later.
        if (self.d.wait_until("button", "Settings", timeout=20)
                or self.d.visible(trigger)):
            if self.d.click(trigger, timeout=6):
                if self.d.wait_until("text", "Mode", timeout=8):
                    return True
        if self.d.click_first((css('button:has-text("·")'),
                               css('button:has-text("Nano Banana")')), timeout=5):
            if self.d.wait_until("text", "Mode", timeout=8):
                return True
        log.warning("standard settings popup did not open")
        return False

    def select_toggle(self, group: str, option: str) -> bool:
        """Select an option inside <flow-toggles aria-label="group">."""
        probe = self.d.probe()
        rows = (probe.get("toggles") or {}).get(group)
        if rows:
            for row in rows:
                if option in (row.get("text") or ""):
                    if row.get("checked"):
                        return True
                    if self.d.click_within(
                            css(f'flow-toggles[aria-label="{group}"]'),
                            css(f'mat-button-toggle:has-text("{option}") button'),
                            timeout=8):
                        return True
                    break
            else:
                log.warning("toggle option %r not in group %r", option, group)
        return self.d.click_first((text(option, True),), timeout=3) is not None

    def select_model(self, model_name: str) -> bool:
        """Pick a model in the settings menu. Skips the trigger click when
        the trigger already shows it (clicking would open a menu that then
        intercepts the next click)."""
        trig_text = self.d.inner_text(css(".model-select-trigger-content"), timeout=2)
        if model_name in trig_text:
            return True
        if not self.d.click(css(".model-select-trigger-content"), timeout=6):
            return False
        if self.d.wait_until("text", model_name, timeout=6):
            labels = [el.text() for el in self.d.elements(css(".item-text .label"))]
            if model_name in labels:
                return self.d.click(text(model_name, True), timeout=6)
            return self.d.click_first((text(model_name, True), text(model_name)), timeout=6) is not None
        return False

    def model_menu_labels(self) -> list[str]:
        probe = self.d.probe()
        labels = [m for m in probe.get("menu", []) if m not in CHROME_LABELS]
        if not labels:
            labels = [el.text() for el in self.d.elements(css(".item-text .label"))
                      if el.text() and el.text() not in CHROME_LABELS]
        return labels

    def settings_trigger_mode(self) -> str:
        """'video' / 'image' / 'unknown' from the composer summary buttons."""
        for summary in self.d.probe().get("summary", []):
            if "Video" in summary and "·" in summary:
                return "video"
            if "Nano Banana" in summary and re.search(r"x[1-4]\b", summary):
                return "image"
        return "unknown"

    def set_standard_video_settings(self, model: str, resolution: str = "360p",
                                    expect_credits: int | None = None,
                                    strict_model: bool = False) -> None:
        if not self.open_standard_settings():
            raise FlowError("standard settings popup did not open")
        self.select_toggle("Mode", "Video")
        if not self.select_model(model):
            labels = self.model_menu_labels()
            if strict_model:
                raise FlowError(
                    f"video model {model!r} not available in Flow menu; available: {labels}")
            log.warning("video model %r not selected (menu: %s); continuing", model, labels)
        self.select_toggle("Video resolution", resolution)
        self.select_toggle("Video duration", "8s")
        self.select_toggle("Aspect ratio", "16:9")
        self.select_toggle("Output count", "x1")
        probe = self.d.probe()
        total = probe.get("credit_total")
        if total is not None:
            log.info("video credit total shown: %s (expected %s)", total, expect_credits)
            if expect_credits is not None and total != expect_credits:
                raise FlowError(
                    "video settings did not stick to "
                    f"{model} {resolution} 8s x1 (shows {total} credits)")
        elif expect_credits is not None:
            raise FlowError("video credit total not found; refusing to generate unverified")
        self.d.escape(120)
        log.info("video settings set (%s %s 8s x1)", model, resolution)

    def set_video_type(self, kind: str) -> None:
        if not self.d.click(text(kind, True), timeout=4):
            log.warning("video type %r not clickable; continuing", kind)

    def agree_video_rights(self) -> bool:
        for name in ("I agree — do not show again", "do not show again", "I agree"):
            if self.d.click(role("button", name), timeout=3):
                return True
        return False

    # ---------------------------------------------------------------- uploads
    def upload_image(self, path: str) -> None:
        """Upload through the TOP Add media menu; the ingredients-panel
        Upload button does not work in the current UI."""
        self.d.escape(100)
        if not self.d.click_first((role("button", "Add media"), text("Add media")), timeout=10):
            raise FlowError("'Add media' button not found")
        if not self.d.upload_via(
                lambda: self.d.click_first((role("menuitem", "Upload"), text("Upload", True)),
                                           timeout=10) is not None,
                path, timeout=20):
            raise FlowError("upload file chooser did not open")
        # Wait for the asset to finish processing instead of sleeping 4s.
        self.wait_upload_idle(25)
        log.info("image uploaded: %s", path)

    def add_ingredient(self) -> None:
        """The composer '+' is named 'Add ingredients to the prompt box'
        (aria-label); its visible text is just '+', so text matching misses."""
        if not self.d.click_first((
                role("button", "Add ingredients to the prompt box", True),
                role("button", "Add ingredients", True),
                css('button[aria-label*="Add ingredients"]')), timeout=10):
            raise FlowError("'Add ingredients' button not found")
        # Select the asset in the picker (scoped to the open dialog first, then
        # the timeline) and confirm -- without a selection "Add to prompt" stays
        # disabled and the "edit" would silently run as a plain generation.
        self.d.click_first((css('mat-dialog-container img'), css('[role="dialog"] img'),
                            css('main img'), css("img")), timeout=6)
        if self.d.wait_until("button", "Add to prompt", timeout=12):
            self.d.click_first((role("button", "Add to prompt"), text("Add to prompt")), timeout=8)
        else:
            log.warning("'Add to prompt' never appeared; continuing")
        self.d.wait_until("button", "Start generation", timeout=8)

    def add_video_ingredient(self, path: str) -> None:
        if not self.d.click_first((
                role("button", "Add ingredients to the prompt box", True),
                css('button:has-text("Add ingredients")')), timeout=10):
            raise FlowError("'Add ingredients' button not found (video)")
        if not self.d.upload_via(
                lambda: self.d.click_first((role("button", "Upload media"),
                                            text("Upload media", True)), timeout=10) is not None,
                path, timeout=20):
            raise FlowError("video upload chooser did not open")
        self.wait_upload_idle(40)
        self.agree_video_rights()
        self.d.click_first((css('mat-dialog-container video'), css('[role="dialog"] video'),
                            css("main video"), css("video")), timeout=6)
        if self.d.wait_until("button", "Add to prompt", timeout=15):
            self.d.click_first((role("button", "Add to prompt"), text("Add to prompt")), timeout=8)
        self.agree_video_rights()
        log.info("video ingredient added to prompt")

    # ------------------------------------------------------------------ prompt
    def submit(self, prompt: str) -> None:
        self.close_panels()
        probe = self.d.probe()
        composer_text = (probe.get("composer") or {}).get("text") or ""
        if composer_text and not probe.get("prompt_chips"):
            self.d.clear_composer()
        filled = self.d.fill(COMPOSER, prompt)
        if not filled:
            self.d.click(COMPOSER, timeout=5)
            self.d.type_text(prompt)
        # Verify the prompt landed; contenteditable occasionally swallows fill.
        deadline = time.monotonic() + 6
        landed = False
        while time.monotonic() < deadline:
            value = (self.d.probe().get("composer") or {}).get("text") or ""
            if prompt[:30] in value:
                landed = True
                break
            time.sleep(0.15)
        if not landed:
            self.d.click(COMPOSER, timeout=5)
            self.d.type_text(prompt)
        if self.d.click_first(START_SPECS, timeout=10) is None:
            self.save_debug("nostart")
            raise FlowError("'Start generation' button not found")
        log.info("generation started: %r", prompt[:120])

    # ------------------------------------------------------------------ result
    def card_srcs(self) -> list[str]:
        return [c["src"] for c in self.d.probe().get("cards", []) if c.get("src")]

    def wait_result(self, timeout: float, baseline: list[str] | None = None) -> list[str]:
        """Wait for THIS job's result card.

        In-page predicate: the prompt box is idle again, no Thinking
        indicator (or an explicit completion phrase), and at least one card
        whose src was not in the pre-submit snapshot -- so a reused thread
        full of old cards/phrases cannot satisfy the wait (that off-by-one
        returned the previous job's image).
        """
        baseline = baseline or []
        started = time.monotonic()
        value = self.d.wait_until("result", baseline, timeout=timeout)
        if value == "failed":
            raise FlowError("Flow reported a generation failure")
        if not value:
            self.save_debug("timeout")
            raise FlowError("Timed out waiting for a NEW Flow result card")
        log.info("result detected in %.1fs", time.monotonic() - started)
        return [src for src in self.card_srcs() if src not in baseline]

    def wait_video_result(self, timeout: float) -> None:
        """Video generations are slower and the result is a <video> element
        (no image src to compare against a snapshot), so completion is
        "idle composer + some media on the timeline"."""
        value = self.d.wait_until("result_any", None, timeout=timeout)
        if value == "failed":
            raise FlowError("Flow reported a generation failure")
        if not value:
            self.save_debug("vidtimeout")
            raise FlowError("Timed out waiting for the Flow video result")

    # ---------------------------------------------------------------- download
    def download_image(self, new_srcs: list[str] | None = None,
                       is_edit: bool = False) -> bytes:
        """Open the result card and download the original-size image.

        Three download shapes exist in Flow and all must work:
        (1) 'Download media' opens a size menu ('1K Original size');
        (2) the fullscreen viewer button downloads directly;
        (3) the viewer menu uses different option text.
        Edits always go through the card-click path: the visible button
        belongs to the SELECTED card, which after an edit is the uploaded
        original, not the new output.
        """
        opened = False
        if new_srcs:
            for src in new_srcs:
                if self.d.mark_card(src) and self.d.click_marked_card(timeout=8):
                    opened = True
                    break
            if not opened:
                log.warning("new result card not found by src; falling back to newest card")
        if not opened and not is_edit:
            if (self.d.probe().get("download") or
                    self.d.visible(css('button:has-text("Download media")'))):
                opened = True
        if not opened:
            cards = self.d.probe().get("cards") or []
            if cards:
                first = sorted(cards, key=lambda c: c.get("y", 0))[0]
                if self.d.mark_card(first["src"]):
                    opened = self.d.click_marked_card(timeout=8)
            if not opened:
                opened = self.d.click_first((css("main img"), css("img")), timeout=6) is not None
        # Wait for the viewer's download control instead of sleeping 3s.
        self.wait_button("Download media", timeout=12)

        # Shape (2): the trigger itself downloads.
        data = self.d.download_after(
            lambda: self.d.click_first(DOWNLOAD_SPECS, timeout=8), timeout=4)
        if data:
            log.info("downloaded result directly: %d bytes", len(data))
            return data

        target = self._size_menu_target()
        if target is not None:
            data = self.d.download_after(lambda: self.d.click(target, timeout=8), timeout=90)
            if data:
                log.info("downloaded result via size menu: %d bytes", len(data))
                return data
            log.warning("size-menu download failed, retrying the direct path")
            self.d.escape(100)
            self.wait_button("Download media", timeout=8)
        data = self.d.download_after(
            lambda: self.d.click_first(DOWNLOAD_SPECS, timeout=8), timeout=90)
        if data:
            log.info("downloaded result (fallback): %d bytes", len(data))
            return data
        self.save_debug("nodownload")
        raise FlowError("could not download the result (download control not found)")

    def _size_menu_target(self):
        """Poll the open download menu for a size row; re-click the trigger
        once if the menu never opened."""
        pattern = SIZE_RE
        for attempt in range(2):
            if self.d.wait_until("text", "Original size", timeout=6):
                if self.d.visible(text("1K Original size", True)):
                    return text("1K Original size", True)
                return text("1K Original size") if self.d.visible(text("1K Original size")) \
                    else text("Original size")
            menu = self.d.probe().get("menu") or []
            if menu:
                for item in menu:
                    if pattern.search(item):
                        return text(item, True)
                return text(menu[0], True)
            if attempt == 0:
                self.d.click_first(DOWNLOAD_SPECS, timeout=6)
        return None

    def download_video(self) -> bytes:
        opened = False
        if self.d.probe().get("videos"):
            opened = self.d.click(css("main video"), timeout=8)
        if not opened:
            cards = self.d.probe().get("cards") or []
            if cards:
                first = sorted(cards, key=lambda c: c.get("y", 0))[0]
                if self.d.mark_card(first["src"]):
                    opened = self.d.click_marked_card(timeout=8)
        self.wait_button("Download media", timeout=15)
        data = self.d.download_after(
            lambda: self.d.click_first(DOWNLOAD_SPECS, timeout=10), timeout=150)
        if data:
            return data

        # Tile menu -> Download -> optional quality row. The whole walk has to
        # happen *inside* one expect_download window, otherwise the event fires
        # before anyone is listening and the job waits for nothing.
        def menu_walk() -> None:
            if self.d.click_first((role("button", "More options"),), timeout=6):
                self.d.click_first((text("Download", True),), timeout=6)
                self.d.click_first((text("Original size"),), timeout=6)

        data = self.d.download_after(menu_walk, timeout=150)
        if data:
            return data
        self.save_debug("noviddownload")
        raise FlowError("could not download the video result")

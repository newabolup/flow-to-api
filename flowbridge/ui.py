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

        # A viewer, dialog or panel may still be open from the previous job;
        # closing them is instant and avoids a 20-30s full-page navigation.
        if "flow.google.com/project/" in (url or ""):
            self.close_panels()
            self.d.escape(150)
            if self.wait_composer(timeout=3.0):
                log.info("composer restored without re-navigation")
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

    def format_model_prompt(self, prompt: str, model_name: str, is_edit: bool = False,
                            aspect_ratio: str | None = None) -> str:
        """Inject the explicit Flow directive and optional aspect ratio so the Google
        Flow agent switches to and executes with the exact model and aspect ratio requested."""
        key = model_key(model_name)
        canon = "Nano Banana 2 Lite"
        if "pro" in key.lower():
            canon = "Nano Banana Pro"
        elif "2.1" in key:
            canon = "Nano Banana 2.1"
        action = "edit" if is_edit else "generate"
        ar_clause = f", aspect ratio {aspect_ratio}" if aspect_ratio else ""
        if "Nano Banana" in prompt:
            return f"{prompt}{ar_clause}"
        return f"Use 🍌 {canon} model to {action}: {prompt}{ar_clause}"

    # -------------------------------------------------------- image model set
    def ensure_image_model(self, wanted: str | None = None, strict: bool = False) -> bool:
        """Verify the requested model name is valid and cache it for prompt formatting."""
        wanted = (wanted or self.settings.image_model).strip()
        key = model_key(wanted)
        valid_keys = {"Nano Banana Pro", "Nano Banana 2 Lite", "Nano Banana 2.1"}
        menu = self.d.probe().get("menu", [])
        if menu and not any(key.lower() in m.lower() for m in menu):
            if strict:
                raise FlowError(f"image model {wanted!r} not available in Flow")
            log.warning("model %r not in menu %s", wanted, menu)
            return False
        if key not in valid_keys:
            # Check case-insensitive match
            match = next((v for v in valid_keys if v.lower() == key.lower()), None)
            if match:
                key = match
            elif strict:
                raise FlowError(f"image model {wanted!r} not available in Flow")
            else:
                log.warning("unrecognized image model %r; falling back to %s", wanted, self.settings.image_model)
                key = model_key(self.settings.image_model)
        self._sess["model"] = key
        log.info("image-model selected: %s", key)
        return True

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

    # ---------------------------------------------------------------- uploads
    def upload_image(self, path: str) -> None:
        """Upload through the TOP Add media menu; the ingredients-panel
        Upload button does not work in the current UI."""
        self.upload_images([path])

    def upload_images(self, paths: list[str]) -> None:
        """Upload one or multiple images through the TOP Add media menu."""
        if not paths:
            return
        self.d.escape(100)
        if not self.d.click_first((
                role("button", "Add media"),
                text("Add media"),
                css('[aria-label*="Add media"]')), timeout=10):
            raise FlowError("'Add media' button not found")
        if not self.d.upload_via(
                lambda: self.d.click_first((role("menuitem", "Upload"), text("Upload", True), text("Upload")),
                                           timeout=10) is not None,
                paths if len(paths) > 1 else paths[0], timeout=30):
            raise FlowError("upload file chooser did not open")
        # Wait for the asset to finish processing instead of sleeping 4s.
        self.wait_upload_idle(35)
        log.info("images uploaded (%d): %s", len(paths), paths)

    def add_ingredient(self) -> None:
        self.add_ingredients(count=1)

    def add_ingredients(self, count: int = 1) -> None:
        """Add up to `count` uploaded ingredients into the composer."""
        if count <= 0:
            return
        for idx in range(count):
            self.d.escape(80)
            if not self.d.click_first((
                    role("button", "Add ingredients to the prompt box", True),
                    role("button", "Add ingredients", True),
                    css('button[aria-label*="Add ingredients"]')), timeout=10):
                raise FlowError("'Add ingredients' button not found")
            # Wait for overlay or dialog items to appear
            try:
                self.d.page.wait_for_selector(
                    '.cdk-overlay-pane button.asset-item, mat-dialog-container img, [role="dialog"] img',
                    timeout=8000
                )
            except Exception:
                pass
            # Pick the corresponding ingredient from the overlay or dialog
            clicked = False
            try:
                clicked = bool(self.d.page.evaluate(f"""() => {{
                    const items = document.querySelectorAll('.cdk-overlay-pane button.asset-item, mat-dialog-container img, [role="dialog"] img');
                    if (items.length > {idx}) {{
                        items[{idx}].click();
                        return true;
                    }}
                    if (items.length > 0) {{
                        items[0].click();
                        return true;
                    }}
                    return false;
                }}"""))
            except Exception:
                pass
            if not clicked:
                self.d.click_first((css('mat-dialog-container img'), css('[role="dialog"] img'),
                                    css('main img'), css("img")), timeout=6)

            # Click Add to prompt
            added = False
            try:
                added = bool(self.d.page.evaluate("""() => {
                    const btn = Array.from(document.querySelectorAll('button')).find(
                        b => (b.innerText || '').includes('Add to prompt') || (b.getAttribute('aria-label') || '').includes('Add to prompt')
                    );
                    if (btn && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                    return false;
                }"""))
            except Exception:
                pass
            if not added and self.d.wait_until("button", "Add to prompt", timeout=8):
                self.d.click_first((role("button", "Add to prompt"), text("Add to prompt")), timeout=6)
            # Brief pause for the chip to settle in composer
            self.d.sleep(0.6)
        self.d.wait_until("button", "Start generation", timeout=8)

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
        self.d.sleep(1.2)
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

    # ---------------------------------------------------------------- download
    def download_image(self, new_srcs: list[str] | None = None,
                       is_edit: bool = False) -> bytes:
        """Open the result card and download the full original-size 1K image.
        Prioritizes the official Flow viewer -> 'Download media' -> size menu path
        to ensure full uncompressed bitrate (not thumbnail previews).
        Falls back to direct cache fetch only if viewer interactions fail.
        """
        opened = False
        target_src = None
        if new_srcs:
            for src in reversed(new_srcs):
                if self.d.mark_card(src) and self.d.click_marked_card(timeout=8):
                    opened = True
                    target_src = src
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
                # Pick the newest card (highest y position in timeline), not the oldest top card!
                first = sorted(cards, key=lambda c: c.get("y", 0))[-1]
                target_src = first.get("src")
                if target_src and self.d.mark_card(target_src):
                    opened = self.d.click_marked_card(timeout=8)
            if not opened:
                opened = self.d.click_first((css("main img"), css("img")), timeout=6) is not None

        # Wait for the viewer's download control
        if self.wait_button("Download media", timeout=10):
            target = self._size_menu_target()
            if target is not None:
                data = self.d.download_after(lambda: self.d.click(target, timeout=8), timeout=90)
                if data:
                    log.info("downloaded full original result via size menu: %d bytes", len(data))
                    return data

            # Direct viewer download fallback
            data = self.d.download_after(
                lambda: self.d.click_first(DOWNLOAD_SPECS, timeout=8), timeout=5)
            if data:
                log.info("downloaded result directly from viewer: %d bytes", len(data))
                return data

        # Fast cache fallback if viewer controls failed
        if target_src:
            data = self.d.fetch_image_bytes(target_src)
            if data:
                log.info("downloaded result via direct cache fallback: %d bytes", len(data))
                return data
        if new_srcs:
            for src in reversed(new_srcs):
                data = self.d.fetch_image_bytes(src)
                if data:
                    log.info("downloaded result via direct cache fallback: %d bytes", len(data))
                    return data

        self.save_debug("nodownload")
        raise FlowError("could not download the result (download control not found)")

    def _size_menu_target(self):
        """Find the size row in the download menu (clicks trigger if not open)."""
        pattern = SIZE_RE
        # If the size menu is not yet open, click the download button to open it immediately
        if not self.d.visible(text("1K Original size")) and not self.d.visible(text("Original size")):
            self.d.click_first(DOWNLOAD_SPECS, timeout=6)

        if self.d.wait_until("text", "Original size", timeout=4):
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
        return None

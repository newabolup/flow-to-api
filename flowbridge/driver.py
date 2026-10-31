"""The only module that talks to Playwright.

Reads  -> one `probe()` (single in-page evaluate, consistent snapshot).
Waits  -> `wait_until(kind, ...)` -> `page.wait_for_function` (in-page
          polling at ~250ms, no IPC per poll, no fixed sleeps).
Clicks -> `click(spec)` keeping the old force-click fallback, which is what
          made the bridge survive leftover Material overlays.

`spec` is a plain tuple so the flow code can be exercised in unit tests
against a fake driver (tests/fake_driver.py) without a browser.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any

from . import js

log = logging.getLogger("flowbridge.driver")

ROLE = "role"
CSS = "css"
TEXT = "text"
LABEL = "label"
PLACEHOLDER = "placeholder"


def role(role_name: str, name: str, exact: bool = False) -> tuple:
    return (ROLE, role_name, name, exact)


def css(selector: str) -> tuple:
    return (CSS, selector)


def text(value: str, exact: bool = False) -> tuple:
    return (TEXT, value, exact)


def label(value: str) -> tuple:
    return (LABEL, value)


def placeholder(value: str) -> tuple:
    return (PLACEHOLDER, value)


def describe(spec: tuple) -> str:
    return "/".join(str(p) for p in spec)


class Element:
    """Minimal element view so flow code never holds a Playwright handle."""

    def __init__(self, locator):
        self._loc = locator

    def text(self) -> str:
        try:
            return (self._loc.inner_text(timeout=1500) or "").strip()
        except Exception:
            return ""

    def attr(self, name: str):
        try:
            return self._loc.get_attribute(name, timeout=1500)
        except Exception:
            return None

    def tag(self) -> str:
        try:
            return (self._loc.evaluate("e => e.tagName") or "").upper()
        except Exception:
            return ""

    def visible(self) -> bool:
        try:
            return bool(self._loc.is_visible())
        except Exception:
            return False

    def click(self, timeout: int = 5000) -> bool:
        try:
            self._loc.click(timeout=timeout)
            return True
        except Exception:
            try:
                self._loc.click(timeout=timeout, force=True)
                return True
            except Exception:
                return False


class PageDriver:
    """Playwright page -> small, testable surface."""

    def __init__(self, page, poll_ms: int = 250, click_timeout: int = 15000):
        self.page = page
        self.poll_ms = poll_ms
        self.click_timeout = click_timeout

    # ------------------------------------------------------------------ reads
    @property
    def url(self) -> str:
        try:
            return self.page.url or ""
        except Exception:
            return ""

    def probe(self) -> dict:
        """One round trip. Retries once if a navigation killed the context."""
        for attempt in range(2):
            try:
                data = self.page.evaluate(js.PROBE)
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # execution context destroyed by navigation
                if attempt:
                    log.warning("probe failed: %s", exc)
                time.sleep(0.15)
        return {}

    def wait_until(self, kind: str, arg: Any = None, timeout: float = 30.0) -> Any:
        """In-page wait. Returns the predicate's truthy value, or False on
        timeout. `kind` is a key of js.WAIT_PREDICATES."""
        expr = js.WAIT_PREDICATES[kind]
        try:
            handle = self.page.wait_for_function(
                expr, arg=arg, timeout=max(1, int(timeout * 1000)),
                polling=self.poll_ms,
            )
            if handle is None:
                return True
            try:
                return handle.json_value()
            except Exception:
                return True
        except Exception:
            return False

    def count(self, spec: tuple) -> int:
        try:
            return self._locate(spec).count()
        except Exception:
            return 0

    def visible(self, spec: tuple, timeout: float = 0) -> bool:
        try:
            loc = self._locate(spec)
            if not loc.count():
                return False
            if timeout:
                return bool(loc.first.is_visible(timeout=int(timeout * 1000)))
            return bool(loc.first.is_visible())
        except Exception:
            return False

    def inner_text(self, spec: tuple, timeout: float = 2.0) -> str:
        try:
            loc = self._locate(spec)
            if not loc.count():
                return ""
            return (loc.first.inner_text(timeout=int(timeout * 1000)) or "").strip()
        except Exception:
            return ""

    def elements(self, spec: tuple) -> list[Element]:
        try:
            return [Element(loc) for loc in self._locate(spec).all()]
        except Exception:
            return []

    def first_visible(self, specs: Iterable[tuple]) -> tuple | None:
        for spec in specs:
            if self.visible(spec):
                return spec
        return None

    # ----------------------------------------------------------------- writes
    def _locate(self, spec: tuple):
        page = self.page
        kind = spec[0]
        if kind == ROLE:
            _, role_name, name, exact = spec
            return page.get_by_role(role_name, name=name, exact=exact)
        if kind == CSS:
            return page.locator(spec[1])
        if kind == TEXT:
            _, value, exact = spec
            return page.get_by_text(value, exact=exact)
        if kind == LABEL:
            return page.get_by_label(spec[1])
        if kind == PLACEHOLDER:
            return page.get_by_placeholder(spec[1])
        raise ValueError(f"bad spec: {spec!r}")

    def click(self, spec: tuple, timeout: float | None = None,
              force_fallback: bool = True) -> bool:
        """Click the first match. Falls back to a forced click when an
        overlay swallows the pointer event (same behaviour as the old
        `_click` helper that kept this project alive)."""
        timeout_ms = int((timeout or self.click_timeout / 1000) * 1000)
        loc = self._locate(spec)
        try:
            loc.first.click(timeout=timeout_ms)
            return True
        except Exception:
            if not force_fallback:
                return False
        try:
            loc.first.click(timeout=timeout_ms, force=True)
            return True
        except Exception as exc:
            log.debug("click %s failed: %s", describe(spec), exc)
            return False

    def click_first(self, specs: Sequence[tuple], timeout: float | None = None) -> tuple | None:
        """Click the first spec that is present. Returns the spec used."""
        for spec in specs:
            if self.count(spec) == 0:
                continue
            if self.click(spec, timeout=timeout):
                return spec
        return None

    def click_within(self, group: tuple, spec: tuple, timeout: float | None = None) -> bool:
        try:
            base = self._locate(group).first
            loc = base.locator(self._inner_selector(spec))
            loc.first.click(timeout=int((timeout or 8) * 1000))
            return True
        except Exception:
            return False

    def goto(self, url: str, timeout: float = 25.0) -> bool:
        try:
            self.page.goto(url, wait_until="domcontentloaded",
                           timeout=int(timeout * 1000))
            return True
        except Exception as exc:
            log.warning("goto %s failed: %s", url, exc)
            return False

    def sleep(self, seconds: float) -> None:
        """Only for polling loops that have no in-page predicate."""
        try:
            self.page.wait_for_timeout(int(seconds * 1000))
        except Exception:
            time.sleep(seconds)

    def clear_composer(self) -> bool:
        try:
            if not self.page.evaluate(js.CLEAR_COMPOSER):
                return False
            self.page.keyboard.press("Backspace")
            return True
        except Exception:
            return False

    def escape(self, wait_ms: int = 120) -> None:
        try:
            self.page.keyboard.press("Escape")
            if wait_ms:
                self.page.wait_for_timeout(wait_ms)
        except Exception:
            pass

    def press(self, key: str) -> None:
        try:
            self.page.keyboard.press(key)
        except Exception:
            pass

    def type_text(self, value: str) -> None:
        try:
            self.page.keyboard.type(value)
        except Exception:
            pass

    def fill(self, spec: tuple, value: str) -> bool:
        try:
            self._locate(spec).first.fill(value, timeout=8000)
            return True
        except Exception:
            return False

    def mark_card(self, src: str) -> bool:
        try:
            return bool(self.page.evaluate(js.MARK_CARD_BY_SRC, src))
        except Exception:
            return False

    def click_marked_card(self, timeout: float = 8.0) -> bool:
        return self.click(css('[data-flowbridge-target="1"]'), timeout=timeout)

    def upload_via(self, action, path: str, timeout: float = 20.0) -> bool:
        """Run `action` (which opens a file chooser) and push `path` into it."""
        try:
            with self.page.expect_file_chooser(timeout=int(timeout * 1000)) as info:
                if action() is False:
                    return False
            info.value.set_files(str(path))
            return True
        except Exception as exc:
            log.warning("upload_via failed: %s", exc)
            return False

    def download_after(self, action, timeout: float = 60.0) -> bytes | None:
        """Run `action`; if it triggers a download, return its bytes.

        Returns None instead of raising when no download fires (the caller
        then tries the next download shape in Flow's UI)."""
        try:
            with self.page.expect_download(timeout=int(timeout * 1000)) as info:
                if action is not None:
                    action()
            path = info.value.path()
            with open(path, "rb") as fh:
                return fh.read()
        except Exception as exc:
            log.debug("download_after: no download (%s)", exc)
            return None

    # --------------------------------------------------------------- debugging
    def screenshot(self, path: str) -> bool:
        try:
            self.page.screenshot(path=path)
            return True
        except Exception:
            return False

    def html(self) -> str:
        try:
            return self.page.content() or ""
        except Exception:
            return ""

    # ------------------------------------------------------------------ misc
    @staticmethod
    def _inner_selector(spec: tuple) -> str:
        kind = spec[0]
        if kind == CSS:
            return spec[1]
        if kind == TEXT:
            return f"text={spec[1]}"
        if kind == ROLE:
            return f'[role="{spec[1]}"]'
        return "*"

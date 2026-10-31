"""Browser ownership: one Playwright driver, one persistent Chromium context.

Why a dedicated owner thread
---------------------------
Playwright's sync API is bound to the thread that started it. The old
bridge started it from the job worker and then also poked it from an
asyncio request handler and from an idle-watcher thread, which is exactly
how it ended up with the `Sync API inside the asyncio loop` failures and
with a context being closed in the middle of a job. Here every call goes
through the job worker thread, and the idle release runs *inside* that
worker loop (between jobs) instead of racing it.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

from .driver import PageDriver
from .ui import FlowUI

log = logging.getLogger("flowbridge.runtime")

PROFILE_LOCK_NAMES = ("SingletonLock", "SingletonCookie", "SingletonSocket")


class BrowserRuntime:
    def __init__(self, settings, state):
        self.settings = settings
        self.state = state
        self._pw = None
        self._ctx = None
        self._page = None
        self._driver = None
        self._ui = None
        self._owner: int | None = None
        self.last_used = time.time()
        self.launches = 0

    # ------------------------------------------------------------------ owner
    def _assert_owner(self) -> None:
        me = threading.get_ident()
        if self._owner is None:
            self._owner = me
        elif self._owner != me:
            raise RuntimeError(
                "browser accessed from a non-owner thread "
                f"(owner={self._owner}, caller={me}); Playwright sync objects are thread-bound")

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._ctx is not None and self._page is not None

    def stats(self) -> dict:
        return {
            "browser": "running" if self.running else "stopped",
            "launches": self.launches,
            "idle_s": round(time.time() - self.last_used, 1),
        }

    def touch(self) -> None:
        self.last_used = time.time()

    # ----------------------------------------------------------------- launch
    def _headless(self) -> bool:
        if self.settings.headless is not None:
            return bool(self.settings.headless)
        return not os.environ.get("DISPLAY")

    def _clear_stale_profile_lock(self) -> None:
        profile = Path(self.settings.profile_dir)
        for name in PROFILE_LOCK_NAMES:
            for candidate in list(profile.glob(name)) + list(profile.glob(name + "*")):
                try:
                    if candidate.is_symlink() or candidate.is_file():
                        candidate.unlink()
                    else:
                        shutil.rmtree(candidate, ignore_errors=True)
                    log.info("removed stale Chromium lock %s", candidate.name)
                except Exception as exc:
                    log.warning("could not remove %s: %s", candidate, exc)

    def _start_driver(self):
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            log.info("playwright driver started")
        return self._pw

    def launch(self):
        """Launch (or relaunch) the persistent context and return the page."""
        self._assert_owner()
        if self.running:
            try:
                if not self._page.is_closed():
                    return self._page
            except Exception:
                pass
            self._ctx = self._page = None
        pw = self._start_driver()
        headless = self._headless()
        kwargs = dict(
            headless=headless,
            channel="chromium",
            viewport={"width": self.settings.viewport[0],
                      "height": self.settings.viewport[1]},
            args=list(self.settings.launch_args),
        )
        try:
            self._ctx = pw.chromium.launch_persistent_context(
                str(self.settings.profile_dir), **kwargs)
        except Exception as exc:
            log.warning("chromium launch failed (%s); clearing stale profile locks", exc)
            self._clear_stale_profile_lock()
            self._ctx = pw.chromium.launch_persistent_context(
                str(self.settings.profile_dir), **kwargs)
        self.launches += 1
        pages = self._ctx.pages
        self._page = pages[0] if pages else self._ctx.new_page()
        self._page.set_default_timeout(int(self.settings.click_timeout * 1000))
        self._driver = PageDriver(self._page,
                                  poll_ms=self.settings.probe_poll_ms,
                                  click_timeout=int(self.settings.click_timeout * 1000))
        self._ui = FlowUI(self._driver, self.settings, self.state)
        self.touch()
        log.info("browser launched (headless=%s, launch #%d)", headless, self.launches)
        return self._page

    def ui(self) -> FlowUI:
        """The FlowUI bound to the live page (launching if needed)."""
        self._assert_owner()
        if self._ui is None:
            self.launch()
        return self._ui

    # --------------------------------------------------------------- teardown
    def close_context(self, keep_driver: bool = True) -> None:
        """Free the heavy part (Chromium) but keep the tiny driver, matching
        the behaviour the old bridge needed to avoid restart errors."""
        self._assert_owner()
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception as exc:
            log.warning("context close failed: %s", exc)
        self._ctx = self._page = self._driver = self._ui = None
        if not keep_driver:
            try:
                if self._pw is not None:
                    self._pw.stop()
            except Exception:
                pass
            self._pw = None

    def maybe_release_idle(self, has_pending: bool) -> bool:
        """Called from the worker loop when no job is running."""
        if not self.running or has_pending:
            return False
        idle_limit = self.settings.idle_release_s
        if idle_limit <= 0:
            return False
        if time.time() - self.last_used < idle_limit:
            return False
        self.close_context(keep_driver=True)
        log.info("idle browser released after %ss without jobs (driver kept)", idle_limit)
        return True

    def shutdown(self) -> None:
        self._assert_owner()
        self.close_context(keep_driver=False)

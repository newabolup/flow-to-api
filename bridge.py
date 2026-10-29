#!/usr/bin/env python3
"""Flow bridge: turns Google Flow's web UI into a local HTTP API for your own
apps (e.g. a Telegram bot).

Single-job queue: one browser, one job at a time.
The browser uses a persistent Chromium profile (PROFILE_DIR) that the owner
logs into once via the noVNC login page; the service then reuses that session.

Endpoints (localhost only, port 8123):
  GET  /health    -> {ok, browser, logged_in, busy, queue, project_url}
  GET  /models    -> discover every image/video model Flow offers this
                     account: {image_models, video_models, cached}
                     (?refresh=1 forces a fresh scrape)
  POST /edit      -> multipart form (image file + prompt + optional model)
                     waits for completion (max ~420s), returns image/jpeg bytes
  POST /generate  -> JSON {prompt, model?} (text-to-image, no input image)
  POST /video     -> multipart form (video file? + prompt + mode +
                     resolution + optional model); returns video/mp4
  POST /warmup    -> launch browser + report login state (diagnostics)

Model customization: pass "model" (the exact label from GET /models, e.g.
"Nano Banana 2 Lite") on any generate/edit/video request, or set the
defaults with the FLOW_IMAGE_MODEL / FLOW_VIDEO_MODEL environment
variables. Without a model the defaults apply (image: Nano Banana 2 Lite,
video: Omni 1.1 Flash). The model list depends on the Google account the
browser is logged into -- Flow only offers the models that account can use.

UI recipe (verified live 2026-10-24 in the browser, Pro account):
  New project -> Agent settings (model Nano Banana 2 Lite, count x1) ->
  top "Add media" (+) -> "Upload" (NOT the ingredients-panel Upload button) ->
  "Add ingredients" (+) -> select uploaded asset -> "Add to prompt" ->
  type prompt in "What do you want to create?" -> "Start generation" ->
  wait for result -> open result -> "Download media" -> "1K Original size".

On any failure a screenshot + page HTML are saved under DEBUG_DIR for repair.
"""
import base64
import json
import logging
import os
import queue
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

BASE = Path.home() / "flowbridge"
PROFILE_DIR = BASE / "profile"
STATE_FILE = BASE / "state.json"
DEBUG_DIR = BASE / "debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("flowbridge")

app = FastAPI(title="flowbridge")

# Default models (overridable per request with "model", or here for the
# whole installation via environment variables).
DEFAULT_IMAGE_MODEL = os.environ.get("FLOW_IMAGE_MODEL", "Nano Banana 2 Lite")
DEFAULT_VIDEO_MODEL = os.environ.get("FLOW_VIDEO_MODEL", "Omni 1.1 Flash")

# ---------------- state ----------------

def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------- browser ----------------

_pw = None
_ctx = None
_page = None
_browser_lock = threading.Lock()


def _launch_browser():
    """Launch (or reuse) the persistent-context browser. Headed under Xvfb
    when DISPLAY is set, else headless.

    The Playwright DRIVER (_pw) lives for the whole process: stopping it
    and calling sync_playwright().start() again in the worker thread
    raises 'Sync API inside the asyncio loop' every time, which broke
    every job after a 900s idle release (2026-10-29: the user's photo
    edit failed exactly this way). So the idle watcher now closes only
    the context, _pw is never stopped, and a new context is launched
    from the live driver on the next job."""
    global _pw, _ctx, _page
    if _page is not None:
        try:
            if not _page.is_closed():
                return _page
        except Exception:
            pass
        _page = None
        _ctx = None  # context died with the page; the driver stays
    if _pw is None:
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
    headless = not os.environ.get("DISPLAY")
    try:
        _ctx = _pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=headless,
            channel="chromium",
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-extensions", "--mute-audio"],
        )
    except Exception:
        # Driver died (rare): drop it and retry once with a fresh start.
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None
        from playwright.sync_api import sync_playwright
        _pw = sync_playwright().start()
        _ctx = _pw.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=headless,
            channel="chromium",
            viewport={"width": 1280, "height": 900},
            args=["--no-sandbox", "--disable-dev-shm-usage",
                  "--disable-gpu", "--disable-extensions", "--mute-audio"],
        )
    pages = _ctx.pages
    _page = pages[0] if pages else _ctx.new_page()
    _page.set_default_timeout(60000)
    log.info("browser launched (headless=%s)", headless)
    return _page


def _click_if_visible(page, text, timeout=3000):
    """Click a button/dialog by visible text if present; ignore if absent."""
    try:
        loc = page.get_by_role("button", name=text)
        if loc.count() and loc.first.is_visible(timeout=timeout):
            loc.first.click(timeout=timeout)
            log.info("clicked dialog button: %s", text)
            return True
    except Exception:
        pass
    try:
        loc = page.get_by_text(text, exact=True)
        if loc.count() and loc.first.is_visible(timeout=timeout):
            loc.first.click(timeout=timeout)
            log.info("clicked text: %s", text)
            return True
    except Exception:
        pass
    return False


def _dismiss_onboarding(page):
    for text in ("Get started", "Got it"):
        _click_if_visible(page, text, timeout=2000)
        time.sleep(0.5)


def _click(loc, timeout=15000):
    """Resilient click: normal click, then force click if an overlay
    (e.g. a leftover Angular Material backdrop) intercepts pointer events."""
    try:
        loc.first.click(timeout=timeout)
    except Exception:
        loc.first.click(timeout=timeout, force=True)


def check_logged_in(page):
    """True when Flow shows the app (not a sign-in wall)."""
    page.goto("https://flow.google.com", wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    body = (page.content() or "").lower()
    if "new project" in body:
        return True
    # Sign-in wall indicators
    if "sign in" in body and "new project" not in body:
        return False
    return "flow.google.com" in page.url and "accounts.google.com" not in page.url


def ensure_project(page):
    """Reuse the stored bot project, or create one and store its URL."""
    state = load_state()
    url = state.get("project_url")
    if url:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if "What do you want to create?" in (page.content() or ""):
            log.info("reusing project %s", url)
            return
        log.warning("stored project did not show prompt box; creating a new one")
    page.goto("https://flow.google.com", wait_until="domcontentloaded")
    page.wait_for_timeout(2000)
    _dismiss_onboarding(page)
    btn = page.get_by_role("button", name="New project")
    if not btn.count():
        btn = page.get_by_text("New project")
    btn.first.click()
    page.wait_for_timeout(4000)
    _dismiss_onboarding(page)
    state["project_url"] = page.url
    save_state(state)
    log.info("created project %s", page.url)
    ensure_settings(page)


def ensure_settings(page):
    """Best-effort: model Nano Banana 2 Lite, output count x1. Never fatal."""
    try:
        btn = page.get_by_role("button", name="Settings")
        if btn.count() and btn.first.is_visible(timeout=3000):
            btn.first.click()
            page.wait_for_timeout(1500)
            body = page.content() or ""
            if "Nano Banana 2 Lite" not in body:
                # Try to open the model picker and pick Nano Banana 2 Lite
                for name in ("Nano Banana 2 Lite", "🍌 Nano Banana 2 Lite"):
                    try:
                        m = page.get_by_text(name, exact=False)
                        if m.count():
                            m.first.click(timeout=3000)
                            break
                    except Exception:
                        continue
            # Output count: look for a x1 option
            try:
                x1 = page.get_by_text("x1", exact=True)
                if x1.count():
                    x1.first.click(timeout=3000)
            except Exception:
                pass
            _click_if_visible(page, "Save", timeout=3000)
            page.wait_for_timeout(1000)
            log.info("settings ensured (best effort)")
    except Exception as exc:
        log.warning("ensure_settings skipped: %s", exc)


def _save_debug(page, tag):
    try:
        ts = int(time.time())
        page.screenshot(path=str(DEBUG_DIR / f"{tag}-{ts}.png"))
        (DEBUG_DIR / f"{tag}-{ts}.html").write_text(page.content() or "")
        log.info("debug saved: %s-%s", tag, ts)
    except Exception as exc:
        log.warning("debug save failed: %s", exc)


def _upload_image(page, image_path):
    """Upload via the TOP Add media menu (the ingredients-panel Upload
    button failed in the live test)."""
    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)
    add = page.get_by_role("button", name="Add media")
    if not add.count():
        add = page.get_by_text("Add media")
    _click(add)
    page.wait_for_timeout(1500)
    with page.expect_file_chooser(timeout=15000) as fc:
        up = page.get_by_role("menuitem", name="Upload")
        if not up.count():
            up = page.get_by_text("Upload", exact=True)
        _click(up)
    fc.value.set_files(str(image_path))
    page.wait_for_timeout(4000)
    log.info("image uploaded: %s", image_path)


def _add_ingredient(page):
    # The composer's "+" button is named "Add ingredients to the prompt
    # box" (aria-label; its visible text is just "+"). The old exact name
    # "Add ingredients" matches NOTHING, and button:has-text("Add
    # ingredients") also misses because has-text reads innerText ("+"),
    # not aria-label — that is why EDIT jobs timed out here from 18:20 on
    # (the video ingredient flow already used the full name correctly).
    btn = page.get_by_role("button", name="Add ingredients to the prompt box", exact=True)
    if not btn.count():
        btn = page.get_by_role("button", name="Add ingredients", exact=True)
    btn.first.click()
    page.wait_for_timeout(2000)
    # The asset list shows uploaded images; pick the first/newest one.
    # Prefer an image element inside the asset picker, else first list item.
    try:
        imgs = page.locator("img").all()
        # Click the most recently added asset: the picker usually lists it first.
        # Fall back to "Add to prompt" flow below if this fails.
        if imgs:
            imgs[0].click(timeout=5000)
            page.wait_for_timeout(1000)
    except Exception as exc:
        log.warning("asset click fallback: %s", exc)
    add = page.get_by_role("button", name="Add to prompt")
    if not add.count():
        add = page.get_by_text("Add to prompt")
    _click(add)
    page.wait_for_timeout(1500)
    log.info("ingredient added to prompt")


def _close_agent_panel(page):
    """Close any open side panel (Agent settings, Agent instructions, or
    the agent THREAD panel) that hides the composer. When a panel is open
    it REPLACES the composer bar, so every composer click times out as
    "not visible" (2026-10-27 18:17 jobfail: settings panel open, no
    composer; 18:21 EDIT jobfail: thread panel open, no Add-ingredients
    button). Escape alone does NOT close these panels — their back arrow
    / X does, and the thread panel's X (aria-label Close) closes it
    (proven by diagE1 2026-10-27). Best-effort; returns True when the
    composer is usable afterwards."""
    # Escape first (twice): an open dropdown menu eats the first close
    # click (it just closes the menu), so the panel itself never gets
    # the click. A second Escape covers menus that need it.
    for _esc in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
        except Exception:
            pass
    def _panel_text_present():
        try:
            _b = _visible_text(page)
        except Exception:
            _b = ""
        return ("Agent settings" in _b or "Agent instructions" in _b)

    def _composer_visible():
        try:
            _box = page.get_by_label("What do you want to create?")
            return bool(_box.count()) and _box.first.is_visible(timeout=2000)
        except Exception:
            return False

    if not _panel_text_present() and _composer_visible():
        return True
    # Up to 5 rounds: each clicks the first visible Back/X, then re-checks.
    # (The first Close match can be the blue banner's X — dismissing it is
    # harmless — so rounds matter: keep going until the composer is back.)
    for _round in range(5):
        if not _panel_text_present() and _composer_visible():
            return True
        clicked = False
        for loc in (page.get_by_role("button", name="Back", exact=True),
                    page.locator('button[aria-label="Back"]'),
                    page.locator('button[aria-label="Close"]'),
                    page.get_by_role("button", name="Close", exact=True),
                    # Material icon buttons: accessible names are NOT
                    # "Back"/"Close" — their inner text is the
                    # "arrow_back"/"close" ligature (proven 2026-10-29).
                    page.get_by_text("arrow_back", exact=True),
                    page.get_by_text("close", exact=True)):
            try:
                if loc.count() and loc.first.is_visible(timeout=2000):
                    _click(loc, timeout=5000)
                    page.wait_for_timeout(1000)
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            break
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
        except Exception:
            pass
    ok = _composer_visible() or not _panel_text_present()
    if not ok:
        log.warning("agent panel still open after close attempt")
        try:
            probe = []
            for name, loc in (
                    ("role Back", page.get_by_role("button", name="Back", exact=True)),
                    ("aria Back", page.locator('button[aria-label="Back"]')),
                    ("aria Close", page.locator('button[aria-label="Close"]')),
                    ("role Close", page.get_by_role("button", name="Close", exact=True)),
                    ("text arrow_back", page.get_by_text("arrow_back", exact=True)),
                    ("text close", page.get_by_text("close", exact=True)),
                    ("has-text arrow_back", page.locator('button:has-text("arrow_back")')),
                    ("has-text close", page.locator('button:has-text("close")'))):
                try:
                    cnt = loc.count()
                    vis = False
                    if cnt:
                        try:
                            vis = loc.first.is_visible(timeout=1000)
                        except Exception:
                            vis = False
                    probe.append(f"{name}: count={cnt} visible={vis}")
                except Exception as exc:
                    probe.append(f"{name}: error {exc}")
            btns = []
            for b in page.get_by_role("button").all()[:30]:
                try:
                    tag = b.evaluate("e => e.tagName") or ""
                    al = b.get_attribute("aria-label") or ""
                    t = (b.inner_text(timeout=500) or "").strip().replace("\n", " ")[:40]
                    if t:
                        btns.append(f"{tag}/{al}/{t}")
                except Exception:
                    continue
            log.warning("panel-close probe: %s | buttons: %s", probe, btns)
        except Exception:
            pass
    return ok


def _start_generation(page, prompt):
    # The prompt box is a contenteditable element labelled
    # "What do you want to create?" (aria-label), not a placeholder input.
    # Safety net: a leftover side panel hides the composer entirely.
    try:
        _close_agent_panel(page)
    except Exception:
        pass
    box = page.get_by_label("What do you want to create?")
    if not box.count():
        box = page.locator('[aria-label="What do you want to create?"]')
    if not box.count():
        box = page.get_by_placeholder("What do you want to create?")
    if not box.count():
        box = page.locator("textarea").first
    box.first.click()
    page.wait_for_timeout(500)
    try:
        box.first.fill(prompt)
    except Exception:
        page.keyboard.type(prompt)
    page.wait_for_timeout(1000)
    # Verify the prompt actually landed; retype via keyboard if not.
    try:
        text = box.first.inner_text(timeout=3000) or ""
    except Exception:
        text = ""
    if prompt[:30] not in text:
        box.first.click()
        page.keyboard.type(prompt)
        page.wait_for_timeout(1000)
    go = page.get_by_role("button", name="Start generation")
    if not go.count():
        go = page.locator('button[aria-label="Start generation"]')
    if not go.count():
        # Arrow/submit button near the prompt box
        go = page.locator("button[type=submit]").last
    _click(go)
    log.info("generation started: %r", prompt[:120])


def _visible_text(page):
    try:
        return page.locator("body").inner_text(timeout=5000) or ""
    except Exception:
        return ""


def _wait_for_result(page, timeout_s=300):
    """Wait until generation finishes. Completion signal (proven by
    screenshot): the prompt box goes idle again -- placeholder
    'What do you want to create?' back, no 'Thinking' indicator. The
    project page has NO Download button until a result card is opened,
    so waiting for it here always times out. Error check uses VISIBLE
    text only (page HTML includes JS error strings that false-positive)."""
    # Phase 1: generation must start (Thinking appears) within 60s.
    start_deadline = time.time() + 60
    while time.time() < start_deadline:
        if "thinking" in _visible_text(page).lower():
            break
        time.sleep(3)
    # Phase 2: wait until Thinking disappears and the placeholder returns.
    # Agent threads also count as done when the agent itself reports
    # completion ("I've generated..." / "I've updated..."): its Thinking
    # step header can stay rendered after completion, which otherwise
    # blocks this check forever (2026-10-27 18:13 timeout despite two
    # finished images in the thread). 2026-10-29 variant: the agent
    # reports completion as "Here are the two images..." (with stale
    # Thinking headers from earlier turns still rendered), which also
    # blocked detection despite finished images — so those phrasings
    # count as completion too.
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        low = _visible_text(page).lower()
        if "something went wrong" in low or "failed to generate" in low:
            raise RuntimeError("Flow reported a generation failure")
        if "what do you want to create?" in low and (
                "thinking" not in low
                or "i've generated" in low
                or "i've updated" in low
                or "here are the two images" in low
                or "here are the images" in low
                or "here is the image" in low):
            return True
        time.sleep(5)
    _save_debug(page, "timeout")
    raise RuntimeError("Timed out waiting for Flow result")


def _download_result(page, is_edit=False):
    """Open the result and download the 1K original-size image.

    Three download shapes exist in Flow and all three must work, or a
    finished generation is wasted: (1) the timeline 'Download media'
    button opens a size menu ('1K Original size'); (2) the fullscreen
    viewer button sometimes downloads directly; (3) the viewer menu may
    open with different option text. (2026-10-29: an edit result landed
    in the viewer, the first trigger click left the menu closed, and
    'Original size' never appeared — the finished beach edit was lost.)
    Strategy: capture a direct download from the first click if one
    fires; else poll for the size option, then for ANY menu item
    (preferring a size/download label), retrying the trigger once if no
    menu is open; last resort is another direct-download click.
    is_edit: after an edit job the visible 'Download media' button
    belongs to the SELECTED card, which is the uploaded original, not
    the new output (2026-10-29: two edit downloads returned the
    pixel-identical original). Edits therefore always go through the
    card-click path, which opens the viewer on the newest result."""
    import re as _re
    # Click the newest result card if a Download button is not already visible.
    dl = page.get_by_role("button", name="Download media")
    use_visible = (not is_edit) and dl.count() and dl.first.is_visible(timeout=5000)
    if not use_visible:
        # Click the newest timeline card: the first WIDE image in main
        # content (avatars/icons are small; timeline cards are wide).
        clicked = False
        try:
            for img in page.locator("main img").all():
                try:
                    bb = img.bounding_box()
                except Exception:
                    continue
                if bb and bb["width"] > 150:
                    img.click(timeout=5000)
                    clicked = True
                    break
        except Exception:
            pass
        if not clicked:
            try:
                page.locator("img").first.click(timeout=5000)
            except Exception:
                pass
        page.wait_for_timeout(3000)
        dl = page.get_by_role("button", name="Download media")
    # Shape (2): the trigger itself downloads directly (viewer button).
    try:
        with page.expect_download(timeout=8000) as dlinfo:
            _click(dl)
        dlfile = dlinfo.value
        data = Path(dlfile.path()).read_bytes()
        log.info("downloaded result directly: %d bytes", len(data))
        return data
    except Exception:
        pass  # no direct download: a size menu should be open now
    # Shapes (1) and (3): poll for the size option, then any menu item.
    target = None
    for _attempt in range(2):
        for _ in range(6):
            page.wait_for_timeout(1000)
            one = page.get_by_text("1K Original size")
            if not one.count():
                one = page.get_by_text("Original size")
            if one.count():
                target = one
                break
            items = page.locator('[role="menuitem"], .mat-mdc-menu-item')
            try:
                n = items.count()
            except Exception:
                n = 0
            if n:
                picked = None
                for it in items.all():
                    try:
                        txt = (it.inner_text() or "").strip()
                    except Exception:
                        continue
                    if _re.search(r"Original|1K|Download|Full|Large", txt, _re.I):
                        picked = it
                        break
                target = picked if picked is not None else items.first
                break
        if target is not None:
            break
        # No menu open at all: retry the trigger once.
        try:
            _click(dl)
        except Exception:
            pass
    if target is not None:
        try:
            with page.expect_download(timeout=60000) as dlinfo:
                _click(target)
            dlfile = dlinfo.value
            data = Path(dlfile.path()).read_bytes()
            log.info("downloaded result: %d bytes", len(data))
            return data
        except Exception as exc:
            log.warning("menu-item download failed, trying direct: %s", exc)
    with page.expect_download(timeout=60000) as dlinfo:
        _click(dl)
    dlfile = dlinfo.value
    data = Path(dlfile.path()).read_bytes()
    log.info("downloaded result directly (fallback): %d bytes", len(data))
    return data


# ---------------- video (standard box, Agent OFF) ----------------

def _agent_toggle_state(page):
    """True/False if the Agent toggle state can be read, else None."""
    # exact=True is REQUIRED: without it, name="Agent" substring-matches the
    # "Agent instructions" button too, and .first can click THAT instead of
    # the toggle — opening the instructions panel, hiding the composer, and
    # breaking the whole job (proven by the 2026-10-27 18:05 jobfail shot).
    for loc in (page.get_by_role("switch", name="Agent", exact=True),
                page.get_by_role("button", name="Agent", exact=True),
                page.locator('[aria-label="Agent"]')):
        try:
            if not loc.count():
                continue
            el = loc.first
            for attr in ("aria-checked", "aria-pressed", "aria-selected"):
                try:
                    v = el.get_attribute(attr)
                except Exception:
                    v = None
                if v in ("true", "false"):
                    return v == "true"
            try:
                cls = el.get_attribute("class") or ""
                if "unchecked" in cls:
                    return False
                if "checked" in cls:
                    return True
            except Exception:
                pass
        except Exception:
            continue
    return None


def _agent_is_on(page):
    """Best-effort Agent state: toggle element first, visible text second."""
    state = _agent_toggle_state(page)
    if state is not None:
        return state
    try:
        return "Agent instructions" in _visible_text(page)
    except Exception:
        return False


def _set_agent_toggle(page, on):
    """Set the Agent toggle. Unlike the first version, an unreadable state
    still clicks the toggle instead of silently no-opping. Waits for the
    composer to render first: on a fresh browser the toggle button is not
    visible at first (page still hydrating), and a single immediate attempt
    silently no-ops, leaving Agent OFF — which changed the whole job flow
    (2026-10-27 18:08 job ran Agent-OFF and its result was never detected)."""
    try:
        # Wait for the composer to render (up to ~15s) before touching the
        # toggle; an unrendered composer means every locator below misses.
        for _ in range(15):
            try:
                _box = page.get_by_label("What do you want to create?")
                if _box.count() and _box.first.is_visible(timeout=1000):
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)
        state = _agent_toggle_state(page)
        if state is None:
            try:
                state = "Agent instructions" in _visible_text(page)
            except Exception:
                state = None
        if state == on:
            return True
        clicked = False
        for _round in range(3):
            for loc in (page.get_by_role("button", name="Agent", exact=True),
                        page.get_by_text("Agent", exact=True)):
                try:
                    if loc.count() and loc.first.is_visible(timeout=3000):
                        loc.first.click(timeout=5000)
                        clicked = True
                        break
                except Exception:
                    continue
            if clicked:
                break
            page.wait_for_timeout(2000)
        page.wait_for_timeout(1500)
        state2 = _agent_toggle_state(page)
        if state2 is None:
            try:
                state2 = "Agent instructions" in _visible_text(page)
            except Exception:
                state2 = None
        if state2 == on:
            return True
        log.warning("agent toggle state unconfirmed (wanted on=%s, got %s, clicked=%s)",
                    on, state2, clicked)
        return False
    except Exception as exc:
        log.warning("agent toggle failed: %s", exc)
        return False


def _click_first_visible(page, locators, timeout=5000):
    for loc in locators:
        try:
            if loc.count() and loc.first.is_visible(timeout=timeout):
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def _open_standard_settings(page):
    """The settings trigger in the bottom prompt bar (mode summary text).
    Standard trigger first: clicking a "Settings" button while Agent is
    ON opens the agent panel instead, which silently mis-set probe #1.
    The summary button (the ONLY button containing "·", e.g. "Video ·
    360p · 8s ...") goes first: button:has-text("Video") hits the
    SIDEBAR "Videos" nav instead (proven by probe v4) — and MUST NEVER
    be a candidate: on 2026-10-29 it navigated to the Videos library
    instead of opening settings, so every video job failed with
    'not available in Flow menu; available: []'. If the popup is
    already open, do not click again (that would toggle it closed)."""
    if _agent_toggle_state(page) is True:
        raise RuntimeError("Agent still ON; refusing standard settings")
    # Generation popup already open? Don't toggle it closed by clicking.
    # NOTE: verify with the Mode group specifically: the View-mode popup
    # (top toolbar) also uses flow-toggles, so a bare flow-toggles check
    # passes on the WRONG popup (2026-10-29 vidfail shot).
    def _gen_popup_open():
        try:
            return page.locator('flow-toggles[aria-label="Mode"]').first.is_visible(timeout=1000)
        except Exception:
            return False
    if _gen_popup_open():
        return True
    # Focus the composer first: its submit-controls (including the
    # Settings trigger) only render once the prompt box is focused —
    # with an empty, unfocused box the trigger reads visible=False.
    try:
        box = page.get_by_label("What do you want to create?")
        if box.count():
            box.first.click(timeout=3000)
            page.wait_for_timeout(1000)
    except Exception:
        pass
    # The composer re-renders after the Agent toggle: the prompt box
    # appears before its submit-controls. Poll for the Settings
    # trigger to become visible (2026-10-29: it read visible=False
    # right after the toggle, but rendered ~10s later).
    for _ in range(30):
        try:
            st = page.locator('[aria-label="Settings trigger"]')
            if st.count() and st.first.is_visible(timeout=1000):
                break
        except Exception:
            pass
        page.wait_for_timeout(1000)
    # Primary: the composer's tune button ("Settings trigger", in
    # .submit-controls). Force fallback covers a composer still settling
    # after the Agent toggle (plain clicks get intercepted then). The
    # old "·" summary button no longer exists in the current UI
    # (2026-10-29: zero buttons contain "·").
    try:
        st = page.locator('[aria-label="Settings trigger"]')
        if st.count() and st.first.is_visible(timeout=5000):
            _click(st, timeout=5000)
            page.wait_for_timeout(1500)
            if _gen_popup_open():
                return True
    except Exception as exc:
        log.warning("settings trigger attempt failed: %s", exc)
    ok = _click_first_visible(page, [
        page.locator('button:has-text("·")').first,
        page.locator('button:has-text("Nano Banana")').first,
    ])
    page.wait_for_timeout(1500)
    if not ok:
        try:
            st = page.locator('[aria-label="Settings trigger"]')
            info = "absent"
            if st.count():
                try:
                    bb = st.first.bounding_box()
                    info = f"visible={st.first.is_visible(timeout=1000)} box={bb}"
                except Exception as exc:
                    info = f"read failed: {exc}"
            log.warning("standard settings trigger not found; Settings trigger: %s", info)
        except Exception:
            pass
        return False
    return _gen_popup_open()


def _select_toggle(page, group_label, option_text):
    """Select an option in a <flow-toggles aria-label="..."> group.
    Reads aria-checked first (no-op if already selected); toggle-text
    may carry an info-icon ligature, so matching is startswith."""
    groups = page.locator(f'flow-toggles[aria-label="{group_label}"]')
    if groups.count():
        for t in groups.first.locator("mat-button-toggle").all():
            try:
                txt = (t.inner_text(timeout=2000) or "").strip()
            except Exception:
                continue
            if option_text in txt:
                try:
                    checked = t.get_attribute("aria-checked")
                except Exception:
                    checked = None
                if checked == "true":
                    return True
                try:
                    t.locator("button").first.click(timeout=5000)
                    page.wait_for_timeout(800)
                    return True
                except Exception as exc:
                    log.warning("toggle click %s/%s failed: %s", group_label, option_text, exc)
                    return False
        return False
    return _click_first_visible(page, [page.get_by_text(option_text, exact=True)], timeout=2000)


def _select_model(page, model_name):
    """Pick a model from the model-select menu. Skips the trigger click
    when the trigger already shows the wanted model (clicking it would
    just open the menu and intercept later clicks)."""
    try:
        trig = page.locator(".model-select-trigger-content").first
        if trig.count() and model_name in (trig.inner_text(timeout=2000) or ""):
            return True
    except Exception:
        pass
    if _click_first_visible(page, [page.locator(".model-select-trigger-content").first], timeout=3000):
        page.wait_for_timeout(1000)
        for item in page.locator(".item-text .label").all():
            try:
                if (item.inner_text(timeout=1000) or "").strip() == model_name:
                    item.click(timeout=5000)
                    page.wait_for_timeout(800)
                    return True
            except Exception:
                continue
        return _click_first_visible(page, [page.get_by_text(model_name, exact=True)], timeout=3000)
    return False


def _set_standard_video_settings(page, expect_credits=None, resolution="360p",
                                 check_summary=False, model="Omni 1.1 Flash",
                                 strict_model=False):
    """Standard box -> Mode Video, model (default Omni 1.1 Flash), resolution, 8s, 16:9, x1.
    While the pop-up is open, the live total 'Generating will use N
    credits' is checked when expect_credits is given (generate mode:
    360p -> 6; 720p -> 12 (measured 2026-10-25).
    Edit mode skips the credit check (video-ingredient edits cost ~40).
    check_summary (generate jobs) requires the summary button to show
    the requested resolution after settings are applied."""
    if not _open_standard_settings(page):
        raise RuntimeError("standard settings popup did not open (no summary/Settings trigger found)")
    page.wait_for_timeout(1000)
    _select_toggle(page, "Mode", "Video")
    page.wait_for_timeout(800)
    if not _select_model(page, model):
        labels = []
        try:
            for el in page.locator(".item-text .label").all():
                try:
                    if el.is_visible(timeout=500):
                        t = (el.inner_text(timeout=1000) or "").strip()
                        if t and t not in labels:
                            labels.append(t)
                except Exception:
                    continue
        except Exception:
            pass
        if strict_model:
            raise RuntimeError(f"video model {model!r} not available in Flow menu; available: {labels}")
        log.warning("video model %r not selected (menu labels: %s); continuing", model, labels)
    page.wait_for_timeout(800)
    _select_toggle(page, "Video resolution", resolution)
    _select_toggle(page, "Video duration", "8s")
    _select_toggle(page, "Aspect ratio", "16:9")
    _select_toggle(page, "Output count", "x1")
    page.wait_for_timeout(800)
    if expect_credits is not None:
        import re as _re
        m = _re.search(r"Generating will use (\d+) credits", _visible_text(page))
        if m:
            total = int(m.group(1))
            log.info("video credit total shown: %s (expected %s)", total, expect_credits)
            if total != expect_credits:
                raise RuntimeError(
                    f"video settings did not stick to Omni {resolution} 8s x1 (shows {total} credits)")
        else:
            if expect_credits is not None:
                raise RuntimeError(
                    "video credit total not found in settings pop-up; "
                    "refusing to generate with unverified settings")
            log.warning("credit total not found in settings pop-up; continuing")
    else:
        import re as _re
        m = _re.search(r"Generating will use (\d+) credits", _visible_text(page))
        if m:
            log.info("video credit total shown: %s (no expected value set)", m.group(1))
    page.keyboard.press("Escape")
    page.wait_for_timeout(1000)
    if check_summary:
        try:
            summ = page.locator('button:has-text("·")').first.inner_text(timeout=5000) or ""
        except Exception:
            summ = ""
        if resolution not in summ:
            raise RuntimeError(
                f"video summary does not show {resolution} after settings: {summ[:80]!r}")
    log.info("standard video settings set (%s %s 8s x1)", model, resolution)


def _set_video_type(page, kind):
    """Video type control (Frames / Ingredients). Best-effort."""
    ok = _click_first_visible(page, [page.get_by_text(kind, exact=True)], timeout=3000)
    page.wait_for_timeout(1000)
    if not ok:
        log.warning("video type %r not clickable; continuing", kind)


def _agree_video_rights(page):
    """Dismiss Flow's 'Rights to use this video' modal if present.
    Prefers 'I agree — do not show again' so later edits skip it."""
    for label in ("I agree — do not show again", "do not show again", "I agree"):
        try:
            if _click_first_visible(page, [page.get_by_role("button", name=label)],
                                    timeout=3000):
                page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


def _add_video_ingredient(page, video_path):
    """Upload a video into the Ingredients picker and add it to the prompt."""
    plus = page.get_by_role("button", name="Add ingredients to the prompt box", exact=True)
    if not plus.count():
        plus = page.locator('button:has-text("Add ingredients")')
    _click(plus)
    page.wait_for_timeout(2000)
    with page.expect_file_chooser(timeout=15000) as fc:
        up = page.get_by_role("button", name="Upload media")
        if not up.count():
            up = page.get_by_text("Upload media", exact=True)
        _click(up)
    fc.value.set_files(str(video_path))
    page.wait_for_timeout(5000)
    _agree_video_rights(page)
    # Select the uploaded asset if the picker shows one, then confirm.
    try:
        vids = page.locator("video").all()
        if vids:
            vids[0].click(timeout=5000)
            page.wait_for_timeout(1000)
    except Exception as exc:
        log.warning("video asset click fallback: %s", exc)
    add = page.get_by_role("button", name="Add to prompt")
    if not add.count():
        add = page.get_by_text("Add to prompt")
    _click(add)
    page.wait_for_timeout(1500)
    _agree_video_rights(page)
    page.wait_for_timeout(1500)
    log.info("video ingredient added to prompt")


def _download_video_result(page):
    """Open the newest timeline card and download the video. The exact
    download control for video was NOT observable in recon (no finished
    video in the project), so this tries Download media, then the tile
    More-options menu -> Download, then an Original-size option."""
    clicked = False
    try:
        for vid in page.locator("main video").all():
            try:
                vid.click(timeout=5000)
                clicked = True
                break
            except Exception:
                continue
    except Exception:
        pass
    if not clicked:
        try:
            for img in page.locator("main img").all():
                try:
                    bb = img.bounding_box()
                except Exception:
                    continue
                if bb and bb["width"] > 150:
                    img.click(timeout=5000)
                    clicked = True
                    break
        except Exception:
            pass
    page.wait_for_timeout(2000)
    with page.expect_download(timeout=120000) as dlinfo:
        done = _click_first_visible(page, [
            page.get_by_role("button", name="Download media"),
        ], timeout=5000)
        if not done:
            if _click_first_visible(page, [
                    page.get_by_role("button", name="More options")], timeout=5000):
                page.wait_for_timeout(1000)
                _click_first_visible(page, [
                    page.get_by_text("Download", exact=True)], timeout=5000)
        page.wait_for_timeout(1500)
        # A quality submenu may appear after the download click.
        _click_first_visible(page, [
            page.get_by_text("Original size")], timeout=3000)
    data = Path(dlinfo.value.path()).read_bytes()
    log.info("downloaded video: %d bytes", len(data))
    return data


def _settings_trigger_mode(page):
    """Read the standard settings trigger summary: 'video' if it says
    'Video · ...', 'image' if it says 'Nano Banana ... xN', else
    'unknown'. Only trigger summaries count, so stray menu buttons
    mentioning a model cannot false-positive."""
    import re as _re
    try:
        for btn in page.get_by_role("button").all():
            try:
                t = btn.inner_text(timeout=1000) or ""
            except Exception:
                continue
            if "Video" in t and "·" in t:
                return "video"
            if "Nano Banana" in t and _re.search(r"x[1-4]\b", t):
                return "image"
    except Exception:
        pass
    return "unknown"


def run_flow_video_job(video_path, prompt, mode, resolution="360p", video_model=None):
    """Full video job: standard box, video model resolution 8s x1, wait, download.
    video_model: explicit model label (strict) or None for the default
    (Omni 1.1 Flash). Non-Omni models skip the credit-total check (costs
    differ per model); the shown total is still logged.
    resolution is 360p (default, 6 credits) or 720p (12 credits, owner
    only via the bot's /videoquality setting).
    Restores the Agent toggle to ON afterwards so image jobs keep working."""
    with _browser_lock:
        page = _launch_browser()
        try:
            if not check_logged_in(page):
                raise RuntimeError("not_logged_in: Flow session missing, owner must log in")
            ensure_project(page)
            _set_agent_toggle(page, False)
            page.wait_for_timeout(1000)
            model = video_model or DEFAULT_VIDEO_MODEL
            if mode == "edit" and video_path:
                _set_standard_video_settings(page, expect_credits=None,
                                             resolution=resolution, model=model,
                                             strict_model=bool(video_model))
                _set_video_type(page, "Ingredients")
                _add_video_ingredient(page, video_path)
            else:
                credits = None if model != "Omni 1.1 Flash" else (6 if resolution == "360p" else 12)
                _set_standard_video_settings(
                    page,
                    expect_credits=credits,
                    resolution=resolution, check_summary=True, model=model,
                    strict_model=bool(video_model))
            _mode = _settings_trigger_mode(page)
            if _mode == "image":
                raise RuntimeError("standard box still in Image mode (video settings did not stick)")
            if _mode == "unknown":
                log.warning("settings trigger mode unknown; proceeding (magic check backstops)")
            _start_generation(page, prompt)
            _wait_for_result(page, timeout_s=600)
            data = _download_video_result(page)
            # Backstop for probe #1: never return an image as a "video".
            if data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n":
                raise RuntimeError("Flow returned an image instead of a video (mode switch failed)")
            if not (data[4:8] == b"ftyp" or data[:4] == b"\x1a\x45\xdf\xa3"):
                raise RuntimeError("downloaded file is not a video (unexpected format)")
            return data
        except Exception:
            _save_debug(page, "vidfail")
            raise
        finally:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
            except Exception:
                pass
            try:
                _set_agent_toggle(page, True)
            except Exception:
                pass


# ---------------- model discovery & switching ----------------

_CHROME_LABELS = {
    "Image generation default", "Save", "Cancel", "Settings",
    "Agent settings", "Done", "Back", "Agent instructions",
    "What do you want to create?",
}


def _model_key(label):
    """Model label without a leading emoji/prefix: '🍌 Nano Banana 2 Lite'
    -> 'Nano Banana 2 Lite'. Used for matching user-supplied model names."""
    import re as _re
    t = (label or "").strip()
    t = _re.sub(r"^[^\w]+", "", t).strip()
    return t


def _body_has_model(body, wkey):
    """True when one visible-text line is exactly the wanted model (line
    comparison, not substring: 'Nano Banana 2' must not match the line
    'Nano Banana 2 Lite')."""
    return any(_model_key(ln) == wkey for ln in (body or "").splitlines())


def _visible_lines(page):
    """Short single-line visible texts (for menu diffing): visible body
    text split into lines, stripped, capped at 80 chars."""
    try:
        body = _visible_text(page)
    except Exception:
        return []
    lines = []
    for ln in (body or "").splitlines():
        t = ln.strip()
        if t and len(t) <= 80 and t not in lines:
            lines.append(t)
    return lines


def _menu_option_labels(page):
    """Labels from role=option/menuitem, .item-text .label and mat-option
    rows of an open picker menu."""
    labels = []
    for sel in ('[role="option"]', '[role="menuitem"]', ".item-text .label", "mat-option"):
        try:
            for el in page.locator(sel).all():
                try:
                    if el.is_visible(timeout=500):
                        t = (el.inner_text(timeout=1000) or "").strip()
                        if t and "\n" not in t and len(t) <= 80 and t not in labels and t not in _CHROME_LABELS:
                            labels.append(t)
                except Exception:
                    continue
        except Exception:
            continue
    return labels


def _text_model_labels(page):
    """Short single-line texts naming the Nano Banana family (the image
    dropdown rows are plain text, not menu items)."""
    labels = []
    try:
        for el in page.get_by_text("Nano Banana", exact=False).all():
            try:
                if el.is_visible(timeout=500):
                    t = (el.inner_text(timeout=1000) or "").strip()
                    if t and "\n" not in t and len(t) <= 80 and t not in labels and t not in _CHROME_LABELS:
                        labels.append(t)
            except Exception:
                continue
    except Exception:
        pass
    return labels


def _open_agent_panel(page):
    """Open the Agent settings panel via the composer tune button.
    Returns True when the panel is open afterwards."""
    try:
        _body0 = _visible_text(page)
    except Exception:
        _body0 = ""
    if "Agent settings" in _body0 and "Image generation default" in _body0:
        return True
    tune = page.locator('button:has-text("tune")').first
    for _attempt in range(3):
        try:
            if tune.count() and tune.is_visible(timeout=5000):
                tune.click(timeout=5000)
                page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
        page.wait_for_timeout(2000)
    return False


def _open_image_model_options(page):
    """Open the Agent panel + image-model dropdown; return option labels
    (possibly []). The dropdown is left OPEN for the caller to click a row;
    Escape closes it without changing the selection. Trigger candidates
    are tried in order: the proven 'Nano Banana 2.1' trigger first, then
    any visible 'Nano Banana' text (button-like elements first)."""
    if not _open_agent_panel(page):
        log.warning("model options: no tune Settings button")
        return []
    candidates = []
    for loc in (page.get_by_text("🍌 Nano Banana 2.1", exact=True),
                page.get_by_text("Nano Banana 2.1", exact=False)):
        try:
            candidates.extend(loc.all())
        except Exception:
            continue
    try:
        candidates.extend(page.get_by_text("Nano Banana", exact=False).all())
    except Exception:
        pass
    ordered = []
    for el in candidates:
        try:
            if el.is_visible(timeout=1000):
                ordered.append(el)
        except Exception:
            continue

    def _rank(el):
        try:
            tag = (el.evaluate("e => e.tagName") or "").upper()
            role = (el.get_attribute("role") or "").lower()
        except Exception:
            return 2
        if tag in ("BUTTON", "MAT-SELECT", "SELECT") or role in ("button", "combobox", "listbox"):
            return 0
        return 1

    ordered.sort(key=_rank)
    for el in ordered[:6]:
        try:
            el.click(timeout=5000)
            page.wait_for_timeout(1000)
            opts = _menu_option_labels(page)
            if not opts:
                texts = _text_model_labels(page)
                if len(texts) >= 2:
                    opts = texts
            if opts:
                return opts
        except Exception:
            continue
    return []


def _discover_video_models(page):
    """Open the standard settings model menu and read its labels.
    Agent must be OFF (the caller toggles). Returns labels (possibly [])."""
    if not _open_standard_settings(page):
        log.warning("video-model discovery: settings trigger not found")
        try:
            page.screenshot(path=str(DEBUG_DIR / "video-models-trigger.png"))
        except Exception:
            pass
        try:
            btns = []
            for b in page.get_by_role("button").all()[:40]:
                try:
                    t = (b.inner_text(timeout=1000) or "").strip().replace("\n", " ")
                    if t:
                        btns.append(t[:60])
                except Exception:
                    continue
            log.warning("video-model discovery: visible buttons: %s", btns)
        except Exception:
            pass
        return []
    page.wait_for_timeout(1000)
    # The popup opens in the project's current mode (Image after image
    # jobs): the video model trigger only exists in Video mode, so switch
    # first — exactly like a video job does.
    try:
        _select_toggle(page, "Mode", "Video")
    except Exception as exc:
        log.warning("video-model discovery: Mode=Video select failed: %s", exc)
    page.wait_for_timeout(1000)
    before = _visible_lines(page)
    try:
        trig = page.locator(".model-select-trigger-content").first
        if trig.count() and trig.is_visible(timeout=3000):
            trig.click(timeout=5000)
            page.wait_for_timeout(2000)
    except Exception:
        pass
    labels = _menu_option_labels(page)
    if not labels:
        try:
            after = _visible_lines(page)
            labels = [l for l in after if l not in before
                      and l not in _CHROME_LABELS and "credit" not in l.lower()]
            log.info("standard video menu diff: %s", labels)
        except Exception:
            pass
    log.info("standard video menu labels: %s", labels)
    if not labels:
        try:
            trig = page.locator(".model-select-trigger-content").first
            if trig.count():
                t = (trig.inner_text(timeout=2000) or "").strip()
                if t and "\n" not in t and len(t) <= 80:
                    labels = [t]
        except Exception:
            pass
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
    except Exception:
        pass
    return labels


_models_cache = {"ts": 0, "data": None}
MODELS_CACHE_TTL = 3600


def run_discover_models(refresh=False):
    """Discover image + video models Flow offers this account.
    Image: Agent panel dropdown. Video: standard settings model menu.
    Restores the Agent toggle to ON afterwards so image jobs keep working.
    Result is cached (TTL 1h) and persisted to state.json."""
    if not refresh and _models_cache["data"] is not None:
        if time.time() - _models_cache["ts"] < MODELS_CACHE_TTL:
            data = dict(_models_cache["data"])
            data["cached"] = True
            return data
    with _browser_lock:
        page = _launch_browser()
        try:
            if not check_logged_in(page):
                raise RuntimeError("not_logged_in: Flow session missing, owner must log in")
            ensure_project(page)
            try:
                _close_agent_panel(page)
            except Exception:
                pass
            try:
                _set_agent_toggle(page, True)
            except Exception:
                pass
            image_models = []
            try:
                image_models = _open_image_model_options(page)
            except Exception as exc:
                log.warning("image-model discovery failed: %s", exc)
            # While the Agent panel is still open, read the VIDEO model
            # dropdown too (it lives in the same panel, below the image
            # dropdown): no toggle dance needed for this list. The video
            # trigger is the arrow_drop_down button whose label is NOT a
            # Nano Banana model.
            agent_video_models = []
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
                cands = []
                try:
                    aria = page.get_by_role("button", name="Video generation default model", exact=True)
                    if aria.count():
                        cands.extend(aria.all())
                except Exception:
                    pass
                if not cands:
                    for b in page.get_by_role("button").all():
                        try:
                            if not b.is_visible(timeout=500):
                                continue
                            t = (b.inner_text(timeout=1000) or "").strip()
                        except Exception:
                            continue
                        if "arrow_drop_down" in t and "Nano Banana" not in t:
                            cands.append(b)
                before_lines = _visible_lines(page)
                for el in cands[:3]:
                    try:
                        for _try in range(2):
                            try:
                                el.click(timeout=5000)
                            except Exception:
                                el.click(timeout=5000, force=True)
                            page.wait_for_timeout(2000)
                            opts = _menu_option_labels(page)
                            if not opts:
                                after_lines = _visible_lines(page)
                                opts = [l for l in after_lines if l not in before_lines
                                        and l not in _CHROME_LABELS and "credit" not in l.lower()]
                                log.info("agent-panel video menu: before=%d after=%d diff=%s",
                                         len(before_lines), len(after_lines), opts)
                            if opts:
                                break
                            try:
                                page.keyboard.press("Escape")
                                page.wait_for_timeout(800)
                            except Exception:
                                pass
                        if opts:
                            agent_video_models = opts
                            break
                    except Exception:
                        continue
                    finally:
                        try:
                            page.keyboard.press("Escape")
                            page.wait_for_timeout(800)
                        except Exception:
                            pass
            except Exception as exc:
                log.warning("agent-panel video discovery failed: %s", exc)
            if agent_video_models:
                log.info("agent-panel video models: %s", agent_video_models)
            # Close the panel robustly (an open dropdown defeats a single
            # close attempt): retry until the composer is back.
            for _attempt in range(3):
                try:
                    if _close_agent_panel(page):
                        break
                except Exception:
                    pass
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(800)
                except Exception:
                    pass
            if not image_models:
                try:
                    body = _visible_text(page)
                    import re as _re
                    m = _re.search(r"Nano Banana[^\n]{0,40}", body)
                    if m:
                        image_models = [_model_key(m.group(0).strip())]
                        log.warning("image-model discovery: dropdown did not open; reporting selected model only")
                except Exception:
                    pass
            video_models = []
            try:
                _set_agent_toggle(page, False)
                page.wait_for_timeout(1000)
                try:
                    log.info("video-model discovery: agent_on=%s", _agent_is_on(page))
                except Exception:
                    pass
                video_models = _discover_video_models(page)
                if not video_models:
                    page.wait_for_timeout(3000)
                    video_models = _discover_video_models(page)
            except Exception as exc:
                log.warning("video-model discovery failed: %s", exc)
            finally:
                try:
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(800)
                except Exception:
                    pass
                try:
                    _set_agent_toggle(page, True)
                except Exception:
                    pass
            std_video_models = video_models
            merged = []
            for m in agent_video_models + std_video_models:
                if m not in merged:
                    merged.append(m)
            if agent_video_models and std_video_models and agent_video_models != std_video_models:
                log.warning("video models differ: agent-panel=%s standard=%s", agent_video_models, std_video_models)
            video_models = merged
            if video_models and not std_video_models:
                log.warning("video models from Agent panel only (standard menu unreachable)")
            data = {"image_models": image_models, "video_models": video_models, "cached": False}
            _models_cache["data"] = {"image_models": image_models, "video_models": video_models}
            _models_cache["ts"] = time.time()
            try:
                state = load_state()
                state["models"] = {"image_models": image_models, "video_models": video_models}
                state["models_updated"] = int(time.time())
                save_state(state)
            except Exception:
                pass
            log.info("models discovered: image=%s video=%s", image_models, video_models)
            return data
        except Exception:
            _save_debug(page, "modelfail")
            raise


def _ensure_image_model(page, wanted=None, strict=False):
    """Make the reused project's image model `wanted` (default
    DEFAULT_IMAGE_MODEL). ensure_settings only runs when a NEW project is
    created, so the stored project keeps whatever model it was saved with
    unless we switch it here, on every image job. The model picker lives
    in the composer's TUNE Settings panel (Agent settings); the top-bar
    gear only opens View mode and must NOT be used here. The panel is
    ALWAYS closed in a finally block (Escape alone does NOT close it).
    With strict=False (default jobs) a failed switch only logs a warning
    and never fails the job; with strict=True (an explicit model request)
    a missing model raises."""
    wanted = (wanted or DEFAULT_IMAGE_MODEL).strip()
    wkey = _model_key(wanted)
    try:
        if not _open_agent_panel(page):
            log.warning("image-model switch: no tune Settings button")
            return
        try:
            body = _visible_text(page)
        except Exception:
            body = ""
        if _body_has_model(body, wkey):
            log.info("image-model switch: %s already selected, skipped", wanted)
            return
        options = _open_image_model_options(page)
        if not options:
            msg = f"image-model dropdown did not open for {wanted!r}"
            if strict:
                raise RuntimeError(msg)
            log.warning("image-model switch skipped: %s", msg)
            return
        target = None
        for label in options:
            if label == wanted or _model_key(label) == wkey:
                target = label
                break
        if target is None:
            for label in options:
                if _model_key(label).lower() == wkey.lower():
                    target = label
                    break
        if target is None:
            msg = f"image model {wanted!r} not available in Flow; available: {options}"
            if strict:
                raise RuntimeError(msg)
            log.warning("image-model switch skipped: %s", msg)
            return
        clicked = False
        for loc in (page.get_by_text(target, exact=True),
                    page.get_by_text(wkey, exact=False)):
            try:
                if loc.count() and loc.first.is_visible(timeout=2000):
                    loc.first.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue
        page.wait_for_timeout(1000)
        try:
            page.screenshot(path=str(DEBUG_DIR / "model-check.png"))
        except Exception:
            pass
        if clicked:
            _click_if_visible(page, "Save", timeout=3000)
            page.wait_for_timeout(1000)
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(800)
                _tune2 = page.locator('button:has-text("tune")').first
                if _tune2.count() and _tune2.is_visible(timeout=3000):
                    _tune2.click(timeout=5000)
                    page.wait_for_timeout(1500)
                    _body2 = _visible_text(page)
                    if _body_has_model(_body2, wkey):
                        log.info("image-model switch: %s verified after Save", wanted)
                    else:
                        log.warning("image-model switch: Save verification FAILED (dropdown not %s)", wanted)
            except Exception as exc:
                log.warning("image-model switch: Save verification skipped: %s", exc)
        log.info("image-model switch: %s row clicked=%s", wanted, clicked)
    except Exception as exc:
        if strict:
            raise
        log.warning("image-model switch skipped: %s", exc)
    finally:
        try:
            _close_agent_panel(page)
        except Exception:
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(1000)
            except Exception:
                pass




def run_flow_job(image_path, prompt, image_model=None):
    """Full job: browser -> project -> upload? -> prompt -> wait -> download.
    image_model: explicit model label (strict) or None for the default."""
    with _browser_lock:
        page = _launch_browser()
        try:
            if not check_logged_in(page):
                raise RuntimeError("not_logged_in: Flow session missing, owner must log in")
            ensure_project(page)
            # A leftover side panel (settings/instructions) hides the
            # composer; close it before touching the toggle or the switch.
            try:
                _close_agent_panel(page)
            except Exception:
                pass
            try:
                _set_agent_toggle(page, True)
            except Exception:
                pass
            _ensure_image_model(page, wanted=image_model, strict=bool(image_model))
            if image_path:
                _upload_image(page, image_path)
                _add_ingredient(page)
            _start_generation(page, prompt)
            _wait_for_result(page)
            return _download_result(page, is_edit=bool(image_path))
        except Exception:
            _save_debug(page, "jobfail")
            raise


# ---------------- job queue ----------------

_job_queue = queue.Queue()
_jobs = {}
_jobs_lock = threading.Lock()


def run_model_diagnose():
    """Diagnostic only (owner's Lite-switch investigation): open the
    composer and candidate model-picker triggers, screenshotting each step,
    then reset with Escape + project reload. Never submits anything."""
    with _browser_lock:
        page = _launch_browser()
        if not check_logged_in(page):
            raise RuntimeError("not_logged_in: Flow session missing, owner must log in")
        ensure_project(page)
        try:
            _set_agent_toggle(page, True)
        except Exception:
            pass
        shots = []
        # A: fill the composer (no submit) — typing expands it, unlike a
        # bare click, which left it collapsed in diag1.
        try:
            box = page.get_by_label("What do you want to create?")
            if not box.count():
                box = page.locator('[aria-label="What do you want to create?"]')
            box.first.click()
            page.wait_for_timeout(500)
            try:
                box.first.fill("test")
            except Exception:
                page.keyboard.type("test")
            page.wait_for_timeout(1200)
            page.screenshot(path=str(DEBUG_DIR / "diagA.png"))
            shots.append("diagA")
        except Exception as exc:
            log.warning("diagnose A failed: %s", exc)
        # B: click the composer tune Settings button (button dump proved it
        # exists: aria-label Settings + innerText "tune"; the top-bar gear
        # only opens View mode). Screenshot whatever panel it opens.
        try:
            tune = page.locator('button:has-text("tune")').first
            clicked = False
            if tune.count() and tune.is_visible(timeout=3000):
                tune.click(timeout=5000)
                clicked = True
            log.info("diagnose tune clicked=%s", clicked)
            page.wait_for_timeout(1500)
            page.screenshot(path=str(DEBUG_DIR / "diagT.png"))
            shots.append("diagT")
        except Exception as exc:
            log.warning("diagnose B failed: %s", exc)
        # C: clear the test text, close panels, reload the project clean.
        # Each sub-step is isolated: one failure must not skip the reload
        # (that exact bug left the Agent panel open and broke the next two
        # real jobs with a hidden composer on 2026-10-27).
        try:
            box = page.get_by_label("What do you want to create?")
            if box.count():
                try:
                    box.first.click(timeout=5000)
                    page.wait_for_timeout(300)
                    page.keyboard.press("Control+A")
                    page.keyboard.press("Backspace")
                except Exception:
                    pass
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
        except Exception:
            pass
        try:
            ensure_project(page)
            page.screenshot(path=str(DEBUG_DIR / "diagC.png"))
            shots.append("diagC")
        except Exception as exc:
            log.warning("diagnose reset failed: %s", exc)
        return {"shots": shots}


def run_edit_diagnose():
    """Diagnostic for the EDIT ingredient flow in thread view: screenshot
    the leftover state, try closing the side panel via its X, upload the
    base image, screenshot the post-upload UI (is there an Add-ingredients
    button?), then reset. Never submits anything."""
    with _browser_lock:
        page = _launch_browser()
        if not check_logged_in(page):
            raise RuntimeError("not_logged_in: Flow session missing, owner must log in")
        ensure_project(page)
        shots = []
        try:
            page.screenshot(path=str(DEBUG_DIR / "diagE0.png"))
            shots.append("diagE0")
        except Exception:
            pass
        try:
            x = page.locator('button[aria-label="Close"]').first
            if x.count() and x.is_visible(timeout=3000):
                x.click(timeout=5000)
                log.info("diagnose-edit: Close clicked")
            page.wait_for_timeout(1500)
        except Exception as exc:
            log.warning("diagnose-edit close failed: %s", exc)
        try:
            page.screenshot(path=str(DEBUG_DIR / "diagE1.png"))
            shots.append("diagE1")
        except Exception:
            pass
        try:
            _upload_image(page, "/tmp/digen_edit_all_base.jpg")
            # Wait for the upload to FULLY process (diagE2 showed 35% after
            # ~5s): the Add-ingredients button may only appear at 100%.
            page.wait_for_timeout(20000)
        except Exception as exc:
            log.warning("diagnose-edit upload failed: %s", exc)
        try:
            page.screenshot(path=str(DEBUG_DIR / "diagE2.png"))
            shots.append("diagE2")
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)
            ensure_project(page)
            page.screenshot(path=str(DEBUG_DIR / "diagE3.png"))
            shots.append("diagE3")
        except Exception as exc:
            log.warning("diagnose-edit reset failed: %s", exc)
        return {"shots": shots}


def _worker():
    while True:
        job_id, kind, path, prompt, mode, resolution, extra = _job_queue.get()
        try:
            if kind == "warmup":
                with _browser_lock:
                    page = _launch_browser()
                    data = {"logged_in": check_logged_in(page), "url": page.url}
            elif kind == "diagnose":
                data = run_model_diagnose()
            elif kind == "diagnose-edit":
                data = run_edit_diagnose()
            elif kind == "discover":
                data = run_discover_models(refresh=(extra or {}).get("refresh", False))
            elif kind == "video":
                data = run_flow_video_job(path, prompt, mode or "generate",
                                          resolution or "360p",
                                          video_model=(extra or {}).get("video_model"))
            else:
                data = run_flow_job(path, prompt,
                                    image_model=(extra or {}).get("image_model"))
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "data": data}
        except Exception as exc:
            log.error("job %s failed: %s\n%s", job_id, exc, traceback.format_exc())
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "error": str(exc)}
        finally:
            _idle_state["last_end"] = time.time()
            _job_queue.task_done()


threading.Thread(target=_worker, daemon=True).start()


# ---------------- idle browser release (optimization 2026-10-27) ----------------
# The headed Chromium is the heavy part of this bridge. When the service is
# on but no job has run for IDLE_RELEASE_S, close the browser CONTEXT; the
# next job relaunches a context from the live driver via _launch_browser.
# The driver itself is NEVER stopped here: stopping it and restarting it
# later in the worker thread raises Playwright's 'Sync API inside the
# asyncio loop' error and breaks every job until the service restarts
# (2026-10-29). The driver process is tiny; the browser is what gets freed.
IDLE_RELEASE_S = 900
_idle_state = {"last_end": time.time()}


def _idle_watcher():
    global _pw, _ctx, _page
    while True:
        time.sleep(60)
        try:
            if _page is None:
                continue
            with _jobs_lock:
                queued = sum(1 for j in _jobs.values() if j["status"] == "queued")
            if queued:
                continue
            if time.time() - _idle_state["last_end"] < IDLE_RELEASE_S:
                continue
            with _browser_lock:
                try:
                    if _ctx is not None:
                        _ctx.close()
                except Exception:
                    pass
                # _pw deliberately kept alive (see _launch_browser): stopping
                # it here breaks the next relaunch with Playwright's
                # 'Sync API inside the asyncio loop' error (2026-10-29).
                _page = _ctx = None
                log.info("idle browser context released after %ss without jobs (driver kept)", IDLE_RELEASE_S)
        except Exception:
            pass


threading.Thread(target=_idle_watcher, daemon=True).start()


def _enqueue_job(kind, image_path, prompt, timeout=420, mode=None, resolution=None, extra=None):
    job_id = f"job-{int(time.time() * 1000)}"
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued"}
    _job_queue.put((job_id, kind, image_path, prompt, mode, resolution, extra))
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _jobs_lock:
            st = _jobs[job_id]
        if st["status"] in ("done", "error"):
            if st["status"] == "error":
                raise RuntimeError(st["error"])
            return st["data"]
        time.sleep(2)
    raise RuntimeError("Flow bridge timed out after 420s")


# ---------------- API ----------------

@app.get("/models")
def models(refresh: bool = False):
    """List every image/video model Flow offers this account (fresh scrape
    unless a cached result from the last hour exists, or ?refresh=1).
    Pass a returned label as "model" on /generate, /edit or /video."""
    if not refresh and _models_cache["data"] is not None and time.time() - _models_cache["ts"] < MODELS_CACHE_TTL:
        data = dict(_models_cache["data"])
        data["cached"] = True
        return data
    return _enqueue_job("discover", None, None, timeout=300, extra={"refresh": refresh})


@app.get("/health")
def health():
    with _jobs_lock:
        queued = sum(1 for j in _jobs.values() if j["status"] == "queued")
    return {
        "ok": True,
        "browser": "running" if _page is not None else "stopped",
        "queue": queued,
        "project_url": load_state().get("project_url"),
    }


@app.post("/warmup")
def warmup():
    return _enqueue_job("warmup", None, None, timeout=120)


@app.post("/diagnose-model")
def diagnose_model():
    return _enqueue_job("diagnose", None, None, timeout=180)


@app.post("/diagnose-edit")
def diagnose_edit():
    return _enqueue_job("diagnose-edit", None, None, timeout=180)


@app.post("/edit")
async def edit(image: UploadFile = File(None), prompt: str = Form(...),
               image_b64: str = Form(None), model: str = Form(None)):
    tmp = None
    if image is not None:
        tmp = BASE / f"in-{int(time.time() * 1000)}.jpg"
        tmp.write_bytes(await image.read())
    elif image_b64:
        tmp = BASE / f"in-{int(time.time() * 1000)}.jpg"
        tmp.write_bytes(base64.b64decode(image_b64))
    try:
        data = _enqueue_job("edit", str(tmp) if tmp else None, prompt,
                            extra={"image_model": model} if model else None)
    finally:
        if tmp:
            try:
                tmp.unlink()
            except Exception:
                pass
    return Response(content=data, media_type="image/jpeg")


@app.post("/generate")
def generate(payload: dict):
    prompt = payload.get("prompt", "")
    if not prompt:
        raise HTTPException(400, "prompt required")
    model = payload.get("model")
    data = _enqueue_job("generate", None, prompt,
                        extra={"image_model": model} if model else None)
    return Response(content=data, media_type="image/jpeg")


@app.post("/video")
async def video(video: UploadFile = File(None), prompt: str = Form(...),
                mode: str = Form("generate"), resolution: str = Form("360p"),
                model: str = Form(None)):
    """Text-to-video (mode=generate, no file) or video edit
    (mode=edit, mp4 file as a video ingredient). Video model:
    pass "model" (a label from GET /models) or leave it out for
    Omni 1.1 Flash 8s x1; resolution 360p (6 credits) or 720p
    (12 credits, owner only via the bot setting). Invalid
    resolutions fall back to 360p. Edits cost ~40 credits.
    Non-Omni models skip the credit-total check (costs differ)."""
    if resolution not in ("360p", "720p"):
        log.warning("invalid video resolution %r; falling back to 360p", resolution)
        resolution = "360p"
    tmp = None
    if video is not None:
        tmp = BASE / f"vin-{int(time.time() * 1000)}.mp4"
        tmp.write_bytes(await video.read())
    try:
        data = _enqueue_job("video", str(tmp) if tmp else None, prompt,
                            timeout=840, mode=mode, resolution=resolution,
                            extra={"video_model": model} if model else None)
    finally:
        if tmp:
            try:
                tmp.unlink()
            except Exception:
                pass
    return Response(content=data, media_type="video/mp4")

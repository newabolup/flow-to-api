#!/usr/bin/env python3
"""Flow bridge: turns Google Flow's web UI into a local HTTP API for your own
apps (e.g. a Telegram bot).

Single-job queue: one browser, one job at a time.
The browser uses a persistent Chromium profile (PROFILE_DIR) that the owner
logs into once via the noVNC login page; the service then reuses that session.

Endpoints (localhost only, port 8123):
  GET  /health    -> {ok, browser, logged_in, busy, queue, project_url}
  POST /edit      -> multipart form (image file + prompt) OR JSON {prompt, image_b64}
                     waits for completion (max ~420s), returns image/jpeg bytes
  POST /generate  -> JSON {prompt} (text-to-image, no input image)
  POST /warmup    -> launch browser + report login state (diagnostics)

UI recipe (verified live 2026-10-24 in the Muse browser, Pro account):
  New project -> Agent settings (model Nano Banana 2.1, count x1) ->
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
    when DISPLAY is set, else headless."""
    global _pw, _ctx, _page
    if _page is not None:
        try:
            if not _page.is_closed():
                return _page
        except Exception:
            pass
        _page = _ctx = _pw = None
    from playwright.sync_api import sync_playwright

    headless = not os.environ.get("DISPLAY")
    _pw = sync_playwright().start()
    _ctx = _pw.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        channel="chromium",
        viewport={"width": 1280, "height": 900},
        args=["--no-sandbox", "--disable-dev-shm-usage"],
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
    """Best-effort: model Nano Banana 2.1, output count x1. Never fatal."""
    try:
        btn = page.get_by_role("button", name="Settings")
        if btn.count() and btn.first.is_visible(timeout=3000):
            btn.first.click()
            page.wait_for_timeout(1500)
            body = page.content() or ""
            if "Nano Banana 2.1" not in body:
                # Try to open the model picker and pick Nano Banana 2.1
                for name in ("Nano Banana 2.1", "🍌 Nano Banana 2.1"):
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
    btn = page.get_by_role("button", name="Add ingredients")
    if not btn.count():
        btn = page.get_by_text("Add ingredients")
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


def _start_generation(page, prompt):
    # The prompt box is a contenteditable element labelled
    # "What do you want to create?" (aria-label), not a placeholder input.
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
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        low = _visible_text(page).lower()
        if "something went wrong" in low or "failed to generate" in low:
            raise RuntimeError("Flow reported a generation failure")
        if "thinking" not in low and "what do you want to create?" in low:
            return True
        time.sleep(5)
    _save_debug(page, "timeout")
    raise RuntimeError("Timed out waiting for Flow result")


def _download_result(page):
    """Open the result and download the 1K original-size image."""
    # Click the newest result card if a Download button is not already visible.
    dl = page.get_by_role("button", name="Download media")
    if not dl.count() or not dl.first.is_visible(timeout=5000):
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
        page.wait_for_timeout(2000)
        dl = page.get_by_role("button", name="Download media")
    _click(dl)
    page.wait_for_timeout(1500)
    with page.expect_download(timeout=60000) as dlinfo:
        one = page.get_by_text("1K Original size")
        if not one.count():
            one = page.get_by_text("Original size")
        _click(one)
    dlfile = dlinfo.value
    path = dlfile.path()
    data = Path(path).read_bytes()
    log.info("downloaded result: %d bytes", len(data))
    return data


def run_flow_job(image_path, prompt):
    """Full job: browser -> project -> upload? -> prompt -> wait -> download."""
    with _browser_lock:
        page = _launch_browser()
        try:
            if not check_logged_in(page):
                raise RuntimeError("not_logged_in: Flow session missing, owner must log in")
            ensure_project(page)
            if image_path:
                _upload_image(page, image_path)
                _add_ingredient(page)
            _start_generation(page, prompt)
            _wait_for_result(page)
            return _download_result(page)
        except Exception:
            _save_debug(page, "jobfail")
            raise


# ---------------- job queue ----------------

_job_queue = queue.Queue()
_jobs = {}
_jobs_lock = threading.Lock()


def _worker():
    while True:
        job_id, kind, image_path, prompt = _job_queue.get()
        try:
            if kind == "warmup":
                with _browser_lock:
                    page = _launch_browser()
                    data = {"logged_in": check_logged_in(page), "url": page.url}
            else:
                data = run_flow_job(image_path, prompt)
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "data": data}
        except Exception as exc:
            log.error("job %s failed: %s\n%s", job_id, exc, traceback.format_exc())
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "error": str(exc)}
        finally:
            _job_queue.task_done()


threading.Thread(target=_worker, daemon=True).start()


def _enqueue_job(kind, image_path, prompt, timeout=420):
    job_id = f"job-{int(time.time() * 1000)}"
    with _jobs_lock:
        _jobs[job_id] = {"status": "queued"}
    _job_queue.put((job_id, kind, image_path, prompt))
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


@app.post("/edit")
async def edit(image: UploadFile = File(None), prompt: str = Form(...),
               image_b64: str = Form(None)):
    tmp = None
    if image is not None:
        tmp = BASE / f"in-{int(time.time() * 1000)}.jpg"
        tmp.write_bytes(await image.read())
    elif image_b64:
        tmp = BASE / f"in-{int(time.time() * 1000)}.jpg"
        tmp.write_bytes(base64.b64decode(image_b64))
    try:
        data = _enqueue_job("edit", str(tmp) if tmp else None, prompt)
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
    data = _enqueue_job("generate", None, prompt)
    return Response(content=data, media_type="image/jpeg")

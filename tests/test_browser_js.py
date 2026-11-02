"""Browser-level tests for the in-page JavaScript (js.py) and PageDriver.

These run a real headless Chromium against a mock Flow DOM, so the probe,
the wait predicates and the click helpers are exercised for real -- no
Google account and no credits involved.

Skipped automatically when Playwright/Chromium is unavailable.
"""
from __future__ import annotations

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")

from flowbridge import js  # noqa: E402
from flowbridge.driver import PageDriver, role  # noqa: E402

MOCK_FLOW = """
<!doctype html><html><body>
<style>
  .hide { display: none; }
  #composer { width: 600px; height: 48px; }
  main img { width: 400px; height: 300px; }
  /* keep the mock controls from overlapping: a real Flow toolbar lays its
     controls out side by side, and overlapping boxes would make Playwright
     click the top-most neighbour (which is what the force-click fallback
     is a last resort for). */
  [role=button] { overflow: hidden; white-space: nowrap; margin: 2px; }
</style>
<div role="button" aria-label="Agent" aria-checked="true" style="width:40px;height:20px">Agent</div>
<div role="button" aria-label="Settings trigger" style="width:40px;height:20px">tune</div>
<div role="button" id="addmedia" style="width:80px;height:20px">Add media</div>
<div role="button" style="width:80px;height:20px">Add to prompt</div>
<div role="button" style="width:80px;height:20px">Start generation</div>
<div role="button" style="width:80px;height:20px">Download media</div>
<div role="button" style="width:80px;height:20px">Video · 360p · 8s x1</div>
<div contenteditable="true" aria-label="What do you want to create?"
     id="composer">What do you want to create?</div>
<flow-toggles aria-label="Mode">
  <mat-button-toggle aria-checked="false">Image</mat-button-toggle>
  <mat-button-toggle aria-checked="true">Video</mat-button-toggle>
</flow-toggles>
<div role="option">🍌 Nano Banana 2 Lite</div>
<main id="timeline"><img src="https://example.com/old-card.png"></main>
<script>
  window.__clicks = [];
  document.querySelectorAll('[role=button]').forEach(function (b) {
    b.addEventListener('click', function () {
      window.__clicks.push(b.getAttribute('aria-label') || b.innerText);
    });
  });
  window.addCard = function (src) {
    var img = document.createElement('img');
    img.src = src;
    document.getElementById('timeline').appendChild(img);
  };
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        instance = pw.chromium.launch(args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture()
def fresh_page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    pg.set_content(MOCK_FLOW)
    yield pg
    context.close()


@pytest.fixture(scope="module")
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    pg = context.new_page()
    pg.set_content(MOCK_FLOW)
    yield pg
    context.close()


def test_probe_reads_the_ui_in_one_shot(page):
    driver = PageDriver(page)
    snapshot = driver.probe()
    assert snapshot["composer"]["visible"] is True
    assert snapshot["agent"] is True
    assert snapshot["download"] is True
    assert snapshot["settings_trigger"] is True
    assert [c["src"] for c in snapshot["cards"]] == ["https://example.com/old-card.png"]
    assert any("Nano Banana" in m for m in snapshot["menu"])
    assert snapshot["toggles"]["Mode"][1] == {"text": "Video", "checked": True}
    assert any("Video ·" in s for s in snapshot["summary"])
    buttons = [b["t"] for b in snapshot["buttons"]]
    assert "Start generation" in buttons
    assert snapshot["panel_open"] is False


def test_result_predicate_ignores_old_cards(fresh_page):
    driver = PageDriver(fresh_page, poll_ms=50)
    baseline = [c["src"] for c in driver.probe()["cards"]]
    # nothing new yet -> the wait must not resolve
    assert driver.wait_until("result", baseline, timeout=0.4) is False
    fresh_page.evaluate("window.addCard('https://example.com/new-card.png')")
    value = driver.wait_until("result", baseline, timeout=5)
    assert value == "done"
    assert [c["src"] for c in driver.probe()["cards"]][-1].endswith("new-card.png")


def test_result_predicate_reports_failure(fresh_page):
    driver = PageDriver(fresh_page, poll_ms=50)
    fresh_page.evaluate("document.body.insertAdjacentHTML('beforeend', '<p>Something went wrong</p>')")
    assert driver.wait_until("result", [], timeout=3) == "failed"


def test_button_and_text_predicates(page):
    driver = PageDriver(page, poll_ms=50)
    assert driver.wait_until("button", "Start generation", timeout=2) is True
    assert driver.wait_until("button", "Nonexistent control", timeout=0.4) is False
    assert driver.wait_until("text", "nano banana", timeout=2) is True
    assert driver.wait_until("upload_idle", None, timeout=2) is True


def test_composer_predicate(page):
    driver = PageDriver(page, poll_ms=50)
    assert driver.wait_until("composer", None, timeout=2) is True


def test_click_uses_real_events(fresh_page):
    driver = PageDriver(fresh_page)
    assert driver.click(role("button", "Start generation")) is True
    assert "Start generation" in fresh_page.evaluate("window.__clicks")


def test_click_reports_missing_target(page):
    driver = PageDriver(page)
    assert driver.click(role("button", "No such button"), timeout=1, force_fallback=False) is False


def test_mark_card_then_click_it(fresh_page):
    driver = PageDriver(fresh_page)
    assert driver.mark_card("https://example.com/old-card.png") is True
    assert driver.click_marked_card() is True
    assert driver.mark_card("https://example.com/nope.png") is False


def test_upload_via_pushes_a_file(fresh_page):
    fresh_page.set_content(
        """<html><body><input type="file" id="f" style="display:none">
        <button id="b" onclick="document.getElementById('f').click()">Upload</button>
        </body></html>""")
    opened = []

    def open_chooser():
        fresh_page.click("#b", no_wait_after=True)
        opened.append(True)
        return True

    import pathlib
    import tempfile
    path = pathlib.Path(tempfile.mkdtemp()) / "in.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 16)
    driver = PageDriver(fresh_page)
    assert driver.upload_via(open_chooser, str(path), timeout=8) is True
    assert opened
    assert fresh_page.evaluate("document.getElementById('f').files.length") == 1


def test_clear_composer(fresh_page):
    driver = PageDriver(fresh_page)
    driver.clear_composer()
    fresh_page.keyboard.press("Backspace")
    assert driver.probe()["composer"]["text"] == ""


def test_every_predicate_is_valid_javascript(page):
    """Guard against a typo in a predicate: each one must evaluate."""
    for name, expr in js.WAIT_PREDICATES.items():
        result = page.evaluate(f"() => {{ const f = {expr}; return typeof f === 'function'; }}")
        assert result is True, name
    assert page.evaluate(f"() => typeof ({js.PROBE}) === 'function'")

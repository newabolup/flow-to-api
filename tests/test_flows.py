from flowbridge.image_flow import run_image_job
from flowbridge.media import sniff
from flowbridge.ui import FlowError, FlowUI, NotLoggedIn


def ui_for(driver, settings, state):
    return FlowUI(driver, settings, state)


def test_generate_happy_path(driver, settings, ctx):
    ui = ui_for(driver, settings, ctx.state)
    data = run_image_job(ui, "a red apple")
    mime, _ = sniff(data)
    assert mime == "image/jpeg"
    # no upload happened for a text-to-image job
    assert driver.uploads == []
    # the submitted prompt landed in the composer
    assert "a red apple" in driver.calls or driver.marked == "https://lh3/new-card"
    assert "a red apple" in driver.world["composer_text"]


def test_generate_waits_for_a_new_card_not_the_old_one(driver, settings, ctx):
    ui = ui_for(driver, settings, ctx.state)
    driver.world["cards"] = [{"src": "old", "w": 400, "y": 10}]
    run_image_job(ui, "prompt")
    assert driver.marked == "https://lh3/new-card"
    assert driver.marked != "old"


def test_edit_uploads_before_snapshot_and_downloads(driver, settings, ctx):
    ui = ui_for(driver, settings, ctx.state)
    run_image_job(ui, "make it green", image_path="/tmp/in.jpg")
    assert driver.uploads == ["/tmp/in.jpg"]
    # upload happens before the result wait (that is what keeps the uploaded
    # card out of the "new result" set)
    upload_idx = driver.calls.index("wait:upload_idle:None")
    result_idx = [i for i, c in enumerate(driver.calls) if c.startswith("wait:result")][0]
    assert upload_idx < result_idx


def test_flow_failure_is_propagated(driver, settings, ctx):
    ui = ui_for(driver, settings, ctx.state)
    driver.world["fail_on_wait"] = "boom"
    try:
        run_image_job(ui, "prompt")
    except FlowError as exc:
        assert "failure" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected FlowError")


def test_not_logged_in_raises(driver, settings, ctx):
    ui = ui_for(driver, settings, ctx.state)
    driver.world["composer_visible"] = False
    driver.world["sign_in_wall"] = True
    driver.world["new_project"] = False
    try:
        run_image_job(ui, "prompt")
    except NotLoggedIn as exc:
        assert "not_logged_in" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected NotLoggedIn")

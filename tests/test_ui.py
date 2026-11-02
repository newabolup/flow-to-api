from flowbridge.ui import FlowUI, body_has_model, model_key


def make_ui(driver, settings, state):
    return FlowUI(driver, settings, state)


def test_model_key_strips_emoji():
    assert model_key("🍌 Nano Banana 2 Lite") == "Nano Banana 2 Lite"
    assert model_key("Nano Banana Pro") == "Nano Banana Pro"


def test_body_has_model_is_a_line_match():
    body = "Agent settings\n🍌 Nano Banana 2 Lite\nSave"
    assert body_has_model(body, "Nano Banana 2 Lite")
    # 'Nano Banana 2' must not match the 'Nano Banana 2 Lite' line
    assert not body_has_model(body, "Nano Banana 2")


def test_close_panels_is_free_when_nothing_is_open(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    assert ui.close_panels() is True
    # one probe + one escape, no click storm
    assert sum(1 for c in driver.calls if c.startswith("click")) == 0


def test_set_agent_no_click_when_already_correct(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    driver.world["agent"] = True
    assert ui.set_agent(True) is True
    assert not [c for c in driver.calls if c.startswith("click")]


def test_set_agent_clicks_when_state_differs(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    driver.world["agent"] = False

    def flip(spec, timeout=None, force_fallback=True):
        driver.calls.append(f"click:{spec}")
        if "Agent" in str(spec):
            driver.world["agent"] = True
            return True
        return False

    driver.click = flip  # type: ignore[assignment]
    assert ui.set_agent(True) is True


def test_ensure_image_model_cache_hit_skips_settings_panel(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    driver.world["body"] = ("What do you want to create?\nAgent settings\n"
                             "Nano Banana 2 Lite\nSave")
    assert ui.ensure_image_model(None) is True
    first_clicks = len(driver.calls)
    assert ui.ensure_image_model(None) is True
    assert len(driver.calls) == first_clicks  # second call is a pure cache hit


def test_ensure_image_model_strict_raises_when_model_missing(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    driver.world["body"] = "What do you want to create?\nAgent settings\nNano Banana 2 Lite"
    driver.world["menu"] = ["Nano Banana 2 Lite"]
    try:
        ui.ensure_image_model("Nano Banana Pro", strict=True)
    except Exception as exc:  # FlowError
        assert "not available" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("strict model request should raise")


def test_select_toggle_skips_click_when_already_checked(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    driver.world["toggles"] = {"Mode": [{"text": "Image", "checked": False},
                                        {"text": "Video", "checked": True}]}
    assert ui.select_toggle("Mode", "Video") is True
    assert not [c for c in driver.calls if c.startswith("click_within")]


def test_select_toggle_clicks_unchecked_option(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    driver.world["toggles"] = {"Mode": [{"text": "Image", "checked": True},
                                        {"text": "Video", "checked": False}]}
    assert ui.select_toggle("Mode", "Video") is True
    assert [c for c in driver.calls if c.startswith("click_within")]


def test_card_srcs_reads_cards_from_one_probe(driver, settings, ctx):
    ui = make_ui(driver, settings, ctx.state)
    driver.world["cards"] = [{"src": "a", "w": 400, "y": 1}, {"src": "b", "w": 400, "y": 2}]
    assert ui.card_srcs() == ["a", "b"]

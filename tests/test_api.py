from fastapi.testclient import TestClient

from flowbridge.api import create_app
from tests.fake import JPEG, FakeContext, make_settings


def client_for(settings=None, ctx=None, **overrides):
    settings = settings or make_settings(__import__("pathlib").Path("/tmp/fb-api-test"))
    ctx = ctx or FakeContext(settings)
    app = create_app(settings, ctx=ctx)
    return TestClient(app), ctx


def test_health_is_open_and_reports_queue(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        body = client.get("/health").json()
    assert body["ok"] is True
    assert body["queue"] == 0
    assert "project_url" in body


def test_generate_returns_image_with_timing_header(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        response = client.post("/generate", json={"prompt": "a red apple"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert int(response.headers["X-Flowbridge-Ms"]) >= 0
    assert response.content.startswith(b"\xff\xd8\xff")


def test_generate_requires_prompt(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        assert client.post("/generate", json={"prompt": ""}).status_code == 400
        assert client.post("/generate", json={}).status_code == 400


def test_edit_requires_an_image(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        response = client.post("/edit", data={"prompt": "make it green"})
    assert response.status_code == 400
    assert "image" in response.json()["detail"]


def test_edit_with_upload(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        response = client.post(
            "/edit",
            files={"image": ("in.jpg", JPEG, "image/jpeg")},
            data={"prompt": "make it green"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert ctx.driver.uploads and ctx.driver.uploads[0].endswith(".jpg")


def test_edit_with_base64_image(ctx, settings):
    import base64

    client = TestClient(create_app(settings, ctx=ctx))
    payload = base64.b64encode(JPEG).decode()
    with client:
        response = client.post("/edit", data={"prompt": "p", "image_b64": payload})
    assert response.status_code == 200


def test_upload_size_limit(tmp_path):
    limited = make_settings(tmp_path, warm_on_start=False, max_upload_mb=0)
    limited_ctx = FakeContext(limited)
    client = TestClient(create_app(limited, ctx=limited_ctx))
    with client:
        response = client.post("/edit", files={"image": ("in.jpg", JPEG, "image/jpeg")},
                               data={"prompt": "p"})
    assert response.status_code == 413


def test_video_validation(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        assert client.post("/video", data={"prompt": "p", "resolution": "4k"}).status_code == 400
        assert client.post("/video", data={"prompt": "p", "mode": "edit"}).status_code == 400
        assert client.post("/video", data={"mode": "generate"}).status_code == 422


def test_video_generate(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    ctx.driver.world["download_bytes"] = b"\x00\x00\x00\x18ftypmp42" + b"0" * 32
    ctx.driver.world["summary"] = ["Video · 360p · 8s x1"]
    ctx.driver.world["credit_total"] = 6
    ctx.driver.world["toggles"] = {"Mode": [{"text": "Video", "checked": True}]}
    with client:
        response = client.post("/video", data={"prompt": "a cat", "resolution": "360p"})
    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"


def test_api_key_is_enforced(ctx, settings):
    settings.api_key = "s3cret"
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        assert client.post("/generate", json={"prompt": "p"}).status_code == 401
        ok = client.post("/generate", json={"prompt": "p"},
                         headers={"X-API-Key": "s3cret"})
        assert ok.status_code == 200
        bearer = client.post("/generate", json={"prompt": "p"},
                             headers={"Authorization": "Bearer s3cret"})
        assert bearer.status_code == 200
        # health stays open for monitoring
        assert client.get("/health").status_code == 200


def test_not_logged_in_maps_to_503(ctx, settings):
    ctx.driver.world["composer_visible"] = False
    ctx.driver.world["sign_in_wall"] = True
    ctx.driver.world["new_project"] = False
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        response = client.post("/generate", json={"prompt": "p"})
    assert response.status_code == 503
    assert "not_logged_in" in response.json()["detail"]


def test_models_uses_cache_without_touching_the_browser(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        body = client.get("/models").json()
    assert body["cached"] is True
    assert body["image_models"]


def test_unknown_job_404(ctx, settings):
    client = TestClient(create_app(settings, ctx=ctx))
    with client:
        assert client.get("/jobs/nope").status_code == 404

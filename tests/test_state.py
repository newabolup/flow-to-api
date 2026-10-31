import json
import time

from flowbridge.state import StateStore


def test_project_url_roundtrip(tmp_path):
    store = StateStore(tmp_path / "state.json")
    assert store.project_url(0) is None
    store.set_project_url("https://flow.google.com/project/x", lane=0)
    assert store.project_url(0) == "https://flow.google.com/project/x"
    # read from a fresh instance: the file must be valid JSON on disk
    fresh = StateStore(tmp_path / "state.json")
    assert fresh.project_url(0) == "https://flow.google.com/project/x"
    assert json.loads((tmp_path / "state.json").read_text())["project_url"].endswith("/x")


def test_corrupt_state_file_does_not_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json")
    store = StateStore(path)
    assert store.read() == {}
    store.set("project_url", "u")
    assert store.get("project_url") == "u"


def test_model_cache_respects_ttl(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save_models(["a"], ["b"])
    assert store.cached_models(ttl_s=3600)["image_models"] == ["a"]
    data = json.loads(path.read_text())
    data["models_updated"] = int(time.time()) - 7200
    path.write_text(json.dumps(data))
    stale = StateStore(path)
    assert stale.cached_models(ttl_s=3600) is None

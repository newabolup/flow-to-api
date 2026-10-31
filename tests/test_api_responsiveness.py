"""Regression guard for the worst bug in the old bridge.

`/edit` and `/video` were `async def` endpoints that called a *blocking*
job waiter (`time.sleep` loop), so the asyncio event loop was frozen for
the whole job: `/health` did not answer for a minute or more, and any
health monitor restarted the service mid-job.
"""
import threading
import time

from fastapi.testclient import TestClient

from flowbridge.api import create_app
from tests.fake import FakeContext, make_settings


def test_health_answers_while_a_job_is_running(tmp_path):
    settings = make_settings(tmp_path, warm_on_start=False)
    ctx = FakeContext(settings)

    def slow_dispatch(job):
        time.sleep(1.5)
        return b"\xff\xd8\xff\xe0" + b"0" * 16

    ctx.queue._dispatch = slow_dispatch  # type: ignore[assignment]
    app = create_app(settings, ctx=ctx)

    with TestClient(app) as client:
        worker = threading.Thread(
            target=lambda: client.post("/generate", json={"prompt": "slow"}))
        worker.start()
        time.sleep(0.2)
        started = time.monotonic()
        health = client.get("/health")
        elapsed = time.monotonic() - started
        worker.join(timeout=10)

    assert health.status_code == 200
    assert elapsed < 0.5, f"/health was blocked for {elapsed:.2f}s during a job"
    assert health.json()["running"], "the running job should be reported"

import time

import pytest

from flowbridge.jobs import JobError, JobTimeout, QueueFull


def test_result_is_returned_without_polling_latency(ctx):
    """The old waiter slept 2s between status checks; results now arrive on
    an Event, so a fast job must come back in well under that."""
    ctx.queue.start()
    try:
        started = time.monotonic()
        data = ctx.queue.submit("image", {"prompt": "hello"}, timeout=30)
        elapsed = time.monotonic() - started
    finally:
        ctx.queue.stop()
    assert data.startswith(b"\xff\xd8\xff")
    assert elapsed < 1.0, f"job dispatch took {elapsed:.2f}s (polling regression?)"


def test_busy_state_is_visible_while_running(ctx):
    """Regression guard: the old queue had no 'running' status, so the idle
    watcher could close the browser mid-job."""
    seen = {}
    original = ctx.queue._dispatch

    def slow_dispatch(job):
        seen["stats"] = ctx.queue.stats()
        return original(job)

    ctx.queue._dispatch = slow_dispatch  # type: ignore[assignment]
    ctx.queue.start()
    try:
        ctx.queue.submit("image", {"prompt": "x"}, timeout=30)
    finally:
        ctx.queue.stop()
    assert seen["stats"]["running"], "a running job must be reported as running"


def test_failure_is_raised_as_job_error(ctx, driver):
    driver.world["fail_on_wait"] = "boom"
    ctx.queue.start()
    try:
        with pytest.raises(JobError):
            ctx.queue.submit("image", {"prompt": "x"}, timeout=30)
    finally:
        ctx.queue.stop()


def test_timeout_marks_the_job_cancelled(ctx):
    """A client that gives up must not leave an un-cancelled job behind."""
    cancelled = {}

    def hanging_dispatch(job):
        time.sleep(1.0)
        cancelled["flag"] = job.cancel_requested
        return b"late"

    ctx.queue._dispatch = hanging_dispatch  # type: ignore[assignment]
    ctx.queue.start()
    try:
        with pytest.raises(JobTimeout):
            ctx.queue.submit("image", {"prompt": "x"}, timeout=0.2)
        time.sleep(1.2)
    finally:
        ctx.queue.stop()
    assert cancelled["flag"] is True


def test_bounded_queue_rejects_work(ctx):
    ctx.settings.max_queue = 1

    def slow_dispatch(job):
        time.sleep(0.6)
        return b"ok"

    ctx.queue._dispatch = slow_dispatch  # type: ignore[assignment]
    ctx.queue.start()
    try:
        ctx.queue.submit_async("image", {"prompt": "first"})
        time.sleep(0.2)
        with pytest.raises(QueueFull):
            ctx.queue.submit("image", {"prompt": "second"}, timeout=5)
    finally:
        ctx.queue.stop()


def test_jobs_are_pruned(ctx):
    ctx.settings.keep_jobs_s = 0
    ctx.queue.start()
    try:
        ctx.queue.submit("image", {"prompt": "x"}, timeout=30)
        time.sleep(0.1)
        ctx.queue._prune()
        assert ctx.queue.stats()["queue"] == 0
        assert not ctx.queue._jobs
    finally:
        ctx.queue.stop()


def test_worker_shuts_the_browser_down(ctx):
    ctx.queue.start()
    ctx.queue.stop()
    assert ctx.runtime.shut is True


def test_async_submit_returns_an_id(ctx):
    ctx.queue.start()
    try:
        job_id = ctx.queue.submit_async("warmup", {})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            info = ctx.queue.job(job_id)
            if info and info["status"] in ("done", "error"):
                break
            time.sleep(0.05)
        assert info["status"] == "done"
    finally:
        ctx.queue.stop()

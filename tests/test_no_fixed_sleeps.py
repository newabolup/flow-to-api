"""Guard the performance property of this refactor.

Fixed sleeps were the main source of per-job latency (78 `wait_for_timeout`
calls, 122.5s of literal sleep budget in the old bridge). They are easy to
reintroduce, so this test fails if `page.wait_for_timeout` shows up outside
the driver wrapper, or if the polling `time.sleep`s grow.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "flowbridge"


def read(name: str) -> str:
    return (PACKAGE / name).read_text(encoding="utf-8")


def test_wait_for_timeout_only_lives_in_the_driver():
    offenders = [p.name for p in PACKAGE.glob("*.py")
                 if p.name != "driver.py"
                 and ".wait_for_timeout(" in p.read_text(encoding="utf-8")]
    assert offenders == [], (
        "fixed sleeps must stay inside driver.sleep(); use driver.wait_until "
        f"with an in-page predicate instead. Offenders: {offenders}")


def test_no_async_endpoint_calls_a_blocking_waiter():
    api = read("api.py")
    # every route must be a plain def so FastAPI offloads it to a thread
    async_routes = re.findall(r"@app\.\w+\([^)]*\)\s*\n\s*async def", api)
    assert async_routes == [], "async routes block the event loop with sync work"


def test_polling_sleeps_are_bounded():
    total = 0
    for name in ("ui.py", "image_flow.py", "video_flow.py", "discovery.py"):
        total += len(re.findall(r"time\.sleep\(", read(name)))
    assert total <= 8, f"{total} polling sleeps in the flow layer; expected <= 8"

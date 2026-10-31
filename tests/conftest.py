import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from tests.fake import FakeContext, FakeDriver, make_settings  # noqa: E402


@pytest.fixture()
def settings(tmp_path):
    return make_settings(tmp_path, warm_on_start=False)


@pytest.fixture()
def driver():
    return FakeDriver()


@pytest.fixture()
def ctx(settings, driver):
    return FakeContext(settings, driver=driver)

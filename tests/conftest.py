import json
from pathlib import Path

import pytest

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_json():
    return lambda name: json.loads((FIX / name).read_text())

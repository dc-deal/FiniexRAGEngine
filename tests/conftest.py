"""Shared pytest fixtures."""
import os

# Console-only logging for the suite: booting the app in a test (the client fixture) must not
# append test output — including deliberately-raised errors — to the real logs/finiex.log.
os.environ['FINIEX_LOG_FILE'] = ''

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from finiexragengine.api.api_app import create_app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    # attach_runners=False pins the app to scaffold-mock mode: the free suite must
    # never make paid API calls just because DATABASE_URL/OPENAI_API_KEY are set in
    # the developer's (or CI's) environment. Real runs are the fenced `paid` tests
    # and the 💸 CLIs — exercised deliberately, never as a suite side effect.
    return TestClient(create_app(attach_runners=False))

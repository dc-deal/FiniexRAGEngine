"""Shared pytest fixtures."""
import pytest
from fastapi.testclient import TestClient

from finiexragengine.api.api_app import create_app


@pytest.fixture
def client() -> TestClient:
    # attach_runners=False pins the app to scaffold-mock mode: the free suite must
    # never make paid API calls just because DATABASE_URL/OPENAI_API_KEY are set in
    # the developer's (or CI's) environment. Real runs are the fenced `paid` tests
    # and the 💸 CLIs — exercised deliberately, never as a suite side effect.
    return TestClient(create_app(attach_runners=False))

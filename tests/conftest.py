"""Shared pytest fixtures."""
import pytest
from fastapi.testclient import TestClient

from finiexragengine.api.api_app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())

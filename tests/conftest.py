"""Shared fixtures for the extracted CHARON test suite."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests._fake_backend import install_fake_backend


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Return the standard test authorization header."""
    return {"Authorization": "Bearer test-token-for-testing"}


@pytest.fixture(autouse=True)
def reset_rate_limiter_state():
    """Prevent request buckets from leaking between client fixtures."""
    from mnemos.core.rate_limit import limiter

    reset = getattr(limiter, "reset", None)
    if callable(reset):
        reset()


@pytest_asyncio.fixture
async def client(monkeypatch):
    """Create an authenticated in-process client with isolated persistence."""
    from mnemos.api.dependencies import configure_auth
    from mnemos.api.main import app

    configure_auth({"enabled": False})
    install_fake_backend(monkeypatch)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client

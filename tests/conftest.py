"""Pytest fixtures for the Entergy integration.

HTTP is mocked for every test. Nothing in this suite may contact Entergy.
"""

from __future__ import annotations

import aiohttp
import pytest

from custom_components.entergy_mobile.const import BASE_URL
from tests.common import OLD_HOST, EntergyFakeApi

# Requests to either real host must stay inside the fake. pytest-socket is a second barrier.
_REAL_ENTERGY_HOSTS = (OLD_HOST, "mobileapp.api-prod.entergy.com", "mindgrb.io")

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load this repo's custom component instead of only core integrations."""
    yield


@pytest.fixture
def entergy_api(monkeypatch: pytest.MonkeyPatch):
    """Replace aiohttp requests with the scripted Entergy API."""
    api = EntergyFakeApi()
    original = aiohttp.ClientSession._request

    async def _request(self, method, url, *args, **kwargs):
        url_text = str(url)
        lowered = url_text.lower()
        if any(host in lowered for host in _REAL_ENTERGY_HOSTS):
            return api.dispatch(method, url_text, kwargs)
        return await original(self, method, url, *args, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", _request)
    yield api
    for _method, url in api.seen:
        assert url.startswith(BASE_URL)
        assert "mindgrb" not in url.lower()

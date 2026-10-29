"""Pytest fixtures for the Entergy integration.

HTTP is mocked for every test. Nothing in this suite may contact Entergy.
"""

from __future__ import annotations

import aiohttp
import pytest

from tests.common import EntergyFakeApi

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
        if "mindgrb.io" not in url_text and "entergy" not in url_text.lower():
            return await original(self, method, url, *args, **kwargs)
        return api.dispatch(method, url_text, kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", _request)
    yield api
    for _method, url in api.seen:
        assert url.startswith("https://prod.entergy.mindgrb.io/")

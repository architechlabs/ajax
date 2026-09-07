"""Tests for the hardened direct-only Ajax REST client."""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from custom_components.ajax.api import AjaxRestApi, AjaxRestApiError, RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX
from custom_components.ajax.const import AJAX_REST_API_BASE_URL, AUTH_MODE_DIRECT


def test_init_hashes_plain_password() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="hunter2")
    assert api.password_hash == hashlib.sha256(b"hunter2").hexdigest()


def test_init_keeps_pre_hashed_password_verbatim() -> None:
    pre_hashed = hashlib.sha256(b"hunter2").hexdigest()
    api = AjaxRestApi(api_key="k", email="u@example.com", password=pre_hashed, password_is_hashed=True)
    assert api.password_hash == pre_hashed


def test_api_key_is_required() -> None:
    with pytest.raises(ValueError, match="Enterprise Ajax API key is required"):
        AjaxRestApi(api_key="", email="u@example.com", password="p")


def test_direct_mode_is_fixed() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    assert AUTH_MODE_DIRECT == "direct"
    assert api._get_base_url() == AJAX_REST_API_BASE_URL
    assert api._get_base_url(for_login=True) == AJAX_REST_API_BASE_URL
    assert not hasattr(api, "proxy_url")
    assert not hasattr(api, "sse_url")


def test_api_key_header_is_set() -> None:
    api = AjaxRestApi(api_key="MY-KEY", email="u@example.com", password="p")
    assert api._base_headers["X-Api-Key"] == "MY-KEY"


@pytest.mark.parametrize(
    "endpoint",
    ["", "/login", "\\login", ".", "../login", "://", "https://evil.example", "a\nb", "a\rb", "a\x00b"],
)
def test_build_url_rejects_non_relative_endpoint(endpoint: str) -> None:
    with pytest.raises(AjaxRestApiError, match="Invalid API endpoint"):
        AjaxRestApi._build_url(endpoint)


def test_build_url_uses_only_official_host() -> None:
    url = AjaxRestApi._build_url("login")
    assert url == f"{AJAX_REST_API_BASE_URL}/login"
    assert "@" not in url
    assert "evil" not in url


def test_cache_bypass_is_noop() -> None:
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    api.bypass_cache_next()
    assert api._cache_entry_usable(0.0, 999999999.0) is True


def test_backoff_doubles_and_caps() -> None:
    assert AjaxRestApi._calculate_backoff(0) == RETRY_BACKOFF_BASE
    assert AjaxRestApi._calculate_backoff(1) == RETRY_BACKOFF_BASE * 2
    assert AjaxRestApi._calculate_backoff(20) == RETRY_BACKOFF_MAX


def test_get_session_uses_private_tls_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict[str, object] = {}

    class FakeConnector:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)

    class FakeSession:
        closed = False

    def fake_session(**kwargs: object) -> FakeSession:
        created.update(kwargs)
        return FakeSession()

    monkeypatch.setattr("custom_components.ajax.api._base.aiohttp.TCPConnector", FakeConnector)
    monkeypatch.setattr("custom_components.ajax.api._base.aiohttp.ClientSession", fake_session)

    api = AjaxRestApi(api_key="k", email="u@example.com", password="p")
    import asyncio

    asyncio.run(api._get_session())
    assert created["ssl"] is True
    assert created["trust_env"] is False


def test_session_injection_is_allowed_for_home_assistant() -> None:
    session = MagicMock()
    api = AjaxRestApi(api_key="k", email="u@example.com", password="p", session=session)
    assert api.session is session
    assert api._owns_session is False

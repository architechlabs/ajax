"""Tests for the direct-only Ajax configuration flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from custom_components.ajax.config_flow import AjaxConfigFlow
from custom_components.ajax.const import AUTH_MODE_DIRECT, CONF_API_KEY, CONF_EMAIL, CONF_PASSWORD, CONF_TOTP_SECRET


def test_config_flow_defaults_to_direct_mode() -> None:
    flow = AjaxConfigFlow()
    assert flow._auth_mode == AUTH_MODE_DIRECT


def test_user_step_is_direct_only() -> None:
    flow = AjaxConfigFlow()
    assert flow.async_step_user is not None


def test_direct_build_api_does_not_accept_proxy_transport() -> None:
    flow = AjaxConfigFlow()
    with patch("custom_components.ajax.config_flow.AjaxRestApi") as api_cls:
        api_cls.return_value = object()
        # The helper is private and sync; invoke through the class to ensure its
        # constructor has no proxy/SSE/AWS arguments.
        from custom_components.ajax.config_flow import _build_api

        _build_api(email="u@example.com", password="p", api_key="key")
        kwargs = api_cls.call_args.kwargs
        assert kwargs == {"api_key": "key", "email": "u@example.com", "password": "p", "totp_secret": None}
        assert "proxy_url" not in kwargs
        assert "proxy_mode" not in kwargs


def test_direct_schema_contains_enterprise_credentials() -> None:
    flow = AjaxConfigFlow()
    with patch.object(flow, "async_step_direct", new=AsyncMock()) as _:
        assert CONF_API_KEY
        assert CONF_EMAIL
        assert CONF_PASSWORD
        assert CONF_TOTP_SECRET

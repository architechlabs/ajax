"""Unit tests for ``AjaxConfigFlow``.

The full HA-stack integration tests for the flow (login mocked at the
network layer, 2FA branch, proxy reconfigure, etc.) require fixtures
that conflict with the lightweight setup we use for the pure helpers —
they belong in a dedicated test session against the real HA harness.

These tests pin the no-IO branches: the entry-point form schema, the
auth-mode routing decision, and the way the flow stashes user input
between steps.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.ajax.config_flow import AjaxConfigFlow
from custom_components.ajax.const import (
    AUTH_MODE_DIRECT,
    AUTH_MODE_PROXY_SECURE,
    CONF_AUTH_MODE,
)


def _make_flow() -> AjaxConfigFlow:
    """Build an isolated config-flow instance without a running HA loop."""
    flow = AjaxConfigFlow()
    # ConfigFlow uses self.hass for the few async-bus calls we don't reach
    # in these unit-level tests; a MagicMock is enough.
    flow.hass = MagicMock()
    return flow


@pytest.mark.asyncio
async def test_step_user_without_input_returns_mode_selection_form() -> None:
    """The entry-point step must present the auth-mode chooser as a form."""
    flow = _make_flow()
    result = await flow.async_step_user()
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert result["data_schema"] is not None
    # Defaults to proxy mode (most users don't have an enterprise API key).
    schema = result["data_schema"].schema
    key = next(k for k in schema if str(k) == CONF_AUTH_MODE)
    assert key.default() == AUTH_MODE_PROXY_SECURE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_input", "expected_step_call"),
    [
        ({CONF_AUTH_MODE: AUTH_MODE_DIRECT}, "async_step_direct"),
        ({CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE}, "async_step_proxy"),
    ],
)
async def test_step_user_routes_to_the_right_substep(user_input: dict[str, Any], expected_step_call: str) -> None:
    """user-mode choice must dispatch to the matching follow-up step."""
    flow = _make_flow()
    with patch.object(flow, expected_step_call, new=AsyncMock(return_value={"type": "form"})) as routed:
        await flow.async_step_user(user_input)
    routed.assert_awaited_once()
    # _auth_mode is stashed so later steps remember the choice (used when
    # the proxy step decides between secure / hybrid).
    assert flow._auth_mode == user_input[CONF_AUTH_MODE]


@pytest.mark.asyncio
async def test_step_user_persists_auth_mode_between_steps() -> None:
    """Setting auth mode in step_user must survive into _user_input dict."""
    flow = _make_flow()
    with patch.object(flow, "async_step_proxy", new=AsyncMock(return_value={"type": "form"})):
        await flow.async_step_user({CONF_AUTH_MODE: AUTH_MODE_PROXY_SECURE})
    assert flow._auth_mode == AUTH_MODE_PROXY_SECURE

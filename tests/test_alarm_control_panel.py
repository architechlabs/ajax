"""Tests for AjaxAlarmControlPanel state mapping.

We bypass CoordinatorEntity.__init__ with object.__new__ so the test
doesn't need to stand up a full Home Assistant fixture. The mapping
from Ajax SecurityState to HA AlarmControlPanelState is pure logic
the entity wraps — pinning it here catches the silent regression
where a new SecurityState slips into the API but the entity reports
the wrong HA state (or worse, None, which surfaces as 'unknown').
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.alarm_control_panel import AlarmControlPanelState

from custom_components.ajax.alarm_control_panel import (
    AjaxAlarmControlPanel,
    AjaxGroupAlarmControlPanel,
)
from custom_components.ajax.models import GroupState, SecurityState


def _panel(security_state: SecurityState | None, *, has_space: bool = True) -> AjaxAlarmControlPanel:
    """Build a panel stub wired to a fake coordinator → space."""
    panel = object.__new__(AjaxAlarmControlPanel)
    space = SimpleNamespace(security_state=security_state, name="Maison")
    coordinator = SimpleNamespace(get_space=lambda sid: space if has_space else None)
    panel.coordinator = coordinator
    panel._space_id = "space1"
    return panel


@pytest.mark.parametrize(
    "ajax_state,ha_state",
    [
        (SecurityState.DISARMED, AlarmControlPanelState.DISARMED),
        (SecurityState.ARMED, AlarmControlPanelState.ARMED_AWAY),
        (SecurityState.NIGHT_MODE, AlarmControlPanelState.ARMED_NIGHT),
        (SecurityState.PARTIALLY_ARMED, AlarmControlPanelState.ARMED_HOME),
        (SecurityState.AWAITING_EXIT_TIMER, AlarmControlPanelState.ARMING),
        (SecurityState.AWAITING_CONFIRMATION, AlarmControlPanelState.PENDING),
        (SecurityState.ARMING_INCOMPLETE, AlarmControlPanelState.ARMING),
        (SecurityState.TRIGGERED, AlarmControlPanelState.TRIGGERED),
    ],
)
def test_alarm_state_maps_every_known_security_state(
    ajax_state: SecurityState, ha_state: AlarmControlPanelState
) -> None:
    assert _panel(ajax_state).alarm_state is ha_state


def test_alarm_state_returns_none_for_unknown_state() -> None:
    """Unknown SecurityState (e.g. SecurityState.NONE) must NOT default to DISARMED.

    Reporting DISARMED when we don't actually know the state would mislead
    automations that arm/disarm based on the panel — HA renders None as
    `unknown`, which is the safer signal.
    """
    assert _panel(SecurityState.NONE).alarm_state is None


def test_alarm_state_returns_none_when_space_missing() -> None:
    """A coordinator that lost the space (e.g. user removed it) returns None."""
    assert _panel(SecurityState.ARMED, has_space=False).alarm_state is None


# ---------------------------------------------------------------------------
# Group-level panel
# ---------------------------------------------------------------------------


def _group_panel(group_state: GroupState | None) -> AjaxGroupAlarmControlPanel:
    panel = object.__new__(AjaxGroupAlarmControlPanel)
    group = SimpleNamespace(state=group_state, name="Living Room", id="g1") if group_state is not None else None
    coordinator = SimpleNamespace(get_group=lambda _sid, _gid: group)
    panel.coordinator = coordinator
    panel._space_id = "space1"
    panel._group_id = "g1"
    return panel


def test_group_alarm_state_armed() -> None:
    assert _group_panel(GroupState.ARMED).alarm_state is AlarmControlPanelState.ARMED_AWAY


def test_group_alarm_state_disarmed() -> None:
    assert _group_panel(GroupState.DISARMED).alarm_state is AlarmControlPanelState.DISARMED


def test_group_alarm_state_returns_none_when_group_removed() -> None:
    """If the group disappears from the space (deleted in Ajax), report None."""
    assert _group_panel(None).alarm_state is None

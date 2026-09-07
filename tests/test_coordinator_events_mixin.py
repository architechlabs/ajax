"""Tests for AjaxEventDispatchMixin.

The mixin emits HA bus events on security state changes
(``ajax_armed``/``ajax_disarmed``/``ajax_armed_night``/...). A bug
here means automations that listen to these events silently stop
firing — pinning the routing per state is the only way to catch it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.ajax._coordinator_events import AjaxEventDispatchMixin
from custom_components.ajax.models import AjaxGroup, AjaxSpace, GroupState, SecurityState


def _make_mixin() -> AjaxEventDispatchMixin:
    mixin = object.__new__(AjaxEventDispatchMixin)
    mixin.hass = MagicMock()
    return mixin


def _make_space(
    *,
    group_mode: bool = False,
    groups: dict[str, AjaxGroup] | None = None,
) -> AjaxSpace:
    space = AjaxSpace(id="s1", name="Maison", hub_id="hub1", security_state=SecurityState.DISARMED)
    space.group_mode_enabled = group_mode
    if groups:
        space.groups = groups
    return space


# ---------------------------------------------------------------------------
# Event name routing
# ---------------------------------------------------------------------------


def test_fire_security_state_event_routes_armed_to_ajax_armed() -> None:
    mixin = _make_mixin()
    space = _make_space()
    mixin._fire_security_state_event(space, SecurityState.DISARMED, SecurityState.ARMED)
    name, payload = mixin.hass.bus.async_fire.call_args[0]
    assert name == "ajax_armed"
    assert payload["new_state"] == SecurityState.ARMED.value
    assert payload["old_state"] == SecurityState.DISARMED.value


def test_fire_security_state_event_routes_disarmed_to_ajax_disarmed() -> None:
    mixin = _make_mixin()
    space = _make_space()
    mixin._fire_security_state_event(space, SecurityState.ARMED, SecurityState.DISARMED)
    assert mixin.hass.bus.async_fire.call_args[0][0] == "ajax_disarmed"


def test_fire_security_state_event_routes_night_mode_to_ajax_armed_night() -> None:
    mixin = _make_mixin()
    space = _make_space()
    mixin._fire_security_state_event(space, SecurityState.DISARMED, SecurityState.NIGHT_MODE)
    assert mixin.hass.bus.async_fire.call_args[0][0] == "ajax_armed_night"


def test_fire_security_state_event_routes_partial_to_ajax_armed_home() -> None:
    mixin = _make_mixin()
    space = _make_space()
    mixin._fire_security_state_event(space, SecurityState.DISARMED, SecurityState.PARTIALLY_ARMED)
    assert mixin.hass.bus.async_fire.call_args[0][0] == "ajax_armed_home"


def test_fire_security_state_event_falls_back_to_generic_event() -> None:
    """Unknown / transient states fire a generic event so we don't drop signal entirely."""
    mixin = _make_mixin()
    space = _make_space()
    mixin._fire_security_state_event(space, SecurityState.ARMED, SecurityState.TRIGGERED)
    assert mixin.hass.bus.async_fire.call_args[0][0] == "ajax_security_state_changed"


# ---------------------------------------------------------------------------
# Event payload contents
# ---------------------------------------------------------------------------


def test_fire_security_state_event_includes_space_metadata() -> None:
    mixin = _make_mixin()
    space = _make_space()
    mixin._fire_security_state_event(space, SecurityState.DISARMED, SecurityState.ARMED)
    payload = mixin.hass.bus.async_fire.call_args[0][1]
    assert payload["space_id"] == "s1"
    assert payload["space_name"] == "Maison"
    assert "timestamp" in payload
    # Source fields not added when not provided — avoids junk like None values in automations.
    assert "source_name" not in payload
    assert "source_type" not in payload


def test_fire_security_state_event_attaches_source_when_provided() -> None:
    mixin = _make_mixin()
    space = _make_space()
    mixin._fire_security_state_event(
        space, SecurityState.DISARMED, SecurityState.ARMED, source_name="Stéphane", source_type="USER"
    )
    payload = mixin.hass.bus.async_fire.call_args[0][1]
    assert payload["source_name"] == "Stéphane"
    assert payload["source_type"] == "USER"


def test_fire_security_state_event_lists_armed_and_disarmed_groups() -> None:
    """In group mode, downstream consumers need to know which groups changed."""
    g_armed = AjaxGroup(id="g1", name="Ground", space_id="s1", state=GroupState.ARMED)
    g_disarmed = AjaxGroup(id="g2", name="Upstairs", space_id="s1", state=GroupState.DISARMED)
    space = _make_space(group_mode=True, groups={"g1": g_armed, "g2": g_disarmed})

    mixin = _make_mixin()
    mixin._fire_security_state_event(space, SecurityState.DISARMED, SecurityState.PARTIALLY_ARMED)
    payload = mixin.hass.bus.async_fire.call_args[0][1]
    assert payload["group_mode"] is True
    assert payload["armed_groups"] == ["Ground"]
    assert payload["disarmed_groups"] == ["Upstairs"]


def test_fire_security_state_event_marks_group_mode_false_when_disabled() -> None:
    mixin = _make_mixin()
    space = _make_space(group_mode=False)
    mixin._fire_security_state_event(space, SecurityState.DISARMED, SecurityState.ARMED)
    payload = mixin.hass.bus.async_fire.call_args[0][1]
    assert payload["group_mode"] is False
    assert "armed_groups" not in payload  # not relevant


# ---------------------------------------------------------------------------
# _create_event_from_state_change
# ---------------------------------------------------------------------------


def test_create_event_inserts_into_space_history_capped_at_10() -> None:
    mixin = _make_mixin()
    mixin.hass.config = MagicMock()
    mixin.hass.config.language = "fr"
    space = _make_space()
    space.recent_events = [{"action": f"event_{i}"} for i in range(15)]

    mixin._create_event_from_state_change(space, SecurityState.DISARMED, SecurityState.ARMED)

    assert len(space.recent_events) == 10  # capped
    assert space.recent_events[0]["action"] == "armed"  # newest first


def test_create_event_fires_corresponding_bus_event() -> None:
    """The history entry + bus event are coupled — both must fire together."""
    mixin = _make_mixin()
    mixin.hass.config = MagicMock()
    mixin.hass.config.language = "en"
    space = _make_space()
    mixin._create_event_from_state_change(space, SecurityState.DISARMED, SecurityState.NIGHT_MODE)

    # History entry recorded
    assert space.recent_events[0]["action"] == "night_mode"
    # Bus event fired
    assert mixin.hass.bus.async_fire.call_args[0][0] == "ajax_armed_night"


# ---------------------------------------------------------------------------
# _escape_markdown
# ---------------------------------------------------------------------------


def test_escape_markdown_handles_none() -> None:
    assert AjaxEventDispatchMixin._escape_markdown(None) == ""


def test_escape_markdown_escapes_special_chars() -> None:
    """Persistent notifications use markdown — user names must be escaped to avoid
    accidental bold/italic injection.
    """
    out = AjaxEventDispatchMixin._escape_markdown("*foo*")
    assert "*" not in out or "\\*" in out

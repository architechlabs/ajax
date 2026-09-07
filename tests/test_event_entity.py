"""Tests for AjaxEventEntity.

The event entity routes ``coordinator.fire(event_type)`` calls into HA's
event timeline. Critical contract: a fire with an event_type NOT in the
declared `_attr_event_types` must be a no-op (silent drop) — without
this guard, a typo in the SSE/SQS handler would leak unrelated events
into the entity's history.

Also pins the dispatch-map registration (the coordinator looks up
entities by unique_id when an SSE event lands).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from custom_components.ajax.event import AjaxEventEntity


def _make_event(*, event_types: tuple[str, ...] = ("ring",)) -> AjaxEventEntity:
    entity = object.__new__(AjaxEventEntity)
    entity._space_id = "s1"
    entity._device_id = "d1"
    entity._event_key = "doorbell"
    entity._event_desc = {"key": "doorbell", "event_types": list(event_types)}
    entity._attr_unique_id = "d1_doorbell"
    entity._attr_event_types = list(event_types)
    entity.hass = None  # async_write_ha_state is guarded by `if self.hass is not None`
    entity._trigger_event = MagicMock()  # the EventEntity superclass attribute we exercise
    entity.coordinator = SimpleNamespace(_event_entities={})
    return entity


def test_fire_known_event_type_triggers_event() -> None:
    entity = _make_event(event_types=("ring", "press"))
    entity.fire("ring")
    entity._trigger_event.assert_called_once_with("ring", None)


def test_fire_unknown_event_type_is_silent_no_op() -> None:
    """A typo'd event_type from SSE must NOT leak into the entity's timeline."""
    entity = _make_event(event_types=("ring",))
    entity.fire("totally_not_a_real_event")
    entity._trigger_event.assert_not_called()


def test_fire_passes_event_attributes() -> None:
    entity = _make_event(event_types=("motion",))
    entity.fire("motion", {"rule": "human_detected"})
    entity._trigger_event.assert_called_once_with("motion", {"rule": "human_detected"})


def test_fire_does_not_write_ha_state_when_hass_missing() -> None:
    """Edge case: hass=None means the entity isn't attached — must not crash on async_write_ha_state."""
    entity = _make_event(event_types=("ring",))
    entity.hass = None
    # Must not raise even though async_write_ha_state would crash on a None hass.
    entity.fire("ring")


@pytest.mark.asyncio
async def test_async_added_to_hass_registers_in_dispatch_map() -> None:
    """The coordinator looks up entities by unique_id when an SSE event lands —
    if we forget to register here, the entity stays mute.
    """
    entity = _make_event()
    # Stub the superclass call so we don't need the HA fixture.
    from unittest.mock import AsyncMock, patch

    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
        new=AsyncMock(),
    ):
        await entity.async_added_to_hass()
    assert entity.coordinator._event_entities == {"d1_doorbell": entity}


@pytest.mark.asyncio
async def test_async_will_remove_from_hass_unregisters_from_dispatch_map() -> None:
    entity = _make_event()
    entity.coordinator._event_entities["d1_doorbell"] = entity

    from unittest.mock import AsyncMock, patch

    with patch(
        "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_will_remove_from_hass",
        new=AsyncMock(),
    ):
        await entity.async_will_remove_from_hass()
    assert "d1_doorbell" not in entity.coordinator._event_entities

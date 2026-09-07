"""Tests for AjaxHubTracker (GPS tracker entity for Ajax hub geofence).

This entity surfaces the hub's GPS coordinates and geofence radius on
the HA map. Bugs at this layer make the hub disappear from the map or
report wrong coordinates — both are user-visible failures the existing
test suite did not catch.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant.components.device_tracker import SourceType

from custom_components.ajax.device_tracker import AjaxHubTracker


def _make_tracker(hub_details: dict | None) -> AjaxHubTracker:
    tracker = object.__new__(AjaxHubTracker)
    tracker._space_id = "s1"
    space = SimpleNamespace(hub_details=hub_details, hub_id="hub1", name="Maison") if hub_details is not None else None
    tracker.coordinator = SimpleNamespace(get_space=lambda sid: space)
    return tracker


def test_source_type_is_gps() -> None:
    assert _make_tracker({"geoFence": {}}).source_type is SourceType.GPS


def test_latitude_longitude_returned_as_floats() -> None:
    tracker = _make_tracker({"geoFence": {"latitude": "48.8566", "longitude": "2.3522"}})
    assert tracker.latitude == pytest.approx(48.8566)
    assert tracker.longitude == pytest.approx(2.3522)


def test_latitude_returns_none_when_geofence_missing() -> None:
    tracker = _make_tracker({"geoFence": {}})
    assert tracker.latitude is None
    assert tracker.longitude is None


def test_latitude_returns_none_when_space_missing() -> None:
    tracker = _make_tracker(None)
    assert tracker.latitude is None


def test_latitude_returns_none_for_invalid_value() -> None:
    """Ajax sometimes ships malformed coords — must NOT crash the platform."""
    tracker = _make_tracker({"geoFence": {"latitude": "not-a-number", "longitude": None}})
    assert tracker.latitude is None
    assert tracker.longitude is None


def test_location_accuracy_reports_geofence_radius() -> None:
    tracker = _make_tracker({"geoFence": {"radiusMeters": "150"}})
    assert tracker.location_accuracy == 150


def test_location_accuracy_defaults_to_zero_when_missing() -> None:
    """Convention: 0 m radius == "no fence configured" (vs None which would invalidate the tracker)."""
    assert _make_tracker({"geoFence": {}}).location_accuracy == 0
    assert _make_tracker(None).location_accuracy == 0


def test_extra_state_attributes_includes_radius_and_ids() -> None:
    tracker = _make_tracker({"geoFence": {"radiusMeters": 200}})
    attrs = tracker.extra_state_attributes
    assert attrs == {"radius_meters": 200, "space_id": "s1", "hub_id": "hub1"}


def test_extra_state_attributes_empty_when_space_missing() -> None:
    """A removed space returns an empty dict, not a KeyError."""
    assert _make_tracker(None).extra_state_attributes == {}

"""Extra coverage for AjaxSpace / AjaxAccount query helpers.

The helpers below back the diagnostics sensors and the
get_recording_nvr_id NVR-channel routing. They're small and pure but
every entity that exposes a derived count or a recording link goes
through them — a subtle bug here mis-counts thousands of installations.
"""

from __future__ import annotations

from datetime import datetime

from custom_components.ajax.models import (
    AjaxDevice,
    AjaxNotification,
    AjaxSpace,
    AjaxVideoEdge,
    DeviceType,
    NotificationType,
    SecurityState,
    VideoEdgeType,
)


def _dev(dev_id: str, **kwargs) -> AjaxDevice:
    return AjaxDevice(
        id=dev_id,
        name=dev_id,
        type=DeviceType.MOTION_DETECTOR,
        space_id="s1",
        hub_id="hub1",
        **kwargs,
    )


def _space() -> AjaxSpace:
    return AjaxSpace(id="s1", name="Test", hub_id="hub1", security_state=SecurityState.DISARMED)


# ---------------------------------------------------------------------------
# Filter / query helpers
# ---------------------------------------------------------------------------


def test_get_devices_in_room_filters_by_room_id() -> None:
    space = _space()
    space.devices["a"] = _dev("a", room_id="kitchen")
    space.devices["b"] = _dev("b", room_id="bedroom")
    space.devices["c"] = _dev("c", room_id="kitchen")

    matches = {d.id for d in space.get_devices_in_room("kitchen")}
    assert matches == {"a", "c"}


def test_get_online_devices_excludes_offline() -> None:
    space = _space()
    space.devices["a"] = _dev("a", online=True)
    space.devices["b"] = _dev("b", online=False)
    assert [d.id for d in space.get_online_devices()] == ["a"]


def test_get_devices_with_malfunctions_handles_list_and_int_payloads() -> None:
    """Ajax sometimes ships the count as int, sometimes as a list of malfunction codes."""
    space = _space()
    space.devices["int_zero"] = _dev("int_zero", malfunctions=0)
    space.devices["int_one"] = _dev("int_one", malfunctions=1)
    space.devices["list_empty"] = _dev("list_empty", malfunctions=[])
    space.devices["list_some"] = _dev("list_some", malfunctions=["bat_low"])

    assert {d.id for d in space.get_devices_with_malfunctions()} == {"int_one", "list_some"}


def test_get_bypassed_devices_filters_by_flag() -> None:
    space = _space()
    space.devices["a"] = _dev("a", bypassed=False)
    space.devices["b"] = _dev("b", bypassed=True)
    assert [d.id for d in space.get_bypassed_devices()] == ["b"]


def test_get_devices_by_type_filters_by_type_enum() -> None:
    space = _space()
    space.devices["motion"] = _dev("motion")
    door = AjaxDevice(id="door", name="door", type=DeviceType.DOOR_CONTACT, space_id="s1", hub_id="hub1")
    space.devices["door"] = door
    assert [d.id for d in space.get_devices_by_type(DeviceType.DOOR_CONTACT)] == ["door"]


def test_get_unread_notifications_excludes_read() -> None:
    space = _space()
    now = datetime.now()
    space.notifications = [
        AjaxNotification(
            id="n1", space_id="s1", type=NotificationType.INFO, title="a", message="x", timestamp=now, read=True
        ),
        AjaxNotification(
            id="n2", space_id="s1", type=NotificationType.INFO, title="b", message="y", timestamp=now, read=False
        ),
    ]
    assert [n.id for n in space.get_unread_notifications()] == ["n2"]


# ---------------------------------------------------------------------------
# NVR channel routing
# ---------------------------------------------------------------------------


def _camera() -> AjaxVideoEdge:
    return AjaxVideoEdge(id="cam1", name="Front Door", space_id="s1", video_edge_type=VideoEdgeType.BULLET)


def _nvr_linking(camera_id: str) -> AjaxVideoEdge:
    """NVR with channel 0 wired to ``camera_id`` via sourceAliases."""
    nvr = AjaxVideoEdge(id="nvr1", name="NVR", space_id="s1", video_edge_type=VideoEdgeType.NVR)
    nvr.channels = [
        {
            "id": "0",
            "sourceAliases": {
                "sources": [
                    {"sourceType": "PRIMARY", "videoEdgeId": camera_id, "type": "BULLET"},
                ]
            },
        }
    ]
    return nvr


def test_get_recording_nvr_id_returns_the_linked_nvr() -> None:
    space = _space()
    space.video_edges["cam1"] = _camera()
    space.video_edges["nvr1"] = _nvr_linking("cam1")
    assert space.get_recording_nvr_id("cam1") == "nvr1"


def test_get_recording_nvr_id_returns_none_for_unlinked_camera() -> None:
    space = _space()
    space.video_edges["cam1"] = _camera()
    space.video_edges["nvr1"] = _nvr_linking("other_cam_id")
    assert space.get_recording_nvr_id("cam1") is None


def test_get_recording_nvr_id_returns_none_when_camera_is_itself_an_nvr() -> None:
    """An NVR is never recorded by another NVR — short-circuit."""
    space = _space()
    space.video_edges["nvr1"] = _nvr_linking("cam1")
    assert space.get_recording_nvr_id("nvr1") is None


def test_get_recording_nvr_id_tolerates_malformed_channel_payloads() -> None:
    """Channels with non-dict source entries / missing sourceAliases must not crash."""
    space = _space()
    space.video_edges["cam1"] = _camera()
    nvr = AjaxVideoEdge(id="nvr1", name="NVR", space_id="s1", video_edge_type=VideoEdgeType.NVR)
    nvr.channels = [
        "garbage",  # type: ignore[list-item]
        {"id": "0"},  # No sourceAliases at all
        {"id": "1", "sourceAliases": "not-a-dict"},
        {"id": "2", "sourceAliases": {"sources": "not-a-list"}},
        {"id": "3", "sourceAliases": {"sources": ["not-a-dict"]}},
    ]
    space.video_edges["nvr1"] = nvr
    assert space.get_recording_nvr_id("cam1") is None


# ---------------------------------------------------------------------------
# VideoEdgeHandler._get_channel_by_id — two-pass id-vs-index lookup
# ---------------------------------------------------------------------------


def test_get_channel_by_id_prefers_explicit_id_over_position() -> None:
    """Regression (code-review): channels can arrive out of positional order
    (e.g. after a camera is removed). An explicit-id match must win over a
    same-iteration index fallback so we never return the wrong channel.
    """
    from custom_components.ajax.devices import VideoEdgeHandler

    nvr = AjaxVideoEdge(id="nvr1", name="NVR", space_id="s1", video_edge_type=VideoEdgeType.NVR)
    # Position 0 holds id "2"; the channel whose id is "0" sits at position 1.
    nvr.channels = [
        {"id": "2", "marker": "wrong"},
        {"id": "0", "marker": "right"},
    ]
    handler = VideoEdgeHandler(nvr)
    channel = handler._get_channel_by_id("0")
    assert channel is not None
    assert channel["marker"] == "right"


def test_get_channel_by_id_falls_back_to_index_when_no_explicit_id() -> None:
    """Channels created without an explicit "id" are addressed by position."""
    from custom_components.ajax.devices import VideoEdgeHandler

    nvr = AjaxVideoEdge(id="nvr1", name="NVR", space_id="s1", video_edge_type=VideoEdgeType.NVR)
    nvr.channels = [{"marker": "first"}, {"marker": "second"}]
    handler = VideoEdgeHandler(nvr)
    assert handler._get_channel_by_id("1") == {"marker": "second"}
    assert handler._get_channel_by_id("9") is None

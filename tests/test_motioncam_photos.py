"""Parser tests for MotionCam hub-log photo bursts."""

from __future__ import annotations

from custom_components.ajax._motioncam_photos import (
    is_motioncam_raw_type,
    parse_latest_photo_burst,
    parse_photo_bursts,
)


def test_motioncam_raw_type_matches_photo_models_only() -> None:
    assert is_motioncam_raw_type("MotionCamPhod")
    assert is_motioncam_raw_type("MotionCam PhOD")
    assert is_motioncam_raw_type("superior_motion_cam_s_phod")
    assert is_motioncam_raw_type("MotionCamOutdoor")
    assert not is_motioncam_raw_type("MotionProtect")
    assert not is_motioncam_raw_type("DoorProtect")
    assert not is_motioncam_raw_type(None)
    assert not is_motioncam_raw_type("")


def _entry(
    device_id: str,
    event_id: str,
    timestamp: int,
    links: list[dict[str, str]],
    *,
    version: str = "v1",
) -> dict[str, object]:
    payload = {"orderedResourceLinks": links}
    entry: dict[str, object] = {
        "eventId": event_id,
        "sourceObjectId": device_id,
        "timestamp": timestamp,
    }
    if version == "v1":
        entry["additionalData"] = {"additionalDataType": "PHOTOS_RESOURCE_DESCRIPTION", **payload}
    else:
        entry["additionalDataV2"] = [{"additionalDataV2Type": "RESOURCE_DESCRIPTION", **payload}]
    return entry


def test_ready_burst_keeps_ordered_links_and_ignores_other_devices() -> None:
    logs = [
        _entry(
            "other",
            "old",
            50,
            [{"url": "https://cdn.example/other.jpg", "status": "READY"}],
        ),
        _entry(
            "abc",
            "burst",
            10,
            [
                {"url": "https://cdn.example/1.jpg", "status": "READY"},
                {"url": "https://cdn.example/2.jpg", "status": "READY"},
            ],
        ),
    ]
    burst = parse_latest_photo_burst(logs, "ABC")
    assert burst is not None
    assert burst.event_id == "burst"
    assert burst.ready_urls == ("https://cdn.example/1.jpg", "https://cdn.example/2.jpg")
    assert burst.in_progress is False


def test_failed_links_are_dropped_and_in_progress_is_kept() -> None:
    logs = [
        _entry(
            "dev",
            "mix",
            1,
            [
                {"url": "https://cdn.example/ok.jpg", "status": "READY"},
                {"url": "https://cdn.example/bad.jpg", "status": "FAILED"},
                {"url": "https://cdn.example/soon.jpg", "status": "IN_PROGRESS"},
                {"url": "", "status": "READY"},
            ],
        )
    ]
    burst = parse_latest_photo_burst(logs, "dev")
    assert burst is not None
    assert [(link.status, link.url) for link in burst.links] == [
        ("READY", "https://cdn.example/ok.jpg"),
        ("IN_PROGRESS", "https://cdn.example/soon.jpg"),
    ]
    assert burst.in_progress is True
    assert burst.ready_urls == ("https://cdn.example/ok.jpg",)


def test_v2_resource_description_is_used_when_v1_is_absent() -> None:
    logs = [
        _entry(
            "dev",
            "v2",
            5,
            [{"url": "https://cdn.example/v2.jpg", "status": "READY"}],
            version="v2",
        )
    ]
    burst = parse_latest_photo_burst(logs, "dev")
    assert burst is not None
    assert burst.event_id == "v2"
    assert burst.ready_urls == ("https://cdn.example/v2.jpg",)


def test_newest_timestamp_wins_and_burst_is_capped_at_five() -> None:
    links = [{"url": f"https://cdn.example/{index}.jpg", "status": "READY"} for index in range(6)]
    logs = [
        _entry("dev", "older", 10, [{"url": "https://cdn.example/old.jpg", "status": "READY"}]),
        _entry("dev", "newer", 20, links),
    ]
    burst = parse_latest_photo_burst(logs, "dev")
    assert burst is not None
    assert burst.event_id == "newer"
    assert len(burst.links) == 5
    assert burst.ready_urls[-1] == "https://cdn.example/4.jpg"


def test_parse_photo_bursts_returns_every_burst_newest_first() -> None:
    logs = [
        _entry("dev", "older", 10, [{"url": "https://cdn.example/old.jpg", "status": "READY"}]),
        _entry("other", "elsewhere", 99, [{"url": "https://cdn.example/nope.jpg", "status": "READY"}]),
        _entry("dev", "fail", 30, [{"url": "https://cdn.example/bad.jpg", "status": "FAILED"}]),
        _entry("dev", "newer", 20, [{"url": "https://cdn.example/new.jpg", "status": "READY"}]),
    ]
    bursts = parse_photo_bursts(logs, "dev")
    assert [burst.event_id for burst in bursts] == ["newer", "older"]
    assert bursts[0].ready_urls == ("https://cdn.example/new.jpg",)


def test_non_list_and_failed_only_return_none() -> None:
    assert parse_latest_photo_burst({"nope": True}, "dev") is None
    assert parse_latest_photo_burst([], "dev") is None
    logs = [_entry("dev", "fail", 1, [{"url": "https://cdn.example/x.jpg", "status": "FAILED"}])]
    assert parse_latest_photo_burst(logs, "dev") is None

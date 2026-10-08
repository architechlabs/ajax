"""Gallery tests for saved MotionCam photos."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ajax.api import AjaxRestApi
from custom_components.ajax.image import (
    GalleryFrame,
    MotionCamPhotoStore,
    async_remove_photo_entities,
    photo_file_response,
    render_photo_gallery,
    resolve_photo_file,
)
from custom_components.ajax.models import AjaxAccount, AjaxDevice, AjaxSpace, DeviceType

_JPEG_A = b"\xff\xd8\xff\xe0frame-a"
_JPEG_B = b"\xff\xd8\xff\xe0frame-b"


def _device(raw_type: str, device_id: str = "dev1") -> AjaxDevice:
    return AjaxDevice(
        id=device_id,
        name="Motion and Cam",
        type=DeviceType.MOTION_DETECTOR,
        space_id="space1",
        hub_id="hub1",
        raw_type=raw_type,
    )


def _account(*devices: AjaxDevice) -> AjaxAccount:
    space = AjaxSpace(id="space1", name="Home", hub_id="hub1")
    for device in devices:
        space.devices[device.id] = device
    return AjaxAccount(user_id="user", name="User", email="user@example.com", spaces={"space1": space})


def _session(bodies: list[bytes]) -> MagicMock:
    session = MagicMock()
    contexts = []
    for body in bodies:
        response = MagicMock()
        response.status = 200
        response.content.read = AsyncMock(return_value=body)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        contexts.append(context)
    session.get = MagicMock(side_effect=contexts)
    return session


def _api(logs: list[dict[str, object]], bodies: list[bytes]) -> MagicMock:
    api = MagicMock()
    api.async_get_hub_logs = AsyncMock(return_value=logs)
    api.async_get_camera_snapshot = AsyncMock()
    api.session_token = "token"
    api._base_headers = {"X-Api-Key": "key"}
    api._build_url = AjaxRestApi._build_url
    api._get_session = AsyncMock(return_value=_session(bodies))
    return api


def _store(api: MagicMock, account: AjaxAccount, root: Path) -> tuple[MotionCamPhotoStore, MagicMock, SimpleNamespace]:
    hass = SimpleNamespace(
        bus=SimpleNamespace(async_fire=MagicMock()),
        config=SimpleNamespace(path=lambda *parts: str(root.joinpath(*parts))),
        data={},
    )
    coordinator = SimpleNamespace(account=account, api=api, hass=hass, entry_id="entry", last_update_success=True)
    added = MagicMock()
    store = MotionCamPhotoStore(coordinator, added)
    return store, added, hass


def _logs(
    event_id: str,
    urls: list[str],
    *,
    timestamp: int = 100,
    in_progress: bool = False,
) -> list[dict[str, object]]:
    links = [{"url": url, "status": "READY"} for url in urls]
    if in_progress:
        links.append({"url": "https://cdn.example/later.jpg", "status": "IN_PROGRESS"})
    return [
        {
            "eventId": event_id,
            "sourceObjectId": "dev1",
            "timestamp": timestamp,
            "additionalData": {
                "additionalDataType": "PHOTOS_RESOURCE_DESCRIPTION",
                "orderedResourceLinks": links,
            },
        }
    ]


def _photo_path(root: Path, timestamp: int = 100, frame: int = 1) -> Path:
    return root / "media" / "ajax_photos" / "dev1" / f"{timestamp}_{frame}.jpg"


async def test_motion_protect_does_not_fetch_logs_or_snapshots(tmp_path: Path) -> None:
    api = _api([], [])
    store, added, _hass = _store(api, _account(_device("MotionProtect")), tmp_path)
    await store.async_refresh()
    api.async_get_hub_logs.assert_not_awaited()
    api.async_get_camera_snapshot.assert_not_awaited()
    added.assert_not_called()


async def test_ready_burst_writes_files_and_skips_snapshot_api(tmp_path: Path) -> None:
    logs = _logs("burst-1", ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"])
    api = _api(logs, [_JPEG_A, _JPEG_B])
    store, added, hass = _store(api, _account(_device("MotionCamPhod")), tmp_path)
    await store.async_refresh()
    api.async_get_camera_snapshot.assert_not_awaited()
    added.assert_not_called()
    assert _photo_path(tmp_path, frame=1).read_bytes() == _JPEG_A
    assert _photo_path(tmp_path, frame=2).read_bytes() == _JPEG_B
    assert api._get_session.return_value.get.call_args_list[0].kwargs["headers"] is None
    hass.bus.async_fire.assert_not_called()

    downloads = api._get_session.return_value.get.call_count
    await store.async_refresh()
    assert api._get_session.return_value.get.call_count == downloads


async def test_in_progress_burst_retries_past_the_log_cache(tmp_path: Path) -> None:
    logs = _logs("burst-1", ["https://cdn.example/1.jpg"], in_progress=True)
    api = _api(logs, [_JPEG_A, _JPEG_A])
    store, added, _hass = _store(api, _account(_device("MotionCamPhod")), tmp_path)
    await store.async_refresh()
    after_first = api.async_get_hub_logs.await_count
    await store.async_refresh()
    assert api.async_get_hub_logs.await_count == after_first + 1
    assert api._get_session.return_value.get.call_count == 1
    added.assert_not_called()
    api.async_get_camera_snapshot.assert_not_awaited()


async def test_new_burst_after_startup_fires_activity_event(tmp_path: Path) -> None:
    api = _api(_logs("burst-1", ["https://cdn.example/1.jpg"], timestamp=100), [_JPEG_A])
    store, _added, hass = _store(api, _account(_device("MotionCamPhod")), tmp_path)
    with patch("custom_components.ajax.image.motion_entity_id", return_value="binary_sensor.motion_and_cam"):
        await store.async_refresh()
        api.async_get_hub_logs.return_value = _logs("burst-2", ["https://cdn.example/2.jpg"], timestamp=200)
        api._get_session = AsyncMock(return_value=_session([_JPEG_B]))
        store._logs_cache.clear()
        await store.async_refresh()
    hass.bus.async_fire.assert_called_once()
    event_type, payload = hass.bus.async_fire.call_args.args
    assert event_type == "ajax_motioncam_photo"
    assert payload["entity_id"] == "binary_sensor.motion_and_cam"
    assert payload["device_name"] == "Motion and Cam"
    assert payload["photo_count"] == 1
    assert _photo_path(tmp_path, timestamp=200).read_bytes() == _JPEG_B


async def test_relative_photo_url_uses_the_session_token(tmp_path: Path) -> None:
    logs = _logs("burst-1", ["user/USER/images/photo.jpg"])
    api = _api(logs, [_JPEG_A])
    store, _added, _hass = _store(api, _account(_device("MotionCamPhod")), tmp_path)
    await store.async_refresh()
    call = api._get_session.return_value.get.call_args
    assert call.args[0] == "https://api.ajax.systems/api/user/USER/images/photo.jpg"
    assert call.kwargs["headers"]["X-Session-Token"] == "token"
    assert call.kwargs["allow_redirects"] is False
    api.async_get_camera_snapshot.assert_not_awaited()


def test_download_response_is_an_attachment() -> None:
    response = photo_file_response(_JPEG_A, "100_1.jpg", download=True)
    assert response.body == _JPEG_A
    assert response.headers["Content-Disposition"] == 'attachment; filename="100_1.jpg"'
    assert response.content_type == "image/jpeg"


def test_resolve_photo_file_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "ajax_photos"
    assert resolve_photo_file(root, "..", "100_1.jpg") is None
    assert resolve_photo_file(root, "dev1", "../100_1.jpg") is None
    assert resolve_photo_file(root, "dev1", "100_1.jpg") == (root / "dev1" / "100_1.jpg").resolve()


def test_gallery_lists_a_download_for_each_photo() -> None:
    html = render_photo_gallery([GalleryFrame("dev1", "Motion and Cam", 1_700_000_000_000, 1, "1700000000000_1.jpg")])
    assert "Motion and Cam" in html
    assert "/api/ajax/photos/dev1/1700000000000_1.jpg?download=1" in html
    assert "Download" in html


def test_remove_photo_entities_drops_only_photo_rows() -> None:
    registry = MagicMock()
    photo = SimpleNamespace(entity_id="image.motion_photo_1", unique_id="entry_dev1_photo_1")
    battery = SimpleNamespace(entity_id="sensor.battery", unique_id="entry_dev1_battery")
    with (
        patch("custom_components.ajax.image.er.async_get", return_value=registry),
        patch("custom_components.ajax.image.er.async_entries_for_config_entry", return_value=[photo, battery]),
    ):
        async_remove_photo_entities(SimpleNamespace(), "entry")  # type: ignore[arg-type]
    registry.async_remove.assert_called_once_with("image.motion_photo_1")

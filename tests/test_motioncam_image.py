"""Image-platform tests for MotionCam photo bursts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.ajax.api import AjaxRestApi
from custom_components.ajax.image import AjaxMotionCamPhoto, MotionCamPhotoStore
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


def _store(
    api: MagicMock, account: AjaxAccount
) -> tuple[MotionCamPhotoStore, list[AjaxMotionCamPhoto], SimpleNamespace]:
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=MagicMock()))
    coordinator = SimpleNamespace(account=account, api=api, hass=hass, entry_id="entry", last_update_success=True)
    added: list[AjaxMotionCamPhoto] = []

    def _add(entities: list[AjaxMotionCamPhoto]) -> None:
        added.extend(entities)

    store = MotionCamPhotoStore(coordinator, _add)  # type: ignore[arg-type]
    return store, added, hass


def _logs(event_id: str, urls: list[str], *, in_progress: bool = False) -> list[dict[str, object]]:
    links = [{"url": url, "status": "READY"} for url in urls]
    if in_progress:
        links.append({"url": "https://cdn.example/later.jpg", "status": "IN_PROGRESS"})
    return [
        {
            "eventId": event_id,
            "sourceObjectId": "dev1",
            "timestamp": 100,
            "additionalData": {
                "additionalDataType": "PHOTOS_RESOURCE_DESCRIPTION",
                "orderedResourceLinks": links,
            },
        }
    ]


async def test_async_image_returns_cached_bytes() -> None:
    entity = object.__new__(AjaxMotionCamPhoto)
    entity._jpeg = _JPEG_A
    assert await entity.async_image() == _JPEG_A


async def test_motion_protect_does_not_fetch_logs_or_snapshots() -> None:
    api = _api([], [])
    store, added, _hass = _store(api, _account(_device("MotionProtect")))
    await store.async_refresh()
    api.async_get_hub_logs.assert_not_awaited()
    api.async_get_camera_snapshot.assert_not_awaited()
    assert added == []


async def test_ready_burst_caches_jpegs_and_skips_snapshot_api() -> None:
    logs = _logs("burst-1", ["https://cdn.example/1.jpg", "https://cdn.example/2.jpg"])
    api = _api(logs, [_JPEG_A, _JPEG_B])
    store, added, hass = _store(api, _account(_device("MotionCamPhod")))
    with patch("custom_components.ajax.image.ImageEntity.__init__", lambda self, hass, verify_ssl=False: None):
        await store.async_refresh()
    api.async_get_camera_snapshot.assert_not_awaited()
    assert [entity._frame for entity in added] == [1, 2]
    assert await added[0].async_image() == _JPEG_A
    assert await added[1].async_image() == _JPEG_B
    # Absolute CDN links are fetched with no Ajax credentials.
    assert api._get_session.return_value.get.call_args_list[0].kwargs["headers"] is None
    hass.bus.async_fire.assert_not_called()

    await store.async_refresh()
    assert api.async_get_hub_logs.await_count == 1


async def test_in_progress_burst_retries_past_the_log_cache() -> None:
    logs = _logs("burst-1", ["https://cdn.example/1.jpg"], in_progress=True)
    api = _api(logs, [_JPEG_A, _JPEG_A])
    store, added, _hass = _store(api, _account(_device("MotionCamPhod")))
    with patch("custom_components.ajax.image.ImageEntity.__init__", lambda self, hass, verify_ssl=False: None):
        await store.async_refresh()
        await store.async_refresh()
    assert api.async_get_hub_logs.await_count == 2
    assert len(added) == 1
    api.async_get_camera_snapshot.assert_not_awaited()


async def test_new_burst_after_startup_fires_activity_event() -> None:
    api = _api(_logs("burst-1", ["https://cdn.example/1.jpg"]), [_JPEG_A])
    store, added, hass = _store(api, _account(_device("MotionCamPhod")))
    with (
        patch("custom_components.ajax.image.ImageEntity.__init__", lambda self, hass, verify_ssl=False: None),
        patch.object(AjaxMotionCamPhoto, "async_write_ha_state", lambda self: None),
    ):
        await store.async_refresh()
        added[0].entity_id = "image.motion_and_cam_photo_1"
        api.async_get_hub_logs.return_value = _logs("burst-2", ["https://cdn.example/2.jpg"])
        api._get_session = AsyncMock(return_value=_session([_JPEG_B]))
        store._logs_cache.clear()
        await store.async_refresh()
    hass.bus.async_fire.assert_called_once()
    event_type, payload = hass.bus.async_fire.call_args.args
    assert event_type == "ajax_motioncam_photo"
    assert payload["entity_id"] == "image.motion_and_cam_photo_1"
    assert payload["device_name"] == "Motion and Cam"
    assert payload["photo_count"] == 1
    assert await added[0].async_image() == _JPEG_B


async def test_relative_photo_url_uses_the_session_token() -> None:
    logs = _logs("burst-1", ["user/USER/images/photo.jpg"])
    api = _api(logs, [_JPEG_A])
    store, _added, _hass = _store(api, _account(_device("MotionCamPhod")))
    with patch("custom_components.ajax.image.ImageEntity.__init__", lambda self, hass, verify_ssl=False: None):
        await store.async_refresh()
    call = api._get_session.return_value.get.call_args
    assert call.args[0] == "https://api.ajax.systems/api/user/USER/images/photo.jpg"
    assert call.kwargs["headers"]["X-Session-Token"] == "token"
    assert call.kwargs["allow_redirects"] is False
    api.async_get_camera_snapshot.assert_not_awaited()

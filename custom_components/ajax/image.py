"""Still-image entities for MotionCam alarm photos.

Video Edge cameras stay on the camera platform. MotionCam pictures are JPEGs
attached to hub log rows, so they are served from memory through Home
Assistant's image dialog (view and download) once the Ajax link expires.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import aiohttp
from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import AjaxConfigEntry
from ._ids import device_identifier, via_device_info
from ._motioncam_photos import (
    MAX_BURST_LINKS,
    PhotoBurst,
    is_motioncam_raw_type,
    parse_latest_photo_burst,
)
from .api import AjaxRestApiError
from .const import (
    AJAX_REST_API_TIMEOUT,
    EVENT_AJAX_MOTIONCAM_PHOTO,
    MANUFACTURER,
)
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1

LOG_CACHE_SECONDS = 20
RETRY_WINDOW_SECONDS = 60
POLL_INTERVAL = timedelta(seconds=15)
MAX_PHOTO_BYTES = 8 * 1024 * 1024
_JPEG_MAGIC = b"\xff\xd8"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MotionCam photo entities from a config entry."""
    coordinator = entry.runtime_data
    store = MotionCamPhotoStore(coordinator, async_add_entities)
    entry.async_on_unload(async_track_time_interval(hass, store.async_tick, POLL_INTERVAL))
    entry.async_create_background_task(hass, store.async_refresh(), "ajax_motioncam_photos")


class MotionCamPhotoStore:
    """Fetch hub logs once per hub and keep the latest JPEG burst per device."""

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Bind the store to one config entry's coordinator."""
        self.coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._entities: dict[tuple[str, int], AjaxMotionCamPhoto] = {}
        self._jpeg_by_url: dict[str, bytes] = {}
        self._logs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._retry_until: dict[tuple[str, str], float] = {}
        self._primed: set[str] = set()
        self._announced: dict[str, None] = {}
        self._latest_event: dict[str, tuple[str, int]] = {}

    async def async_tick(self, _now: datetime) -> None:
        """Periodic refresh. API errors stay inside ``async_refresh``."""
        await self.async_refresh()

    async def async_refresh(self) -> None:
        """Pull hub logs and update photo entities."""
        account = self.coordinator.account
        if account is None:
            return

        by_hub: dict[str, list[AjaxDevice]] = {}
        for space in account.spaces.values():
            for device in space.devices.values():
                if is_motioncam_raw_type(device.raw_type) and device.hub_id:
                    by_hub.setdefault(device.hub_id, []).append(device)

        for hub_id, devices in by_hub.items():
            logs = await self._hub_logs(hub_id)
            if logs is None:
                continue
            for device in devices:
                burst = parse_latest_photo_burst(logs, device.id)
                first_sight = device.id not in self._primed
                self._primed.add(device.id)
                if burst is None:
                    await self._clear_device(device.id)
                    continue
                self._track_retry(hub_id, burst)
                count = await self._apply_burst(device, burst)
                if first_sight:
                    # The log already contains this burst. Show it, but do not
                    # write a new Activity line for a photo taken before startup.
                    if count > 0:
                        self._announced[burst.event_id] = None
                    continue
                self._latest_event[device.id] = (burst.event_id, count)
                self._announce(device, burst.event_id, count)

    def try_announce(self, device_id: str) -> None:
        """Fire the activity line once the image entity has an entity id."""
        pending = self._latest_event.get(device_id)
        if pending is None:
            return
        device = self._device(device_id)
        if device is None:
            return
        self._announce(device, pending[0], pending[1])

    async def _hub_logs(self, hub_id: str) -> list[dict[str, Any]] | None:
        """Return cached logs, or None when the request failed."""
        now = time.monotonic()
        force = any(
            deadline > now for (cached_hub, _event_id), deadline in self._retry_until.items() if cached_hub == hub_id
        )
        cached = self._logs_cache.get(hub_id)
        if cached is not None and not force and (now - cached[0]) < LOG_CACHE_SECONDS:
            return cached[1]
        try:
            logs = await self.coordinator.api.async_get_hub_logs(hub_id)
        except Exception:
            _LOGGER.debug("MotionCam photo log fetch failed for hub %s", hub_id, exc_info=True)
            return None
        self._logs_cache[hub_id] = (now, logs)
        return logs

    def _track_retry(self, hub_id: str, burst: PhotoBurst) -> None:
        """Retry this hub for up to a minute while a burst is still transferring."""
        key = (hub_id, burst.event_id)
        if burst.in_progress:
            self._retry_until.setdefault(key, time.monotonic() + RETRY_WINDOW_SECONDS)
            return
        self._retry_until.pop(key, None)

    async def _apply_burst(self, device: AjaxDevice, burst: PhotoBurst) -> int:
        """Download READY frames and add or update their image entities."""
        count = 0
        seen: set[int] = set()
        for index, link in enumerate(burst.links, start=1):
            seen.add(index)
            jpeg: bytes | None = None
            if link.status == "READY" and link.url:
                jpeg = await self._download(link.url)
            if jpeg is None:
                await self._drop_frame(device.id, index)
                continue
            count += 1
            self._set_frame(device, index, jpeg)
        for index in range(1, MAX_BURST_LINKS + 1):
            if index not in seen:
                await self._drop_frame(device.id, index)
        return count

    def _set_frame(self, device: AjaxDevice, index: int, jpeg: bytes) -> None:
        """Create or update the entity for one frame."""
        key = (device.id, index)
        entity = self._entities.get(key)
        if entity is None:
            entity = AjaxMotionCamPhoto(
                coordinator=self.coordinator,
                store=self,
                space_id=device.space_id,
                device_id=device.id,
                frame=index,
            )
            entity.set_photo(jpeg)
            self._entities[key] = entity
            self._async_add_entities([entity])
            return
        entity.set_photo(jpeg)

    async def _drop_frame(self, device_id: str, index: int) -> None:
        """Remove a frame that the latest burst no longer has."""
        entity = self._entities.pop((device_id, index), None)
        if entity is not None and getattr(entity, "hass", None) is not None and _entity_id(entity) is not None:
            await entity.async_remove()

    async def _clear_device(self, device_id: str) -> None:
        """Drop every frame when the device has no photo burst."""
        for index in range(1, MAX_BURST_LINKS + 1):
            await self._drop_frame(device_id, index)

    def _announce(self, device: AjaxDevice, event_id: str, photo_count: int) -> None:
        """Write one Activity line per new burst, attached to the photo entity."""
        if not event_id or event_id in self._announced or photo_count < 1:
            return
        entity = self._announce_entity(device.id)
        if entity is None or entity.hass is None:
            self._latest_event[device.id] = (event_id, photo_count)
            return
        entity_id = _entity_id(entity)
        if entity_id is None:
            self._latest_event[device.id] = (event_id, photo_count)
            return
        self._announced[event_id] = None
        while len(self._announced) > 200:
            self._announced.pop(next(iter(self._announced)))
        entity.hass.bus.async_fire(
            EVENT_AJAX_MOTIONCAM_PHOTO,
            {
                "entity_id": entity_id,
                "device_name": device.name,
                "device_id": device.id,
                "photo_count": photo_count,
            },
        )

    def _announce_entity(self, device_id: str) -> AjaxMotionCamPhoto | None:
        """Return the first frame that Home Assistant has registered."""
        for index in range(1, MAX_BURST_LINKS + 1):
            entity = self._entities.get((device_id, index))
            if (
                entity is not None
                and _entity_id(entity) is not None
                and getattr(entity, "hass", None) is not None
                and entity._jpeg
            ):
                return entity
        return None

    def _device(self, device_id: str) -> AjaxDevice | None:
        """Look up a device across the account."""
        account = self.coordinator.account
        if account is None:
            return None
        for space in account.spaces.values():
            device = space.devices.get(device_id)
            if device is not None:
                return device
        return None

    async def _download(self, url: str) -> bytes | None:
        """Download a READY photo once and keep the JPEG until the process restarts."""
        cached = self._jpeg_by_url.get(url)
        if cached is not None:
            return cached
        target, headers = self._download_target(url)
        if target is None:
            return None
        api = self.coordinator.api
        try:
            session = await api._get_session()
            async with session.get(
                target,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
                allow_redirects=headers is None,
            ) as response:
                if response.status != 200:
                    _LOGGER.debug("MotionCam photo download returned HTTP %s", response.status)
                    return None
                data = await response.content.read(MAX_PHOTO_BYTES + 1)
        except (aiohttp.ClientError, TimeoutError, AjaxRestApiError):
            _LOGGER.debug("MotionCam photo download failed", exc_info=True)
            return None
        if len(data) > MAX_PHOTO_BYTES or not data.startswith(_JPEG_MAGIC):
            _LOGGER.debug("MotionCam photo download was not a JPEG")
            return None
        self._jpeg_by_url[url] = data
        while len(self._jpeg_by_url) > 64:
            self._jpeg_by_url.pop(next(iter(self._jpeg_by_url)))
        return data

    def _download_target(self, url: str) -> tuple[str | None, dict[str, str] | None]:
        """Split a resource link into a URL and, for relative links, auth headers.

        Absolute CDN links are fetched with no Ajax credentials. Relative links
        are joined to the Enterprise API and sent with the session token.
        """
        if not url or any(char in url for char in ("\n", "\r", "\x00")):
            return None, None
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.netloc:
            return url, None
        # Reject other schemes and protocol-relative URLs. A leading slash is a
        # relative Enterprise API path and is joined below.
        if parts.scheme or parts.netloc or url.startswith(("//", "\\")):
            return None, None
        api = self.coordinator.api
        token = api.session_token
        if not token:
            return None, None
        endpoint = url.lstrip("/")
        try:
            target = api._build_url(endpoint)
        except AjaxRestApiError:
            return None, None
        headers = {key: value for key, value in api._base_headers.items() if value is not None}
        headers["X-Session-Token"] = token
        return target, headers


class AjaxMotionCamPhoto(CoordinatorEntity[AjaxDataCoordinator], ImageEntity):
    """One frame of the latest MotionCam burst."""

    _attr_has_entity_name = True
    _attr_translation_key = "photo"
    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        store: MotionCamPhotoStore,
        space_id: str,
        device_id: str,
        frame: int,
    ) -> None:
        """Attach this frame to the MotionCam device."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self.hass = coordinator.hass
        self._store = store
        self._space_id = space_id
        self._device_id = device_id
        self._frame = frame
        self._jpeg: bytes | None = None
        self._attr_unique_id = f"{coordinator.entry_id}_{device_id}_photo_{frame}"
        self._attr_translation_placeholders = {"number": str(frame)}

    def set_photo(self, jpeg: bytes) -> None:
        """Replace the cached JPEG. Unchanged bytes do not refresh the dialog cache."""
        if jpeg == self._jpeg:
            return
        self._jpeg = jpeg
        self._attr_image_last_updated = dt_util.utcnow()
        # ``image_last_updated`` is a cached_property. Drop the cache so the
        # image proxy and the state timestamp follow this new JPEG.
        self.__dict__.pop("image_last_updated", None)
        if getattr(self, "hass", None) is not None and _entity_id(self) is not None:
            self.async_write_ha_state()
            self._store.try_announce(self._device_id)

    async def async_added_to_hass(self) -> None:
        """Register the entity, then emit the activity line if a burst is waiting."""
        await super().async_added_to_hass()
        self._store.try_announce(self._device_id)

    async def async_image(self) -> bytes | None:
        """Return the cached JPEG for this frame."""
        return self._jpeg

    @property
    def available(self) -> bool:
        """Return True when a photo is cached and the coordinator is healthy."""
        return self._jpeg is not None and super().available

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return the MotionCam device this photo belongs to."""
        device = self._device()
        if device is None:
            return None
        model_name = device.raw_type or device.type.value.replace("_", " ").title()
        if device.device_color:
            model_name = f"{model_name} ({str(device.device_color).title()})"
        return DeviceInfo(
            identifiers={device_identifier(self.coordinator.entry_id, self._device_id)},
            name=device.name,
            manufacturer=MANUFACTURER,
            model=model_name,
            **via_device_info(self.coordinator.hass, self.coordinator.entry_id, self._space_id),
            sw_version=device.firmware_version,
            hw_version=device.hardware_version,
            suggested_area=device.room_name,
        )

    def _device(self) -> AjaxDevice | None:
        """Return the live device from the coordinator."""
        space = self.coordinator.get_space(self._space_id)
        if space is None:
            return None
        return space.devices.get(self._device_id)


def _entity_id(entity: AjaxMotionCamPhoto) -> str | None:
    """Return the entity id once Home Assistant has assigned one.

    The attribute is ``None`` until the entity is added to the platform.
    """
    raw = getattr(entity, "entity_id", None)
    if isinstance(raw, str) and raw:
        return raw
    return None

"""MotionCam photo storage and the Ajax photos gallery.

Video Edge cameras stay on the camera platform. MotionCam pictures are JPEGs
attached to hub log rows. Home Assistant lists image entities inside the
Sensors card, so this platform saves the files and serves them from a
separate sidebar gallery instead of publishing sensor rows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp import web
from homeassistant.components import frontend
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.panel_custom import async_register_panel
from homeassistant.components.persistent_notification import async_create
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.http import HomeAssistantView

from . import AjaxConfigEntry
from ._ids import find_device
from ._motioncam_photos import PhotoBurst, is_motioncam_raw_type, parse_photo_bursts
from .api import AjaxRestApiError
from .const import AJAX_REST_API_TIMEOUT, EVENT_AJAX_MOTIONCAM_PHOTO
from .coordinator import AjaxDataCoordinator
from .models import AjaxDevice

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 1

LOG_CACHE_SECONDS = 5
RETRY_WINDOW_SECONDS = 60
POLL_INTERVAL = timedelta(seconds=5)
_NOTIFICATION_TTL = timedelta(hours=12)
MAX_PHOTO_BYTES = 8 * 1024 * 1024
MAX_PHOTOS_PER_DEVICE = 100
BACKFILL_PAGES = 5
_JPEG_MAGIC = b"\xff\xd8"
_DEVICE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_FILENAME = re.compile(r"^[0-9]+_[1-5]\.jpg$")
_PHOTO_ENTITY = re.compile(r"_photo_[0-9]+$")
_PANEL_KEY = "ajax_photo_panel_registered"

_GALLERY_URL = "/api/ajax/photos"
_PANEL_PATH = "ajax-photos"
_GALLERY_DEVICE_URL = "homeassistant://ajax-photos"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AjaxConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Save MotionCam photos and serve the gallery. No image entities are added."""
    async_remove_photo_entities(hass, entry.entry_id)
    await _async_register_gallery(hass)
    coordinator = entry.runtime_data
    async_expose_photo_gallery(hass, coordinator)
    store = MotionCamPhotoStore(coordinator, async_add_entities)
    entry.async_on_unload(async_track_time_interval(hass, store.async_tick, POLL_INTERVAL))
    entry.async_create_background_task(hass, store.async_refresh(), "ajax_motioncam_photos")


def async_remove_photo_entities(hass: HomeAssistant, entry_id: str) -> None:
    """Drop Photo 1/2/3 rows left by the earlier image entities."""
    try:
        registry = er.async_get(hass)
    except (KeyError, TypeError, AttributeError, RuntimeError):
        return
    for entity in list(er.async_entries_for_config_entry(registry, entry_id)):
        unique_id = entity.unique_id or ""
        if _PHOTO_ENTITY.search(unique_id):
            registry.async_remove(entity.entity_id)


def async_expose_photo_gallery(hass: HomeAssistant, coordinator: AjaxDataCoordinator) -> None:
    """Put a Visit button on each MotionCam device page that opens the gallery.

    The device page cannot host a photo card. ``homeassistant://`` configuration
    URLs become an in-app Visit button on that page.
    """
    account = coordinator.account
    if account is None:
        return
    try:
        registry = dr.async_get(hass)
    except (KeyError, TypeError, AttributeError, RuntimeError):
        return
    for space in account.spaces.values():
        for device in space.devices.values():
            if not is_motioncam_raw_type(device.raw_type):
                continue
            found = find_device(registry, coordinator.entry_id, device.id)
            if found is None or found.configuration_url == _GALLERY_DEVICE_URL:
                continue
            registry.async_update_device(found.id, configuration_url=_GALLERY_DEVICE_URL)


def motion_entity_id(hass: HomeAssistant, entry_id: str, device_id: str) -> str | None:
    """Return the motion binary sensor entity id for a MotionCam, if it exists."""
    try:
        registry = er.async_get(hass)
    except (KeyError, TypeError, AttributeError, RuntimeError):
        return None
    unique_id = f"{entry_id}_{device_id}_motion"
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.unique_id == unique_id:
            return entity.entity_id
    return None


def photo_media_root(hass: HomeAssistant) -> Path:
    """Return ``config/media/ajax_photos``."""
    return Path(hass.config.path("media")) / "ajax_photos"


def content_disposition(filename: str) -> str:
    """Attachment header for a validated photo filename."""
    return f'attachment; filename="{filename}"'


def resolve_photo_file(root: Path, device_id: str, filename: str) -> Path | None:
    """Return the photo path, or None when the name could escape the folder."""
    if not _DEVICE_ID.fullmatch(device_id) or not _FILENAME.fullmatch(filename):
        return None
    folder = (root / device_id).resolve()
    path = (folder / filename).resolve()
    if path.parent != folder:
        return None
    return path


def photo_file_response(body: bytes, filename: str, *, download: bool) -> web.Response:
    """Build the JPEG response. Download sets an attachment filename."""
    disposition = content_disposition(filename) if download else "inline"
    return web.Response(
        body=body,
        content_type="image/jpeg",
        headers={"Content-Disposition": disposition},
    )


class MotionCamPhotoStore:
    """Fetch hub logs and keep MotionCam JPEGs on disk."""

    def __init__(
        self,
        coordinator: AjaxDataCoordinator,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Bind the store to one config entry. Entities are not created."""
        self.coordinator = coordinator
        self._async_add_entities = async_add_entities
        self._logs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        self._older_logs: dict[str, list[dict[str, Any]]] = {}
        self._backfilled: set[str] = set()
        self._retry_until: dict[tuple[str, str], float] = {}
        self._primed: set[str] = set()
        self._announced: dict[str, None] = {}

    async def async_tick(self, _now: datetime) -> None:
        """Periodic refresh. API errors stay inside ``async_refresh``."""
        await self.async_refresh()

    async def async_refresh(self) -> None:
        """Pull hub logs and write any READY photos that are not on disk yet."""
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
            async_expose_photo_gallery(self.coordinator.hass, self.coordinator)
            for device in devices:
                bursts = parse_photo_bursts(logs, device.id)
                first_sight = device.id not in self._primed
                self._primed.add(device.id)
                for burst in bursts:
                    self._track_retry(hub_id, burst)
                    saved, new_files = await self._save_burst(device, burst)
                    if first_sight:
                        if saved:
                            self._announced[burst.event_id] = None
                        continue
                    if new_files and burst.event_id not in self._announced:
                        self._announce(device, burst.event_id, saved, new_files[-1])

    async def _hub_logs(self, hub_id: str) -> list[dict[str, Any]] | None:
        """Return page 1 plus, on the first success, a few older pages."""
        now = time.monotonic()
        force = any(
            deadline > now for (cached_hub, _event_id), deadline in self._retry_until.items() if cached_hub == hub_id
        )
        if hub_id not in self._backfilled:
            page1, older, ok = await self._fetch_backfill(hub_id)
            if not ok or page1 is None:
                return None
            self._backfilled.add(hub_id)
            self._logs_cache[hub_id] = (now, page1)
            self._older_logs[hub_id] = older
            return page1 + older

        cached = self._logs_cache.get(hub_id)
        older = self._older_logs.get(hub_id, [])
        if cached is not None and not force and (now - cached[0]) < LOG_CACHE_SECONDS:
            return cached[1] + older

        page1 = await self._fetch_page(hub_id, 1)
        if page1 is None:
            return None
        self._logs_cache[hub_id] = (now, page1)
        return page1 + older

    async def _fetch_backfill(self, hub_id: str) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]], bool]:
        """Read page 1 and older pages until a page adds no new events."""
        page1 = await self._fetch_page(hub_id, 1)
        if page1 is None:
            return None, [], False
        seen = _log_event_ids(page1)
        older: list[dict[str, Any]] = []
        for page in range(2, BACKFILL_PAGES + 1):
            batch = await self._fetch_page(hub_id, page)
            if not batch:
                break
            fresh = [entry for entry in batch if _log_row_id(entry) not in seen]
            if not fresh:
                break
            for entry in fresh:
                seen.add(_log_row_id(entry))
            older.extend(fresh)
        return page1, older, True

    async def _fetch_page(self, hub_id: str, page: int) -> list[dict[str, Any]] | None:
        """Return one log page, or None when the request failed."""
        try:
            return await self.coordinator.api.async_get_hub_logs(hub_id, page=page)
        except Exception:
            _LOGGER.debug("MotionCam photo log fetch failed for hub %s page %s", hub_id, page, exc_info=True)
            return None

    def _track_retry(self, hub_id: str, burst: PhotoBurst) -> None:
        """Retry this hub for up to a minute while a burst is still transferring."""
        key = (hub_id, burst.event_id)
        if burst.in_progress:
            self._retry_until.setdefault(key, time.monotonic() + RETRY_WINDOW_SECONDS)
            return
        self._retry_until.pop(key, None)

    async def _save_burst(self, device: AjaxDevice, burst: PhotoBurst) -> tuple[int, list[str]]:
        """Write READY frames that are not already on disk.

        Returns how many frames exist and the filenames written on this pass.
        """
        device_key = safe_device_id(device.id)
        if device_key is None:
            return 0, []
        root = photo_media_root(self.coordinator.hass)
        saved = 0
        new_files: list[str] = []
        for frame, link in enumerate(burst.links, start=1):
            if link.status != "READY" or not link.url:
                continue
            filename = f"{burst.timestamp_ms}_{frame}.jpg"
            path = root / device_key / filename
            if path.is_file() and path.stat().st_size > 0:
                saved += 1
                continue
            jpeg = await self._download(link.url)
            if jpeg is None:
                continue
            await asyncio.to_thread(_write_photo, path, jpeg, root, device_key, device.name)
            saved += 1
            new_files.append(filename)
        return saved, new_files

    def _announce(self, device: AjaxDevice, event_id: str, photo_count: int, filename: str) -> None:
        """Write one Activity line and one notification for a new burst."""
        if not event_id or event_id in self._announced or photo_count < 1:
            return
        hass = self.coordinator.hass
        bus = getattr(hass, "bus", None)
        if bus is None:
            return
        self._announced[event_id] = None
        while len(self._announced) > 200:
            self._announced.pop(next(iter(self._announced)))
        payload: dict[str, Any] = {
            "device_name": device.name,
            "device_id": device.id,
            "photo_count": photo_count,
        }
        entity_id = motion_entity_id(hass, self.coordinator.entry_id, device.id)
        if entity_id:
            payload["entity_id"] = entity_id
        bus.async_fire(EVENT_AJAX_MOTIONCAM_PHOTO, payload)
        self._notify_photo(device, event_id, photo_count, filename)

    def _notify_photo(self, device: AjaxDevice, event_id: str, photo_count: int, filename: str) -> None:
        """Post the new picture to the Home Assistant notifications drawer."""
        hass = self.coordinator.hass
        device_key = safe_device_id(device.id)
        try:
            signed = _signed_photo_path(hass, device_key, filename) if device_key else None
            async_create(
                hass,
                photo_notification_message(device.name, signed, photo_count),
                title=device.name,
                notification_id=f"ajax_photo_{_safe_note_id(event_id)}",
            )
        except Exception:
            _LOGGER.debug("Could not post Ajax photo notification", exc_info=True)

    async def _download(self, url: str) -> bytes | None:
        """Download one READY photo. Absolute CDN links are fetched with no Ajax credentials."""
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
        return data

    def _download_target(self, url: str) -> tuple[str | None, dict[str, str] | None]:
        """Split a resource link into a URL and, for relative links, auth headers."""
        if not url or any(char in url for char in ("\n", "\r", "\x00")):
            return None, None
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.netloc:
            return url, None
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


def safe_device_id(device_id: str) -> str | None:
    """Return a device id that is safe to use as a folder name."""
    if _DEVICE_ID.fullmatch(device_id):
        return device_id
    return None


def photo_notification_message(device_name: str, signed_url: str | None, photo_count: int) -> str:
    """Notification text, including the picture when a signed URL is available."""
    if photo_count > 1:
        text = f"{device_name} received {photo_count} photos."
    else:
        text = f"{device_name} received a photo."
    if signed_url:
        text += f"\n\n![photo]({signed_url})"
    return text + "\n\n[Open Ajax photos](/ajax-photos)"


def _signed_photo_path(hass: HomeAssistant, device_id: str, filename: str) -> str | None:
    """Return a temporary signed path the notification drawer can load."""
    path = f"/api/ajax/photos/{quote(device_id)}/{quote(filename)}"
    try:
        return async_sign_path(hass, path, _NOTIFICATION_TTL, use_content_user=True)
    except (KeyError, TypeError, RuntimeError, ValueError):
        _LOGGER.debug("Could not sign Ajax photo notification URL", exc_info=True)
        return None


def _safe_note_id(event_id: str) -> str:
    """Keep a notification id inside the characters Home Assistant accepts."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", event_id)
    return cleaned[:40] or "photo"


def _write_photo(path: Path, jpeg: bytes, root: Path, device_key: str, device_name: str) -> None:
    """Write one JPEG, remember the device name, and drop photos past the cap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg)
    _remember_name(root, device_key, device_name)
    _prune_device(path.parent)


def _remember_name(root: Path, device_key: str, device_name: str) -> None:
    """Store the display name used by the gallery."""
    path = root / "names.json"
    current: dict[str, str] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            current = {str(key): str(value) for key, value in loaded.items() if isinstance(value, str)}
    if current.get(device_key) == device_name:
        return
    current[device_key] = device_name
    path.write_text(json.dumps(current), encoding="utf-8")


def _prune_device(folder: Path) -> None:
    """Keep the newest ``MAX_PHOTOS_PER_DEVICE`` JPEGs in one device folder."""
    files = [path for path in folder.glob("*.jpg") if _FILENAME.fullmatch(path.name)]
    files.sort(key=_photo_sort_key, reverse=True)
    for old in files[MAX_PHOTOS_PER_DEVICE:]:
        old.unlink(missing_ok=True)


def _photo_sort_key(path: Path) -> tuple[int, int]:
    """Sort ``{timestamp}_{frame}.jpg`` newest first when reversed."""
    timestamp_text, _, frame_text = path.stem.partition("_")
    timestamp = int(timestamp_text) if timestamp_text.isdigit() else 0
    frame = int(frame_text) if frame_text.isdigit() else 0
    return timestamp, frame


def _log_row_id(entry: dict[str, Any]) -> str:
    """Identity of a log row, used to stop the backfill when a page repeats."""
    for key in ("eventId", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(entry.get("timestamp", id(entry)))


def _log_event_ids(entries: list[dict[str, Any]]) -> set[str]:
    """Collect row identities from one log page."""
    return {_log_row_id(entry) for entry in entries if isinstance(entry, dict)}


class GalleryFrame:
    """One saved JPEG shown in the gallery."""

    def __init__(self, device_id: str, device_name: str, timestamp_ms: int, frame: int, filename: str) -> None:
        """Store the fields the gallery page renders."""
        self.device_id = device_id
        self.device_name = device_name
        self.timestamp_ms = timestamp_ms
        self.frame = frame
        self.filename = filename


def load_gallery(root: Path) -> list[GalleryFrame]:
    """Read saved photos, newest burst first."""
    if not root.is_dir():
        return []
    names = _load_names(root)
    frames: list[GalleryFrame] = []
    for folder in root.iterdir():
        if not folder.is_dir() or not _DEVICE_ID.fullmatch(folder.name):
            continue
        device_name = names.get(folder.name, folder.name)
        for path in folder.glob("*.jpg"):
            if not _FILENAME.fullmatch(path.name):
                continue
            timestamp_text, _, frame_text = path.stem.partition("_")
            frames.append(
                GalleryFrame(
                    device_id=folder.name,
                    device_name=device_name,
                    timestamp_ms=int(timestamp_text),
                    frame=int(frame_text),
                    filename=path.name,
                )
            )
    frames.sort(key=lambda item: (-item.timestamp_ms, item.device_id, item.frame))
    return frames


def _load_names(root: Path) -> dict[str, str]:
    """Return device id to display name."""
    path = root / "names.json"
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {str(key): str(value) for key, value in loaded.items() if isinstance(value, str)}


def render_photo_gallery(frames: list[GalleryFrame]) -> str:
    """Return the scrollable gallery page."""
    if not frames:
        body = "<p class='empty'>No photos yet. They appear here after the MotionCam sends a picture.</p>"
    else:
        groups: list[str] = []
        index = 0
        while index < len(frames):
            frame = frames[index]
            members = [frame]
            index += 1
            while (
                index < len(frames)
                and frames[index].device_id == frame.device_id
                and frames[index].timestamp_ms == frame.timestamp_ms
            ):
                members.append(frames[index])
                index += 1
            members.sort(key=lambda item: item.frame)
            cards = []
            for item in members:
                href = f"{_GALLERY_URL}/{quote(item.device_id)}/{quote(item.filename)}"
                cards.append(
                    "<figure>"
                    f"<img src='{escape(href)}' alt='{escape(item.device_name)} photo {item.frame}'>"
                    f"<figcaption><a class='download' href='{escape(href)}?download=1' "
                    f"download='{escape(item.filename)}'>Download</a></figcaption>"
                    "</figure>"
                )
            taken = datetime.fromtimestamp(frame.timestamp_ms / 1000, tz=UTC).strftime("%d %b %Y, %H:%M UTC")
            groups.append(
                "<section class='burst'>"
                f"<h2>{escape(frame.device_name)}</h2>"
                f"<p class='when'>{escape(taken)}</p>"
                f"<div class='frames'>{''.join(cards)}</div>"
                "</section>"
            )
        body = "".join(groups)
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Ajax photos</title><style>"
        "body{margin:0;background:#111;color:#eee;font-family:sans-serif;}"
        "main{box-sizing:border-box;height:100vh;overflow-y:auto;max-width:920px;margin:0 auto;padding:24px;}"
        "h1{font-size:1.4rem;font-weight:600;margin:0 0 20px;}"
        ".burst{margin:0 0 28px;}"
        ".when{margin:0 0 10px;color:#aaa;font-size:.9rem;}"
        ".frames{display:flex;gap:12px;overflow-x:auto;padding-bottom:8px;}"
        "figure{margin:0;min-width:220px;}"
        "img{display:block;max-height:420px;max-width:100%;background:#000;}"
        "figcaption{margin-top:8px;}"
        "a.download{display:inline-block;background:#3ddc84;color:#05210f;text-decoration:none;"
        "font-weight:600;padding:6px 12px;border-radius:16px;}"
        ".empty{color:#aaa;}"
        "</style></head><body><main><h1>Ajax photos</h1>"
        f"{body}</main></body></html>"
    )


_PANEL_JS = """
class AjaxPhotosPanel extends HTMLElement {
  constructor() {
    super();
    this._seen = new Set();
    this._blobs = new Map();
    this._timer = null;
    this._lightbox = null;
    this._onKey = null;
    this._started = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._started) {
      this._started = true;
      this._build();
      this._refresh();
    }
  }

  connectedCallback() {
    this._startTimer();
  }

  disconnectedCallback() {
    this._stopTimer();
    this._close();
  }

  _startTimer() {
    if (this._timer) {
      return;
    }
    this._timer = setInterval(() => this._refresh(), 5000);
  }

  _stopTimer() {
    if (!this._timer) {
      return;
    }
    clearInterval(this._timer);
    this._timer = null;
  }

  async _authFetch(url) {
    if (!this._hass || !this._hass.fetchWithAuth) {
      throw new Error("authenticated fetch unavailable");
    }
    return this._hass.fetchWithAuth(url);
  }

  _build() {
    this.style.cssText = "display:flex;flex-direction:column;height:100%;min-height:0;overflow:hidden;background:#111;color:#eee;font-family:sans-serif;box-sizing:border-box;";
    const bar = document.createElement("div");
    bar.style.cssText = "display:flex;align-items:center;gap:4px;flex:0 0 auto;padding:4px 8px;background:#1c1c1c;";
    const menu = document.createElement("ha-menu-button");
    const title = document.createElement("div");
    title.textContent = "Ajax photos";
    title.style.cssText = "font-size:1.2rem;font-weight:600;";
    bar.append(menu, title);
    this._list = document.createElement("div");
    this._list.style.cssText = "flex:1 1 auto;min-height:0;overflow-y:auto;padding:16px;max-width:920px;width:100%;margin:0 auto;box-sizing:border-box;";
    this.append(bar, this._list);
    this._startTimer();
  }

  async _refresh() {
    if (!this._list) {
      return;
    }
    let photos = [];
    try {
      const response = await this._authFetch("/api/ajax/photos/index");
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      const payload = await response.json();
      photos = payload.photos || [];
    } catch (err) {
      if (!this._seen.size) {
        this._list.textContent = "Photos could not be loaded.";
      }
      return;
    }
    const ids = new Set(photos.map((photo) => photo.device_id + "/" + photo.filename));
    let changed = ids.size !== this._seen.size;
    for (const id of ids) {
      if (!this._seen.has(id)) {
        changed = true;
        break;
      }
    }
    this._seen = ids;
    if (changed || !this._list.childElementCount) {
      this._render(photos);
    }
  }

  _render(photos) {
    this._list.textContent = "";
    if (!photos.length) {
      this._list.textContent = "No photos yet. They appear here after the MotionCam sends a picture.";
      return;
    }
    let lastKey = "";
    let frames = null;
    for (const photo of photos) {
      const key = photo.device_id + ":" + photo.timestamp_ms;
      if (key !== lastKey) {
        lastKey = key;
        const section = document.createElement("section");
        section.style.margin = "0 0 28px";
        const heading = document.createElement("h2");
        heading.textContent = photo.device_name;
        heading.style.cssText = "font-size:1.1rem;margin:0 0 4px";
        const when = document.createElement("p");
        when.textContent = new Date(photo.timestamp_ms).toLocaleString();
        when.style.cssText = "margin:0 0 10px;color:#aaa;font-size:.9rem";
        frames = document.createElement("div");
        frames.style.cssText = "display:flex;gap:12px;overflow-x:auto;padding-bottom:8px";
        section.append(heading, when, frames);
        this._list.appendChild(section);
      }
      const figure = document.createElement("figure");
      figure.style.cssText = "margin:0;min-width:220px";
      const img = document.createElement("img");
      img.alt = photo.device_name + " photo " + photo.frame;
      img.style.cssText = "display:block;max-height:420px;max-width:100%;background:#000;cursor:pointer";
      const fileUrl = "/api/ajax/photos/" + encodeURIComponent(photo.device_id) + "/" + encodeURIComponent(photo.filename);
      const cached = this._blobs.get(fileUrl);
      if (cached) {
        img.src = cached;
      } else {
        this._show(img, fileUrl);
      }
      img.addEventListener("click", () => this._open(img, fileUrl, photo.filename, photo.device_name));
      const caption = document.createElement("figcaption");
      caption.style.marginTop = "8px";
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Download";
      button.style.cssText = "background:#3ddc84;color:#05210f;font-weight:600;border:0;padding:6px 12px;border-radius:16px;cursor:pointer";
      button.addEventListener("click", (ev) => {
        ev.stopPropagation();
        this._download(fileUrl, photo.filename);
      });
      caption.appendChild(button);
      figure.append(img, caption);
      frames.appendChild(figure);
    }
  }

  async _show(img, url) {
    try {
      const response = await this._authFetch(url);
      if (!response.ok) {
        return;
      }
      const blobUrl = URL.createObjectURL(await response.blob());
      this._blobs.set(url, blobUrl);
      img.src = blobUrl;
    } catch (err) {
      img.alt = "Photo unavailable";
    }
  }

  async _open(img, fileUrl, filename, name) {
    let blobUrl = img.src && img.src.startsWith("blob:") ? img.src : this._blobs.get(fileUrl);
    if (!blobUrl) {
      await this._show(img, fileUrl);
      blobUrl = this._blobs.get(fileUrl);
    }
    if (!blobUrl) {
      return;
    }
    this._close();
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.9);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:16px;box-sizing:border-box;";
    overlay.addEventListener("click", () => this._close());
    const picture = document.createElement("img");
    picture.src = blobUrl;
    picture.alt = name;
    picture.style.cssText = "max-width:100%;max-height:80vh;object-fit:contain;";
    picture.addEventListener("click", (ev) => ev.stopPropagation());
    const row = document.createElement("div");
    row.style.cssText = "display:flex;gap:12px;";
    row.addEventListener("click", (ev) => ev.stopPropagation());
    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "Close";
    close.style.cssText = "background:#333;color:#fff;font-weight:600;border:0;padding:8px 16px;border-radius:16px;cursor:pointer";
    close.addEventListener("click", () => this._close());
    const download = document.createElement("button");
    download.type = "button";
    download.textContent = "Download";
    download.style.cssText = "background:#3ddc84;color:#05210f;font-weight:600;border:0;padding:8px 16px;border-radius:16px;cursor:pointer";
    download.addEventListener("click", () => this._download(fileUrl, filename));
    row.append(close, download);
    overlay.append(picture, row);
    this.appendChild(overlay);
    this._lightbox = overlay;
    this._onKey = (ev) => {
      if (ev.key === "Escape") {
        this._close();
      }
    };
    window.addEventListener("keydown", this._onKey);
  }

  _close() {
    if (this._onKey) {
      window.removeEventListener("keydown", this._onKey);
      this._onKey = null;
    }
    if (this._lightbox) {
      this._lightbox.remove();
      this._lightbox = null;
    }
  }

  async _download(url, filename) {
    const response = await this._authFetch(url + "?download=1");
    if (!response.ok) {
      return;
    }
    const blobUrl = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = blobUrl;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(blobUrl);
  }
}
if (!customElements.get("ajax-photos-panel")) {
  customElements.define("ajax-photos-panel", AjaxPhotosPanel);
}
"""


async def _async_register_gallery(hass: HomeAssistant) -> None:
    """Register the gallery routes and the sidebar panel once."""
    if hass.data.get(_PANEL_KEY):
        return
    hass.http.register_view(AjaxPhotoGalleryView())
    hass.http.register_view(AjaxPhotoIndexView())
    hass.http.register_view(AjaxPhotoFileView())
    hass.http.register_view(AjaxPhotoPanelView())
    try:
        await async_register_panel(
            hass,
            frontend_url_path=_PANEL_PATH,
            webcomponent_name="ajax-photos-panel",
            sidebar_title="Ajax photos",
            sidebar_icon="mdi:image-multiple",
            module_url="/api/ajax/photos/panel.js",
            require_admin=False,
        )
    except ValueError:
        _LOGGER.debug("Ajax photos sidebar panel already exists; refreshing it")
    if frontend.async_panel_exists(hass, _PANEL_PATH):
        frontend.async_register_built_in_panel(
            hass,
            "custom",
            sidebar_title="Ajax photos",
            sidebar_icon="mdi:image-multiple",
            frontend_url_path=_PANEL_PATH,
            config={
                "_panel_custom": {
                    "name": "ajax-photos-panel",
                    "embed_iframe": False,
                    "trust_external": False,
                    "handle_safe_area": False,
                    "module_url": "/api/ajax/photos/panel.js",
                }
            },
            require_admin=False,
            update=True,
            show_in_sidebar=True,
        )
    hass.data[_PANEL_KEY] = True
    _LOGGER.info("Ajax photos gallery is available in the sidebar at /ajax-photos")


class AjaxPhotoIndexView(HomeAssistantView):
    """JSON list of saved photos for the sidebar panel."""

    url = "/api/ajax/photos/index"
    name = "api:ajax:photo-index"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return the saved photos, newest first."""
        hass: HomeAssistant = request.app["hass"]
        frames = await asyncio.to_thread(load_gallery, photo_media_root(hass))
        return self.json(
            {
                "photos": [
                    {
                        "device_id": frame.device_id,
                        "device_name": frame.device_name,
                        "timestamp_ms": frame.timestamp_ms,
                        "frame": frame.frame,
                        "filename": frame.filename,
                    }
                    for frame in frames
                ]
            }
        )


class AjaxPhotoPanelView(HomeAssistantView):
    """Sidebar panel script. It has no secrets, so the browser can load it."""

    url = "/api/ajax/photos/panel.js"
    name = "api:ajax:photo-panel"
    requires_auth = False

    async def get(self, request: web.Request) -> web.Response:
        """Return the panel module."""
        return web.Response(
            text=_PANEL_JS,
            content_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )


class AjaxPhotoGalleryView(HomeAssistantView):
    """Scrollable list of saved MotionCam photos."""

    url = _GALLERY_URL
    name = "api:ajax:photos"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return the gallery page."""
        hass: HomeAssistant = request.app["hass"]
        frames = await asyncio.to_thread(load_gallery, photo_media_root(hass))
        return web.Response(text=render_photo_gallery(frames), content_type="text/html")


class AjaxPhotoFileView(HomeAssistantView):
    """Serve one saved JPEG, inline or as a download."""

    url = "/api/ajax/photos/{device_id}/{filename}"
    name = "api:ajax:photo"
    requires_auth = True

    async def get(self, request: web.Request, device_id: str, filename: str) -> web.Response:
        """Return the file. ``?download=1`` saves it in the browser."""
        hass: HomeAssistant = request.app["hass"]
        path = resolve_photo_file(photo_media_root(hass), device_id, filename)
        if path is None or not path.is_file():
            return web.Response(status=404)
        body = await asyncio.to_thread(path.read_bytes)
        return photo_file_response(body, filename, download=request.query.get("download") == "1")

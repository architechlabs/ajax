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

LOG_CACHE_SECONDS = 20
RETRY_WINDOW_SECONDS = 60
POLL_INTERVAL = timedelta(seconds=15)
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
_NOTIFIED_KEY = "ajax_photo_gallery_notified"


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
                    saved = await self._save_burst(device, burst)
                    if first_sight:
                        if saved:
                            self._announced[burst.event_id] = None
                        continue
                    if saved and burst.event_id not in self._announced:
                        self._announce(device, burst.event_id, saved)

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

    async def _save_burst(self, device: AjaxDevice, burst: PhotoBurst) -> int:
        """Write READY frames that are not already on disk. Return how many exist."""
        device_key = safe_device_id(device.id)
        if device_key is None:
            return 0
        root = photo_media_root(self.coordinator.hass)
        saved = 0
        for frame, link in enumerate(burst.links, start=1):
            if link.status != "READY" or not link.url:
                continue
            path = root / device_key / f"{burst.timestamp_ms}_{frame}.jpg"
            if path.is_file() and path.stat().st_size > 0:
                saved += 1
                continue
            jpeg = await self._download(link.url)
            if jpeg is None:
                continue
            await asyncio.to_thread(_write_photo, path, jpeg, root, device_key, device.name)
            saved += 1
        return saved

    def _announce(self, device: AjaxDevice, event_id: str, photo_count: int) -> None:
        """Write one Activity line per new burst, attached to the motion entity."""
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
  set hass(hass) {
    if (this._hass) {
      return;
    }
    this._hass = hass;
    this._load();
  }

  async _authFetch(url) {
    if (this._hass.fetchWithAuth) {
      return this._hass.fetchWithAuth(url);
    }
    return fetch(url, {
      headers: { Authorization: "Bearer " + this._hass.auth.data.access_token },
    });
  }

  async _load() {
    this.style.display = "block";
    this.style.height = "100%";
    this.style.overflow = "auto";
    this.style.background = "#111";
    this.style.color = "#eee";
    this.style.fontFamily = "sans-serif";
    this.innerHTML = "<p style='padding:24px'>Loading photos...</p>";
    let photos = [];
    try {
      const response = await this._authFetch("/api/ajax/photos/index");
      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }
      const payload = await response.json();
      photos = payload.photos || [];
    } catch (err) {
      this.innerHTML = "<p style='padding:24px'>Photos could not be loaded.</p>";
      return;
    }
    if (!photos.length) {
      this.innerHTML = "<main style='padding:24px'><h1>Ajax photos</h1><p>No photos yet. They appear here after the MotionCam sends a picture.</p></main>";
      return;
    }
    const main = document.createElement("main");
    main.style.cssText = "box-sizing:border-box;max-width:920px;margin:0 auto;padding:24px";
    const title = document.createElement("h1");
    title.textContent = "Ajax photos";
    title.style.cssText = "font-size:1.4rem;font-weight:600;margin:0 0 20px";
    main.appendChild(title);
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
        main.appendChild(section);
      }
      const figure = document.createElement("figure");
      figure.style.cssText = "margin:0;min-width:220px";
      const img = document.createElement("img");
      img.alt = photo.device_name + " photo " + photo.frame;
      img.style.cssText = "display:block;max-height:420px;max-width:100%;background:#000";
      const fileUrl = "/api/ajax/photos/" + encodeURIComponent(photo.device_id) + "/" + encodeURIComponent(photo.filename);
      this._show(img, fileUrl);
      const caption = document.createElement("figcaption");
      caption.style.marginTop = "8px";
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = "Download";
      button.style.cssText = "background:#3ddc84;color:#05210f;font-weight:600;border:0;padding:6px 12px;border-radius:16px;cursor:pointer";
      button.addEventListener("click", () => this._download(fileUrl, photo.filename));
      caption.appendChild(button);
      figure.append(img, caption);
      frames.appendChild(figure);
    }
    this.innerHTML = "";
    this.appendChild(main);
  }

  async _show(img, url) {
    try {
      const response = await this._authFetch(url);
      if (!response.ok) {
        return;
      }
      img.src = URL.createObjectURL(await response.blob());
    } catch (err) {
      img.alt = "Photo unavailable";
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
    if not hass.data.get(_NOTIFIED_KEY):
        async_create(
            hass,
            "MotionCam pictures are listed under **Ajax photos** in the sidebar. "
            "On the Motion and Cam device page, **Visit** opens the same list, "
            "with a Download button on each photo.\n\n[Open Ajax photos](/ajax-photos)",
            title="Ajax photos",
            notification_id="ajax_photos_where",
        )
        hass.data[_NOTIFIED_KEY] = True
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
        return web.Response(text=_PANEL_JS, content_type="text/javascript")


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

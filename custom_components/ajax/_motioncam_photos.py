"""Parse MotionCam photo bursts out of Ajax hub log entries.

Pure functions only: no Home Assistant and no network. The image platform
turns the bursts this module returns into cached JPEGs.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_BURST_LINKS = 5

_PHOTOS_V1 = "PHOTOS_RESOURCE_DESCRIPTION"
_PHOTOS_V2 = "RESOURCE_DESCRIPTION"
_READY = "READY"
_IN_PROGRESS = "IN_PROGRESS"
_FAILED = "FAILED"


@dataclass(frozen=True)
class PhotoLink:
    """One frame inside a MotionCam burst."""

    url: str
    status: str


@dataclass(frozen=True)
class PhotoBurst:
    """The newest photo event for a single MotionCam device."""

    event_id: str
    device_id: str
    timestamp_ms: int
    links: tuple[PhotoLink, ...]

    @property
    def in_progress(self) -> bool:
        """True when Wings is still delivering a frame of this burst."""
        return any(link.status == _IN_PROGRESS for link in self.links)

    @property
    def ready_urls(self) -> tuple[str, ...]:
        """URLs whose image bytes can be downloaded now."""
        return tuple(link.url for link in self.links if link.status == _READY and link.url)


def is_motioncam_raw_type(raw_type: str | None) -> bool:
    """True for MotionCam family models, including PhOD, Outdoor, and Fibra.

    Plain MotionProtect detectors share the motion-detector device type but
    their raw type does not contain ``motioncam``.
    """
    if not raw_type:
        return False
    normalized = raw_type.lower().replace("_", "").replace("-", "").replace(" ", "")
    return "motioncam" in normalized


def parse_latest_photo_burst(logs: object, device_id: str) -> PhotoBurst | None:
    """Return the newest photo burst for ``device_id``, or None.

    Failed links are dropped. In-progress links are kept so the caller can
    retry until Wings finishes. At most ``MAX_BURST_LINKS`` links are kept,
    in the order Ajax sent them.
    """
    if not isinstance(logs, list) or not isinstance(device_id, str) or not device_id.strip():
        return None

    wanted = device_id.strip().lower()
    best: PhotoBurst | None = None
    best_index = -1

    for index, entry in enumerate(logs):
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("sourceObjectId")
        if not isinstance(source_id, str) or source_id.strip().lower() != wanted:
            continue
        links = _entry_links(entry)
        if not links:
            continue
        timestamp_ms = _timestamp_ms(entry)
        event_id = _event_id(entry, device_id, timestamp_ms)
        burst = PhotoBurst(
            event_id=event_id,
            device_id=device_id,
            timestamp_ms=timestamp_ms,
            links=tuple(links),
        )
        if (
            best is None
            or timestamp_ms > best.timestamp_ms
            or (timestamp_ms == best.timestamp_ms and index > best_index)
        ):
            best = burst
            best_index = index

    return best


def _event_id(entry: dict[str, object], device_id: str, timestamp_ms: int) -> str:
    """Return a stable id for a log row."""
    for key in ("eventId", "id"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{device_id}:{timestamp_ms}"


def _timestamp_ms(entry: dict[str, object]) -> int:
    """Return the log timestamp, or 0 when it is missing or not numeric."""
    raw = entry.get("timestamp")
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _entry_links(entry: dict[str, object]) -> list[PhotoLink]:
    """Read photo links from v1 additional data, then v2 if v1 has none."""
    additional = entry.get("additionalData")
    if isinstance(additional, dict) and additional.get("additionalDataType") == _PHOTOS_V1:
        links = _links_from(additional.get("orderedResourceLinks"))
        if links:
            return links

    v2 = entry.get("additionalDataV2")
    blocks: list[object]
    if isinstance(v2, list):
        blocks = v2
    elif isinstance(v2, dict):
        blocks = [v2]
    else:
        blocks = []

    for block in blocks:
        if isinstance(block, dict) and block.get("additionalDataV2Type") == _PHOTOS_V2:
            links = _links_from(block.get("orderedResourceLinks"))
            if links:
                return links
    return []


def _links_from(raw: object) -> list[PhotoLink]:
    """Keep READY and IN_PROGRESS links, drop FAILED, cap the burst."""
    if not isinstance(raw, list):
        return []
    links: list[PhotoLink] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        if status == _FAILED or status not in {_READY, _IN_PROGRESS}:
            continue
        url = item.get("url")
        url_str = url.strip() if isinstance(url, str) else ""
        if status == _READY and not url_str:
            continue
        links.append(PhotoLink(url=url_str, status=status))
        if len(links) >= MAX_BURST_LINKS:
            break
    return links

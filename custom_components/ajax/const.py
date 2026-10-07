"""Constants for the Ajax integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import AjaxDataCoordinator


def get_integration_version() -> str:
    """Get the integration version from manifest.json.

    Returns:
        Version string (e.g., "0.11.2")
    """
    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        with manifest_path.open() as f:
            manifest = json.load(f)
            return manifest.get("version", "0.0.0")  # type: ignore[no-any-return]
    except (OSError, json.JSONDecodeError):
        return "0.0.0"


# Integration version (read once at import time)
INTEGRATION_VERSION = get_integration_version()

# Integration domain
DOMAIN = "ajax"

# Type alias for ConfigEntry with runtime_data (Platinum pattern)
type AjaxConfigEntry = ConfigEntry[AjaxDataCoordinator]

# Global/general strings
MANUFACTURER = "Ajax Systems"

# Configuration and defaults
CONF_API_KEY = "api_key"
CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_TOTP_SECRET = "totp_secret"  # Optional Base32 TOTP secret (2FA, mandatory 2025-09-01)
CONF_NOTIFICATION_FILTER = "notification_filter"
CONF_PERSISTENT_NOTIFICATION = "persistent_notification"
CONF_MONITORED_SPACES = "monitored_spaces"
CONF_ENABLED_SPACES = "enabled_spaces"  # Which spaces to load (filter devices/entities)

# Direct API authentication. The hardened build intentionally has no proxy/AWS/SSE configuration.
CONF_AUTH_MODE = "auth_mode"
AUTH_MODE_DIRECT = "direct"

# Polling configuration
CONF_DOOR_SENSOR_FAST_POLL = "door_sensor_fast_poll"  # Enable fast door sensor polling

# RTSP/ONVIF credentials for Video Edge cameras
CONF_RTSP_USERNAME = "rtsp_username"
CONF_RTSP_PASSWORD = "rtsp_password"

# Discovered hub MAC addresses (for DHCP discovery deduplication)
CONF_DISCOVERED_MACS = "discovered_macs"

# Notification filter options
NOTIFICATION_FILTER_NONE = "none"
NOTIFICATION_FILTER_ALARMS_ONLY = "alarms_only"
NOTIFICATION_FILTER_SECURITY_EVENTS = "security_events"
NOTIFICATION_FILTER_ALL = "all"

# REST API endpoints (official)
AJAX_REST_API_BASE_URL = "https://api.ajax.systems/api"
AJAX_REST_API_TIMEOUT = 30  # seconds

# Update intervals (seconds)
UPDATE_INTERVAL = 30  # Default poll interval when disarmed (need faster updates)
UPDATE_INTERVAL_ARMED = 60  # Poll interval when armed
UPDATE_INTERVAL_DOOR_SENSORS = 5  # Fast poll interval for door sensors when disarmed
METADATA_REFRESH_INTERVAL = 3600  # Full metadata refresh every hour (rooms, users, groups)

# Dispatcher signals
SIGNAL_NEW_DEVICE = f"{DOMAIN}_new_device"
SIGNAL_NEW_VIDEO_EDGE = f"{DOMAIN}_new_video_edge"
SIGNAL_NEW_SMART_LOCK = f"{DOMAIN}_new_smart_lock"
SIGNAL_NEW_SPACE = f"{DOMAIN}_new_space"
SIGNAL_NEW_GROUP = f"{DOMAIN}_new_group"

# Battery percentage at or below which ``AjaxDevice.is_low_battery`` reports True.
BATTERY_LOW_THRESHOLD: Final = 20

# Bus event types fired by the integration (also described in logbook.py).
EVENT_AJAX_ARMED: Final = "ajax_armed"
EVENT_AJAX_DISARMED: Final = "ajax_disarmed"
EVENT_AJAX_ARMED_NIGHT: Final = "ajax_armed_night"
EVENT_AJAX_ARMED_HOME: Final = "ajax_armed_home"
EVENT_AJAX_SECURITY_STATE_CHANGED: Final = "ajax_security_state_changed"
EVENT_AJAX_BUTTON_PRESSED: Final = "ajax_button_pressed"
EVENT_AJAX_DOORBELL_RING: Final = "ajax_doorbell_ring"
EVENT_AJAX_SMART_LOCK_DOORBELL: Final = "ajax_smart_lock_doorbell"
EVENT_AJAX_SCENARIO_TRIGGERED: Final = "ajax_scenario_triggered"
EVENT_AJAX_CAMERA_DETECTION: Final = "ajax_camera_detection"
EVENT_AJAX_MOTIONCAM_PHOTO: Final = "ajax_motioncam_photo"

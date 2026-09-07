"""Ajax data coordinator for Home Assistant.

This coordinator manages:
- Periodic polling updates from the official Ajax REST API
- Space, Room, Device, and Notification data
- State synchronization between Ajax and Home Assistant

The hardened build intentionally uses REST polling only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from ._coordinator_arm import AjaxArmServiceMixin
from ._coordinator_devices import AjaxDevicesMixin
from ._coordinator_door_poll import AjaxDoorPollingMixin
from ._coordinator_events import AjaxEventDispatchMixin
from ._coordinator_init import AjaxBootstrapMixin
from ._coordinator_onvif import AjaxOnvifMixin
from ._coordinator_spaces import AjaxSpacesMixin
from ._coordinator_state import AjaxStateUpdaterMixin
from .api import AjaxRestApi, AjaxRestApiError, AjaxRestAuthError
from .const import (
    DOMAIN,
    METADATA_REFRESH_INTERVAL,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_ARMED,
    AjaxConfigEntry,
)
from .models import (
    AjaxAccount,
    AjaxDevice,
    AjaxGroup,
    AjaxRoom,
    AjaxSpace,
    SecurityState,
)


if TYPE_CHECKING:
    from .onvif_manager import AjaxOnvifManager

_LOGGER = logging.getLogger(__name__)


class AjaxDataCoordinator(
    AjaxArmServiceMixin,
    AjaxBootstrapMixin,
    AjaxDevicesMixin,
    AjaxDoorPollingMixin,
    AjaxEventDispatchMixin,
    AjaxOnvifMixin,
    AjaxSpacesMixin,
    AjaxStateUpdaterMixin,
    DataUpdateCoordinator[AjaxAccount],
):
    """Coordinator to manage Ajax data updates.

    Architecture:
        AjaxAccount (User)
        └── AjaxSpace (Hub/System)
            ├── Security State (Armed/Disarmed/etc)
            ├── Rooms (Zones/Pieces)
            │   └── Devices in room
            ├── Devices (All)
            │   ├── Sensors
            │   ├── Controls
            │   └── Cameras
            └── Notifications (Events)
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: AjaxConfigEntry,
        api: AjaxRestApi,
        enabled_spaces: list[str] | None = None,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance
            entry: ConfigEntry this coordinator is bound to
            api: Ajax REST API instance
            enabled_spaces: List of space IDs to enable (None = all spaces)
        """
        self.api = api
        self.config_entry = entry
        self.account: AjaxAccount | None = None
        self._enabled_spaces: list[str] | None = enabled_spaces
        self.all_discovered_spaces: dict[str, str] = {}  # space_id -> name (for options flow)
        # Cached space_binding responses to avoid repeated per-tick API calls.
        self._space_binding_cache: dict[str, dict[str, Any]] = {}  # hub_id -> space_binding
        self._door_sensor_poll_task: asyncio.Task[Any] | None = (
            None  # Continuous door sensor polling when disarmed or in night mode
        )
        self._door_sensor_poll_security_state: SecurityState = SecurityState.DISARMED
        self._initial_load_done: bool = False  # Track if initial data load is complete
        self._force_metadata_refresh: bool = False  # Flag to force full metadata refresh
        self._pending_ha_actions: dict[str, float] = {}  # hub_id -> timestamp of HA action
        # Per-space lock so concurrent arm/disarm calls cannot reach the API
        # out-of-order. Without this, two automations firing arm() then
        # disarm() within ms can land in the wrong order on Ajax's side.
        self._arm_locks: dict[str, asyncio.Lock] = {}
        # Cycle counter retained for forced/light-refresh scheduling.
        self._cycle_counter: int = 0
        self._realtime_skip_factor: int = 1

        # Lightweight diagnostics counters: each handler increments the
        # matching key, diagnostics.py reads the whole dict. Plain ints so
        # no lock is needed (single-threaded asyncio).
        self.stats: dict[str, int] = {
            "events_onvif_received": 0,
            "auth_errors": 0,
            "discovery_refreshes": 0,
        }

        # ONVIF local AI detections (optional, for video edge cameras)
        self.onvif_manager: AjaxOnvifManager | None = None
        self._onvif_initialized = False
        self._onvif_reconcile_in_progress = False
        self._onvif_last_bootstrap_attempt: float = 0.0

        # Device details refresh optimization
        # Battery/signal don't change often, so refresh every 5 minutes instead of every poll
        self._last_device_details_refresh: float = 0
        self._device_details_refresh_interval: int = 300  # 5 minutes in seconds

        # Metadata refresh optimization (rooms, users, groups)
        # These don't change often, so refresh every hour instead of every poll
        self._last_metadata_refresh: float = 0

        # Door sensor fast polling option (disabled by default to reduce API calls)
        self._door_sensor_fast_poll_enabled: bool = False

        # Snapshot of the connection-relevant entry config, set at setup by
        # ``__init__.async_setup_entry``. The update listener compares against
        # it to decide whether a config change requires a reload.
        self._reload_config_snapshot: tuple[dict[str, Any], str, str] | None = None

        # Event entity registry: device_id -> AjaxEventEntity
        self._event_entities: dict[str, Any] = {}


        # Set by async_force_state_refresh; the next _async_update_data run skips
        # the cycle counter and the video/smart-lock fan-out.
        self._light_refresh_pending: bool = False

        # Wall-clock start of the last forced light refresh.
        self._last_forced_state_refresh_started: float = 0.0

        # Auth error resilience: tolerate transient auth failures before triggering reauth
        self._consecutive_auth_errors: int = 0
        self._max_auth_errors: int = 3  # Trigger reauth after 3 consecutive auth failures


        # Exposed as a plain str so entities can namespace their unique_id and
        # device identifiers per config entry (multi-account collision safety,
        # schema v1.3) without the Optional dance on self.config_entry.
        self.entry_id: str = entry.entry_id

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
            # Debounce bursts of local HA actions into fewer API calls.
            request_refresh_debouncer=Debouncer(
                hass,
                _LOGGER,
                cooldown=0.5,
                immediate=False,
            ),
        )

    def _update_polling_interval(self, security_state: SecurityState) -> None:
        """Update polling interval based on security state.

        - Armed/Night/Partial: 60s
        - Disarmed: 30s

        Also manages door sensor fast polling (5s) when disarmed.

        Args:
            security_state: Current security state of the space
        """
        is_disarmed = security_state == SecurityState.DISARMED

        if security_state in (
            SecurityState.ARMED,
            SecurityState.NIGHT_MODE,
            SecurityState.PARTIALLY_ARMED,
        ):
            base_interval = UPDATE_INTERVAL_ARMED
        else:
            base_interval = UPDATE_INTERVAL

        new_interval = base_interval
        current_interval = self.update_interval.total_seconds() if self.update_interval else UPDATE_INTERVAL

        if new_interval != current_interval:
            self.update_interval = timedelta(seconds=new_interval)
            _LOGGER.info(
                "Polling interval changed to %ds (security state: %s)",
                new_interval,
                security_state.value,
            )

        # Manage door sensor fast polling based on security state
        # Poll when disarmed OR in night mode (for sensors excluded from night mode)
        should_poll = is_disarmed or security_state == SecurityState.NIGHT_MODE
        self._manage_door_sensor_polling(should_poll, security_state)

    def _should_refresh_metadata(self) -> bool:
        """Check if metadata (rooms, users, groups) should be refreshed.

        Returns True if more than METADATA_REFRESH_INTERVAL seconds have passed.
        """
        current_time = time.time()
        return current_time - self._last_metadata_refresh >= METADATA_REFRESH_INTERVAL

    async def async_force_metadata_refresh(self) -> None:
        """Force a full metadata refresh (rooms, users, groups).

        Can be called from a service or button to manually refresh.
        Uses async_refresh() instead of async_request_refresh() to bypass
        the DataUpdateCoordinator debouncer and execute immediately.
        async_request_refresh() defers to the debounce timer (30-60s),
        which causes group state updates to be delayed.
        """
        _LOGGER.info("Forcing full metadata refresh (immediate)")
        self._force_metadata_refresh = True  # Set flag to force refresh
        await self.async_refresh()

    async def async_force_state_refresh(self) -> None:
        """Immediate light refresh after a realtime security event.

        Bypasses the DataUpdateCoordinator debouncer (async_refresh, same
        rationale as async_force_metadata_refresh) WITHOUT forcing the
        account-wide metadata pass: hub state and per-group states are
        fetched on every tick anyway (#150), so an arm/disarm event only
        needs immediacy - not rooms/users/video re-fetches across all hubs.

        Sets ``_light_refresh_pending`` so the upcoming ``_async_update_data``
        run neither advances the #194 armed-aware cycle counter nor performs
        the video-edge/smart-lock fan-out, even if the throttle's modulo
        would otherwise land on it or the affected space just went from
        armed to disarmed. Trade-off: video AI detections and smart-lock
        state discovered off an arm/disarm event catch up on the next
        regular periodic tick (UPDATE_INTERVAL, see const.py) instead of
        immediately.
        """
        _LOGGER.info("Forcing light state refresh (immediate)")
        self._light_refresh_pending = True
        self._last_forced_state_refresh_started = time.time()
        await self.async_refresh()

    async def async_request_refresh_bypass_cache(self) -> None:
        """Request an immediate refresh.

        Kept as a compatibility method for existing service/entity callers;
        the direct-only transport has no intermediary cache.
        """
        await self.async_request_refresh()

    async def _async_update_data(self) -> AjaxAccount:
        """Fetch data from Ajax REST API.

        Uses optimized polling strategy:
        - Light polling (every cycle): Hub state + devices only
        - Full metadata refresh (hourly): Rooms, users, groups
        """
        try:
            # Log when connection is restored after a failure
            if not self.last_update_success:
                _LOGGER.info("Connection to Ajax API restored")

            # Consume the light-refresh flag set by async_force_state_refresh:
            # this tick must not advance the cycle counter or trigger the
            # video/smart-lock fan-out (see refresh_video_smart below).
            light_refresh = self._light_refresh_pending
            self._light_refresh_pending = False

            # Initialize account if needed
            if self.account is None:
                await self._async_init_account()

            # After init, account must exist
            if self.account is None:
                raise UpdateFailed("Account data not available after initialization")

            # Only do full data load on first run or manual reload
            if not self._initial_load_done:
                # Full update - use hubs endpoint directly to get hubId
                await self._async_update_spaces_from_hubs(full_refresh=True)
                self._last_metadata_refresh = time.time()

                # Load per-space data in parallel to minimize startup latency.
                tasks = []
                for space_id in self.account.spaces:
                    tasks.append(self._async_update_devices(space_id))
                    tasks.append(self._async_update_video_edges(space_id))
                    tasks.append(self._async_update_smart_locks(space_id))
                    tasks.append(self._async_update_notifications(space_id, limit=20))
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, BaseException):
                        raise result

                # Restore legacy real-time-discovered smart locks from storage

                # Mark initial load as complete
                self._initial_load_done = True
                _LOGGER.info("Initial data load complete")

                # Clean up HA device registry: remove devices deleted from Ajax
                self._async_cleanup_stale_devices()

                # Start door sensor polling if any space is disarmed or in night mode
                for space in self.account.spaces.values():
                    if space.security_state in (
                        SecurityState.DISARMED,
                        SecurityState.NIGHT_MODE,
                    ):
                        self._manage_door_sensor_polling(True, space.security_state)
                        break

                # Initialize local ONVIF event handling only.
                if self.config_entry is not None and not self._onvif_initialized:
                    self.config_entry.async_create_background_task(
                        self.hass, self._async_init_onvif(), "ajax_init_onvif"
                    )

                # Check if we need full metadata refresh (hourly or forced)
                need_metadata_refresh = self._force_metadata_refresh or self._should_refresh_metadata()
                if need_metadata_refresh:
                    if self._force_metadata_refresh:
                        _LOGGER.info("Forced metadata refresh (groups will be updated)")
                        self._force_metadata_refresh = False  # Clear the flag
                    else:
                        _LOGGER.info("Hourly metadata refresh (rooms, users, groups)")
                    self._last_metadata_refresh = time.time()

                # Light or full update based on metadata refresh need
                await self._async_update_spaces_from_hubs(full_refresh=need_metadata_refresh)

                if not light_refresh:
                    self._cycle_counter += 1
                refresh_video_smart = need_metadata_refresh or not light_refresh

                for space_id in self.account.spaces:
                    space_obj: AjaxSpace | None = self.account.spaces.get(space_id)
                    if space_obj:
                        self._reset_expired_motion_detections(space_obj)
                        await self._async_update_devices(space_id)
                        if refresh_video_smart:
                            # Refresh video edges to get AI detection states
                            if space_obj.video_edges:
                                await self._async_update_video_edges(space_id)
                            # Refresh smart locks (API data is minimal, state is event-driven)
                            if space_obj.smart_locks:
                                await self._async_update_smart_locks(space_id)

                # Reconcile ONVIF clients with the refreshed camera inventory
                # (cameras added/removed since startup). Runs in the background:
                # a camera that fails to connect must not stall the poll loop
                # with its connection timeouts.
                if refresh_video_smart and self._onvif_initialized and self.config_entry is not None:
                    self.config_entry.async_create_background_task(
                        self.hass, self._async_reconcile_onvif(), "ajax_onvif_reconcile"
                    )

            # Reset auth error counter on success
            self._consecutive_auth_errors = 0
            return self.account

        except AjaxRestAuthError as err:
            self._consecutive_auth_errors += 1
            self.stats["auth_errors"] += 1
            if self._consecutive_auth_errors >= self._max_auth_errors:
                _LOGGER.error(
                    "Authentication failed %d times consecutively, triggering reauth: %s",
                    self._consecutive_auth_errors,
                    err,
                )
                raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
            _LOGGER.warning(
                "Authentication error (%d/%d), will retry next poll: %s",
                self._consecutive_auth_errors,
                self._max_auth_errors,
                err,
            )
            raise UpdateFailed(f"Transient auth error: {err}") from err
        except AjaxRestApiError as err:
            if self.last_update_success:
                _LOGGER.warning("Connection to Ajax API lost: %s", err)
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    # Account/bootstrap state lives in
    # ``_coordinator_init.AjaxBootstrapMixin``.

    # ONVIF init + event handler + NVR routing live in
    # ``_coordinator_onvif.AjaxOnvifMixin``.

    # Per-tick spaces / rooms / users / groups reconciliation lives in
    # ``_coordinator_spaces.AjaxSpacesMixin``.

    # Device polling, attribute normalisation, stale-device cleanup and
    # motion-detection auto-reset live in
    # ``_coordinator_devices.AjaxDevicesMixin``.

    # Notification refresh, video edges, smart locks, and the parsers for
    # security state / device type / notification type live in
    # ``_coordinator_state.AjaxStateUpdaterMixin``.

    # ============================================================================
    # Control methods
    # ============================================================================

    # Arm / disarm / night-mode / panic / group actions live in
    # ``_coordinator_arm.AjaxArmServiceMixin`` to keep this file focused
    # on the polling-and-state-update pipeline.

    # ============================================================================
    # Helper methods
    # ============================================================================

    def get_space(self, space_id: str) -> AjaxSpace | None:
        """Get a space by ID."""
        return self.account.spaces.get(space_id) if self.account else None

    def get_device(self, space_id: str, device_id: str) -> AjaxDevice | None:
        """Get a device by space and device ID."""
        space = self.get_space(space_id)
        return space.devices.get(device_id) if space else None

    def get_room(self, space_id: str, room_id: str) -> AjaxRoom | None:
        """Get a room by space and room ID."""
        space = self.get_space(space_id)
        return space.rooms.get(room_id) if space else None

    def get_group(self, space_id: str, group_id: str) -> AjaxGroup | None:
        """Get a group by space and group ID."""
        space = self.get_space(space_id)
        return space.groups.get(group_id) if space else None

    async def async_shutdown(self) -> None:
        """Shutdown the coordinator and cleanup resources."""
        _LOGGER.info("Shutting down Ajax coordinator")

        # Stop ONVIF Manager (local AI detections)
        if self.onvif_manager:
            try:
                _LOGGER.debug("Stopping ONVIF Manager...")
                await self.onvif_manager.async_stop()
            except Exception as err:
                _LOGGER.error("Error stopping ONVIF Manager: %s", err)

        # Stop door sensor polling task
        if self._door_sensor_poll_task is not None:
            self._door_sensor_poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._door_sensor_poll_task
            self._door_sensor_poll_task = None

        # Close API connection
        await self.api.close()

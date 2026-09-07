"""Options flow for the Ajax integration.

Split out of ``config_flow`` (which keeps the credential-driven
``AjaxConfigFlow``). ``AjaxOptionsFlow`` only reads the existing config
entry / runtime data — it never talks to the Ajax API.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_DOOR_SENSOR_FAST_POLL,
    CONF_ENABLED_SPACES,
    CONF_MONITORED_SPACES,
    CONF_NOTIFICATION_FILTER,
    CONF_PERSISTENT_NOTIFICATION,
    CONF_RTSP_PASSWORD,
    CONF_RTSP_USERNAME,
    NOTIFICATION_FILTER_ALARMS_ONLY,
    NOTIFICATION_FILTER_ALL,
    NOTIFICATION_FILTER_NONE,
    NOTIFICATION_FILTER_SECURITY_EVENTS,
)

_LOGGER = logging.getLogger(__name__)


class AjaxOptionsFlow(OptionsFlow):
    """Handle Ajax options."""

    def _mask_credential(self, value: str | None) -> str:
        """Mask a credential for display (show first 4 and last 4 chars)."""
        if not value or len(value) < 10:
            return "—"
        return f"{value[:4]}****{value[-4:]}"

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options - main menu."""
        menu_options = ["enabled_spaces", "notifications", "polling_settings"]

        # RTSP/ONVIF credentials are strictly local-network credentials.
        menu_options.append("rtsp_credentials")

        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
        )

    async def async_step_enabled_spaces(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage enabled spaces."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_spaces = user_input.get(CONF_ENABLED_SPACES, [])

            if not selected_spaces:
                errors["base"] = "no_spaces_selected"
            else:
                # Update config entry data with new enabled spaces
                new_data = {**self.config_entry.data}
                new_data[CONF_ENABLED_SPACES] = selected_spaces

                # The update listener detects the data change and schedules
                # the reload (single reload decision point).
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data=new_data,
                )

                return self.async_create_entry(title="", data=self.config_entry.options)

        # Get all available spaces from coordinator
        space_options = []
        try:
            coordinator = self.config_entry.runtime_data
        except AttributeError:
            coordinator = None

        if coordinator and hasattr(coordinator, "all_discovered_spaces"):
            # Use all discovered spaces (not just enabled ones)
            for space_id, space_name in coordinator.all_discovered_spaces.items():
                space_options.append({"value": space_id, "label": space_name})
        elif coordinator and hasattr(coordinator, "account") and coordinator.account:
            # Fallback to currently loaded spaces
            for space_id, space in coordinator.account.spaces.items():
                space_options.append({"value": space_id, "label": space.name})

        # Get currently enabled spaces
        current_enabled = self.config_entry.data.get(CONF_ENABLED_SPACES, [])
        if not current_enabled and space_options:
            # If no spaces configured, default to all available
            current_enabled = [opt["value"] for opt in space_options]

        # Build schema
        if space_options:
            data_schema = vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED_SPACES,
                        default=current_enabled,
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=space_options,  # type: ignore[typeddict-item]
                            mode=SelectSelectorMode.LIST,
                            multiple=True,
                        )
                    ),
                }
            )
        else:
            # No spaces available - show message
            return self.async_abort(reason="no_spaces_available")

        return self.async_show_form(
            step_id="enabled_spaces",
            data_schema=data_schema,
            errors=errors,
        )

    async def async_step_notifications(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage notification options."""
        if user_input is not None:
            # Merge with existing options
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        # Get current options
        current_filter = self.config_entry.options.get(CONF_NOTIFICATION_FILTER, NOTIFICATION_FILTER_NONE)
        current_persistent = self.config_entry.options.get(CONF_PERSISTENT_NOTIFICATION, False)
        current_spaces = self.config_entry.options.get(CONF_MONITORED_SPACES, [])

        # Get available spaces from coordinator
        space_options = []
        try:
            coordinator = self.config_entry.runtime_data
        except AttributeError:
            coordinator = None

        if coordinator and hasattr(coordinator, "account") and coordinator.account:
            for space_id, space in coordinator.account.spaces.items():
                space_options.append(
                    {
                        "value": space_id,
                        "label": space.name,
                    }
                )
            # If no spaces selected, select all by default
            if not current_spaces:
                current_spaces = list(coordinator.account.spaces.keys())

        # Build options schema
        schema_dict = {
            vol.Optional(
                CONF_PERSISTENT_NOTIFICATION,
                default=current_persistent,
            ): bool,
            vol.Optional(
                CONF_NOTIFICATION_FILTER,
                default=current_filter,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        NOTIFICATION_FILTER_NONE,
                        NOTIFICATION_FILTER_ALARMS_ONLY,
                        NOTIFICATION_FILTER_SECURITY_EVENTS,
                        NOTIFICATION_FILTER_ALL,
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="notification_filter",
                )
            ),
        }

        # Add spaces selector only if spaces are available
        if space_options:
            schema_dict[
                vol.Optional(
                    CONF_MONITORED_SPACES,
                    default=current_spaces,
                )
            ] = SelectSelector(
                SelectSelectorConfig(
                    options=space_options,  # type: ignore[typeddict-item]
                    mode=SelectSelectorMode.DROPDOWN,
                    multiple=True,
                )
            )

        return self.async_show_form(
            step_id="notifications",
            data_schema=vol.Schema(schema_dict),
        )

    async def async_step_polling_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage polling settings."""
        if user_input is not None:
            # Merge with existing options
            new_options = {**self.config_entry.options, **user_input}
            return self.async_create_entry(title="", data=new_options)

        # Get current options (default: disabled to reduce API calls)
        current_fast_poll = self.config_entry.options.get(CONF_DOOR_SENSOR_FAST_POLL, False)

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DOOR_SENSOR_FAST_POLL,
                    default=current_fast_poll,
                ): bool,
            }
        )

        return self.async_show_form(
            step_id="polling_settings",
            data_schema=data_schema,
        )

    async def async_step_rtsp_credentials(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage RTSP/ONVIF credentials for Video Edge cameras."""
        if user_input is not None:
            # Update options with RTSP credentials
            new_options = {**self.config_entry.options}

            # Store credentials (even if empty to clear them)
            new_options[CONF_RTSP_USERNAME] = user_input.get(CONF_RTSP_USERNAME, "")
            new_options[CONF_RTSP_PASSWORD] = user_input.get(CONF_RTSP_PASSWORD, "")

            # The ONVIF manager only reads these credentials at bootstrap
            # (`_async_init_onvif`) — the update listener detects the change
            # and schedules the reload so local AI detections use them. The
            # RTSP camera stream path reads the options on every request and
            # would work either way.
            return self.async_create_entry(title="", data=new_options)

        # Get current credentials
        current_username = self.config_entry.options.get(CONF_RTSP_USERNAME, "")
        current_password = self.config_entry.options.get(CONF_RTSP_PASSWORD, "")

        data_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_RTSP_USERNAME,
                    description={"suggested_value": current_username},
                ): str,
                vol.Optional(
                    CONF_RTSP_PASSWORD,
                    description={"suggested_value": ""},  # Don't show password
                ): str,
            }
        )

        return self.async_show_form(
            step_id="rtsp_credentials",
            data_schema=data_schema,
            description_placeholders={
                "current_username": current_username or "—",
                "current_password": self._mask_credential(current_password) if current_password else "—",
            },
        )

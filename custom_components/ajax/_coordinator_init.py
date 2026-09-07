"""Bootstrap mixin for ``AjaxDataCoordinator``.

Owns the lazy initialisations the coordinator runs after the REST login
is complete: build the in-memory account, build the in-memory account and persist local smart-lock metadata.

Cloud event transports are intentionally absent from this hardened build.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any


from .const import DOMAIN
from .models import AjaxAccount

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    
    from .api import AjaxRestApi
_LOGGER = logging.getLogger(__name__)


class AjaxBootstrapMixin:
    """Coordinator mixin for account/bootstrap state."""

    # Host attributes — provided by the coordinator __init__.
    if TYPE_CHECKING:
        account: AjaxAccount | None
        api: AjaxRestApi
        hass: HomeAssistant
        config_entry: ConfigEntry | None

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------

    async def _async_init_account(self) -> None:
        """Initialise the in-memory account from the login response.

        Ajax has no /user endpoint, so we synthesise the account from
        what the login already returned (``user_id`` + ``email``).
        """
        self.account = AjaxAccount(
            user_id=self.api.user_id or "",
            name=self.api.email.split("@")[0] if self.api.email else "Unknown",
            email=self.api.email or "",
        )
        # Log only a truncated user_id — the full value is PII (and doubles as a
        # session token in direct mode), so keep it out of shared INFO logs.
        _LOGGER.info(
            "Initialized account for %s (user_id: %s…)",
            self.account.name,
            self.account.user_id[:8],
        )


"""Ajax REST API — MotionCam camera endpoints."""

from __future__ import annotations

from typing import Any

import aiohttp

from ..const import (
    AJAX_REST_API_TIMEOUT,
)
from ._base import (
    _LOGGER,
    AjaxRestApiError,
    AjaxRestClientBase,
    AjaxRestConnectionError,
)


class _CamerasMixin(AjaxRestClientBase):
    """MotionCam camera endpoint methods (mixed into ``AjaxRestApi``)."""

    async def async_get_cameras(self, hub_id: str) -> list[dict[str, Any]]:
        """Get all cameras for a specific hub.

        Args:
            hub_id: Hub ID

        Returns:
            List of camera dictionaries
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/cameras")  # type: ignore[no-any-return]

    async def async_get_camera(self, hub_id: str, camera_id: str) -> dict[str, Any]:
        """Get camera details.

        Args:
            hub_id: Hub ID
            camera_id: Camera ID

        Returns:
            Camera details dictionary
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        return await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/cameras/{camera_id}")  # type: ignore[no-any-return]

    async def async_get_camera_snapshot(self, hub_id: str, camera_id: str) -> bytes:
        """Get camera snapshot.

        Args:
            hub_id: Hub ID
            camera_id: Camera ID

        Returns:
            Snapshot image data as bytes
        """
        if not self.session_token:
            raise AjaxRestApiError("Not logged in. Call async_login() first.")

        await self._proactive_token_refresh()

        url = self._build_url(f"user/{self.user_id}/hubs/{hub_id}/cameras/{camera_id}/snapshot")
        session = await self._get_session()

        headers: dict[str, str] = {k: v for k, v in self._base_headers.items() if v is not None}
        headers["X-Session-Token"] = self.session_token

        # Capture token version BEFORE the request to detect concurrent refresh
        token_version_before = self._token_version

        try:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    # Try to recover auth and retry once
                    async with self._auth_lock:
                        if self._token_version != token_version_before:
                            _LOGGER.debug("Token already refreshed by another coroutine")
                        else:
                            await self._recover_auth()
                    headers["X-Session-Token"] = self.session_token
                    async with session.get(
                        url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
                        allow_redirects=False,
                    ) as retry_response:
                        retry_response.raise_for_status()
                        return await retry_response.read()

                response.raise_for_status()
                return await response.read()
        except aiohttp.ClientError as err:
            raise AjaxRestConnectionError(f"Camera snapshot failed: {err}") from err
        except TimeoutError as err:
            raise AjaxRestConnectionError("Camera snapshot timeout") from err

    async def async_get_camera_stream_url(self, hub_id: str, camera_id: str) -> str:
        """Get camera stream URL.

        Args:
            hub_id: Hub ID
            camera_id: Camera ID

        Returns:
            Stream URL string
        """
        data = await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/cameras/{camera_id}/stream")
        return data.get("url", "")  # type: ignore[no-any-return]

    async def async_get_hub_logs(self, hub_id: str, page: int = 1) -> list[dict[str, Any]]:
        """Get one page of hub event-log entries.

        MotionCam alarm photos are not camera snapshots. They are attached to
        log rows (``PHOTOS_RESOURCE_DESCRIPTION``) on this endpoint.

        Args:
            hub_id: Hub ID
            page: 1-based page, clamped to the API maximum of 50

        Returns:
            List of log-entry dictionaries. A non-list payload becomes an empty list.
        """
        if not self.user_id:
            raise AjaxRestApiError("No user_id available. Call async_login() first.")
        safe_page = 1
        if isinstance(page, int) and not isinstance(page, bool):
            safe_page = min(max(page, 1), 50)
        data = await self._request("GET", f"user/{self.user_id}/hubs/{hub_id}/logs?page={safe_page}")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

"""Ajax REST API client based on official PDF documentation."""

from __future__ import annotations

from ._arm import _ArmMixin
from ._base import (
    ADAPTIVE_TTL_MIN,
    MAX_RETRIES,
    MIN_LOGIN_INTERVAL,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW,
    RETRY_BACKOFF_BASE,
    RETRY_BACKOFF_MAX,
    SESSION_TOKEN_TTL,
    TOKEN_REFRESH_MARGIN,
    AjaxRestApiError,
    AjaxRestAuthError,
    AjaxRestConnectionError,
    AjaxRestRateLimitError,
)
from ._cameras import _CamerasMixin
from ._devices import _DevicesMixin
from ._hubs import _HubsMixin
from ._video import _VideoMixin


class AjaxRestApi(_HubsMixin, _DevicesMixin, _CamerasMixin, _VideoMixin, _ArmMixin):
    """Ajax REST API client.

    Authentication as User (from PDF page 4-5):
    - First login via Credentials: E-mail + SHA256(password)
    - Generate a temporary token
    - Use this token for subsequent API calls

    This hardened fork supports only direct Enterprise API authentication.
    """


__all__ = [
    "AjaxRestApi",
    "AjaxRestApiError",
    "AjaxRestAuthError",
    "AjaxRestConnectionError",
    "AjaxRestRateLimitError",
    "MAX_RETRIES",
    "RETRY_BACKOFF_BASE",
    "RETRY_BACKOFF_MAX",
    "RATE_LIMIT_REQUESTS",
    "RATE_LIMIT_WINDOW",
    "MIN_LOGIN_INTERVAL",
    "SESSION_TOKEN_TTL",
    "TOKEN_REFRESH_MARGIN",
    "ADAPTIVE_TTL_MIN",
]

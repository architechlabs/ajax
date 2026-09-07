"""Ajax REST API client — shared transport core (session, auth, retry, request).

Houses ``AjaxRestClientBase`` plus the module-level constants and typed
exceptions. Owns ``asyncio`` / ``aiohttp`` (backoff sleeps and session
creation live here, which the auth tests patch).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

from ..const import (
    AJAX_REST_API_BASE_URL,
    AJAX_REST_API_TIMEOUT,
)

_LOGGER = logging.getLogger("custom_components.ajax.api")

# Retry configuration
MAX_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0  # Base delay in seconds
RETRY_BACKOFF_MAX = 30.0  # Maximum delay in seconds

# Rate limiting configuration
RATE_LIMIT_REQUESTS = 60  # Max requests per window
RATE_LIMIT_WINDOW = 60  # Window in seconds

# Login cooldown to avoid 429 on rapid re-authentication
MIN_LOGIN_INTERVAL = 30  # Minimum seconds between full logins

# Proactive token refresh: refresh before expiry to avoid 401 cascades
SESSION_TOKEN_TTL = 900  # 15 minutes (Ajax API default)
TOKEN_REFRESH_MARGIN = 120  # Refresh 2 minutes before expiry
# Adaptive TTL: if a 401 occurs before this fraction of TTL, reduce effective TTL
ADAPTIVE_TTL_MIN = 45  # Minimum effective TTL in seconds


class AjaxRestApiError(Exception):
    """Base exception for Ajax REST API errors."""


class AjaxRestAuthError(AjaxRestApiError):
    """Authentication error."""

    def __init__(self, message: str = "Authentication failed", error_type: str = "generic"):
        """Initialize auth error with type.

        Args:
            message: Error message
            error_type: Type of error (generic, invalid_password, invalid_api_key, invalid_account_type)
        """
        super().__init__(message)
        self.error_type = error_type


class AjaxRestConnectionError(AjaxRestApiError):
    """Connection error (network issues)."""


class AjaxRestRateLimitError(AjaxRestApiError):
    """Rate limit exceeded."""


class AjaxRestClientBase:
    """Transport, authentication and request-retry core shared by the API mixins."""

    def __init__(
        self,
        api_key: str,
        email: str,
        password: str,
        password_is_hashed: bool = False,
        totp_secret: str | None = None,
        session: aiohttp.ClientSession | None = None,
    ):
        """Initialize the API client.

        Args:
            api_key: Enterprise API key provided by Ajax Systems
            email: User email address
            password: User password (plain or SHA256 hashed)
            password_is_hashed: True if password is already SHA256 hashed
            totp_secret: Optional Base32 TOTP secret (2FA); when set, a fresh
                code is generated for every /login call
            session: Optional Home Assistant aiohttp session.
        """
        if not api_key:
            raise ValueError("Enterprise Ajax API key is required for direct mode")

        self.api_key = api_key
        self.email = email
        self.totp_secret = totp_secret or None

        # Hash password if not already hashed
        if password_is_hashed:
            self.password_hash = password
        else:
            self.password_hash = hashlib.sha256(password.encode()).hexdigest()

        self.session: aiohttp.ClientSession | None = session
        self._owns_session = session is None  # Track if we created the session
        self.session_token: str | None = None  # Session token (15 min TTL)
        self.refresh_token: str | None = None  # Refresh token (7 days TTL)
        self.user_id: str | None = None  # User ID from login
        self._auth_lock: asyncio.Lock = asyncio.Lock()  # Prevent concurrent token refresh

        self.last_cache_ttl: int | None = None
        self.last_cache_hit: bool = False

        # Rate limiting state
        self._request_timestamps: list[float] = []
        self._rate_limit_lock: asyncio.Lock = asyncio.Lock()

        # Login cooldown to avoid 429 on rapid re-authentication
        self._last_login_time: float = 0.0
        # Token version to detect if another coroutine already refreshed
        self._token_version: int = 0
        # Timestamp when session token was obtained (for proactive refresh)
        self._token_obtained_at: float = 0.0
        # Adaptive TTL: reduced when the API expires tokens earlier than expected.
        self._effective_ttl: float = SESSION_TOKEN_TTL
        # Track consecutive refresh failures before falling back to login.
        self._refresh_failures: int = 0

        # Short-lived in-memory cache of GET /spaces/{id} responses.
        # The same payload is consumed by ``async_get_video_edges`` and
        # ``async_get_smart_locks`` within a single coordinator tick, so
        # without coalescing we double the spaces fetch for every cycle
        # that has both a camera and a smart lock.
        self._space_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._space_cache_ttl: float = 5.0

        # Short-lived cache of GET /hubs/{id}/devices responses, keyed by
        # (hub_id, enrich). The periodic update loop and the door-sensor
        # fast-poll loop both hit this endpoint; when their schedules cross
        # the cache coalesces them into a single request.
        self._devices_cache: dict[tuple[str, bool], tuple[float, list[dict[str, Any]]]] = {}
        self._devices_cache_ttl: float = 5.0

        # Short-lived cache of the GET /user/{id}/hubs list. At setup the
        # connection test and the coordinator's first refresh both fetch it
        # back-to-back; without coalescing the first boot pays for the round
        # trip twice. A short TTL also folds in rapid consecutive ticks with no
        # staleness risk because hub membership changes slowly.
        self._hubs_cache: tuple[float, list[dict[str, Any]]] | None = None
        self._hubs_cache_ttl: float = 5.0

        # Base headers always contain the Enterprise API key.
        self._base_headers = {
            "Content-Type": "application/json",
        }
        if api_key:
            self._base_headers["X-Api-Key"] = api_key

    def _get_base_url(self, for_login: bool = False) -> str:
        """Return the fixed official Ajax API base URL."""
        return AJAX_REST_API_BASE_URL

    @staticmethod
    def _build_url(endpoint: str) -> str:
        """Build an API URL while rejecting host-changing paths."""
        if (
            not endpoint
            or endpoint.startswith(("/", "\\"))
            or endpoint.startswith((".",))
            or "://" in endpoint
            or "\x00" in endpoint
            or "\n" in endpoint
            or "\r" in endpoint
        ):
            raise AjaxRestApiError("Invalid API endpoint")
        return f"{AJAX_REST_API_BASE_URL}/{endpoint}"

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(limit=5, limit_per_host=5, ssl=True)
            self.session = aiohttp.ClientSession(connector=connector, trust_env=False)
            self._owns_session = True
        return self.session

    def bypass_cache_next(self) -> None:
        """Compatibility no-op retained for existing callers.

        The hardened direct transport has no intermediary cache to bypass.
        """
        return

    def _cache_entry_usable(self, written_at: float, ttl: float) -> bool:
        """True if a cache entry may be served instead of re-fetching.

        Within TTL always. The direct API has no intermediary cache-bypass
        header, so this helper simply applies the local cache TTL.
        """
        if (time.time() - written_at) >= ttl:
            return False
        return True

    async def close(self) -> None:
        """Close the session if we own it."""
        if self._owns_session and self.session and not self.session.closed:
            await self.session.close()

    async def _check_rate_limit(self) -> None:
        """Check and enforce rate limiting.

        Releases the lock during sleep to avoid blocking other requests.
        """
        wait_time = 0.0

        # Phase 1: Check if we need to wait (under lock)
        async with self._rate_limit_lock:
            now = time.time()
            # Remove timestamps outside the window
            self._request_timestamps = [ts for ts in self._request_timestamps if now - ts < RATE_LIMIT_WINDOW]
            # Check if we're at the limit
            if len(self._request_timestamps) >= RATE_LIMIT_REQUESTS:
                oldest = self._request_timestamps[0]
                wait_time = RATE_LIMIT_WINDOW - (now - oldest)

        # Phase 2: Wait outside the lock (allows other requests to proceed)
        if wait_time > 0:
            _LOGGER.warning(
                "Rate limit reached (%d/%d requests in %ds), waiting %.1fs",
                RATE_LIMIT_REQUESTS,
                RATE_LIMIT_REQUESTS,
                RATE_LIMIT_WINDOW,
                wait_time,
            )
            await asyncio.sleep(wait_time)

        # Phase 3: Record this request (under lock)
        async with self._rate_limit_lock:
            self._request_timestamps.append(time.time())

    @staticmethod
    def _calculate_backoff(attempt: int) -> float:
        """Calculate exponential backoff delay.

        Args:
            attempt: Current retry attempt (0-based)

        Returns:
            Delay in seconds
        """
        delay: float = RETRY_BACKOFF_BASE * (2**attempt)
        return min(delay, RETRY_BACKOFF_MAX)

    @staticmethod
    def _parse_retry_after(value: str | None, default: int = 60) -> int:
        """Parse a Retry-After header value into a number of seconds.

        Per RFC 7231/9110 the header may be either a number of seconds or an
        HTTP-date. An unparsable value falls back to ``default`` instead of
        raising (a bare ``int()`` raises ValueError on an HTTP-date, which
        would crash the retry path exactly when the server asks us to back off).

        Args:
            value: Raw header value (may be None when absent)
            default: Fallback in seconds when the value is missing or invalid

        Returns:
            Delay in seconds (never negative)
        """
        if value is None:
            return default
        try:
            return int(value)
        except ValueError:
            pass
        try:
            retry_dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return default
        delta = retry_dt.timestamp() - time.time()
        if delta <= 0:
            return 0
        return int(delta)

    async def async_login(self) -> str:
        """Login with email and SHA256(password) to get session token.

        According to Swagger API 1.147.0:
        - Authenticates with email + SHA256(password) (+ TOTP code if 2FA is
          enabled on the account — mandatory for all Enterprise API logins
          from 2025-09-01)
        - Returns sessionToken (15 min TTL), refreshToken (7 days TTL, or
          1 year when a valid TOTP was supplied), and userId
        - POST body: {"login": email, "passwordHash": hash, "totp": code?}
        - This is a single-step login; the API no longer exposes a separate
          two-step verification endpoint

        For proxy modes, the proxy may also return:
        - apiKey: API key to use for direct requests (hybrid mode)
        - sseUrl: URL for SSE event stream

        Returns:
            Session token string

        Raises:
            AjaxRestAuthError: If authentication fails (including an invalid
                TOTP secret)
            AjaxRestApiError: For other API errors
        """
        _LOGGER.debug("Starting Ajax API login")

        session = await self._get_session()
        url = self._build_url("login")

        # Login request body according to Swagger
        payload: dict[str, str] = {
            "login": self.email,
            "passwordHash": self.password_hash,
        }
        if self.totp_secret:
            import pyotp  # local import: keeps setup import light

            try:
                payload["totp"] = pyotp.TOTP(self.totp_secret).now()
            except Exception as err:  # invalid base32 secret, etc.
                raise AjaxRestAuthError("Invalid TOTP secret", error_type="invalid_totp_secret") from err

        try:
            async with session.post(
                url,
                headers=self._base_headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
                allow_redirects=False,
            ) as response:
                _LOGGER.debug("Login response status: %s", response.status)

                if response.status == 401:
                    # Try to get error message from response body
                    try:
                        result = await response.json()
                        error_msg = result.get("message", "")
                        _LOGGER.debug("Login 401 error message: %s", error_msg)

                        if "not authorized" in error_msg.lower():
                            # "User is not authorized" = missing or invalid API key
                            raise AjaxRestAuthError("Invalid API key", error_type="invalid_api_key")
                        elif "wrong login or password" in error_msg.lower():
                            # "Wrong login or password" = bad credentials
                            raise AjaxRestAuthError(
                                "Invalid email or password",
                                error_type="invalid_password",
                            )
                        elif "pro" in error_msg.lower() or "enterprise" in error_msg.lower():
                            # Account type mismatch
                            raise AjaxRestAuthError(
                                "Account type not supported (PRO account detected)",
                                error_type="invalid_account_type",
                            )
                        else:
                            raise AjaxRestAuthError(
                                error_msg or "Authentication failed",
                                error_type="generic",
                            )
                    except AjaxRestAuthError:
                        raise
                    except (json.JSONDecodeError, KeyError, ValueError, aiohttp.ContentTypeError):
                        raise AjaxRestAuthError("Invalid email or password", error_type="invalid_password") from None
                elif response.status == 500:
                    # 500 often means invalid API key
                    _LOGGER.debug("Login 500 error - likely invalid API key")
                    raise AjaxRestAuthError("Invalid API key or server error", error_type="invalid_api_key")
                elif response.status == 403:
                    # API 1.147.0 has no distinct signal for "2FA required" vs
                    # a bad password/TOTP — surface it as an auth failure so
                    # the config flow re-prompts instead of a generic error.
                    _LOGGER.debug("Login 403 - authentication rejected")
                    raise AjaxRestAuthError("Authentication failed", error_type="invalid_password")

                response.raise_for_status()
                result = await response.json()

                # Extract tokens from response
                # Proxy format: {"user_id": "xxx"}
                # Direct API format: {"sessionToken": "xxx", "userId": "xxx"}
                self.session_token = result.get("sessionToken")
                self.refresh_token = result.get("refreshToken")
                self.user_id = result.get("userId") or result.get("user_id")

                if not self.session_token:
                    raise AjaxRestApiError("No sessionToken in login response")

                self._token_version += 1
                self._last_login_time = time.monotonic()
                self._token_obtained_at = time.monotonic()
                self._refresh_failures = 0  # Reset on successful login
                _LOGGER.info(
                    "Login successful, session token obtained (user: %s, effective TTL: %.0fs)",
                    (self.user_id or "")[:8] or "?",
                    self._effective_ttl,
                )
                return self.session_token

        except aiohttp.ClientError as err:
            _LOGGER.error("Login request failed: %s", err)
            raise AjaxRestApiError(f"Login failed: {err}") from err
        except TimeoutError as err:
            _LOGGER.error("Login request timeout")
            raise AjaxRestApiError("Login timeout") from err

    async def async_refresh_token(self) -> str:
        """Refresh session token using refresh token.

        According to Swagger API 1.130.0:
        - Uses refreshToken to get a new sessionToken without re-authenticating
        - Extends session without requiring email/password
        - POST body: {"refreshToken": refresh_token, "userId": user_id}

        Returns:
            New session token string

        Raises:
            AjaxRestAuthError: If refresh token is invalid or expired
            AjaxRestApiError: For other API errors
        """
        if not self.refresh_token or not self.user_id:
            raise AjaxRestApiError("No refresh token available. Call async_login() first.")

        _LOGGER.debug("Refreshing Ajax session token")

        session = await self._get_session()
        url = self._build_url("refresh")

        # Refresh request body according to Swagger
        payload = {
            "refreshToken": self.refresh_token,
            "userId": self.user_id,
        }

        # Include session token in headers (required by proxy even for refresh)
        headers = {
            **self._base_headers,
            "X-Session-Token": self.session_token or "",
        }

        try:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
                allow_redirects=False,
            ) as response:
                _LOGGER.debug("Refresh response status: %s", response.status)

                if response.status == 401:
                    _LOGGER.warning("Refresh token rejected (401)")
                    raise AjaxRestAuthError("Refresh token expired or invalid")

                if response.status == 429:
                    _LOGGER.warning("Refresh endpoint rate limited (429)")
                    raise AjaxRestAuthError("Refresh rate limited")

                response.raise_for_status()
                result = await response.json()

                # Extract new tokens from response
                self.session_token = result.get("sessionToken")
                self.refresh_token = result.get("refreshToken")
                # userId should remain the same

                if not self.session_token:
                    raise AjaxRestApiError("No sessionToken in refresh response")

                self._token_version += 1
                self._token_obtained_at = time.monotonic()
                _LOGGER.info(
                    "Session token refreshed successfully (user: %s)",
                    (self.user_id or "")[:8] or "?",
                )
                return self.session_token

        except aiohttp.ClientError as err:
            _LOGGER.error("Token refresh request failed: %s", err)
            raise AjaxRestApiError(f"Token refresh failed: {err}") from err
        except TimeoutError as err:
            _LOGGER.error("Token refresh request timeout")
            raise AjaxRestApiError("Token refresh timeout") from err

    async def _proactive_token_refresh(self) -> None:
        """Refresh token proactively before it expires.

        Uses adaptive TTL that adjusts when proxies invalidate tokens earlier
        than the standard 15-minute Ajax API TTL. Falls back to full login
        when refresh consistently fails (common in proxy mode).
        """
        if not self._token_obtained_at:
            return

        token_age = time.monotonic() - self._token_obtained_at
        # Use adaptive TTL (may be reduced from 900s if proxy expires tokens early)
        refresh_threshold = max(self._effective_ttl - TOKEN_REFRESH_MARGIN, ADAPTIVE_TTL_MIN * 0.7)

        if token_age < refresh_threshold:
            return  # Token still fresh enough

        _LOGGER.debug(
            "Token age %.0fs (effective TTL %.0fs), proactive refresh",
            token_age,
            self._effective_ttl,
        )

        async with self._auth_lock:
            # Re-check after acquiring lock (another coroutine may have refreshed)
            if time.monotonic() - self._token_obtained_at < refresh_threshold:
                return

            # Skip refresh if it consistently fails (proxy mode)
            if self._refresh_failures < 3 and self.refresh_token:
                try:
                    await self.async_refresh_token()
                    self._refresh_failures = 0
                    _LOGGER.info("Proactive token refresh successful")
                    return
                except (AjaxRestAuthError, AjaxRestApiError) as err:
                    self._refresh_failures += 1
                    _LOGGER.warning(
                        "Proactive refresh failed (%s), attempt %d/3",
                        err,
                        self._refresh_failures,
                    )

            # Refresh failed or disabled — fall back to login immediately
            elapsed = time.monotonic() - self._last_login_time
            if elapsed >= MIN_LOGIN_INTERVAL:
                try:
                    await self.async_login()
                    _LOGGER.info("Proactive login successful (refresh unavailable)")
                except (AjaxRestAuthError, AjaxRestApiError) as err:
                    _LOGGER.warning("Proactive login failed: %s", err)

    async def _recover_auth(self) -> None:
        """Recover from authentication failure.

        Tries refresh token first (if available and not consistently failing),
        then falls back to full login. Adjusts effective TTL when tokens expire
        earlier than expected (common with proxies).
        Must be called while holding _auth_lock.
        """
        # Adaptive TTL: if 401 occurs much earlier than expected, reduce TTL
        if self._token_obtained_at:
            token_age = time.monotonic() - self._token_obtained_at
            if token_age < self._effective_ttl * 0.5:
                # Token died at less than half the expected TTL
                new_ttl = max(token_age * 0.8, ADAPTIVE_TTL_MIN)
                if new_ttl < self._effective_ttl:
                    _LOGGER.info(
                        "Adaptive TTL: token expired after %.0fs, reducing effective TTL from %.0fs to %.0fs",
                        token_age,
                        self._effective_ttl,
                        new_ttl,
                    )
                    self._effective_ttl = new_ttl

        # Skip refresh if no refresh token or if refresh consistently fails
        if self.refresh_token and self._refresh_failures < 3:
            try:
                await self.async_refresh_token()
                self._refresh_failures = 0
                _LOGGER.info("Token refreshed successfully, retrying request")
                return
            except (AjaxRestAuthError, AjaxRestApiError):
                self._refresh_failures += 1
                _LOGGER.warning(
                    "Refresh token failed (attempt %d/3), falling back to full login",
                    self._refresh_failures,
                )
        elif self._refresh_failures >= 3:
            _LOGGER.debug("Skipping refresh (failed %d times), using login", self._refresh_failures)

        # Fallback to full login (with cooldown)
        elapsed = time.monotonic() - self._last_login_time
        if elapsed < MIN_LOGIN_INTERVAL:
            wait = MIN_LOGIN_INTERVAL - elapsed
            _LOGGER.warning(
                "Login cooldown active, last login %.0fs ago (min %ds), waiting %.0fs",
                elapsed,
                MIN_LOGIN_INTERVAL,
                wait,
            )
            await asyncio.sleep(wait)
        try:
            await self.async_login()
            _LOGGER.info("Full login successful")
        except Exception as err:
            _LOGGER.error("Failed to renew token: %s", err)
            raise AjaxRestAuthError("Token renewal failed") from err

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        _retry_on_auth_error: bool = True,
        _retry_count: int = 0,
        bypass_cache: bool = False,
    ) -> Any:
        """Make API request with session token.

        Automatically renews the token if it expires (401 error).
        Implements retry with exponential backoff for transient errors.

        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint (without base URL)
            data: Optional JSON data for POST/PUT requests
            _retry_on_auth_error: Internal flag to prevent infinite retry loop
            _retry_count: Current retry attempt (internal)
            bypass_cache: If True, send X-Cache-Control: no-cache to bypass proxy cache

        Returns:
            API response as dict

        Raises:
            AjaxRestAuthError: If authentication fails
            AjaxRestApiError: For other API errors
        """
        if not self.session_token:
            raise AjaxRestApiError("Not logged in. Call async_login() first.")

        # Proactive token refresh before it expires (avoids 401 cascades)
        if _retry_on_auth_error:
            await self._proactive_token_refresh()

        # Apply rate limiting
        await self._check_rate_limit()

        url = self._build_url(endpoint)
        session = await self._get_session()

        # Headers with session token (Swagger spec)
        headers = {
            **self._base_headers,
            "X-Session-Token": self.session_token,
        }
        # The bypass is a short time-window (not a one-shot flag) so that EVERY
        # request in the refresh cycle gets the no-cache header, not just the
        # first one.

        # Capture token version BEFORE the request to detect if another
        # coroutine refreshes the token while our request is in flight
        token_version_before = self._token_version

        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    if _retry_on_auth_error:
                        async with self._auth_lock:
                            if self._token_version != token_version_before:
                                # Another coroutine already refreshed tokens
                                _LOGGER.debug(
                                    "Token already refreshed by another coroutine (v%d → v%d)",
                                    token_version_before,
                                    self._token_version,
                                )
                            else:
                                await self._recover_auth()
                        # Retry the request with the new token (only once), preserving bypass_cache
                        return await self._request(
                            method, endpoint, data, _retry_on_auth_error=False, bypass_cache=bypass_cache
                        )
                    else:
                        # Already retried once, give up
                        _LOGGER.error("Authentication failed after token renewal")
                        raise AjaxRestAuthError("Invalid or expired token")
                elif response.status == 403:
                    _LOGGER.error("Access denied (403) - Insufficient permissions")
                    raise AjaxRestAuthError("Access denied")
                elif response.status == 429:
                    # Rate limited by server - retry with backoff
                    retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                    if _retry_count < MAX_RETRIES:
                        # Cap retry_after to reasonable max (don't wait 60s on first retry)
                        wait_time = min(retry_after, 5 + (_retry_count * 5))
                        _LOGGER.warning(
                            "Rate limited on %s, waiting %ds (attempt %d/%d)",
                            endpoint,
                            wait_time,
                            _retry_count + 1,
                            MAX_RETRIES,
                        )
                        await asyncio.sleep(wait_time)
                        # Do not trigger a proactive reauth during a rate-limit retry
                        return await self._request(method, endpoint, data, False, _retry_count + 1, bypass_cache)
                    _LOGGER.error("Rate limited on %s after %d retries", endpoint, MAX_RETRIES)
                    raise AjaxRestRateLimitError(f"Rate limited, retry after {retry_after}s")

                # Handle server errors (5xx) as transient - retry with backoff
                if response.status >= 500:
                    if _retry_count < MAX_RETRIES:
                        delay = self._calculate_backoff(_retry_count)
                        _LOGGER.warning(
                            "Server error %s on %s, retrying in %.1fs (attempt %d/%d)",
                            response.status,
                            endpoint,
                            delay,
                            _retry_count + 1,
                            MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        return await self._request(
                            method, endpoint, data, _retry_on_auth_error, _retry_count + 1, bypass_cache
                        )
                    _LOGGER.error(
                        "Server error %s on %s after %d retries",
                        response.status,
                        endpoint,
                        MAX_RETRIES,
                    )
                    raise AjaxRestApiError(f"Server error: {response.status}")

                response.raise_for_status()

                return await response.json()

        except aiohttp.ClientError as err:
            # Transient network errors - retry with backoff
            if _retry_count < MAX_RETRIES:
                delay = self._calculate_backoff(_retry_count)
                _LOGGER.warning(
                    "Connection error on %s: %s, retrying in %.1fs (attempt %d/%d)",
                    endpoint,
                    err,
                    delay,
                    _retry_count + 1,
                    MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                return await self._request(method, endpoint, data, _retry_on_auth_error, _retry_count + 1, bypass_cache)
            _LOGGER.error("API request to %s failed after %d retries: %s", endpoint, MAX_RETRIES, err)
            raise AjaxRestConnectionError(f"Connection failed: {err}") from err
        except TimeoutError as err:
            # Timeout - retry with backoff
            if _retry_count < MAX_RETRIES:
                delay = self._calculate_backoff(_retry_count)
                _LOGGER.warning(
                    "Timeout on %s, retrying in %.1fs (attempt %d/%d)",
                    endpoint,
                    delay,
                    _retry_count + 1,
                    MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                return await self._request(method, endpoint, data, _retry_on_auth_error, _retry_count + 1, bypass_cache)
            _LOGGER.error("API request to %s timed out after %d retries", endpoint, MAX_RETRIES)
            raise AjaxRestConnectionError("Request timeout") from err

    async def _request_no_response(
        self,
        method: str,
        endpoint: str,
        data: dict[str, Any] | None = None,
        _retry_on_auth_error: bool = True,
        _retry_count: int = 0,
    ) -> None:
        """Make API request that returns no content (204).

        Implements retry with exponential backoff for transient errors.

        Args:
            method: HTTP method (PUT, DELETE, etc.)
            endpoint: API endpoint (without base URL)
            data: Optional JSON data for request body
            _retry_on_auth_error: Internal flag to prevent infinite retry loop
            _retry_count: Current retry attempt (internal)

        Raises:
            AjaxRestAuthError: If authentication fails
            AjaxRestApiError: For other API errors
        """
        if not self.session_token:
            raise AjaxRestApiError("Not logged in. Call async_login() first.")

        # Proactive token refresh before it expires (avoids 401 cascades)
        if _retry_on_auth_error:
            await self._proactive_token_refresh()

        # Apply rate limiting
        await self._check_rate_limit()

        url = self._build_url(endpoint)
        session = await self._get_session()

        headers = {
            **self._base_headers,
            "X-Session-Token": self.session_token,
        }

        # Capture token version BEFORE the request to detect if another
        # coroutine refreshes the token while our request is in flight
        token_version_before = self._token_version

        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=data,
                timeout=aiohttp.ClientTimeout(total=AJAX_REST_API_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    if _retry_on_auth_error:
                        async with self._auth_lock:
                            if self._token_version != token_version_before:
                                _LOGGER.debug(
                                    "Token already refreshed by another coroutine (v%d → v%d)",
                                    token_version_before,
                                    self._token_version,
                                )
                            else:
                                await self._recover_auth()
                        return await self._request_no_response(method, endpoint, data, _retry_on_auth_error=False)
                    else:
                        raise AjaxRestAuthError("Invalid or expired token")

                if response.status == 429:
                    # Rate limited by server - retry with backoff (same as _request)
                    retry_after = self._parse_retry_after(response.headers.get("Retry-After"))
                    if _retry_count < MAX_RETRIES:
                        wait_time = min(retry_after, 5 + (_retry_count * 5))
                        _LOGGER.warning(
                            "Rate limited on %s, waiting %ds (attempt %d/%d)",
                            endpoint,
                            wait_time,
                            _retry_count + 1,
                            MAX_RETRIES,
                        )
                        await asyncio.sleep(wait_time)
                        return await self._request_no_response(
                            method, endpoint, data, _retry_on_auth_error, _retry_count + 1
                        )
                    _LOGGER.error("Rate limited on %s after %d retries", endpoint, MAX_RETRIES)
                    raise AjaxRestRateLimitError(f"Rate limited, retry after {retry_after}s")

                # Handle server errors (5xx) as transient - retry with backoff
                if response.status >= 500:
                    if _retry_count < MAX_RETRIES:
                        delay = self._calculate_backoff(_retry_count)
                        _LOGGER.warning(
                            "Server error %s on %s, retrying in %.1fs (attempt %d/%d)",
                            response.status,
                            endpoint,
                            delay,
                            _retry_count + 1,
                            MAX_RETRIES,
                        )
                        await asyncio.sleep(delay)
                        return await self._request_no_response(
                            method, endpoint, data, _retry_on_auth_error, _retry_count + 1
                        )
                    error_text = await response.text()
                    _LOGGER.error(
                        "Server error %s after %d retries",
                        response.status,
                        MAX_RETRIES,
                    )
                    _LOGGER.debug("Server error body: %s", error_text[:200] if error_text else "(empty)")
                    raise AjaxRestApiError(f"Server error {response.status}")

                if response.status not in (200, 202, 204):
                    error_text = await response.text()
                    _LOGGER.error("API error %s", response.status)
                    _LOGGER.debug("API error body: %s", error_text[:200] if error_text else "(empty)")
                    raise AjaxRestApiError(f"API error {response.status}")

        except aiohttp.ClientError as err:
            # Transient network errors - retry with backoff
            if _retry_count < MAX_RETRIES:
                delay = self._calculate_backoff(_retry_count)
                _LOGGER.warning(
                    "Connection error on %s: %s, retrying in %.1fs (attempt %d/%d)",
                    endpoint,
                    err,
                    delay,
                    _retry_count + 1,
                    MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                return await self._request_no_response(method, endpoint, data, _retry_on_auth_error, _retry_count + 1)
            _LOGGER.error("API request to %s failed after %d retries: %s", endpoint, MAX_RETRIES, err)
            raise AjaxRestConnectionError(f"Connection failed: {err}") from err
        except TimeoutError as err:
            # Timeout - retry with backoff
            if _retry_count < MAX_RETRIES:
                delay = self._calculate_backoff(_retry_count)
                _LOGGER.warning(
                    "Timeout on %s, retrying in %.1fs (attempt %d/%d)",
                    endpoint,
                    delay,
                    _retry_count + 1,
                    MAX_RETRIES,
                )
                await asyncio.sleep(delay)
                return await self._request_no_response(method, endpoint, data, _retry_on_auth_error, _retry_count + 1)
            _LOGGER.error("API request to %s timed out after %d retries", endpoint, MAX_RETRIES)
            raise AjaxRestConnectionError("Request timeout") from err

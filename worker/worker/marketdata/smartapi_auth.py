"""SmartAPI authentication: TOTP login, JWT renewal, feed-token custody.

Everything here was verified against the official docs and the official
Python/Node SDKs (see ``docs/research/smartapi-ws-findings.md`` and the working
TypeScript client in the India Earnings Tracker):

* base host ``https://apiconnect.angelone.in``;
* ``POST /rest/auth/angelbroking/user/v1/loginByPassword`` with body
  ``{clientcode, password, totp}`` — the API key travels in ``X-PrivateKey``,
  never in the body;
* ``POST /rest/auth/angelbroking/jwt/v1/generateTokens`` with body
  ``{refreshToken}`` and ``Authorization: Bearer <jwt>``;
* since the 2025 SEBI hardening every call must carry
  ``X-ClientLocalIP`` / ``X-ClientPublicIP`` / ``X-MACAddress`` /
  ``X-UserType`` / ``X-SourceID`` or it is rejected with ``AG8001``;
* the login response also carries ``feedToken``, which the market-data
  WebSocket needs as a handshake header;
* the exchange throttles logins (the dashboard documents ~1/second and blocks
  repeated failures), so at most one TOTP login attempt per 90 seconds.

Secrets are never logged: the JWT/refresh/feed tokens and the PIN are
``repr=False`` and error text is scrubbed before it reaches a log record.
"""

from __future__ import annotations

import asyncio
import socket
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pyotp

from ..config import AngelSettings
from ..logging_setup import get_logger, scrub

log = get_logger(__name__)

LOGIN_PATH = "/rest/auth/angelbroking/user/v1/loginByPassword"
RENEW_PATH = "/rest/auth/angelbroking/jwt/v1/generateTokens"

# Exchange-side lockout guard: a wrong TOTP repeated quickly gets the API key
# temporarily blocked, which would take the whole session down.
LOGIN_COOLDOWN_SECONDS = 90.0

DEFAULT_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "x-usertype": "USER",
    # Verified 2026-10-12 against apiconnect.angelone.in (live login → HTTP 200
    # with a JWT). "PARDHD" is rejected by Kong with a bodyless HTTP 400 —
    # "WEB" is the official SDK constant.
    "x-sourceid": "WEB",
}


class AngelAuthError(RuntimeError):
    """Authentication failed. ``retryable`` decides reconnect vs. stop."""

    def __init__(self, message: str, *, retryable: bool = True, code: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class AngelSession:
    """A live SmartAPI session."""

    jwt_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    feed_token: str = field(repr=False)
    client_code: str
    issued_at: float
    via: str  # "login" | "renew"

    def age_seconds(self, *, now: float) -> float:
        return max(0.0, now - self.issued_at)

    def is_stale(self, *, settings: AngelSettings, now: float) -> bool:
        return self.age_seconds(now=now) >= settings.reauth_minutes * 60.0

    def redacted(self) -> dict[str, object]:
        """Safe-for-logs description."""
        return {
            "client_code": self.client_code,
            "issued_at": self.issued_at,
            "via": self.via,
            "has_jwt": bool(self.jwt_token),
            "has_feed_token": bool(self.feed_token),
            "has_refresh_token": bool(self.refresh_token),
        }


def detect_local_ip() -> str:
    """Best-effort primary outbound IPv4 (no packet is actually sent)."""
    global _LOCAL_IP_CACHE
    if _LOCAL_IP_CACHE:
        return _LOCAL_IP_CACHE
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        _LOCAL_IP_CACHE = str(sock.getsockname()[0])
    except OSError:
        _LOCAL_IP_CACHE = "127.0.0.1"
    finally:
        sock.close()
    return _LOCAL_IP_CACHE


_LOCAL_IP_CACHE = ""


def detect_mac() -> str:
    """Deterministic MAC-style string from the machine node id."""
    return ":".join(format(uuid.getnode(), "012x")[i : i + 2] for i in range(0, 12, 2))


@dataclass
class SmartApiAuth:
    """Caches one session and renews it before it expires."""

    settings: AngelSettings
    client: httpx.AsyncClient | None = None
    now: Callable[[], float] = time.time
    _session: AngelSession | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _last_login_attempt: float = field(default=0.0, repr=False)
    _owned_client: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.request_timeout_seconds),
                follow_redirects=False,
            )
            self._owned_client = True

    # ── public ────────────────────────────────────────────────────────────

    @property
    def session(self) -> AngelSession | None:
        return self._session

    async def get_session(self) -> AngelSession:
        """Return a usable session, logging in or renewing as needed."""
        current = self._session
        if current and not current.is_stale(settings=self.settings, now=self.now()):
            return current
        async with self._lock:
            # Re-check inside the lock: a concurrent caller may have renewed.
            current = self._session
            if current and not current.is_stale(settings=self.settings, now=self.now()):
                return current
            return await self._establish(current)

    async def force_reauth(self, reason: str) -> AngelSession:
        """Drop the cached session (401/AG8001) and build a fresh one."""
        async with self._lock:
            log.warning("smartapi_reauth", reason=scrub(reason))
            previous = self._session
            self._session = None
            return await self._establish(previous)

    async def websocket_headers(self) -> dict[str, str]:
        """Handshake headers for ``smart-stream`` (docs: 4 mandatory headers)."""
        session = await self.get_session()
        if not session.feed_token:
            raise AngelAuthError(
                "no feed token: login with ANGEL_PIN + ANGEL_TOTP_SECRET (or supply "
                "ANGEL_REFRESH_TOKEN and ANGEL_FEED_TOKEN) to open the market-data socket",
                retryable=False,
            )
        return {
            "Authorization": session.jwt_token,
            "x-api-key": self.settings.api_key,
            "x-client-code": session.client_code,
            "x-feed-token": session.feed_token,
        }

    async def rest_headers(self, *, jwt: str | None = None) -> dict[str, str]:
        """Headers for a signed REST call (identity headers included)."""
        session = await self.get_session()
        headers = dict(DEFAULT_HEADERS)
        headers.update(
            {
                "x-privatekey": self.settings.api_key,
                "x-clientlocalip": self._local_ip(),
                "x-clientpublicip": self._public_ip(),
                "x-macaddress": self._mac(),
                "authorization": f"Bearer {jwt or session.jwt_token}",
            }
        )
        return headers

    async def close(self) -> None:
        if self._owned_client:
            await self.client.aclose()

    # ── internals ─────────────────────────────────────────────────────────

    def _local_ip(self) -> str:
        return self.settings.local_ip or detect_local_ip()

    def _public_ip(self) -> str:
        # The tracker learned that an empty public-IP header is accepted while
        # a wrong one can trip fraud checks; without configuration we simply
        # reuse the local address, which the API tolerates for market data.
        return self.settings.public_ip or self._local_ip()

    def _mac(self) -> str:
        return self.settings.mac_address or detect_mac()

    async def _establish(self, previous: AngelSession | None) -> AngelSession:
        stored_refresh = previous.refresh_token if previous else self.settings.refresh_token
        if stored_refresh:
            try:
                return await self._renew(stored_refresh, jwt=previous.jwt_token if previous else "")
            except AngelAuthError as exc:
                if not exc.retryable:
                    raise
                log.warning("smartapi_renew_failed", error=scrub(exc.message), code=exc.code)
        return await self._login()

    async def _login(self) -> AngelSession:
        if not self.settings.can_login:
            raise AngelAuthError(
                "Angel One login needs ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN "
                "and ANGEL_TOTP_SECRET",
                retryable=False,
            )
        waited = self.now() - self._last_login_attempt
        if waited < LOGIN_COOLDOWN_SECONDS:
            raise AngelAuthError(
                f"login attempt rate-limited locally ({waited:.0f}s since the last one, "
                f"{LOGIN_COOLDOWN_SECONDS:.0f}s cooldown)",
                retryable=True,
            )
        self._last_login_attempt = self.now()
        payload = {
            "clientcode": self.settings.client_code,
            "password": self.settings.pin,
            "totp": pyotp.TOTP(self.settings.totp_secret).now(),
        }
        data = await self._post(LOGIN_PATH, payload, jwt=None)
        session = self._session_from(data, via="login", refresh_fallback=self.settings.refresh_token)
        self._session = session
        log.info("smartapi_login_ok", **session.redacted())
        return session

    async def _renew(self, refresh_token: str, *, jwt: str) -> AngelSession:
        data = await self._post(
            RENEW_PATH, {"refreshToken": refresh_token}, jwt=jwt or None
        )
        # Renewal rotates the refresh token; keep the returned one so the next
        # renewal works. The chain dies on a process restart, which then falls
        # back to a TOTP login — that is the documented behaviour.
        session = self._session_from(data, via="renew", refresh_fallback=refresh_token)
        self._session = session
        log.info("smartapi_renew_ok", **session.redacted())
        return session

    def _session_from(
        self, data: dict[str, object], *, via: str, refresh_fallback: str = ""
    ) -> AngelSession:
        jwt_token = str(data.get("jwtToken") or data.get("jwt_token") or "")
        refresh = str(data.get("refreshToken") or data.get("refresh_token") or "") or refresh_fallback
        feed = str(data.get("feedToken") or data.get("feed_token") or "") or self.settings.feed_token
        if not jwt_token:
            raise AngelAuthError(f"Angel One returned no JWT on {via}", retryable=False)
        if not feed:
            log.warning("smartapi_no_feed_token", hint="market-data socket cannot authenticate")
        return AngelSession(
            jwt_token=jwt_token,
            refresh_token=refresh,
            feed_token=feed,
            client_code=self.settings.client_code,
            issued_at=self.now(),
            via=via,
        )

    async def _post(self, path: str, payload: dict[str, object], *, jwt: str | None) -> dict[str, object]:
        headers = dict(DEFAULT_HEADERS)
        headers["x-privatekey"] = self.settings.api_key
        headers["x-clientlocalip"] = self._local_ip()
        headers["x-clientpublicip"] = self._public_ip()
        headers["x-macaddress"] = self._mac()
        if jwt:
            headers["authorization"] = f"Bearer {jwt}"
        url = f"{self.settings.base_url}{path}"
        try:
            response = await self.client.post(
                url, json=payload, headers=headers, timeout=self.settings.request_timeout_seconds
            )
        except httpx.HTTPError as exc:
            raise AngelAuthError(f"network error during {path}: {type(exc).__name__}", retryable=True) from exc

        body: dict[str, object] = {}
        try:
            parsed = response.json()
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            body = parsed
        envelope = body.get("data") if isinstance(body.get("data"), dict) else body

        status = response.status_code
        message = str(body.get("message") or body.get("errorcode") or "")
        if status in (401, 403):
            raise AngelAuthError(
                f"authentication refused ({status}) {message[:200]}".strip(),
                retryable=True,
                code=str(body.get("errorcode", "")),
            )
        if status == 429:
            raise AngelAuthError("Angel One rate-limited the auth endpoint (429)", retryable=True)
        if status >= 400 or not isinstance(envelope, dict) or not envelope:
            raise AngelAuthError(
                f"{path} failed: HTTP {status} {message[:200]}".strip(),
                retryable=status >= 500,
                code=str(body.get("errorcode", "")),
            )
        return envelope

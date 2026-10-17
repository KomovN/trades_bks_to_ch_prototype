"""Безопасное получение и кэширование access token БКС."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
import logging
import math
import time
from typing import Any, Literal, Protocol

import httpx


logger = logging.getLogger(__name__)

TOKEN_URL = (
    "https://be.broker.ru/trade-api-keycloak/realms/tradeapi/"
    "protocol/openid-connect/token"
)
CLIENT_ID = "trade-api-read"
DEFAULT_HTTP_TIMEOUT_SEC = 15.0
DEFAULT_SAFETY_MARGIN_SEC = 30.0
DEFAULT_RETRY_AFTER_CAP_SEC = 60.0


class AuthError(RuntimeError):
    """Refresh token отклонён; повтор без новых credentials не поможет."""


class TemporaryAuthError(RuntimeError):
    """Временная ошибка token endpoint с допустимым bounded retry."""

    def __init__(self, message: str, *, retry_after_sec: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class AuthConfigurationError(RuntimeError):
    """Ошибка конфигурации или протокола token endpoint."""


class TokenTransportError(RuntimeError):
    """Санитизированная сетевая ошибка HTTP transport."""


class TokenTransportTimeout(TokenTransportError):
    """Санитизированный timeout HTTP transport."""


@dataclass(frozen=True, slots=True)
class TokenEndpointResponse:
    """Минимальный ответ transport; payload и headers скрыты из repr."""

    status_code: int
    payload: Any = field(default=None, repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


class TokenTransport(Protocol):
    def __call__(
        self, url: str, form: Mapping[str, str]
    ) -> Awaitable[TokenEndpointResponse]: ...


Clock = Callable[[], float]


async def httpx_token_transport(
    url: str, form: Mapping[str, str]
) -> TokenEndpointResponse:
    """Выполнить token request без секретных деталей в исключениях."""

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_HTTP_TIMEOUT_SEC) as client:
            response = await client.post(url, data=form)
    except httpx.TimeoutException:
        raise TokenTransportTimeout("token endpoint request timed out") from None
    except httpx.RequestError:
        raise TokenTransportError("token endpoint network request failed") from None

    try:
        payload: Any = response.json()
    except ValueError:
        payload = None

    retry_after = response.headers.get("Retry-After")
    safe_headers = {} if retry_after is None else {"retry-after": retry_after}
    return TokenEndpointResponse(
        status_code=response.status_code,
        payload=payload,
        headers=safe_headers,
    )


class AccessTokenProvider:
    """Выдать static token или обновляемый token с monotonic deadline."""

    def __init__(
        self,
        *,
        refresh_token: str | None,
        static_access_token: str | None,
        max_age_sec: float,
        transport: TokenTransport = httpx_token_transport,
        clock: Clock = time.monotonic,
        safety_margin_sec: float = DEFAULT_SAFETY_MARGIN_SEC,
        retry_after_cap_sec: float = DEFAULT_RETRY_AFTER_CAP_SEC,
    ) -> None:
        self._validate_duration("max_age_sec", max_age_sec, allow_zero=False)
        self._validate_duration("safety_margin_sec", safety_margin_sec, allow_zero=True)
        self._validate_duration(
            "retry_after_cap_sec", retry_after_cap_sec, allow_zero=True
        )

        self._refresh_token = refresh_token
        self._static_access_token = static_access_token
        self._max_age_sec = float(max_age_sec)
        self._transport = transport
        self._clock = clock
        self._safety_margin_sec = float(safety_margin_sec)
        self._retry_after_cap_sec = float(retry_after_cap_sec)
        self._cached_token: str | None = None
        self._deadline: float | None = None
        self._lock = asyncio.Lock()

    def __repr__(self) -> str:
        cached = self._cached_token is not None
        return f"{type(self).__name__}(mode={self.mode!r}, cached={cached})"

    @staticmethod
    def _validate_duration(name: str, value: float, *, allow_zero: bool) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AuthConfigurationError(f"{name} must be a finite number")
        numeric = float(value)
        if not math.isfinite(numeric) or numeric < 0 or (numeric == 0 and not allow_zero):
            raise AuthConfigurationError(f"{name} is outside the allowed range")

    @property
    def mode(self) -> Literal["static", "refresh"]:
        """Режим для распознавания необновляемого static token reader-ом."""

        return "static" if self._static_access_token is not None else "refresh"

    @property
    def is_static(self) -> bool:
        return self.mode == "static"

    @property
    def deadline(self) -> float | None:
        """Monotonic deadline кэша; для static token он неизвестен."""

        return None if self.is_static else self._deadline

    def ttl(self) -> float | None:
        """Оставшийся TTL; ``None`` для static token или пустого кэша."""

        deadline = self.deadline
        if deadline is None:
            return None
        return max(0.0, deadline - self._clock())

    async def get_token(self) -> str:
        if self._static_access_token is not None:
            return self._static_access_token
        if self._refresh_token is None:
            raise AuthConfigurationError(
                "ACCESS_TOKEN or REFRESH_TOKEN must be configured"
            )

        async with self._lock:
            now = self._clock()
            if (
                self._cached_token is not None
                and self._deadline is not None
                and now < self._deadline
            ):
                return self._cached_token

            response = await self._request_token()
            token, lifetime_sec = self._parse_success(response)
            cached_at = self._clock()
            margin_sec = min(self._safety_margin_sec, lifetime_sec * 0.1)
            self._cached_token = token
            self._deadline = cached_at + lifetime_sec - margin_sec
            logger.info("access token refreshed")
            return token

    def invalidate(self) -> bool:
        """Сбросить refresh-кэш; вернуть False для static token."""

        if self.is_static:
            return False
        self._cached_token = None
        self._deadline = None
        return True

    async def _request_token(self) -> TokenEndpointResponse:
        assert self._refresh_token is not None
        form = {
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        try:
            response = await self._transport(TOKEN_URL, form)
        except TokenTransportTimeout:
            raise TemporaryAuthError("token endpoint request timed out") from None
        except TokenTransportError:
            raise TemporaryAuthError("token endpoint network request failed") from None
        except TimeoutError:
            raise TemporaryAuthError("token endpoint request timed out") from None

        if not isinstance(response, TokenEndpointResponse):
            raise AuthConfigurationError(
                "token transport returned an invalid response"
            )

        status = response.status_code
        retry_after = self._retry_after(response.headers)
        if status == 429:
            raise TemporaryAuthError(
                "token endpoint rate limited the request",
                retry_after_sec=retry_after,
            )
        if 500 <= status <= 599:
            raise TemporaryAuthError(
                f"token endpoint returned HTTP {status}",
                retry_after_sec=retry_after,
            )
        if status in {401, 403}:
            raise AuthError(f"token endpoint rejected credentials with HTTP {status}")
        if status == 400 and self._error_code(response.payload) == "invalid_grant":
            raise AuthError("token endpoint rejected the refresh token")
        if 400 <= status <= 499:
            raise AuthConfigurationError(f"token endpoint returned HTTP {status}")
        if status != 200:
            raise AuthConfigurationError(
                f"token endpoint returned unexpected HTTP {status}"
            )
        return response

    def _parse_success(self, response: TokenEndpointResponse) -> tuple[str, float]:
        payload = response.payload
        if not isinstance(payload, Mapping):
            raise AuthConfigurationError("token endpoint returned invalid JSON payload")

        token = payload.get("access_token")
        if not isinstance(token, str) or not token.strip():
            raise AuthConfigurationError(
                "token endpoint response does not contain a valid access_token"
            )

        expires_in = payload.get("expires_in")
        if (
            isinstance(expires_in, bool)
            or not isinstance(expires_in, (int, float))
            or not math.isfinite(float(expires_in))
            or float(expires_in) <= 0
        ):
            lifetime_sec = self._max_age_sec
            logger.warning(
                "token endpoint returned no valid expires_in; using configured max age"
            )
        else:
            lifetime_sec = min(self._max_age_sec, float(expires_in))
        return token, lifetime_sec

    @staticmethod
    def _error_code(payload: Any) -> str | None:
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("error")
        return value if isinstance(value, str) else None

    def _retry_after(self, headers: Mapping[str, str]) -> float | None:
        raw: str | None = None
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() == "retry-after":
                raw = value
                break
        if not isinstance(raw, str):
            return None
        try:
            seconds = float(raw)
        except ValueError:
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
        return min(seconds, self._retry_after_cap_sec)

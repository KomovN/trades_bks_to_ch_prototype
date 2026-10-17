import asyncio
import logging
import traceback

import pytest

from app.token_provider import (
    CLIENT_ID,
    TOKEN_URL,
    AccessTokenProvider,
    AuthConfigurationError,
    AuthError,
    TemporaryAuthError,
    TokenEndpointResponse,
    TokenTransportError,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeTransport:
    def __init__(self, responses: list[TokenEndpointResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def __call__(self, url: str, form: object) -> TokenEndpointResponse:
        assert isinstance(form, dict)
        self.calls.append((url, dict(form)))
        return self.responses.pop(0)


def provider_for(
    transport: object,
    *,
    clock: FakeClock | None = None,
    static: str | None = None,
    refresh: str | None = "refresh-secret",
    max_age: float = 100.0,
) -> AccessTokenProvider:
    return AccessTokenProvider(
        refresh_token=refresh,
        static_access_token=static,
        max_age_sec=max_age,
        transport=transport,  # type: ignore[arg-type]
        clock=clock or FakeClock(),
        safety_margin_sec=10.0,
        retry_after_cap_sec=60.0,
    )


def test_static_token_has_priority_and_explicit_mode() -> None:
    async def forbidden_transport(url: str, form: object) -> TokenEndpointResponse:
        raise AssertionError((url, form))

    provider = provider_for(
        forbidden_transport,
        static="static-secret",
        refresh="refresh-secret",
    )

    assert asyncio.run(provider.get_token()) == "static-secret"
    assert provider.mode == "static"
    assert provider.is_static is True
    assert provider.deadline is None
    assert provider.ttl() is None
    assert provider.invalidate() is False


def test_refresh_form_cache_deadline_and_invalidate() -> None:
    clock = FakeClock()
    transport = FakeTransport(
        [
            TokenEndpointResponse(200, {"access_token": "one", "expires_in": 50}),
            TokenEndpointResponse(200, {"access_token": "two", "expires_in": 50}),
        ]
    )
    provider = provider_for(transport, clock=clock)

    assert asyncio.run(provider.get_token()) == "one"
    assert transport.calls == [
        (
            TOKEN_URL,
            {
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": "refresh-secret",
            },
        )
    ]
    assert provider.mode == "refresh"
    assert provider.deadline == pytest.approx(145.0)
    assert provider.ttl() == pytest.approx(45.0)

    clock.advance(44.0)
    assert asyncio.run(provider.get_token()) == "one"
    assert provider.ttl() == pytest.approx(1.0)
    assert provider.invalidate() is True
    assert provider.deadline is None
    assert asyncio.run(provider.get_token()) == "two"
    assert len(transport.calls) == 2


def test_max_age_limits_expires_in_and_expired_cache_refreshes() -> None:
    clock = FakeClock()
    transport = FakeTransport(
        [
            TokenEndpointResponse(
                200, {"access_token": "one", "expires_in": 500}
            ),
            TokenEndpointResponse(
                200, {"access_token": "two", "expires_in": 500}
            ),
        ]
    )
    provider = provider_for(transport, clock=clock, max_age=20.0)

    assert asyncio.run(provider.get_token()) == "one"
    assert provider.ttl() == pytest.approx(18.0)
    clock.advance(18.0)
    assert asyncio.run(provider.get_token()) == "two"


@pytest.mark.parametrize("expires_in", [None, "3600", 0, -1, float("nan"), True])
def test_invalid_expires_in_uses_safe_max_age(
    expires_in: object, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "sentinel-access-secret"
    payload = {"access_token": secret}
    if expires_in is not None:
        payload["expires_in"] = expires_in
    provider = provider_for(
        FakeTransport([TokenEndpointResponse(200, payload)]), max_age=20.0
    )

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(provider.get_token()) == secret

    assert provider.ttl() == pytest.approx(18.0)
    assert "expires_in" in caplog.text
    assert secret not in caplog.text


def test_simultaneous_get_token_calls_share_one_refresh() -> None:
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def transport(url: str, form: object) -> TokenEndpointResponse:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return TokenEndpointResponse(
            200, {"access_token": "shared", "expires_in": 50}
        )

    async def scenario() -> list[str]:
        provider = provider_for(transport)
        tasks = [asyncio.create_task(provider.get_token()) for _ in range(20)]
        await entered.wait()
        release.set()
        return await asyncio.gather(*tasks)

    assert asyncio.run(scenario()) == ["shared"] * 20
    assert calls == 1


@pytest.mark.parametrize("status", [429, 500, 502, 599])
def test_temporary_http_errors_have_bounded_retry_after(status: int) -> None:
    provider = provider_for(
        FakeTransport(
            [
                TokenEndpointResponse(
                    status,
                    {"sentinel": "body-secret"},
                    {"Retry-After": "999"},
                )
            ]
        )
    )

    with pytest.raises(TemporaryAuthError) as raised:
        asyncio.run(provider.get_token())

    assert raised.value.retry_after_sec == 60.0
    assert "body-secret" not in repr(raised.value)


@pytest.mark.parametrize(
    ("status", "payload"),
    [
        (400, {"error": "invalid_grant", "detail": "body-secret"}),
        (401, {"detail": "body-secret"}),
        (403, {"detail": "body-secret"}),
    ],
)
def test_permanent_credential_errors_are_auth_error(
    status: int, payload: object
) -> None:
    provider = provider_for(FakeTransport([TokenEndpointResponse(status, payload)]))

    with pytest.raises(AuthError) as raised:
        asyncio.run(provider.get_token())

    assert "body-secret" not in repr(raised.value)


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_4xx_are_configuration_errors(status: int) -> None:
    provider = provider_for(
        FakeTransport(
            [TokenEndpointResponse(status, {"error": "body-secret"})]
        )
    )

    with pytest.raises(AuthConfigurationError):
        asyncio.run(provider.get_token())


def test_timeout_and_network_errors_are_sanitized() -> None:
    sentinel = "transport-secret"

    async def timeout_transport(url: str, form: object) -> TokenEndpointResponse:
        raise TimeoutError(sentinel)

    async def network_transport(url: str, form: object) -> TokenEndpointResponse:
        raise TokenTransportError(sentinel)

    for transport in (timeout_transport, network_transport):
        provider = provider_for(transport)
        try:
            asyncio.run(provider.get_token())
        except TemporaryAuthError as exc:
            rendered = "".join(traceback.format_exception(exc))
            assert sentinel not in rendered
            assert sentinel not in repr(exc)
        else:
            raise AssertionError("TemporaryAuthError was not raised")


def test_repr_and_logs_do_not_expose_credentials_or_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = provider_for(
        FakeTransport(
            [
                TokenEndpointResponse(
                    400,
                    {"error": "invalid_grant", "detail": "response-secret"},
                )
            ]
        ),
        static=None,
        refresh="refresh-secret",
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(AuthError):
        asyncio.run(provider.get_token())

    rendered = repr(provider) + caplog.text
    assert "refresh-secret" not in rendered
    assert "response-secret" not in rendered


@pytest.mark.parametrize(
    "payload",
    [None, [], {}, {"access_token": None}, {"access_token": " "}],
)
def test_malformed_success_is_configuration_error(payload: object) -> None:
    provider = provider_for(FakeTransport([TokenEndpointResponse(200, payload)]))

    with pytest.raises(AuthConfigurationError):
        asyncio.run(provider.get_token())


def test_missing_tokens_and_invalid_durations_fail_without_transport() -> None:
    provider = provider_for(FakeTransport([]), refresh=None)
    with pytest.raises(AuthConfigurationError):
        asyncio.run(provider.get_token())

    with pytest.raises(AuthConfigurationError):
        AccessTokenProvider(
            refresh_token="refresh",
            static_access_token=None,
            max_age_sec=float("nan"),
        )

import httpx
import pytest

from goway_lib.sms import ConsoleSmsProvider, EskizSmsProvider, build_sms_provider


def test_console_is_the_default_provider():
    assert isinstance(build_sms_provider("console"), ConsoleSmsProvider)
    assert isinstance(build_sms_provider(""), ConsoleSmsProvider)
    assert isinstance(build_sms_provider("eskiz", eskiz_email="a@b.c", eskiz_password="x"), EskizSmsProvider)


async def test_console_provider_logs_the_message(caplog):
    with caplog.at_level("INFO", logger="goway_lib.sms"):
        await ConsoleSmsProvider().send("+998901234567", "GoWay kodi: 123456")
    assert "+998901234567" in caplog.text
    assert "123456" in caplog.text


def _eskiz(handler) -> EskizSmsProvider:
    provider = EskizSmsProvider("ops@goway.uz", "secret")
    provider._client = httpx.AsyncClient(base_url=EskizSmsProvider.BASE_URL, transport=httpx.MockTransport(handler))
    return provider


async def test_eskiz_logs_in_once_then_reuses_the_token():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"data": {"token": "tok-1"}})
        assert request.headers["Authorization"] == "Bearer tok-1"
        assert b"998901234567" in request.content
        return httpx.Response(200, json={"status": "waiting"})

    provider = _eskiz(handler)
    await provider.send("+998901234567", "salom")
    await provider.send("+998901234567", "yana")
    assert calls.count("/api/auth/login") == 1
    assert calls.count("/api/message/sms/send") == 2


async def test_eskiz_logs_in_again_when_the_token_expired():
    tokens = iter(["stale", "fresh"])
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"data": {"token": next(tokens)}})
        token = request.headers["Authorization"].removeprefix("Bearer ")
        seen.append(token)
        return httpx.Response(401 if token == "stale" else 200, json={})

    provider = _eskiz(handler)
    await provider.send("+998901234567", "salom")
    assert seen == ["stale", "fresh"]


async def test_eskiz_raises_when_the_provider_keeps_refusing():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/login"):
            return httpx.Response(200, json={"data": {"token": "tok"}})
        return httpx.Response(500, json={})

    with pytest.raises(httpx.HTTPStatusError):
        await _eskiz(handler).send("+998901234567", "salom")

import logging
from typing import Protocol

import httpx

from goway_lib import metrics
from goway_lib.clients import DEFAULT_LIMITS, DEFAULT_TIMEOUT

logger = logging.getLogger(__name__)


class SmsProvider(Protocol):
    """Anything that can put a short text on a phone. Providers never raise for business reasons."""

    async def send(self, phone: str, text: str) -> None: ...


class ConsoleSmsProvider:
    """Writes the text to the local log instead of sending it; a sign-in code in it never leaves for the collector."""

    async def send(self, phone: str, text: str) -> None:
        logger.info("SMS to %s: %s", phone, text, extra={"local_only": True})
        metrics.sms_sent.add(1, {"provider": "console", "outcome": "ok"})


class EskizSmsProvider:
    BASE_URL = "https://notify.eskiz.uz/api"

    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        self._token: str | None = None
        self._client = httpx.AsyncClient(base_url=self.BASE_URL, timeout=DEFAULT_TIMEOUT, limits=DEFAULT_LIMITS)

    async def _login(self) -> str:
        response = await self._client.post("/auth/login", data={"email": self._email, "password": self._password})
        response.raise_for_status()
        self._token = response.json()["data"]["token"]
        return self._token

    async def send(self, phone: str, text: str) -> None:
        try:
            await self._send(phone, text)
        except Exception:
            metrics.sms_sent.add(1, {"provider": "eskiz", "outcome": "error"})
            raise
        metrics.sms_sent.add(1, {"provider": "eskiz", "outcome": "ok"})

    async def _send(self, phone: str, text: str) -> None:
        token = self._token or await self._login()
        response = await self._client.post(
            "/message/sms/send",
            data={"mobile_phone": phone.removeprefix("+"), "message": text, "from": "4546"},
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code == 401:
            token = await self._login()
            response = await self._client.post(
                "/message/sms/send",
                data={"mobile_phone": phone.removeprefix("+"), "message": text, "from": "4546"},
                headers={"Authorization": f"Bearer {token}"},
            )
        response.raise_for_status()


def build_sms_provider(provider: str, *, eskiz_email: str = "", eskiz_password: str = "") -> SmsProvider:
    if provider == "eskiz":
        return EskizSmsProvider(eskiz_email, eskiz_password)
    return ConsoleSmsProvider()

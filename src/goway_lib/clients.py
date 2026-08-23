from typing import Any

import httpx

from goway_lib.errors import UpstreamUnavailable

DEFAULT_TIMEOUT = httpx.Timeout(connect=2.0, read=8.0, write=8.0, pool=2.0)
DEFAULT_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)


class InternalClient:
    def __init__(self, base_url: str, *, service_secret: str, service_name: str):
        self.service_name = service_name
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=DEFAULT_TIMEOUT,
            limits=DEFAULT_LIMITS,
            headers={"x-service-secret": service_secret},
        )

    async def get(self, path: str, params: dict | None = None) -> Any:
        return await self._request("GET", path, params=params)

    async def post(self, path: str, json: Any = None) -> Any:
        return await self._request("POST", path, json=json)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError:
            raise
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(
                f"{self.service_name} is unavailable", params={"service": self.service_name}
            ) from exc
        if response.status_code == 204:
            return None
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()

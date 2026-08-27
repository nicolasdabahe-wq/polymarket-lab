"""Cliente HTTP compartido con reintentos, backoff y respeto de rate limits."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger("pmbot.http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    """Wrapper de httpx.AsyncClient con reintentos exponenciales.

    - 429: respeta Retry-After si viene, si no backoff exponencial.
    - 5xx y errores de red: backoff exponencial con jitter.
    - 4xx (excepto 429): no reintenta, propaga.
    """

    def __init__(self, timeout: float = 20.0, max_retries: int = 4) -> None:
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": "pmbot/0.1 (research; paper-trading)"},
            follow_redirects=True,
        )
        self.max_retries = max_retries

    async def get_json(self, url: str, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None) -> Any:
        resp = await self._request("GET", url, params=params, headers=headers)
        return resp.json()

    async def get_json_con_cabeceras(
            self, url: str, params: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
    ) -> tuple[Any, dict[str, str]]:
        """Como get_json pero devolviendo también las cabeceras.

        The Odds API informa del saldo de créditos en `x-requests-remaining`
        y sin leerlo el bot gastaría el plan a ciegas: cuando se acaben, los
        deportes dejarían de operar sin que nadie se entere.
        """
        resp = await self._request("GET", url, params=params, headers=headers)
        return resp.json(), dict(resp.headers)

    async def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        resp = await self._request("GET", url, params=params)
        return resp.text

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(method, url, **kwargs)
                if resp.status_code in RETRYABLE_STATUS:
                    delay = self._retry_delay(resp, attempt)
                    log.warning("HTTP %s %s -> %s, retry en %.1fs",
                                method, url, resp.status_code, delay)
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                return resp
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_exc = exc
                delay = 2 ** attempt + random.random()
                log.warning("HTTP %s %s error de red (%s), retry en %.1fs",
                            method, url, type(exc).__name__, delay)
                await asyncio.sleep(delay)
        raise last_exc or RuntimeError(f"agotados los reintentos para {url}")

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 60.0)
            except ValueError:
                pass
        return 2 ** attempt + random.random()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "HttpClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

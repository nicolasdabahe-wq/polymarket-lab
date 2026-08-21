"""Precios spot y volatilidad realizada de cripto (Coinbase, API pública).

Binance está geobloqueado en servidores de EE.UU.; Coinbase no.
"""
from __future__ import annotations

import logging
import math
import time

from ..http import HttpClient

log = logging.getLogger("pmbot.data.prices")

SPOT_URL = "https://api.coinbase.com/v2/prices/{product}/spot"
CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"

CACHE_SECONDS = 120.0


class PriceFeed:
    """Spot + volatilidad diaria realizada, con cache corto."""

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._cache: dict[str, tuple[float, float]] = {}       # product -> (ts, spot)
        self._vol_cache: dict[str, tuple[float, float]] = {}   # product -> (ts, vol)

    async def spot(self, product: str) -> float | None:
        now = time.monotonic()
        hit = self._cache.get(product)
        if hit and now - hit[0] < CACHE_SECONDS:
            return hit[1]
        try:
            data = await self.http.get_json(SPOT_URL.format(product=product))
            price = float(data["data"]["amount"])
        except Exception as exc:
            log.warning("spot %s falló: %s", product, exc)
            return None
        self._cache[product] = (now, price)
        return price

    async def daily_vol(self, product: str, lookback_days: int = 30) -> float | None:
        """Desvío estándar de los retornos log diarios (volatilidad diaria)."""
        now = time.monotonic()
        hit = self._vol_cache.get(product)
        if hit and now - hit[0] < 3600:  # la vol diaria cambia lento
            return hit[1]
        try:
            candles = await self.http.get_json(
                CANDLES_URL.format(product=product),
                params={"granularity": 86400})
            closes = [float(c[4]) for c in candles[:lookback_days + 1]]
        except Exception as exc:
            log.warning("velas %s fallaron: %s", product, exc)
            return None
        if len(closes) < 10:
            return None
        # candles vienen de la más nueva a la más vieja
        returns = [math.log(closes[i] / closes[i + 1])
                   for i in range(len(closes) - 1)]
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
        vol = math.sqrt(var)
        self._vol_cache[product] = (now, vol)
        return vol

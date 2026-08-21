"""Cliente de la Gamma API (https://gamma-api.polymarket.com).

Ingerimos EVENTOS (no mercados sueltos) porque los eventos traen `tags`, que
es la única fuente confiable de categoría, y los mercados vienen anidados con
bestBid/bestAsk/outcomePrices incluidos.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterator

from ..http import HttpClient

log = logging.getLogger("pmbot.data.gamma")

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Mapeo de tags de Polymarket a nuestras categorías canónicas (las mismas que
# usa config.yaml en intel.feeds). El primer match gana; 'other' es el default.
TAG_CATEGORY_MAP: list[tuple[tuple[str, ...], str]] = [
    (("politic", "election", "congress", "senate", "trump", "white house",
      "supreme court", "governor"), "politics"),
    (("geopolitic", "world", "ukraine", "israel", "gaza", "china", "russia",
      "iran", "nato", "middle east"), "geopolitics"),
    (("econom", "fed", "fomc", "inflation", "rates", "macro", "business",
      "finance", "gdp", "jobs", "tariff", "cpi"), "economy"),
    (("crypto", "bitcoin", "ethereum", "solana", "defi", "nft"), "crypto"),
    (("sport", "nba", "nfl", "mlb", "nhl", "soccer", "football", "tennis",
      "esport", "ufc", "boxing", "golf", "f1", "olympic", "chess"), "sports"),
    (("culture", "entertainment", "movie", "music", "celebrity", "awards",
      "tv", "pop"), "culture"),
    (("science", "tech", "ai", "space", "climate", "health"), "tech"),
]


@dataclass
class Market:
    condition_id: str
    gamma_id: str
    slug: str
    question: str
    category: str
    end_date: str | None
    liquidity: float
    volume_24h: float
    yes_price: float | None
    best_bid: float | None
    best_ask: float | None
    clob_token_ids: list[str]
    active: bool
    raw: dict[str, Any]


def categorize_tags(tags: list[dict[str, Any]] | None) -> str:
    """Los tags de un evento van de más específico a más general (p.ej.
    ['fomc', ..., 'Politics']), así que el primer tag que mapee decide."""
    for tag in tags or []:
        label = (tag.get("label") or "").lower()
        for needles, category in TAG_CATEGORY_MAP:
            if any(n in label for n in needles):
                return category
    return "other"


def _parse_market(m: dict[str, Any], category: str) -> Market | None:
    condition_id = m.get("conditionId")
    if not condition_id or not m.get("question"):
        return None
    try:
        prices = json.loads(m.get("outcomePrices") or "[]")
        yes_price = float(prices[0]) if prices else None
    except (ValueError, TypeError):
        yes_price = None
    try:
        token_ids = json.loads(m.get("clobTokenIds") or "[]")
    except (ValueError, TypeError):
        token_ids = []

    def fnum(key: str) -> float:
        try:
            return float(m.get(key) or 0)
        except (TypeError, ValueError):
            return 0.0

    return Market(
        condition_id=condition_id,
        gamma_id=str(m.get("id", "")),
        slug=m.get("slug", ""),
        question=m["question"],
        category=category,
        end_date=m.get("endDate"),
        liquidity=fnum("liquidityNum") or fnum("liquidity"),
        volume_24h=fnum("volume24hr"),
        yes_price=yes_price,
        best_bid=float(m["bestBid"]) if m.get("bestBid") is not None else None,
        best_ask=float(m["bestAsk"]) if m.get("bestAsk") is not None else None,
        clob_token_ids=token_ids,
        active=bool(m.get("active")) and not m.get("closed"),
        raw=m,
    )


class GammaClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def fetch_active_markets(
        self, max_markets: int = 600, page_size: int = 100,
        min_liquidity: float = 0.0,
    ) -> list[Market]:
        """Eventos activos por volumen 24h desc, aplanados a mercados."""
        markets: dict[str, Market] = {}
        offset = 0
        while len(markets) < max_markets:
            events = await self.http.get_json(
                f"{GAMMA_BASE}/events",
                params={
                    "limit": page_size, "offset": offset,
                    "active": "true", "closed": "false", "archived": "false",
                    "order": "volume24hr", "ascending": "false",
                },
            )
            if not events:
                break
            for market in self._flatten(events):
                if market.liquidity >= min_liquidity and market.active:
                    markets.setdefault(market.condition_id, market)
            offset += page_size
        result = list(markets.values())[:max_markets]
        log.info("Gamma: %d mercados activos cacheados", len(result))
        return result

    async def fetch_market(self, condition_id: str) -> Market | None:
        """Trae un mercado puntual (para oportunidades fuera del top de
        volumen: las ballenas tienen posiciones grandes en mercados de
        resolución lejana que no entran en el cache diario)."""
        try:
            rows = await self.http.get_json(
                f"{GAMMA_BASE}/markets", params={"condition_ids": condition_id})
        except Exception:
            return None
        if not rows:
            return None
        m = rows[0]
        if m.get("closed") or not m.get("active"):
            return None
        # Sin tags disponibles en este endpoint: se infiere del texto.
        text = f"{m.get('question','')} {m.get('slug','')}"
        category = categorize_tags([{"label": text}])
        return _parse_market(m, category)

    async def market_status(self, condition_id: str) -> dict[str, Any] | None:
        """Estado actual de un mercado puntual (para liquidar posiciones):
        {'closed': bool, 'outcome_prices': [float, ...]} o None si no existe."""
        result = await self.market_statuses([condition_id])
        return result.get(condition_id)

    async def market_statuses(self, condition_ids: list[str],
                              batch_size: int = 20) -> dict[str, dict[str, Any]]:
        """Estado de varios mercados en lotes (la API acepta el parámetro
        condition_ids repetido).

        OJO: algunos mercados cerrados no aparecen sin closed=true explícito,
        así que los que falten se re-consultan con ese flag."""
        out: dict[str, dict[str, Any]] = {}

        async def fetch(batch: list[str], closed_flag: bool) -> None:
            params: list[tuple[str, str]] = [("condition_ids", c) for c in batch]
            if closed_flag:
                params.append(("closed", "true"))
            rows = await self.http.get_json(f"{GAMMA_BASE}/markets",
                                            params=params)
            for m in rows or []:
                cid = m.get("conditionId")
                if not cid:
                    continue
                try:
                    prices = [float(p) for p in
                              json.loads(m.get("outcomePrices") or "[]")]
                except (ValueError, TypeError):
                    prices = []
                out[cid] = {"closed": bool(m.get("closed")),
                            "outcome_prices": prices,
                            "uma_status": m.get("umaResolutionStatus"),
                            "question": m.get("question", "")}

        for i in range(0, len(condition_ids), batch_size):
            await fetch(condition_ids[i:i + batch_size], closed_flag=False)
        missing = [c for c in condition_ids if c not in out]
        for i in range(0, len(missing), batch_size):
            await fetch(missing[i:i + batch_size], closed_flag=True)
        return out

    @staticmethod
    def _flatten(events: list[dict[str, Any]]) -> Iterator[Market]:
        for event in events:
            category = categorize_tags(event.get("tags"))
            for m in event.get("markets") or []:
                parsed = _parse_market(m, category)
                if parsed:
                    yield parsed

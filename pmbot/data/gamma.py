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

    @staticmethod
    def _flatten(events: list[dict[str, Any]]) -> Iterator[Market]:
        for event in events:
            category = categorize_tags(event.get("tags"))
            for m in event.get("markets") or []:
                parsed = _parse_market(m, category)
                if parsed:
                    yield parsed

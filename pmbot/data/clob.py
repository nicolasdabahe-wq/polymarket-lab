"""Cliente read-only del CLOB (https://clob.polymarket.com).

Lectura de order books y precios no requiere autenticación. Las órdenes
(fase de ejecución) usarán py-clob-client con las claves del .env.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..http import HttpClient

CLOB_BASE = "https://clob.polymarket.com"


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: list[BookLevel]  # ordenadas de mejor (mayor) a peor
    asks: list[BookLevel]  # ordenadas de mejor (menor) a peor

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.bids and self.asks:
            return self.asks[0].price - self.bids[0].price
        return None

    @property
    def mid(self) -> float | None:
        if self.bids and self.asks:
            return (self.asks[0].price + self.bids[0].price) / 2
        return None


class ClobClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def order_book(self, token_id: str) -> OrderBook:
        data = await self.http.get_json(f"{CLOB_BASE}/book",
                                        params={"token_id": token_id})
        bids = sorted(
            (BookLevel(float(l["price"]), float(l["size"])) for l in data.get("bids", [])),
            key=lambda l: -l.price)
        asks = sorted(
            (BookLevel(float(l["price"]), float(l["size"])) for l in data.get("asks", [])),
            key=lambda l: l.price)
        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def midpoint(self, token_id: str) -> float | None:
        data = await self.http.get_json(f"{CLOB_BASE}/midpoint",
                                        params={"token_id": token_id})
        try:
            return float(data["mid"])
        except (KeyError, TypeError, ValueError):
            return None

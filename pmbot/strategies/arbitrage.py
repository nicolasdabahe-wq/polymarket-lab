"""Arbitraje binario: comprar YES y NO del mismo mercado si la suma de los
mejores asks es < 1 - fees - margen. Al resolver, una de las dos paga 1.

detect_arbitrage() es pura (testeable); la estrategia verifica el edge contra
el order book real del CLOB antes de ejecutar (los precios del cache de Gamma
pueden estar viejos) y dimensiona según la profundidad disponible.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..data.clob import ClobClient, OrderBook
from ..execution import PaperBroker
from ..risk import OrderRequest

log = logging.getLogger("pmbot.strategies.arbitrage")


@dataclass
class ArbOpportunity:
    condition_id: str
    question: str
    category: str
    yes_token: str
    no_token: str
    yes_ask: float
    no_ask: float

    @property
    def edge(self) -> float:
        return 1.0 - (self.yes_ask + self.no_ask)


def detect_arbitrage(yes_ask: float | None, no_ask: float | None,
                     min_edge: float, fee_bps: float = 0.0) -> float | None:
    """Edge neto si existe arbitraje, None si no. Pura.

    fee_bps se aplica sobre el costo total de entrada (dos compras).
    """
    if not yes_ask or not no_ask or yes_ask <= 0 or no_ask <= 0:
        return None
    cost = yes_ask + no_ask
    fee = cost * fee_bps / 10_000
    edge = 1.0 - cost - fee
    return edge if edge >= min_edge else None


class ArbitrageStrategy:
    name = "arbitrage"

    def __init__(self, conn: sqlite3.Connection, clob: ClobClient,
                 broker: PaperBroker, cfg: dict[str, Any]) -> None:
        self.conn = conn
        self.clob = clob
        self.broker = broker
        self.enabled = bool(cfg.get("enabled", True))
        self.min_edge = float(cfg.get("min_edge", 0.02))
        self.budget_pct = float(cfg.get("budget_pct", 0.30))
        self.max_usdc = float(cfg.get("max_usdc_per_trade", 50))
        self.min_usdc = float(cfg.get("min_usdc_per_trade", 12))
        self.scan_top = int(cfg.get("scan_top_markets", 200))

    def candidates(self) -> list[sqlite3.Row]:
        """Preselección barata con los bestAsk cacheados de Gamma."""
        rows = self.conn.execute(
            """SELECT * FROM markets WHERE active = 1 AND best_ask IS NOT NULL
               AND best_bid IS NOT NULL ORDER BY volume_24h DESC LIMIT ?""",
            (self.scan_top,)).fetchall()
        out = []
        for r in rows:
            # bestAsk del YES; el ask del NO se estima como 1 - bestBid del
            # YES (los books son espejo en mercados binarios). Solo filtro:
            # la verificación real se hace contra el CLOB.
            no_ask_est = 1.0 - r["best_bid"]
            if detect_arbitrage(r["best_ask"], no_ask_est,
                                self.min_edge * 0.5) is not None:
                out.append(r)
        return out

    async def scan_and_execute(self) -> list[str]:
        """Devuelve descripciones de los arbitrajes ejecutados."""
        if not self.enabled:
            return []
        executed: list[str] = []
        for row in self.candidates():
            try:
                tokens = json.loads(row["clob_token_ids"] or "[]")
                if len(tokens) != 2:
                    continue
                yes_book, no_book = (await self.clob.order_book(tokens[0]),
                                     await self.clob.order_book(tokens[1]))
            except Exception as exc:
                log.debug("book falló para %s: %s", row["condition_id"][:10], exc)
                continue
            result = await self._try_execute(row, tokens, yes_book, no_book)
            if result:
                executed.append(result)
        return executed

    async def _try_execute(self, row: sqlite3.Row, tokens: list[str],
                           yes_book: OrderBook, no_book: OrderBook) -> str | None:
        yes_ask, no_ask = yes_book.best_ask, no_book.best_ask
        edge = detect_arbitrage(yes_ask, no_ask, self.min_edge,
                                self.broker.fee_bps * 2)
        if edge is None:
            return None
        # Tamaño: limitado por profundidad del primer nivel de ambos books,
        # por el máximo por trade y por el presupuesto (risk/ re-verifica).
        depth = min(yes_book.asks[0].size, no_book.asks[0].size)
        size = min(depth, self.max_usdc / (yes_ask + no_ask))
        # Piso: por debajo no compensa el riesgo operativo de dos patas.
        if size * (yes_ask + no_ask) < self.min_usdc or size < 5:
            return None
        today = datetime.now(timezone.utc).date().isoformat()
        reason = (f"arbitraje: YES {yes_ask:.3f} + NO {no_ask:.3f} = "
                  f"{yes_ask + no_ask:.3f} < 1 (edge {edge:.1%})")
        fills = []
        for idx, (token, price, outcome) in enumerate(
                [(tokens[0], yes_ask, "Yes"), (tokens[1], no_ask, "No")]):
            fill = await self.broker.execute(
                f"arb:{row['condition_id']}:{idx}:{today}",
                OrderRequest(
                    strategy=self.name, condition_id=row["condition_id"],
                    category=row["category"], token_id=token, outcome=outcome,
                    outcome_index=idx, side="BUY", size=size,
                    # margen de slippage: hasta comerse la mitad del edge
                    price=price + edge / 2,
                    reason=reason, strategy_budget_pct=self.budget_pct,
                    meta={"question": row["question"]}))
            fills.append(fill)
        if all(f.status == "FILLED" for f in fills):
            log.info("ARB ejecutado en '%s' (edge %.1f%%)",
                     row["question"][:50], edge * 100)
            return f"{row['question'][:60]} — {reason}"
        # Si una pata quedó sin llenar, deshacer la otra para no quedar
        # direccional (en paper vendemos al book; en real sería crítico).
        for idx, fill in enumerate(fills):
            if fill.status == "FILLED":
                await self.broker.execute(
                    f"arb-unwind:{row['condition_id']}:{idx}:{today}",
                    OrderRequest(
                        strategy=self.name, condition_id=row["condition_id"],
                        category=row["category"], token_id=tokens[idx],
                        outcome="Yes" if idx == 0 else "No", outcome_index=idx,
                        side="SELL", size=fill.size, price=0.0,
                        reason="unwind: la otra pata del arbitraje no llenó"))
        return None

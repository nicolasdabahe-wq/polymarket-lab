"""Persistencia de mercados en SQLite."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..db import to_json
from .gamma import Market


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class MarketStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def upsert_markets(self, markets: list[Market]) -> int:
        now = _now()
        with self.conn:
            # Marca todo inactivo; los activos se re-marcan en el upsert.
            self.conn.execute("UPDATE markets SET active = 0")
            self.conn.executemany(
                """
                INSERT INTO markets (condition_id, gamma_id, slug, question,
                    category, end_date, liquidity, volume_24h, yes_price,
                    best_bid, best_ask, clob_token_ids, active, raw, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(condition_id) DO UPDATE SET
                    slug=excluded.slug, question=excluded.question,
                    category=excluded.category, end_date=excluded.end_date,
                    liquidity=excluded.liquidity, volume_24h=excluded.volume_24h,
                    yes_price=excluded.yes_price, best_bid=excluded.best_bid,
                    best_ask=excluded.best_ask,
                    clob_token_ids=excluded.clob_token_ids,
                    active=excluded.active, raw=excluded.raw,
                    updated_at=excluded.updated_at
                """,
                [(m.condition_id, m.gamma_id, m.slug, m.question, m.category,
                  m.end_date, m.liquidity, m.volume_24h, m.yes_price,
                  m.best_bid, m.best_ask, to_json(m.clob_token_ids),
                  int(m.active), to_json(m.raw), now)
                 for m in markets],
            )
        return len(markets)

    def upsert_one(self, market: Market) -> None:
        """Inserta/actualiza un mercado sin tocar el resto del cache."""
        now = _now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO markets (condition_id, gamma_id, slug, question,
                    category, end_date, liquidity, volume_24h, yes_price,
                    best_bid, best_ask, clob_token_ids, active, raw, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(condition_id) DO UPDATE SET
                     question=excluded.question, category=excluded.category,
                     end_date=excluded.end_date, liquidity=excluded.liquidity,
                     volume_24h=excluded.volume_24h,
                     yes_price=excluded.yes_price, best_bid=excluded.best_bid,
                     best_ask=excluded.best_ask,
                     clob_token_ids=excluded.clob_token_ids,
                     active=excluded.active, raw=excluded.raw,
                     updated_at=excluded.updated_at""",
                (market.condition_id, market.gamma_id, market.slug,
                 market.question, market.category, market.end_date,
                 market.liquidity, market.volume_24h, market.yes_price,
                 market.best_bid, market.best_ask,
                 to_json(market.clob_token_ids), int(market.active),
                 to_json(market.raw), now))

    def active_markets(self, category: str | None = None,
                       limit: int = 1000) -> list[sqlite3.Row]:
        sql = "SELECT * FROM markets WHERE active = 1"
        params: list[object] = []
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY volume_24h DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def category_summary(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT category, COUNT(*) AS n, SUM(volume_24h) AS vol24h
               FROM markets WHERE active = 1
               GROUP BY category ORDER BY vol24h DESC"""
        ).fetchall()

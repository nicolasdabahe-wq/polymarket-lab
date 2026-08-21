"""Seguimiento de posiciones y actividad de las wallets top.

- refresh_positions(): snapshot de posiciones actuales en SQLite.
- poll_new_activity(): detecta trades nuevos desde la última corrida usando
  un watermark de timestamp por wallet y los registra como señales
  informativas (fase 1 no opera, solo observa).
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..data.data_api import Activity, DataApiClient
from ..db import to_json

log = logging.getLogger("pmbot.smart_money.tracker")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WalletTracker:
    def __init__(self, api: DataApiClient, conn: sqlite3.Connection,
                 cfg: dict[str, Any]) -> None:
        self.api = api
        self.conn = conn
        self.cfg = cfg

    async def refresh_positions(self, wallets: list[str]) -> int:
        limit = int(self.cfg.get("positions_per_wallet", 25))
        sem = asyncio.Semaphore(5)
        total = 0

        async def one(wallet: str) -> None:
            nonlocal total
            async with sem:
                try:
                    positions = await self.api.positions(wallet, limit=limit)
                except Exception as exc:
                    log.warning("posiciones de %s fallaron: %s", wallet, exc)
                    return
            now = _now()
            with self.conn:
                self.conn.execute(
                    "DELETE FROM wallet_positions WHERE wallet = ?", (wallet,))
                self.conn.executemany(
                    """INSERT OR REPLACE INTO wallet_positions
                       (wallet, condition_id, title, outcome, size, avg_price,
                        cur_price, value_usdc, cash_pnl, percent_pnl, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    [(p.wallet, p.condition_id, p.title, p.outcome, p.size,
                      p.avg_price, p.cur_price, p.current_value, p.cash_pnl,
                      p.percent_pnl, now) for p in positions],
                )
            total += len(positions)

        await asyncio.gather(*(one(w) for w in wallets))
        log.info("Posiciones actualizadas: %d filas de %d wallets",
                 total, len(wallets))
        return total

    async def poll_new_activity(self, wallets: list[str]) -> list[Activity]:
        """Trades nuevos desde el último poll. Registra señal por cada uno."""
        new_trades: list[Activity] = []
        sem = asyncio.Semaphore(5)

        async def one(wallet: str) -> None:
            async with sem:
                try:
                    # 150 y no 50: en deportes una wallet puede hacer decenas
                    # de fills entre polls y perderíamos las señales grandes.
                    activity = await self.api.activity(wallet, limit=150)
                except Exception as exc:
                    log.warning("actividad de %s falló: %s", wallet, exc)
                    return
            row = self.conn.execute(
                "SELECT last_activity_ts FROM wallet_watermarks WHERE wallet = ?",
                (wallet,)).fetchone()
            watermark = row["last_activity_ts"] if row else 0
            fresh = [a for a in activity
                     if a.type == "TRADE" and a.timestamp > watermark]
            latest = max((a.timestamp for a in activity), default=watermark)
            with self.conn:
                self.conn.execute(
                    """INSERT INTO wallet_watermarks (wallet, last_activity_ts)
                       VALUES (?, ?) ON CONFLICT(wallet)
                       DO UPDATE SET last_activity_ts = excluded.last_activity_ts""",
                    (wallet, latest))
                # Primera corrida (watermark 0): solo fijar watermark, no
                # inundar de "señales" con el histórico completo.
                if watermark > 0:
                    for a in fresh:
                        self.conn.execute(
                            """INSERT INTO signals (source, kind, condition_id,
                               payload, created_at) VALUES (?,?,?,?,?)""",
                            ("smart_money", "new_trade", a.condition_id,
                             to_json({"wallet": a.wallet, "side": a.side,
                                      "title": a.title, "outcome": a.outcome,
                                      "outcome_index": a.outcome_index,
                                      "price": a.price, "usdc": a.usdc_size,
                                      "ts": a.timestamp}), _now()))
                    new_trades.extend(fresh)

        await asyncio.gather(*(one(w) for w in wallets))
        if new_trades:
            log.info("smart_money: %d trades nuevos de wallets top",
                     len(new_trades))
        return new_trades

    def positions_of(self, wallet: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM wallet_positions WHERE wallet = ?
               ORDER BY value_usdc DESC""", (wallet,)).fetchall()

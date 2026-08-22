"""La cinta: todos los trades grandes de Polymarket, en vivo.

El problema que resuelve: preguntar wallet por wallet cuesta una petición
por wallet, así que con 40 vigiladas no se puede consultar más seguido que
cada 3 minutos sin chocar con los límites de la API. En un partido en vivo,
3 minutos es una eternidad: cuando copiábamos, el precio ya había volado.

data-api/trades devuelve TODOS los trades del sitio en una sola llamada, y
acepta un filtro de tamaño en el servidor. Medido el 2026-08-22: con filtro
de $150 pasan ~31 trades por minuto y el más reciente tenía 1 segundo de
antigüedad. Una petición cada pocos segundos ve todo, con menos carga que
el método anterior.

El watermark es global (un timestamp), no uno por wallet.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..data.data_api import DATA_BASE
from ..http import HttpClient
from ..db import to_json

log = logging.getLogger("pmbot.smart_money.tape")

# Cuántos trades pedir por vuelta. A 31/min con filtro de $150, 400 cubren
# ~13 minutos: margen de sobra aunque el bot se caiga y vuelva.
PAGE = 400


@dataclass
class TapeTrade:
    wallet: str
    side: str
    condition_id: str
    title: str
    outcome: str
    outcome_index: int
    price: float
    usdc: float
    timestamp: int


class TradeTape:
    def __init__(self, conn: sqlite3.Connection, http: HttpClient,
                 min_usdc: float = 150.0,
                 candidate_min_usdc: float = 500.0) -> None:
        self.conn = conn
        self.http = http
        self.min_usdc = min_usdc
        # Tamaño a partir del cual una wallet desconocida merece que se le
        # mire el historial. Más bajo = universo más grande y más backtests.
        self.candidate_min_usdc = candidate_min_usdc

    async def fetch(self, limit: int = PAGE) -> list[TapeTrade]:
        """Últimos trades del sitio por encima del filtro de tamaño."""
        rows = await self.http.get_json(
            f"{DATA_BASE}/trades",
            params={"limit": limit, "filterType": "CASH",
                    "filterAmount": int(self.min_usdc)})
        out: list[TapeTrade] = []
        for r in rows or []:
            try:
                size, price = float(r.get("size") or 0), float(r.get("price") or 0)
                out.append(TapeTrade(
                    wallet=(r.get("proxyWallet") or "").lower(),
                    side=(r.get("side") or "").upper(),
                    condition_id=r.get("conditionId") or "",
                    title=r.get("title") or "",
                    outcome=r.get("outcome") or "",
                    outcome_index=int(r.get("outcomeIndex") or 0),
                    price=price, usdc=size * price,
                    timestamp=int(r.get("timestamp") or 0)))
            except (TypeError, ValueError):
                continue
        return out

    def registrar_candidatas(self, trades: list[TapeTrade],
                             min_usdc: float) -> int:
        """Toda wallet que opere en grande entra al universo de candidatas.

        El leaderboard solo muestra a los acumulados históricos; la cinta
        ve a TODO el que mueve dinero ahora mismo, tenga o no historial
        visible. Después el backtest decide quién sirve: acá no se filtra
        a nadie, solo se anota que existe.
        """
        ahora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        nuevas = 0
        with self.conn:
            for t in trades:
                if t.usdc < min_usdc or not t.wallet:
                    continue
                cur = self.conn.execute(
                    """INSERT INTO wallet_candidates
                       (wallet, fuente, trades_grandes, max_usdc,
                        primera_vez, ultima_vez)
                       VALUES (?, 'cinta', 1, ?, ?, ?)
                       ON CONFLICT(wallet) DO UPDATE SET
                         trades_grandes = trades_grandes + 1,
                         max_usdc = MAX(COALESCE(max_usdc, 0), excluded.max_usdc),
                         ultima_vez = excluded.ultima_vez""",
                    (t.wallet, t.usdc, ahora, ahora))
                nuevas += 1 if cur.rowcount and cur.lastrowid else 0
        return nuevas

    def _watermark(self) -> int:
        row = self.conn.execute(
            "SELECT value FROM paper_state WHERE key = 'tape_watermark'"
        ).fetchone()
        return int(float(row["value"])) if row else 0

    def _save_watermark(self, ts: int) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO paper_state (key, value) VALUES ('tape_watermark', ?)
                   ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                (str(ts),))

    async def poll(self, watched: set[str]) -> list[TapeTrade]:
        """Trades nuevos de las wallets vigiladas. Los registra como señales
        new_trade, el mismo formato que ya consume copy_trading."""
        if not watched:
            return []
        trades = await self.fetch()
        if not trades:
            return []
        # Antes de filtrar por wallets vigiladas: anotar a TODO el que opere
        # en grande. Ese es el universo del que salen las candidatas nuevas.
        self.registrar_candidatas(trades, self.candidate_min_usdc)
        watermark = self._watermark()
        latest = max(t.timestamp for t in trades)
        self._save_watermark(latest)
        if watermark == 0:
            # Primera vuelta: fijar el watermark sin inundar de señales viejas.
            log.info("cinta: watermark inicial en %d", latest)
            return []
        nuevos = [t for t in trades
                  if t.timestamp > watermark and t.wallet in watched
                  and t.side == "BUY" and t.condition_id]
        if not nuevos:
            return []
        with self.conn:
            for t in nuevos:
                self.conn.execute(
                    """INSERT INTO signals (source, kind, condition_id,
                       payload, created_at) VALUES (?,?,?,?,?)""",
                    ("smart_money", "new_trade", t.condition_id,
                     to_json({"wallet": t.wallet, "side": t.side,
                              "title": t.title, "outcome": t.outcome,
                              "outcome_index": t.outcome_index,
                              "price": t.price, "usdc": t.usdc,
                              "ts": t.timestamp}),
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
        demora = max(0, int(datetime.now(timezone.utc).timestamp()) - latest)
        log.info("cinta: %d trades nuevos de wallets vigiladas (demora %ds)",
                 len(nuevos), demora)
        return nuevos

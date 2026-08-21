"""Scoring de wallets a partir del leaderboard oficial + Data API.

Pipeline:
1. Leaderboard OVERALL en ventanas week / month / all -> candidatos.
2. Enriquecer cada candidato con su actividad (nº trades, mercados distintos,
   antigüedad) y sus posiciones (concentración).
3. Filtros duros (min_trades, min_distinct_markets, min_account_age_days):
   descartan cuentas nuevas, de una sola apuesta o con pinta de insider.
4. Score compuesto en [0, 1]: PnL normalizado por ventana + ROI + diversificación.

La normalización de PnL es relativa al mejor candidato de cada ventana
(percentil simple), así el score es comparable entre corridas.
"""
from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..data.data_api import DataApiClient, LeaderboardRow
from ..db import to_json

log = logging.getLogger("pmbot.smart_money.ranking")

SECONDS_PER_DAY = 86400.0


@dataclass
class WalletStats:
    """Datos crudos por wallet antes de puntuar."""
    wallet: str
    username: str = ""
    pnl_week: float = 0.0
    pnl_month: float = 0.0
    pnl_all: float = 0.0
    vol_all: float = 0.0
    trades: int = 0
    distinct_markets: int = 0
    account_age_days: float = 0.0
    top_market_value_share: float = 0.0  # concentración de posiciones actuales


@dataclass
class WalletScore:
    wallet: str
    username: str
    score: float
    stats: WalletStats
    passed_filters: bool
    reject_reason: str | None
    components: dict[str, float] = field(default_factory=dict)


def normalize_positive(value: float, best: float) -> float:
    """Escala [0,1] con curva log para no dejar que un outlier aplaste al resto."""
    if value <= 0 or best <= 0:
        return 0.0
    return math.log1p(value) / math.log1p(best)


def score_wallets(stats: list[WalletStats], weights: dict[str, float],
                  filters: dict[str, Any]) -> list[WalletScore]:
    """Puro y determinista: testeable sin red."""
    best_week = max((s.pnl_week for s in stats), default=0.0)
    best_month = max((s.pnl_month for s in stats), default=0.0)
    best_all = max((s.pnl_all for s in stats), default=0.0)

    results: list[WalletScore] = []
    for s in stats:
        reject = _check_filters(s, filters)

        # ROI aproximado: PnL total / volumen total operado. Capado en 1.0
        # (un ROI >100% sobre volumen suele ser una apuesta única con suerte).
        roi = min(s.pnl_all / s.vol_all, 1.0) if s.vol_all > 0 else 0.0
        roi_score = max(roi, 0.0)

        # Diversificación: más mercados distintos y menos concentración actual.
        breadth = min(s.distinct_markets / 20.0, 1.0)
        concentration_penalty = s.top_market_value_share  # 0..1
        diversification = max(breadth * (1.0 - 0.5 * concentration_penalty), 0.0)

        components = {
            "pnl_week": normalize_positive(s.pnl_week, best_week),
            "pnl_month": normalize_positive(s.pnl_month, best_month),
            "pnl_all": normalize_positive(s.pnl_all, best_all),
            "roi": roi_score,
            "diversification": diversification,
        }
        score = sum(weights.get(k, 0.0) * v for k, v in components.items())
        results.append(WalletScore(
            wallet=s.wallet, username=s.username, score=round(score, 4),
            stats=s, passed_filters=reject is None, reject_reason=reject,
            components={k: round(v, 4) for k, v in components.items()},
        ))
    results.sort(key=lambda w: (w.passed_filters, w.score), reverse=True)
    return results


def _check_filters(s: WalletStats, filters: dict[str, Any]) -> str | None:
    if s.trades < int(filters.get("min_trades", 0)):
        return f"pocos trades ({s.trades})"
    if s.distinct_markets < int(filters.get("min_distinct_markets", 0)):
        return f"pocos mercados distintos ({s.distinct_markets})"
    if s.account_age_days < float(filters.get("min_account_age_days", 0)):
        return f"cuenta muy nueva ({s.account_age_days:.0f}d)"
    if s.pnl_all <= 0:
        return "PnL total negativo"
    return None


class WalletScorer:
    def __init__(self, api: DataApiClient, conn: sqlite3.Connection,
                 cfg: dict[str, Any]) -> None:
        self.api = api
        self.conn = conn
        self.cfg = cfg

    async def refresh_ranking(self) -> list[WalletScore]:
        limit = int(self.cfg.get("leaderboard_limit", 50))
        week, month, all_time = await asyncio.gather(
            self.api.leaderboard("week", limit),
            self.api.leaderboard("month", limit),
            self.api.leaderboard("all", limit),
        )
        stats = self._merge_leaderboards(week, month, all_time)
        log.info("Leaderboard: %d wallets candidatas", len(stats))

        # Enriquecer con actividad/posiciones (concurrencia limitada para
        # no golpear el rate limit de la Data API).
        sem = asyncio.Semaphore(5)

        async def enrich(s: WalletStats) -> None:
            async with sem:
                await self._enrich_wallet(s)

        await asyncio.gather(*(enrich(s) for s in stats.values()))

        scored = score_wallets(
            list(stats.values()),
            weights=self.cfg.get("weights", {}),
            filters=self.cfg.get("filters", {}),
        )
        self._persist(scored)
        return scored

    @staticmethod
    def _merge_leaderboards(week: list[LeaderboardRow], month: list[LeaderboardRow],
                            all_time: list[LeaderboardRow]) -> dict[str, WalletStats]:
        stats: dict[str, WalletStats] = {}

        def get(row: LeaderboardRow) -> WalletStats:
            s = stats.get(row.wallet)
            if s is None:
                s = stats[row.wallet] = WalletStats(wallet=row.wallet,
                                                    username=row.username)
            if row.username and not s.username:
                s.username = row.username
            return s

        for row in week:
            get(row).pnl_week = row.pnl
        for row in month:
            get(row).pnl_month = row.pnl
        for row in all_time:
            s = get(row)
            s.pnl_all = row.pnl
            s.vol_all = row.volume
        return stats

    async def _enrich_wallet(self, s: WalletStats) -> None:
        try:
            activity = await self.api.activity(s.wallet, limit=500)
            positions = await self.api.positions(s.wallet, limit=100)
            created_at = await self.api.profile_created_at(s.wallet)
        except Exception as exc:  # una wallet fallida no tumba el ranking
            log.warning("No se pudo enriquecer %s: %s", s.wallet, exc)
            return
        trades = [a for a in activity if a.type == "TRADE"]
        s.trades = len(trades)
        s.distinct_markets = len({a.condition_id for a in trades if a.condition_id})
        if created_at:
            try:
                created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                s.account_age_days = (
                    datetime.now(timezone.utc) - created).total_seconds() / SECONDS_PER_DAY
            except ValueError:
                created_at = None
        if not created_at and activity:
            # Fallback: cota inferior por la actividad visible (500 ítems).
            oldest = min(a.timestamp for a in activity if a.timestamp > 0)
            s.account_age_days = (time.time() - oldest) / SECONDS_PER_DAY
        total_value = sum(p.current_value for p in positions)
        if total_value > 0:
            s.top_market_value_share = max(
                p.current_value for p in positions) / total_value

    def _persist(self, scored: list[WalletScore]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute("DELETE FROM wallet_ranking")
            self.conn.executemany(
                """INSERT INTO wallet_ranking (wallet, username, score,
                     pnl_week, pnl_month, pnl_all, vol_all, roi, trades,
                     distinct_markets, account_age_days, passed_filters,
                     reject_reason, details, ranked_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(w.wallet, w.username, w.score, w.stats.pnl_week,
                  w.stats.pnl_month, w.stats.pnl_all, w.stats.vol_all,
                  w.components.get("roi", 0.0), w.stats.trades,
                  w.stats.distinct_markets, w.stats.account_age_days,
                  int(w.passed_filters), w.reject_reason,
                  to_json(w.components), now)
                 for w in scored],
            )

    def top_wallets(self, n: int | None = None) -> list[sqlite3.Row]:
        n = n or int(self.cfg.get("top_n", 15))
        return self.conn.execute(
            """SELECT * FROM wallet_ranking WHERE passed_filters = 1
               ORDER BY score DESC LIMIT ?""", (n,)).fetchall()

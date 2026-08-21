"""Validación automática de wallets por backtest.

El score del leaderboard dice quién GANA; el backtest dice a quién nos
conviene COPIAR — y son cosas distintas: los scalpers de deportes en vivo
ganan mucho y son incopiables (llegamos tarde a su precio).

Este módulo corre el backtest de copia sobre las wallets del ranking y
guarda el veredicto en wallet_backtest. copy_trading solo copia a las que
tienen veredicto 'copiable' (o a las que aún no se testearon, si su score
es alto), así el universo de copiables crece solo, con evidencia.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from ..backtest import CopyBacktester

log = logging.getLogger("pmbot.smart_money.validator")


class WalletValidator:
    def __init__(self, conn: sqlite3.Connection, backtester: CopyBacktester,
                 cfg: dict[str, Any]) -> None:
        self.conn = conn
        self.backtester = backtester
        vcfg = cfg.get("validation") or {}
        self.enabled = bool(vcfg.get("enabled", True))
        self.days = int(vcfg.get("days", 30))
        self.stake = float(vcfg.get("stake_usdc", 16))
        self.thresholds = [float(x) for x in
                           (vcfg.get("thresholds") or [150, 300, 500, 1000, 2000])]
        self.min_roi = float(vcfg.get("min_roi", 0.0))
        self.min_copies = int(vcfg.get("min_copies", 5))
        self.max_wallets = int(vcfg.get("max_wallets_per_run", 20))
        self.revalidate_hours = float(vcfg.get("revalidate_hours", 24))

    def _needs_test(self, wallet: str) -> bool:
        row = self.conn.execute(
            "SELECT tested_at FROM wallet_backtest WHERE wallet = ?",
            (wallet,)).fetchone()
        if not row:
            return True
        try:
            tested = datetime.fromisoformat(row["tested_at"])
        except ValueError:
            return True
        age_h = (datetime.now(timezone.utc) - tested).total_seconds() / 3600
        return age_h >= self.revalidate_hours

    async def validate_ranked(self) -> list[dict[str, Any]]:
        """Testea las wallets del ranking que lo necesiten. Devuelve resumen."""
        if not self.enabled:
            return []
        rows = self.conn.execute(
            """SELECT wallet, username FROM wallet_ranking
               WHERE passed_filters = 1 ORDER BY score DESC LIMIT ?""",
            (self.max_wallets,)).fetchall()
        pending = [(r["wallet"], r["username"]) for r in rows
                   if self._needs_test(r["wallet"])]
        if not pending:
            return []
        log.info("validando %d wallets por backtest", len(pending))

        results: list[dict[str, Any]] = []
        for wallet, username in pending:
            try:
                reports = await self.backtester.run_multi(
                    wallet, days=self.days, stake_usdc=self.stake,
                    thresholds=self.thresholds)
            except Exception as exc:
                log.warning("backtest de %s falló: %s", wallet[:10], exc)
                continue

            # Elegir el umbral de tamaño más rentable con muestra suficiente:
            # muchas wallets ganan solo en sus apuestas de convicción.
            best_th, best_roi, best_rep = None, None, None
            for th, rep in sorted(reports.items()):
                n_th = len(rep.trades)
                if n_th < self.min_copies:
                    continue
                staked_th = rep.total_staked
                roi_th = ((rep.realized_pnl + rep.unrealized_pnl) / staked_th
                          if staked_th > 0 else 0.0)
                if best_roi is None or roi_th > best_roi:
                    best_th, best_roi, best_rep = th, roi_th, rep
            if best_rep is None:
                # ningún umbral con muestra suficiente
                fallback = reports[min(reports)]
                best_th, best_roi, best_rep = min(reports), 0.0, fallback
                verdict = "sin_datos"
            elif best_roi >= self.min_roi:
                verdict = "copiable"
            else:
                verdict = "rechazada"
            report, roi = best_rep, best_roi
            n = len(report.trades)
            wr = report.win_rate
            with self.conn:
                self.conn.execute(
                    """INSERT INTO wallet_backtest (wallet, roi, win_rate,
                       n_copies, days_covered, verdict, min_usdc, tested_at)
                       VALUES (?,?,?,?,?,?,?,?)
                       ON CONFLICT(wallet) DO UPDATE SET
                         roi=excluded.roi, win_rate=excluded.win_rate,
                         n_copies=excluded.n_copies,
                         days_covered=excluded.days_covered,
                         verdict=excluded.verdict, min_usdc=excluded.min_usdc,
                         tested_at=excluded.tested_at""",
                    (wallet, roi, wr, n, report.days_covered, verdict, best_th,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
            results.append({"wallet": wallet, "username": username, "roi": roi,
                            "win_rate": wr, "n": n, "verdict": verdict,
                            "min_usdc": best_th})
            log.info("backtest %s: ROI %+.1f%% en %d copias (umbral $%.0f) -> %s",
                     username or wallet[:10], roi * 100, n, best_th or 0, verdict)
            await asyncio.sleep(1)  # respirar entre wallets (rate limits)
        return results

    def copiables(self) -> set[str]:
        return {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_backtest WHERE verdict = 'copiable'")}

    def rechazadas(self) -> set[str]:
        return {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_backtest WHERE verdict = 'rechazada'")}

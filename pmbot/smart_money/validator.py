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
from .behavior import perfil_operador

log = logging.getLogger("pmbot.smart_money.validator")


class WalletValidator:
    def __init__(self, conn: sqlite3.Connection, backtester: CopyBacktester,
                 cfg: dict[str, Any], api: Any = None) -> None:
        self.conn = conn
        self.backtester = backtester
        self.api = api
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
        dcfg = vcfg.get("discovery") or {}
        self.discover_enabled = bool(dcfg.get("enabled", True))
        self.discover_markets = int(dcfg.get("markets", 15))
        self.discover_per_market = int(dcfg.get("holders_per_market", 10))
        self.discover_max = int(dcfg.get("max_candidates", 25))

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

    async def discover_candidates(self) -> list[str]:
        """Wallets con posiciones grandes en los mercados más activos.

        El leaderboard es un acumulado histórico: muchas de sus estrellas ya
        no operan. Los holders de los mercados calientes son, por definición,
        gente jugando AHORA — que es a quien se puede copiar.
        """
        if not (self.discover_enabled and self.api):
            return []
        markets = self.conn.execute(
            """SELECT condition_id FROM markets WHERE active = 1
               ORDER BY volume_24h DESC LIMIT ?""",
            (self.discover_markets,)).fetchall()
        known = {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_backtest")}
        known |= {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_ranking")}
        found: list[str] = []
        for row in markets:
            if len(found) >= self.discover_max:
                break
            for wallet in await self.api.holders(row["condition_id"],
                                                 self.discover_per_market):
                if wallet not in known and wallet not in found:
                    found.append(wallet)
                    if len(found) >= self.discover_max:
                        break
            await asyncio.sleep(0.4)
        if found:
            log.info("descubiertas %d wallets activas desde holders", len(found))
        return found

    async def validate_ranked(self, force: bool = False) -> list[dict[str, Any]]:
        """Testea las wallets del ranking que lo necesiten. Devuelve resumen.

        force ignora la ventana de revalidación: sirve para reevaluar a todas
        cuando cambian los criterios (por ejemplo al agregar el perfil de
        operador) sin esperar 24 horas."""
        if not self.enabled:
            return []
        rows = self.conn.execute(
            """SELECT wallet, username FROM wallet_ranking
               WHERE passed_filters = 1 ORDER BY score DESC LIMIT ?""",
            (self.max_wallets,)).fetchall()
        pending = [(r["wallet"], r["username"]) for r in rows
                   if force or self._needs_test(r["wallet"])]
        # Las que ya están habilitadas pero NO salen del leaderboard (las
        # descubiertas en mercados calientes) también hay que reevaluarlas:
        # si no, conservan para siempre el veredicto con el criterio viejo.
        # Así fue como una wallet que hace mercado siguió en el carril
        # rápido después de agregar el perfil de operador (2026-08-22).
        for r in self.conn.execute(
                "SELECT wallet FROM wallet_backtest WHERE verdict = 'copiable'"):
            if force or self._needs_test(r["wallet"]):
                pending.append((r["wallet"], ""))
        # Sumar candidatas activas descubiertas en los mercados calientes.
        try:
            for wallet in await self.discover_candidates():
                pending.append((wallet, ""))
        except Exception as exc:
            log.warning("descubrimiento de wallets falló: %s", exc)
        # Una sola vez cada una, conservando el nombre si lo tenemos.
        vistas: dict[str, str] = {}
        for wallet, username in pending:
            if wallet not in vistas or (username and not vistas[wallet]):
                vistas[wallet] = username
        pending = list(vistas.items())
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
            # Perfil de operador: un creador de mercado es incopiable por
            # más ROI que muestre el backtest. Cuando reaccionamos a su
            # orden, él ya movió su cotización.
            perfil = perfil_operador(
                getattr(best_rep, "_raw_trades", None) or [])
            if perfil and perfil.es_creador_de_mercado:
                verdict = "rechazada"

            report, roi = best_rep, best_roi
            n = len(report.trades)
            wr = report.win_rate
            with self.conn:
                self.conn.execute(
                    """INSERT INTO wallet_backtest (wallet, roi, win_rate,
                       n_copies, days_covered, verdict, min_usdc, perfil,
                       trades_por_dia, mediana_usdc, tested_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(wallet) DO UPDATE SET
                         roi=excluded.roi, win_rate=excluded.win_rate,
                         n_copies=excluded.n_copies,
                         days_covered=excluded.days_covered,
                         verdict=excluded.verdict, min_usdc=excluded.min_usdc,
                         perfil=excluded.perfil,
                         trades_por_dia=excluded.trades_por_dia,
                         mediana_usdc=excluded.mediana_usdc,
                         tested_at=excluded.tested_at""",
                    (wallet, roi, wr, n, report.days_covered, verdict, best_th,
                     perfil.etiqueta if perfil else None,
                     perfil.trades_por_dia if perfil else None,
                     perfil.mediana_usdc if perfil else None,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")))
            results.append({"wallet": wallet, "username": username, "roi": roi,
                            "win_rate": wr, "n": n, "verdict": verdict,
                            "min_usdc": best_th,
                            "perfil": perfil.etiqueta if perfil else "",
                            "detalle": perfil.resumen() if perfil else ""})
            log.info("backtest %s: ROI %+.1f%% en %d copias (umbral $%.0f) "
                     "-> %s%s", username or wallet[:10], roi * 100, n,
                     best_th or 0, verdict,
                     f" [creador de mercado: {perfil.resumen()}]"
                     if perfil and perfil.es_creador_de_mercado else "")
            await asyncio.sleep(1)  # respirar entre wallets (rate limits)
        return results

    def copiables(self) -> set[str]:
        return {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_backtest WHERE verdict = 'copiable'")}

    def rechazadas(self) -> set[str]:
        return {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_backtest WHERE verdict = 'rechazada'")}

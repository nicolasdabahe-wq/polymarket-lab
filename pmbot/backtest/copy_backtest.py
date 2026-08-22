"""Backtest de copy trading: replay del historial real de una wallet.

Metodología (conservadora a propósito):
- Se copian solo los BUY de la wallet con tamaño >= min_copy_usdc.
- Nuestra entrada paga una penalidad de slippage sobre SU precio (por llegar
  después): entry = precio_wallet * (1 + slippage).
- Un stake fijo por copia (no interés compuesto): aísla la calidad de las
  señales del efecto del sizing.
- Salida: primer SELL de la wallet en ese mercado/outcome (a su precio menos
  slippage), o resolución del mercado (payout 0/1).
- Posiciones aún abiertas se marcan al precio actual (PnL no realizado).

Limitaciones honestas:
- La Data API expone el histórico completo pero lo paginamos con un tope
  (max_activities); en wallets hiperactivas la ventana efectiva puede ser
  menor a la pedida (el reporte lo indica).
- No modela profundidad del book en el momento histórico (imposible sin
  datos de book históricos): la penalidad de slippage es la aproximación.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..data.data_api import Activity, DataApiClient
from ..data.gamma import GammaClient

log = logging.getLogger("pmbot.backtest.copy")

SECONDS_PER_DAY = 86400


@dataclass
class SimTrade:
    condition_id: str
    outcome_index: int
    title: str
    entry_ts: int
    entry_price: float       # nuestro precio (con slippage)
    wallet_price: float      # el precio al que entró la wallet
    shares: float
    stake: float
    status: str = "open"     # open | closed_follow | resolved
    exit_price: float | None = None
    pnl: float = 0.0


@dataclass
class BacktestReport:
    wallet: str
    days_requested: int
    days_covered: float
    n_wallet_trades: int
    trades: list[SimTrade] = field(default_factory=list)

    @property
    def closed(self) -> list[SimTrade]:
        return [t for t in self.trades if t.status != "open"]

    @property
    def realized_pnl(self) -> float:
        return sum(t.pnl for t in self.closed)

    @property
    def unrealized_pnl(self) -> float:
        return sum(t.pnl for t in self.trades if t.status == "open")

    @property
    def total_staked(self) -> float:
        return sum(t.stake for t in self.trades)

    @property
    def win_rate(self) -> float | None:
        closed = self.closed
        if not closed:
            return None
        return sum(1 for t in closed if t.pnl > 0) / len(closed)


def simulate_copy(trades: list[dict[str, Any]],
                  outcomes: dict[str, dict[str, Any]],
                  stake_usdc: float, min_copy_usdc: float,
                  slippage: float = 0.01,
                  max_entry_price: float = 0.95) -> list[SimTrade]:
    """Replay puro y testeable. trades debe venir en orden cronológico.

    Cada trade: {ts, condition_id, outcome_index, side, price, usdc, title}.
    outcomes: {condition_id: {closed, outcome_prices}}.
    """
    open_positions: dict[tuple[str, int], SimTrade] = {}
    done: list[SimTrade] = []

    for t in trades:
        key = (t["condition_id"], int(t["outcome_index"]))
        price = float(t["price"])
        if t["side"] == "BUY":
            if key in open_positions or price <= 0:
                continue  # ya copiamos esta posición
            if float(t["usdc"]) < min_copy_usdc:
                continue
            entry = min(price * (1 + slippage), 0.99)
            if entry > max_entry_price:
                continue  # sin espacio de ganancia razonable
            open_positions[key] = SimTrade(
                condition_id=t["condition_id"],
                outcome_index=int(t["outcome_index"]),
                title=t.get("title", ""), entry_ts=int(t["ts"]),
                entry_price=entry, wallet_price=price,
                shares=stake_usdc / entry, stake=stake_usdc)
        elif t["side"] == "SELL" and key in open_positions:
            pos = open_positions.pop(key)
            exit_price = max(price * (1 - slippage), 0.0)
            pos.status = "closed_follow"
            pos.exit_price = exit_price
            pos.pnl = pos.shares * (exit_price - pos.entry_price)
            done.append(pos)

    # Posiciones que la wallet no cerró: resolvió el mercado o siguen vivas.
    for pos in open_positions.values():
        status = outcomes.get(pos.condition_id)
        prices = (status or {}).get("outcome_prices") or []
        if status and pos.outcome_index < len(prices):
            price = prices[pos.outcome_index]
            # "de facto resuelto": el resultado ya se conoce aunque Gamma
            # todavía no lo marque closed (precio clavado en ~0 o ~1).
            if status.get("closed") or price <= 0.005 or price >= 0.995:
                payout = round(price)
                pos.status = "resolved"
                pos.exit_price = float(payout)
                pos.pnl = pos.shares * (payout - pos.entry_price)
            else:
                pos.status = "open"
                pos.exit_price = price
                pos.pnl = pos.shares * (price - pos.entry_price)
        else:
            pos.status = "open"
            pos.pnl = 0.0
        done.append(pos)
    done.sort(key=lambda p: p.entry_ts)
    return done


class CopyBacktester:
    def __init__(self, api: DataApiClient, gamma: GammaClient) -> None:
        self.api = api
        self.gamma = gamma

    async def run_multi(self, wallet: str, days: int, stake_usdc: float,
                        thresholds: list[float], slippage: float = 0.01
                        ) -> dict[float, BacktestReport]:
        """Un solo fetch de actividad, simulado con varios umbrales de tamaño.

        El umbral óptimo cambia por wallet: muchas son rentables solo en sus
        apuestas grandes (convicción) y pierden en las medianas.
        """
        base = await self.run(wallet, days=days, stake_usdc=stake_usdc,
                              min_copy_usdc=min(thresholds), slippage=slippage,
                              _keep_raw=True)
        out: dict[float, BacktestReport] = {}
        raw_trades = getattr(base, "_raw_trades", None)
        raw_outcomes = getattr(base, "_raw_outcomes", None)
        if raw_trades is None:
            return {min(thresholds): base}
        for th in thresholds:
            sim = simulate_copy(raw_trades, raw_outcomes, stake_usdc, th,
                                slippage)
            out[th] = BacktestReport(
                wallet=wallet, days_requested=days,
                days_covered=base.days_covered,
                n_wallet_trades=base.n_wallet_trades, trades=sim)
            # Los trades crudos viajan con cada informe: el validador los usa
            # para el perfil de operador sin volver a bajarlos.
            out[th]._raw_trades = raw_trades   # type: ignore[attr-defined]
        return out

    async def run(self, wallet: str, days: int = 90, stake_usdc: float = 8.0,
                  min_copy_usdc: float = 500.0, slippage: float = 0.01,
                  max_activities: int = 5000,
                  _keep_raw: bool = False) -> BacktestReport:
        wallet = wallet.lower()
        cutoff = time.time() - days * SECONDS_PER_DAY
        activity: list[Activity] = []
        offset = 0
        while offset < max_activities:
            page = await self.api.activity(wallet, limit=500, offset=offset)
            if not page:
                break
            activity.extend(page)
            if min(a.timestamp for a in page) < cutoff:
                break
            offset += 500

        trades = [
            {"ts": a.timestamp, "condition_id": a.condition_id,
             "outcome_index": a.outcome_index, "side": a.side,
             "price": a.price, "usdc": a.usdc_size, "title": a.title}
            for a in sorted(activity, key=lambda a: a.timestamp)
            if a.type == "TRADE" and a.timestamp >= cutoff and a.condition_id
        ]
        cids = sorted({t["condition_id"] for t in trades
                       if t["side"] == "BUY" and t["usdc"] >= min_copy_usdc})
        outcomes = await self.gamma.market_statuses(cids) if cids else {}

        sim = simulate_copy(trades, outcomes, stake_usdc, min_copy_usdc,
                            slippage)
        covered = ((max(t["ts"] for t in trades) - min(t["ts"] for t in trades))
                   / SECONDS_PER_DAY if trades else 0.0)
        report = BacktestReport(wallet=wallet, days_requested=days,
                                days_covered=covered,
                                n_wallet_trades=len(trades), trades=sim)
        if _keep_raw:
            report._raw_trades = trades      # type: ignore[attr-defined]
            report._raw_outcomes = outcomes  # type: ignore[attr-defined]
        log.info("backtest %s: %d trades de la wallet, %d copias simuladas",
                 wallet[:10], len(trades), len(sim))
        return report


def format_report(report: BacktestReport, username: str = "") -> str:
    name = username or report.wallet[:12]
    lines = [f"🔬 Backtest de copia — {name} "
             f"(pedidos {report.days_requested}d, cubiertos "
             f"{report.days_covered:.0f}d, {report.n_wallet_trades} trades "
             f"de la wallet)"]
    if not report.trades:
        lines.append("  Sin trades copiables en la ventana "
                     "(ninguno supera el tamaño mínimo).")
        return "\n".join(lines)
    closed = report.closed
    open_n = len(report.trades) - len(closed)
    staked = report.total_staked
    lines.append(
        f"  Copias: {len(report.trades)} (cerradas {len(closed)}, "
        f"abiertas {open_n}) | capital total apostado ${staked:.0f}")
    wr = report.win_rate
    lines.append(
        f"  PnL realizado: {report.realized_pnl:+.2f} USDC | "
        f"no realizado: {report.unrealized_pnl:+.2f} | "
        + (f"win rate (cerradas): {wr:.0%}" if wr is not None else ""))
    if staked > 0:
        ret = (report.realized_pnl + report.unrealized_pnl) / staked
        lines.append(f"  Retorno sobre lo apostado: {ret:+.1%}")
    ranked = sorted(report.trades, key=lambda t: t.pnl)
    lines.append("  Peores:")
    for t in ranked[:3]:
        lines.append(f"    {t.pnl:+7.2f}  [{t.status}] "
                     f"{t.entry_price:.2f}→{t.exit_price if t.exit_price is not None else '?'} "
                     f"{t.title[:55]}")
    lines.append("  Mejores:")
    for t in ranked[-3:][::-1]:
        lines.append(f"    {t.pnl:+7.2f}  [{t.status}] "
                     f"{t.entry_price:.2f}→{t.exit_price if t.exit_price is not None else '?'} "
                     f"{t.title[:55]}")
    return "\n".join(lines)

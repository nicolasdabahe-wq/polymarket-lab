"""Límites duros de riesgo.

`evaluate()` es una función pura (testeable sin red ni DB); RiskManager la
alimenta con el estado real del portfolio y agrega el kill switch.

Reglas (todas sobre equity actual, no sobre capital inicial):
- costo mínimo por orden (mínimos reales de Polymarket)
- máx % por mercado, por categoría, por wallet copiada y por estrategia
- máx exposición total
- stop de pérdida diario: si el equity cae X% desde el inicio del día,
  no se abren posiciones nuevas (vender siempre está permitido)
- kill switch (archivo var/KILL o env PMBOT_KILL=1): bloquea compras;
  las ventas se permiten porque reducen riesgo.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("pmbot.risk")


@dataclass(frozen=True)
class Limits:
    max_pct_per_market: float
    max_pct_per_category: float
    max_pct_per_copied_wallet: float
    max_total_exposure_pct: float
    daily_stop_loss_pct: float
    min_order_usdc: float = 1.0
    max_drawdown_pct: float = 1.0   # freno total desde el capital inicial

    @classmethod
    def from_config(cls, cfg: dict[str, Any]) -> "Limits":
        return cls(
            max_pct_per_market=float(cfg.get("max_pct_per_market", 0.10)),
            max_pct_per_category=float(cfg.get("max_pct_per_category", 0.40)),
            max_pct_per_copied_wallet=float(cfg.get("max_pct_per_copied_wallet", 0.15)),
            max_total_exposure_pct=float(cfg.get("max_total_exposure_pct", 0.80)),
            daily_stop_loss_pct=float(cfg.get("daily_stop_loss_pct", 0.05)),
            min_order_usdc=float(cfg.get("min_order_usdc", 1.0)),
            max_drawdown_pct=float(cfg.get("max_drawdown_pct", 1.0)),
        )


@dataclass
class OrderRequest:
    strategy: str
    condition_id: str
    category: str
    token_id: str
    outcome: str
    outcome_index: int
    side: str                    # BUY | SELL
    size: float                  # shares
    price: float                 # precio límite (para BUY, el máximo aceptable)
    reason: str
    strategy_budget_pct: float = 1.0
    copied_wallet: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def cost(self) -> float:
        return self.size * self.price


@dataclass
class PortfolioState:
    equity: float
    cash: float
    day_start_equity: float
    exposure_total: float
    exposure_by_market: dict[str, float]
    exposure_by_category: dict[str, float]
    exposure_by_wallet: dict[str, float]
    exposure_by_strategy: dict[str, float]
    starting_equity: float = 0.0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


def evaluate(order: OrderRequest, state: PortfolioState,
             limits: Limits) -> RiskDecision:
    """Pura: aprueba o rechaza una orden contra el estado del portfolio."""
    if order.side != "BUY":
        return RiskDecision(True, "venta: reduce riesgo")

    cost = order.cost
    equity = state.equity
    if equity <= 0:
        return RiskDecision(False, "equity agotado")
    if cost < limits.min_order_usdc:
        return RiskDecision(False, f"orden muy chica (${cost:.2f} < "
                            f"${limits.min_order_usdc:.2f})")
    if cost > state.cash:
        return RiskDecision(False, f"cash insuficiente (${state.cash:.2f})")

    if (state.day_start_equity > 0 and
            equity <= state.day_start_equity * (1 - limits.daily_stop_loss_pct)):
        return RiskDecision(False, "stop diario activado: no se abren posiciones hoy")

    # Freno total: protege el capital cuando nadie está mirando. A diferencia
    # del stop diario (que se reinicia cada día UTC), este no se levanta solo.
    if (state.starting_equity > 0 and
            equity <= state.starting_equity * (1 - limits.max_drawdown_pct)):
        return RiskDecision(
            False, f"FRENO TOTAL: caída de {limits.max_drawdown_pct:.0%} desde "
                   f"el capital inicial (${state.starting_equity:.2f})")

    checks = [
        ("mercado", state.exposure_by_market.get(order.condition_id, 0.0),
         limits.max_pct_per_market),
        ("categoría " + order.category,
         state.exposure_by_category.get(order.category, 0.0),
         limits.max_pct_per_category),
        ("estrategia " + order.strategy,
         state.exposure_by_strategy.get(order.strategy, 0.0),
         order.strategy_budget_pct),
        ("total", state.exposure_total, limits.max_total_exposure_pct),
    ]
    if order.copied_wallet:
        checks.append(("wallet " + order.copied_wallet[:10],
                       state.exposure_by_wallet.get(order.copied_wallet, 0.0),
                       limits.max_pct_per_copied_wallet))
    for label, current, max_pct in checks:
        if current + cost > max_pct * equity:
            return RiskDecision(
                False, f"límite por {label}: ${current:.2f} + ${cost:.2f} "
                       f"> {max_pct:.0%} de ${equity:.2f}")
    return RiskDecision(True, "ok")


class RiskManager:
    def __init__(self, conn: sqlite3.Connection, risk_cfg: dict[str, Any],
                 var_dir: Path) -> None:
        self.conn = conn
        self.limits = Limits.from_config(risk_cfg)
        self.kill_file = var_dir / "KILL"

    def kill_switch_on(self) -> bool:
        return self.kill_file.exists() or os.environ.get("PMBOT_KILL") == "1"

    def check(self, order: OrderRequest, state: PortfolioState) -> RiskDecision:
        if order.side == "BUY" and self.kill_switch_on():
            decision = RiskDecision(False, "kill switch activado")
        else:
            decision = evaluate(order, state, self.limits)
        if not decision.approved:
            log.info("risk RECHAZA [%s] %s %s: %s", order.strategy,
                     order.side, order.condition_id[:10], decision.reason)
        return decision

    def day_start_equity(self, current_equity: float) -> float:
        """Equity al inicio del día UTC; se fija la primera vez que se pide."""
        key = f"day_start_equity:{datetime.now(timezone.utc).date().isoformat()}"
        row = self.conn.execute(
            "SELECT value FROM paper_state WHERE key = ?", (key,)).fetchone()
        if row:
            return float(row["value"])
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO paper_state (key, value) VALUES (?, ?)",
                (key, str(current_equity)))
        return current_equity

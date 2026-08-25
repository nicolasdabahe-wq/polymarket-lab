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
    # Velocidad del capital. Con una cuenta chica, el dinero atado a un
    # mercado que se resuelve en tres meses no compone: no pierde, pero
    # tampoco da vueltas. Se limita cuánto se puede tener dormido.
    max_days_to_resolution: float = 3650.0   # tope duro por apuesta
    slow_days: float = 10.0                  # a partir de acá es "lenta"
    max_pct_slow: float = 1.0                # máx del equity en lentas
    # Ganancia mínima que tiene que estar en juego para que valga la pena.
    # Una apuesta paga (1/precio - 1) por dólar: a 0.83 se arriesgan $8.53
    # para ganar $1.66, y hace falta acertar 83 de cada 100 solo para
    # empatar. Esta regla ata precio y tamaño en un solo número: si el
    # premio no llega, la apuesta no se hace. Decisión del dueño
    # (2026-08-24): "no quiero ganar 2 dólares, mínimo 15 o 20".
    min_ganancia_usdc: float = 0.0

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
            max_days_to_resolution=float(
                cfg.get("max_days_to_resolution", 3650)),
            slow_days=float(cfg.get("slow_days", 10)),
            max_pct_slow=float(cfg.get("max_pct_slow", 1.0)),
            min_ganancia_usdc=float(cfg.get("min_ganancia_usdc", 0.0)),
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
    # Días hasta que el mercado se resuelva. Con capital chico, el dinero
    # parado no compone: una apuesta a tres meses secuestra munición que
    # podría dar varias vueltas en ese tiempo.
    days_to_resolution: float | None = None

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
    # Precio promedio de lo que ya tenemos en cada mercado:
    # {condition_id: {outcome_index: avg_price}}. Sirve para no pagar más
    # de $1 entre los dos lados del mismo evento.
    held_outcomes: dict[str, dict[int, float]] = field(default_factory=dict)
    # Valor invertido en mercados que tardan en resolverse.
    exposure_slow: float = 0.0


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


# Costo máximo del par YES+NO que todavía deja ganancia (deja margen para
# el tick y el redondeo).
MAX_PAR_COST = 0.98


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

    # Premio en juego: lo que se cobraría de más si la apuesta gana.
    if limits.min_ganancia_usdc > 0 and order.price > 0:
        premio = cost * (1.0 / order.price - 1.0)
        if premio < limits.min_ganancia_usdc:
            return RiskDecision(
                False, f"premio flaco: arriesga ${cost:.2f} a {order.price:.2f} "
                       f"para ganar solo ${premio:.2f} (mínimo "
                       f"${limits.min_ganancia_usdc:.2f})")

    # Velocidad del capital: no atar dinero a resoluciones lejanas.
    dias = order.days_to_resolution
    if dias is not None:
        if dias < -1:
            # Fecha vencida y el mercado sigue abierto: está en limbo (ej.
            # una primaria que se fue a segunda vuelta). Peor que lento:
            # dormido SIN fecha. Así se recompró Nordone el 2026-08-22.
            return RiskDecision(
                False, "mercado en limbo: su fecha venció hace "
                       f"{-dias:.0f} días y sigue sin resolverse")
        # Tope duro y sin excepciones (decisión del dueño, 2026-08-22): el
        # dinero no se ata más allá de este plazo por buena que parezca la
        # apuesta. Hubo una excepción por "oportunidad dorada" y se eliminó
        # porque no era lo que se había pedido.
        if dias > limits.max_days_to_resolution:
            return RiskDecision(
                False, f"se resuelve en {dias:.0f} días (máx "
                       f"{limits.max_days_to_resolution:.0f}): el dinero "
                       f"quedaría parado demasiado tiempo")
        if dias >= limits.slow_days:
            tope = limits.max_pct_slow * equity
            if state.exposure_slow + cost > tope:
                return RiskDecision(
                    False, f"ya hay ${state.exposure_slow:.2f} en mercados "
                           f"lentos y esta suma ${cost:.2f}: pasa el "
                           f"{limits.max_pct_slow:.0%} del capital")

    # Los dos lados del mismo evento solo si JUNTOS cuestan menos de $1.
    # Comprar Musetti a 0.50 y Tiafoe a 0.69 es pagar 1.19 por algo que paga
    # 1.00: se pierde gane quien gane. Pero si el rival se desploma a 0.25,
    # el par asegura ganancia y hay que dejarlo pasar.
    # Recargar el MISMO lado no se toca: es convicción, no contradicción.
    opuestos = [p for idx, p in
                (state.held_outcomes.get(order.condition_id) or {}).items()
                if idx != order.outcome_index]
    if opuestos:
        par = min(opuestos) + order.price
        if par > MAX_PAR_COST:
            return RiskDecision(
                False, f"ya tenemos el otro lado a {min(opuestos):.2f}; "
                       f"con esta entrada a {order.price:.2f} el par cuesta "
                       f"{par:.2f} y solo paga 1.00")

    # Los dos frenos de capital se APAGAN poniendo su porcentaje en 1.0
    # ("frená cuando haya perdido el 100%"), y hay que comprobarlo aparte:
    # un 0.0 no los apaga, los vuelve permanentes (frenaría ante el primer
    # centavo de pérdida). El dueño los quitó explícitamente el 2026-08-22:
    # "quita todo tipo de stop o freno".
    if (limits.daily_stop_loss_pct < 1.0 and state.day_start_equity > 0 and
            equity <= state.day_start_equity * (1 - limits.daily_stop_loss_pct)):
        return RiskDecision(False, "stop diario activado: no se abren posiciones hoy")

    # Freno total: protege el capital cuando nadie está mirando. A diferencia
    # del stop diario (que se reinicia cada día UTC), este no se levanta solo.
    if (limits.max_drawdown_pct < 1.0 and state.starting_equity > 0 and
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

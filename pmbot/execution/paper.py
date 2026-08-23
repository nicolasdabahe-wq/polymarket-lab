"""Broker paper: simula la ejecución contra el order book real del CLOB.

- BUY camina los asks (peor precio a medida que consume niveles) hasta llenar
  o hasta que el precio supere el límite -> slippage realista.
- SELL camina los bids.
- REDEEM liquida a 0/1 cuando el mercado resuelve.
- Idempotencia: cada orden lleva un id determinístico; si ya existe en la
  tabla orders no se re-ejecuta.
- Mínimos reales de Polymarket: 5 shares por orden.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ..data.clob import BookLevel, ClobClient
from ..db import from_json, to_json
from ..risk import OrderRequest, PortfolioState, RiskManager

log = logging.getLogger("pmbot.execution.paper")

MIN_SHARES = 5.0  # mínimo real de Polymarket por orden


@dataclass
class Fill:
    order_id: str
    status: str          # FILLED | REJECTED | NO_LIQUIDITY | DUPLICATE
    size: float = 0.0
    price: float = 0.0
    usdc: float = 0.0
    fee: float = 0.0
    realized_pnl: float | None = None
    detail: str = ""
    # True solo si la orden llegó al exchange (aunque no llenara). Distingue
    # un rechazo de risk/ —que nunca salió— de uno del CLOB, que sí pudo
    # llenarse tarde en mercados con delay.
    sent: bool = False


def simulate_book_fill(levels: list[BookLevel], size: float,
                       limit_price: float, side: str) -> tuple[float, float]:
    """Camina el book y devuelve (shares llenadas, precio promedio).

    Para BUY, levels son asks (ordenados de menor a mayor) y el límite es el
    precio máximo aceptable; para SELL, bids (mayor a menor) y límite mínimo.
    Puro, testeable.
    """
    filled = 0.0
    cost = 0.0
    for level in levels:
        if side == "BUY" and level.price > limit_price:
            break
        if side == "SELL" and level.price < limit_price:
            break
        take = min(size - filled, level.size)
        filled += take
        cost += take * level.price
        if filled >= size - 1e-9:
            break
    return (filled, cost / filled if filled > 0 else 0.0)


class PaperBroker:
    def __init__(self, conn: sqlite3.Connection, clob: ClobClient,
                 risk: RiskManager, capital_cfg: dict[str, Any],
                 exec_cfg: dict[str, Any] | None = None) -> None:
        self.conn = conn
        self.clob = clob
        self.risk = risk
        self.fee_bps = float((exec_cfg or {}).get("taker_fee_bps", 0))
        self._ensure_account(float(capital_cfg.get("paper_starting_usdc", 500)))

    # ---------- cuenta y estado ----------

    def _ensure_account(self, starting: float) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO paper_account
                   (id, starting_usdc, cash_usdc, updated_at)
                   VALUES (1, ?, ?, ?)""",
                (starting, starting, _now()))

    @property
    def cash(self) -> float:
        row = self.conn.execute(
            "SELECT cash_usdc FROM paper_account WHERE id = 1").fetchone()
        return float(row["cash_usdc"])

    def starting_capital(self) -> float:
        """Base contra la que se mide el PnL total."""
        row = self.conn.execute(
            "SELECT starting_usdc FROM paper_account WHERE id = 1").fetchone()
        return float(row["starting_usdc"])

    def _set_cash(self, value: float) -> None:
        self.conn.execute(
            "UPDATE paper_account SET cash_usdc = ?, updated_at = ? WHERE id = 1",
            (value, _now()))

    def positions(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM paper_positions ORDER BY strategy, opened_at").fetchall()

    def mark_price(self, condition_id: str, outcome_index: int,
                   fallback: float) -> float:
        """Precio actual del outcome según el cache de mercados (binarios:
        índice 0 = yes_price, índice 1 = 1 - yes_price)."""
        row = self.conn.execute(
            "SELECT yes_price FROM markets WHERE condition_id = ?",
            (condition_id,)).fetchone()
        if not row or row["yes_price"] is None:
            return fallback
        yes = float(row["yes_price"])
        return yes if outcome_index == 0 else 1.0 - yes

    def _condiciones_lentas(self) -> set[str]:
        """Mercados de las posiciones abiertas que tardan en resolverse.

        La fecha sale del cache de mercados; si no la conocemos, se asume
        rápida (no bloquear por ignorancia).
        """
        umbral = getattr(self.risk.limits, "slow_days", 10.0)
        corte = (datetime.now(timezone.utc) + timedelta(days=umbral)).isoformat()
        return {r["condition_id"] for r in self.conn.execute(
            """SELECT condition_id FROM markets
               WHERE end_date IS NOT NULL AND end_date > ?""", (corte,))}

    def positions_value(self) -> float:
        """Valor de mercado de las posiciones propias (sin tocar el estado
        completo: lo usa starting_capital, que portfolio_state consulta)."""
        return sum(p["size"] * self.mark_price(
            p["condition_id"], p["outcome_index"] or 0, p["avg_price"])
            for p in self.positions())

    def external_value(self) -> float:
        """Valor de las posiciones on-chain que el bot NO gestiona: las que
        abrió el dueño por su cuenta y los payouts ya ganados que todavía no
        volvieron al saldo. Suman al equity (son dinero suyo) pero no a la
        exposición (el bot no las administra ni las vende).

        En paper no existen: siempre 0."""
        return 0.0

    def portfolio_state(self) -> PortfolioState:
        cash = self.cash
        by_market: dict[str, float] = {}
        by_category: dict[str, float] = {}
        by_wallet: dict[str, float] = {}
        by_strategy: dict[str, float] = {}
        held: dict[str, dict[int, float]] = {}
        lentas = self._condiciones_lentas()
        exposure_slow = 0.0
        positions_value = 0.0
        for p in self.positions():
            value = p["size"] * self.mark_price(
                p["condition_id"], p["outcome_index"] or 0, p["avg_price"])
            positions_value += value
            if p["condition_id"] in lentas:
                exposure_slow += value
            held.setdefault(p["condition_id"], {})[p["outcome_index"] or 0] = \
                float(p["avg_price"])
            by_market[p["condition_id"]] = by_market.get(p["condition_id"], 0) + value
            by_category[p["category"] or "other"] = \
                by_category.get(p["category"] or "other", 0) + value
            by_strategy[p["strategy"]] = by_strategy.get(p["strategy"], 0) + value
            meta = from_json(p["meta"]) or {}
            wallet = meta.get("copied_wallet")
            if wallet:
                by_wallet[wallet] = by_wallet.get(wallet, 0) + value
        equity = cash + positions_value + self.external_value()
        return PortfolioState(
            equity=equity, cash=cash,
            day_start_equity=self.risk.day_start_equity(equity),
            starting_equity=self.starting_capital(),
            exposure_total=positions_value,
            exposure_by_market=by_market, exposure_by_category=by_category,
            exposure_by_wallet=by_wallet, exposure_by_strategy=by_strategy,
            held_outcomes=held, exposure_slow=exposure_slow,
        )

    def equity(self) -> float:
        return self.portfolio_state().equity

    def snapshot_equity(self) -> None:
        state = self.portfolio_state()
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO equity_history
                   (ts, cash_usdc, positions_usdc, equity_usdc)
                   VALUES (?,?,?,?)""",
                (_now(), state.cash, state.exposure_total, state.equity))

    # ---------- ejecución ----------

    async def execute(self, order_id: str, request: OrderRequest) -> Fill:
        """Único punto de entrada de órdenes. Idempotente por order_id."""
        existing = self.conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if existing:
            return Fill(order_id, "DUPLICATE",
                        detail=f"ya ejecutada ({existing['status']})")

        if request.side == "BUY" and request.size < MIN_SHARES:
            return self._record(order_id, request, Fill(
                order_id, "REJECTED",
                detail=f"mínimo {MIN_SHARES:.0f} shares"))

        decision = self.risk.check(request, self.portfolio_state())
        if not decision.approved:
            return self._record(order_id, request,
                                Fill(order_id, "REJECTED", detail=decision.reason))

        try:
            book = await self.clob.order_book(request.token_id)
        except Exception as exc:
            return self._record(order_id, request, Fill(
                order_id, "NO_LIQUIDITY", detail=f"book inaccesible: {exc}"))

        levels = book.asks if request.side == "BUY" else book.bids
        filled, avg_price = simulate_book_fill(
            levels, request.size, request.price, request.side)
        if filled < MIN_SHARES:
            return self._record(order_id, request, Fill(
                order_id, "NO_LIQUIDITY",
                detail=f"solo {filled:.1f} shares dentro del límite"))

        usdc = filled * avg_price
        fee = usdc * self.fee_bps / 10_000
        if request.side == "BUY":
            fill = self._apply_buy(request, filled, avg_price, usdc, fee)
        else:
            fill = self._apply_sell(request, filled, avg_price, usdc, fee)
        fill.order_id = order_id
        result = self._record(order_id, request, fill)
        log.info("orden %s [%s] %s %.0f×%s @ %.3f → %s %s",
                 order_id[:24], request.strategy, request.side, filled,
                 request.outcome, avg_price, fill.status, fill.detail)
        return result

    def _apply_buy(self, r: OrderRequest, filled: float, price: float,
                   usdc: float, fee: float) -> Fill:
        with self.conn:
            self._set_cash(self.cash - usdc - fee)
            row = self.conn.execute(
                """SELECT size, avg_price FROM paper_positions
                   WHERE strategy=? AND condition_id=? AND outcome=?""",
                (r.strategy, r.condition_id, r.outcome)).fetchone()
            if row:
                new_size = row["size"] + filled
                new_avg = (row["size"] * row["avg_price"] + usdc) / new_size
                self.conn.execute(
                    """UPDATE paper_positions SET size=?, avg_price=?, updated_at=?
                       WHERE strategy=? AND condition_id=? AND outcome=?""",
                    (new_size, new_avg, _now(), r.strategy, r.condition_id,
                     r.outcome))
            else:
                self.conn.execute(
                    """INSERT INTO paper_positions (strategy, condition_id,
                       outcome, outcome_index, token_id, question, category,
                       size, avg_price, meta, opened_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (r.strategy, r.condition_id, r.outcome, r.outcome_index,
                     r.token_id, r.meta.get("question", ""), r.category,
                     filled, price, to_json(r.meta), _now(), _now()))
        return Fill("", "FILLED", filled, price, usdc, fee)

    def _apply_sell(self, r: OrderRequest, filled: float, price: float,
                    usdc: float, fee: float) -> Fill:
        row = self.conn.execute(
            """SELECT size, avg_price FROM paper_positions
               WHERE strategy=? AND condition_id=? AND outcome=?""",
            (r.strategy, r.condition_id, r.outcome)).fetchone()
        if not row:
            return Fill("", "REJECTED", detail="no hay posición que vender")
        filled = min(filled, row["size"])
        usdc = filled * price
        fee = usdc * self.fee_bps / 10_000
        realized = usdc - fee - filled * row["avg_price"]
        with self.conn:
            self._set_cash(self.cash + usdc - fee)
            remaining = row["size"] - filled
            if remaining > 1e-6:
                self.conn.execute(
                    """UPDATE paper_positions SET size=?, updated_at=?
                       WHERE strategy=? AND condition_id=? AND outcome=?""",
                    (remaining, _now(), r.strategy, r.condition_id, r.outcome))
            else:
                self.conn.execute(
                    """DELETE FROM paper_positions
                       WHERE strategy=? AND condition_id=? AND outcome=?""",
                    (r.strategy, r.condition_id, r.outcome))
        return Fill("", "FILLED", filled, price, usdc, fee, realized_pnl=realized)

    def redeem(self, position: sqlite3.Row, payout_price: float,
               reason: str) -> Fill:
        """Liquida una posición de un mercado resuelto a 0 o 1."""
        size = position["size"]
        usdc = size * payout_price
        realized = usdc - size * position["avg_price"]
        order_id = (f"redeem:{position['strategy']}:{position['condition_id']}"
                    f":{position['outcome_index']}")
        if self.conn.execute("SELECT 1 FROM orders WHERE id=?",
                             (order_id,)).fetchone():
            return Fill(order_id, "DUPLICATE")
        with self.conn:
            self._set_cash(self.cash + usdc)
            self.conn.execute(
                """DELETE FROM paper_positions
                   WHERE strategy=? AND condition_id=? AND outcome=?""",
                (position["strategy"], position["condition_id"],
                 position["outcome"]))
            self.conn.execute(
                """INSERT INTO orders (id, strategy, condition_id, token_id,
                   outcome, outcome_index, side, req_size, limit_price, status,
                   fill_size, fill_price, fill_usdc, fee_usdc, realized_pnl,
                   reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, position["strategy"], position["condition_id"],
                 position["token_id"], position["outcome"],
                 position["outcome_index"], "REDEEM", size, payout_price,
                 "FILLED", size, payout_price, usdc, 0.0, realized, reason,
                 _now()))
        log.info("REDEEM [%s] %s '%s' @ %.0f → PnL %+.2f",
                 position["strategy"], position["outcome"],
                 (position["question"] or "")[:40], payout_price, realized)
        return Fill(order_id, "FILLED", size, payout_price, usdc, 0.0, realized)

    def _record(self, order_id: str, r: OrderRequest, fill: Fill) -> Fill:
        with self.conn:
            # Un intento rechazado que nunca salió al exchange se puede
            # reintentar (ver LiveBroker.execute), y entonces su fila tiene
            # que actualizarse con el resultado nuevo. Con INSERT OR IGNORE
            # el reintento se ejecutaba de verdad pero quedaba sin registrar,
            # y el bot creía que seguía teniendo la posición.
            # La condición del UPDATE es la garantía: una orden que SÍ llegó
            # al exchange (sent = 1) o que se llenó no se pisa jamás.
            self.conn.execute(
                """INSERT INTO orders (id, strategy, condition_id,
                   token_id, outcome, outcome_index, side, req_size,
                   limit_price, status, fill_size, fill_price, fill_usdc,
                   fee_usdc, realized_pnl, reason, reject_reason, sent,
                   created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     status=excluded.status, req_size=excluded.req_size,
                     limit_price=excluded.limit_price,
                     fill_size=excluded.fill_size,
                     fill_price=excluded.fill_price,
                     fill_usdc=excluded.fill_usdc,
                     fee_usdc=excluded.fee_usdc,
                     realized_pnl=excluded.realized_pnl,
                     reason=excluded.reason,
                     reject_reason=excluded.reject_reason,
                     sent=excluded.sent, created_at=excluded.created_at
                   WHERE orders.status = 'REJECTED' AND orders.sent = 0""",
                (order_id, r.strategy, r.condition_id, r.token_id, r.outcome,
                 r.outcome_index, r.side, r.size, r.price, fill.status,
                 fill.size or None, fill.price or None, fill.usdc or None,
                 fill.fee or None, fill.realized_pnl, r.reason,
                 fill.detail if fill.status != "FILLED" else None,
                 int(fill.sent or fill.status == "FILLED"), _now()))
        return fill


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

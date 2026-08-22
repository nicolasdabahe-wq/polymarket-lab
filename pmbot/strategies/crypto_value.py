"""Valor en mercados de precio de cripto: modelo vs mercado.

Idea: los mercados "¿BTC arriba de $X el día Y?" son opciones digitales, y
"¿BTC toca $X antes de Y?" son one-touch. Su probabilidad teórica se puede
calcular con el spot y la volatilidad realizada (Coinbase). Cuando el precio
de Polymarket discrepa fuerte del modelo (>= min_edge), compramos el lado
barato. Los apostadores casuales precian por intuición; el modelo no.

Modelo (GBM sin drift, la convención estándar para horizontes cortos):
- Terminal  P(S_T > K)  = Phi( (ln(S/K) - s²T/2) / (s·sqrt(T)) )
- One-touch (máximo toca K>S):
    a = ln(K/S), m = -s²/2
    P = Phi((-a + mT)/(s·sqrt(T))) + exp(2·m·a/s²)·Phi((-a - mT)/(s·sqrt(T)))
  (fórmula del máximo de un browniano con drift; para K<S es simétrico)

Todo puro y testeable; la estrategia solo orquesta.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..data.prices import PriceFeed
from ..execution import PaperBroker
from ..risk import OrderRequest

log = logging.getLogger("pmbot.strategies.crypto_value")

ASSET_PRODUCTS = {
    "bitcoin": "BTC-USD", "btc": "BTC-USD",
    "ethereum": "ETH-USD", "eth": "ETH-USD", "ether": "ETH-USD",
    "solana": "SOL-USD", "sol": "SOL-USD",
    "xrp": "XRP-USD", "dogecoin": "DOGE-USD", "doge": "DOGE-USD",
}

# "Will Bitcoin reach $100,000 in August?"  -> touch above
# "Will Bitcoin dip to $60,000 in August?"  -> touch below
# "Will the price of Ethereum be above $2,500 on August 25?" -> terminal
QUESTION_RE = re.compile(
    r"will\s+(?:the\s+price\s+of\s+)?(?P<asset>bitcoin|btc|ethereum|ether|eth|"
    r"solana|sol|xrp|dogecoin|doge)\b(?P<middle>.{0,60}?)"
    r"\$(?P<strike>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE)


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_terminal_above(spot: float, strike: float, vol_daily: float,
                        days: float) -> float:
    """P(S_T > K) bajo GBM sin drift."""
    if spot <= 0 or strike <= 0 or vol_daily <= 0 or days <= 0:
        return 1.0 if spot > strike else 0.0
    s = vol_daily * math.sqrt(days)
    return _phi((math.log(spot / strike) - s * s / 2) / s)


def prob_touch(spot: float, strike: float, vol_daily: float,
               days: float) -> float:
    """P(el precio toca K en algún momento antes de T).

    Log-precio X_t con drift mu = -s²/2 (GBM sin drift). Para barrera por
    arriba (a = ln(K/S) > 0): P(max X >= a) = Phi((mu·T - a)/(s√T)) +
    exp(2·mu·a/s²)·Phi((-a - mu·T)/(s√T)). Para barrera por abajo se aplica
    la misma fórmula al proceso reflejado -X, cuyo drift es +s²/2.
    """
    if spot <= 0 or strike <= 0 or vol_daily <= 0 or days <= 0:
        return 0.0
    if math.isclose(spot, strike, rel_tol=1e-9):
        return 1.0
    a = abs(math.log(strike / spot))
    sig2 = vol_daily * vol_daily
    mu = -sig2 / 2 if strike > spot else sig2 / 2
    s = vol_daily * math.sqrt(days)
    term1 = _phi((mu * days - a) / s)
    term2 = math.exp(2 * mu * a / sig2) * _phi((-a - mu * days) / s)
    return min(max(term1 + term2, 0.0), 1.0)


@dataclass
class ParsedMarket:
    product: str        # ej. BTC-USD
    strike: float
    kind: str           # 'touch_above' | 'touch_below' | 'terminal_above' | 'terminal_below'


def parse_crypto_question(question: str) -> ParsedMarket | None:
    """Extrae activo, strike y tipo de payoff de la pregunta. Puro.

    Devuelve None si la pregunta es ambigua (mejor no operar que adivinar).
    """
    match = QUESTION_RE.search(question)
    if not match:
        return None
    product = ASSET_PRODUCTS.get(match.group("asset").lower())
    if not product:
        return None
    try:
        strike = float(match.group("strike").replace(",", ""))
    except ValueError:
        return None
    middle = match.group("middle").lower()
    q = question.lower()
    touch_words = ("reach", "hit", "touch")
    touch_below_words = ("dip", "drop", "fall")
    is_touch = any(w in middle for w in touch_words)
    is_below_marker = "(low)" in q or any(w in middle for w in touch_below_words)
    if is_touch:
        return ParsedMarket(product, strike,
                            "touch_below" if is_below_marker else "touch_above")
    if any(w in middle for w in touch_below_words):
        return ParsedMarket(product, strike, "touch_below")
    if "above" in middle:
        return ParsedMarket(product, strike, "terminal_above")
    if "below" in middle:
        return ParsedMarket(product, strike, "terminal_below")
    return None  # ambiguo: no operar


def model_probability(parsed: ParsedMarket, spot: float, vol_daily: float,
                      days: float) -> float:
    """Probabilidad teórica del YES según el tipo de payoff."""
    if parsed.kind == "terminal_above":
        return prob_terminal_above(spot, parsed.strike, vol_daily, days)
    if parsed.kind == "terminal_below":
        return 1.0 - prob_terminal_above(spot, parsed.strike, vol_daily, days)
    if parsed.kind == "touch_above":
        if spot >= parsed.strike:
            return 1.0
        return prob_touch(spot, parsed.strike, vol_daily, days)
    if parsed.kind == "touch_below":
        if spot <= parsed.strike:
            return 1.0
        return prob_touch(spot, parsed.strike, vol_daily, days)
    raise ValueError(parsed.kind)


class CryptoValueStrategy:
    name = "crypto_value"

    def __init__(self, conn: sqlite3.Connection, prices: PriceFeed,
                 broker: PaperBroker, cfg: dict[str, Any]) -> None:
        self.conn = conn
        self.prices = prices
        self.broker = broker
        self.enabled = bool(cfg.get("enabled", True))
        self.budget_pct = float(cfg.get("budget_pct", 0.20))
        self.base_pct = float(cfg.get("base_pct_per_trade", 0.05))
        self.min_edge = float(cfg.get("min_edge", 0.12))
        self.max_entry = float(cfg.get("max_entry_price", 0.85))
        self.min_days = float(cfg.get("min_days_to_resolution", 0.5))
        self.max_days = float(cfg.get("max_days_to_resolution", 45))
        self.vol_lookback = int(cfg.get("vol_lookback_days", 30))
        self.min_trade_usdc = float(cfg.get("min_trade_usdc", 10.0))

    async def scan_and_execute(self) -> list[str]:
        if not self.enabled:
            return []
        now = datetime.now(timezone.utc)
        executed: list[str] = []
        rows = self.conn.execute(
            """SELECT * FROM markets WHERE active = 1 AND category = 'crypto'
               AND yes_price IS NOT NULL AND end_date IS NOT NULL
               ORDER BY volume_24h DESC LIMIT 150""").fetchall()
        for row in rows:
            parsed = parse_crypto_question(row["question"])
            if not parsed:
                continue
            try:
                end = datetime.fromisoformat(
                    row["end_date"].replace("Z", "+00:00"))
            except ValueError:
                continue
            days = (end - now).total_seconds() / 86400
            if not (self.min_days <= days <= self.max_days):
                continue
            spot = await self.prices.spot(parsed.product)
            vol = await self.prices.daily_vol(parsed.product, self.vol_lookback)
            if not spot or not vol:
                continue
            model_p = model_probability(parsed, spot, vol, days)
            market_p = float(row["yes_price"])
            edge = model_p - market_p
            if abs(edge) < self.min_edge:
                continue
            desc = await self._try_trade(row, parsed, model_p, market_p, edge,
                                         spot, days)
            if desc:
                executed.append(desc)
        return executed

    async def _try_trade(self, row: sqlite3.Row, parsed: ParsedMarket,
                         model_p: float, market_p: float, edge: float,
                         spot: float, days: float) -> str | None:
        import json as _json
        tokens = _json.loads(row["clob_token_ids"] or "[]")
        if len(tokens) != 2:
            return None
        if edge > 0:      # el mercado subestima el YES -> comprar YES
            idx, outcome, win_prob = 0, "Yes", model_p
        else:             # el mercado sobreestima el YES -> comprar NO
            idx, outcome, win_prob = 1, "No", 1.0 - model_p

        # CRÍTICO: re-verificar contra el book EN VIVO. El cache puede tener
        # minutos de atraso y en cripto eso fabrica edges falsos: el edge
        # real es (prob. de ganar según modelo) - (ask que pagaríamos ahora).
        try:
            book = await self.broker.clob.order_book(tokens[idx])
        except Exception as exc:
            log.debug("book en vivo falló %s: %s", row["condition_id"][:10], exc)
            return None
        ask = book.best_ask
        if ask is None or ask > self.max_entry or ask <= 0.01:
            return None
        live_edge = win_prob - ask
        if live_edge < self.min_edge:
            return None

        equity = self.broker.equity()
        # tamaño escala con el edge real: 12 puntos = base, 24+ ~ doble
        size_usdc = max(equity * self.base_pct * min(live_edge / self.min_edge, 2.0),
                        self.min_trade_usdc)
        size = size_usdc / ask
        today = datetime.now(timezone.utc).date().isoformat()
        reason = (f"valor cripto: modelo da {win_prob:.0%} al {outcome} y el "
                  f"book pide {ask:.0%} (edge {live_edge:+.0%}; "
                  f"{parsed.product} spot ${spot:,.0f}, strike "
                  f"${parsed.strike:,.0f}, {days:.1f}d, {parsed.kind})")
        fill = await self.broker.execute(
            f"cvalue:{row['condition_id']}:{idx}:{today}",
            OrderRequest(
                strategy=self.name, condition_id=row["condition_id"],
                category="crypto", token_id=tokens[idx], outcome=outcome,
                outcome_index=idx, side="BUY", size=size,
                price=min(ask * 1.02, 0.99), days_to_resolution=days,
                reason=reason, strategy_budget_pct=self.budget_pct,
                meta={"question": row["question"], "model_p": model_p,
                      "live_ask": ask}))
        if fill.status == "FILLED":
            log.info("CRYPTO VALUE: %s", reason)
            return f"{row['question'][:60]} — {reason}"
        return None

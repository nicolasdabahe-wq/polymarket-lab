"""Decisión de liquidación de una posición: ¿el mercado ya se resolvió?

No alcanza con esperar a que Gamma marque `closed`: la resolución tarda
horas y en deportes el resultado se sabe al instante. Señales, de más a
menos definitiva:

 1. Gamma marca el mercado como closed          -> payout oficial.
 2. La posición on-chain figura como redeemable  -> el payout ya se puede
    cobrar, así que el resultado es firme.
 3. umaResolutionStatus == 'proposed'/'resolved' Y el precio quedó clavado
    en ~0/~1 -> el resultado ya se propuso al oráculo. Este es el caso que
    llega primero (verificado 2026-08: partidos terminados aparecen como
    'proposed' con precio 0.0005/0.9995 mientras closed sigue en false).

El precio POR SÍ SOLO nunca liquida: hay mercados vivos que cotizan a
0.003 durante semanas ("¿la Fed baja 50+ bps?") y liquidarlos sería
inventar una pérdida y perder de vista la posición. Tampoco sirve mirar
endDate: los mercados deportivos lo ponen una semana después del partido.

La función es pura: el scheduler le pasa lo observado y ella decide.
"""
from __future__ import annotations

from typing import Sequence

# Umbrales de "precio clavado": mismos que usa el backtest.
PIN_LOW = 0.005
PIN_HIGH = 0.995

# Estados del oráculo que dan el resultado por conocido. 'disputed' queda
# fuera a propósito: ahí el resultado todavía se pelea.
UMA_SETTLED = {"proposed", "resolved"}


class Settlement:
    """payout: precio de liquidación (0 o 1), o None si no se liquida aún.
    reason: texto corto para el registro y el aviso de Telegram."""

    __slots__ = ("payout", "reason")

    def __init__(self, payout: float | None, reason: str) -> None:
        self.payout = payout
        self.reason = reason

    def __eq__(self, other: object) -> bool:  # pragma: no cover - solo tests
        return (isinstance(other, Settlement)
                and (self.payout, self.reason) == (other.payout, other.reason))

    def __repr__(self) -> str:  # pragma: no cover - solo depuración
        return f"Settlement(payout={self.payout}, reason={self.reason!r})"


def _pinned(price: float | None) -> bool:
    return price is not None and (price <= PIN_LOW or price >= PIN_HIGH)


def decide_settlement(*, gamma_closed: bool,
                      gamma_prices: Sequence[float] | None,
                      outcome_index: int,
                      uma_status: str | None,
                      onchain_price: float | None,
                      onchain_redeemable: bool) -> Settlement:
    """Decide si la posición se liquida ya, y a qué precio. Pura."""
    prices = list(gamma_prices or [])
    price_gamma = (float(prices[outcome_index])
                   if outcome_index < len(prices) else None)

    if gamma_closed and price_gamma is not None:
        return Settlement(price_gamma, "resuelto en Gamma")

    if onchain_redeemable and onchain_price is not None:
        return Settlement(1.0 if onchain_price >= 0.5 else 0.0,
                          "payout redimible on-chain")

    # El precio on-chain manda (viene de la posición real); si la posición
    # ya no está on-chain (payout cobrado), queda el de Gamma.
    price = onchain_price if onchain_price is not None else price_gamma
    if (uma_status or "").lower() in UMA_SETTLED and _pinned(price):
        assert price is not None
        won = price >= PIN_HIGH
        return Settlement(1.0 if won else 0.0,
                          f"resultado propuesto al oráculo (uma: {uma_status})")

    return Settlement(None, "abierto")

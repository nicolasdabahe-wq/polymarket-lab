"""Decisión de liquidación de una posición: ¿el mercado ya se resolvió?

Tres fuentes, de más a menos definitiva:
 1. Gamma marca el mercado como closed  -> payout oficial.
 2. La posición on-chain figura como redeemable (el payout ya se puede
    reclamar en la app) -> ganó o perdió según el precio final.
 3. "De facto": el precio quedó clavado en ~0 o ~1 aunque Gamma todavía no
    cierre el mercado (la resolución UMA tarda; en deportes en vivo el
    resultado se sabe mucho antes). Para no liquidar por un pico pasajero
    se exige que siga clavado durante confirm_minutes.

La función es pura: el scheduler le pasa lo observado y ella decide.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

# Umbrales de "precio clavado": mismos que usa el backtest.
PIN_LOW = 0.005
PIN_HIGH = 0.995


class Settlement:
    """Resultado de la decisión.

    payout: precio de liquidación (0 o 1) o None si todavía no se liquida.
    pinned_since: nuevo valor a persistir para la ventana de confirmación
                  (None borra la marca).
    reason: texto corto para el registro y el aviso de Telegram.
    """

    __slots__ = ("payout", "pinned_since", "reason")

    def __init__(self, payout: float | None, pinned_since: datetime | None,
                 reason: str) -> None:
        self.payout = payout
        self.pinned_since = pinned_since
        self.reason = reason

    def __eq__(self, other: object) -> bool:  # pragma: no cover - solo tests
        return (isinstance(other, Settlement)
                and (self.payout, self.pinned_since, self.reason)
                == (other.payout, other.pinned_since, other.reason))

    def __repr__(self) -> str:  # pragma: no cover - solo depuración
        return (f"Settlement(payout={self.payout}, "
                f"pinned_since={self.pinned_since}, reason={self.reason!r})")


def decide_settlement(*, gamma_closed: bool,
                      gamma_prices: Sequence[float] | None,
                      outcome_index: int,
                      onchain_price: float | None,
                      onchain_redeemable: bool,
                      pinned_since: datetime | None,
                      now: datetime,
                      confirm_minutes: float) -> Settlement:
    """Decide si la posición se liquida ya, y a qué precio. Pura."""
    prices = list(gamma_prices or [])
    if gamma_closed and outcome_index < len(prices):
        return Settlement(float(prices[outcome_index]), None,
                          "resuelto en Gamma")

    if onchain_redeemable and onchain_price is not None:
        return Settlement(1.0 if onchain_price >= 0.5 else 0.0, None,
                          "payout redimible on-chain")

    # Precio de referencia: el on-chain manda (viene de la posición real);
    # si no hay, el de Gamma del mercado abierto.
    price = onchain_price
    if price is None and outcome_index < len(prices):
        price = float(prices[outcome_index])
    if price is None:
        return Settlement(None, None, "sin precio")

    if not (price <= PIN_LOW or price >= PIN_HIGH):
        return Settlement(None, None, "abierto")  # borra la marca previa

    if pinned_since is None:
        return Settlement(None, now, "precio clavado: esperando confirmación")
    if now - pinned_since < timedelta(minutes=confirm_minutes):
        return Settlement(None, pinned_since,
                          "precio clavado: esperando confirmación")
    minutes = (now - pinned_since).total_seconds() / 60
    return Settlement(1.0 if price >= PIN_HIGH else 0.0, None,
                      f"de facto resuelto: precio en {price:.3f} "
                      f"desde hace {minutes:.0f} min")

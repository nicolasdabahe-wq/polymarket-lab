"""Cuánto apostar según la ventaja, no un monto fijo para todo.

El problema que resuelve: hasta ahora cada copia entraba con ~$12 sin
importar si la oportunidad era buenísima o apenas decente. Eso desperdicia
las buenas y sobrepaga las malas.

Método: criterio de Kelly fraccionado.

1. Se estima el retorno esperado por dólar (`retorno_esperado`). La
   evidencia es el ROI que dio copiar a esa wallet en el backtest: si
   copiarla rindió +10% por operación, comprar a 0.60 implica que su
   probabilidad real es 0.60 x 1.10 = 0.66, o sea 6 puntos de ventaja.
2. Con esa probabilidad, Kelly dice qué fracción del capital apostar:
   f = (q - p) / (1 - p), donde p es el precio y q la probabilidad real.
3. Se usa solo una fracción de Kelly (25% por defecto). Kelly completo es
   óptimo únicamente si la probabilidad estimada es exacta; la nuestra es
   ruidosa, y apostar Kelly completo con estimaciones malas arruina.

Todo puro y testeable.
"""
from __future__ import annotations

# Techo al ROI que se toma en serio: un backtest con pocas copias puede dar
# +80% por casualidad y no hay que apostar la casa por eso.
MAX_ROI = 0.25


def retorno_esperado(roi_backtest: float, n_wallets: int,
                     slippage_usado: float, consensus_boost: float = 0.4,
                     ) -> float:
    """Retorno esperado por dólar apostado en esta copia.

    roi_backtest: ROI por operación que dio copiar a la mejor wallet.
    n_wallets: cuántas coinciden en la misma entrada (más = más señal).
    slippage_usado: fracción del slippage tolerado que el precio ya se
        comió, de 0 a 1. Si el precio ya voló, la ventaja se evaporó.
    """
    r = min(max(roi_backtest, 0.0), MAX_ROI)
    if n_wallets > 1:
        r *= min(1 + consensus_boost * (n_wallets - 1), 2.0)
    r *= max(0.0, 1.0 - max(slippage_usado, 0.0))
    return r


def kelly_usdc(equity: float, price: float, exp_return: float,
               fraction: float, min_usdc: float, max_pct: float) -> float:
    """USDC a apostar. 0 si no hay ventaja o los datos no sirven.

    Nunca devuelve menos que min_usdc si hay ventaja positiva: por debajo
    del piso no compensa el costo operativo, así que o se apuesta el piso
    o no se apuesta.
    """
    if exp_return <= 0 or not 0 < price < 1 or equity <= 0:
        return 0.0
    q = min(price * (1 + exp_return), 0.99)
    kelly = (q - price) / (1 - price)
    if kelly <= 0:
        return 0.0
    usdc = equity * fraction * kelly
    return min(max(usdc, min_usdc), equity * max_pct)


def dias_hasta(fecha_iso: str | None) -> float | None:
    """Días que faltan para una fecha ISO. None si no se sabe.

    Lo usan las estrategias para avisarle a risk/ cuánto tiempo quedaría
    inmovilizado el dinero de una apuesta.
    """
    if not fecha_iso:
        return None
    from datetime import datetime, timezone
    try:
        fin = datetime.fromisoformat(str(fecha_iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if fin.tzinfo is None:
        fin = fin.replace(tzinfo=timezone.utc)
    return (fin - datetime.now(timezone.utc)).total_seconds() / 86400

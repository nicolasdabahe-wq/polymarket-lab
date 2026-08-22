"""Perfil de operador: ¿esta wallet apuesta con convicción o hace mercado?

El ROI del backtest dice si copiarla habría dado dinero, pero no distingue
entre alguien que estudia un partido y entra fuerte, y un bot que rocía
cientos de micro-órdenes por minuto para capturar el spread. Copiar al
segundo es imposible: cuando reaccionamos, ya movió su cotización.

Medido contra wallets reales el 2026-08-22, las dos poblaciones no se
tocan:

    perfil                trades/día    mediana    % bajo $50
    creadores de mercado   128 - 1755   $13 - $22    60% - 69%
    convicción              15 -   30   $812-$1014    7% -  9%

Por eso el corte es frecuencia + tamaño típico. "Comprar los dos lados"
NO sirve como criterio solo: una wallet de convicción con mediana de
$1,014 también lo hace en el 36% de sus mercados (cubre posiciones).
Se calcula igual, para poder mirarlo, pero no decide.

Todo puro: recibe los trades que el backtest ya bajó y no toca la red.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Sequence

SEGUNDOS_POR_DIA = 86_400

# Umbrales del corte, elegidos entre las dos poblaciones medidas.
MAX_TRADES_POR_DIA = 50.0
MIN_MEDIANA_USDC = 100.0
MICRO_USDC = 50.0


@dataclass
class Perfil:
    n_trades: int
    dias: float
    trades_por_dia: float
    mediana_usdc: float
    pct_micro: float          # fracción de trades por debajo de $50
    mercados: int
    pct_ambos_lados: float    # mercados donde compró YES y NO

    @property
    def es_creador_de_mercado(self) -> bool:
        """Alta frecuencia Y tamaño típico chico: hace mercado, no apuesta."""
        return (self.trades_por_dia >= MAX_TRADES_POR_DIA
                and self.mediana_usdc < MIN_MEDIANA_USDC)

    @property
    def etiqueta(self) -> str:
        return "creador_de_mercado" if self.es_creador_de_mercado else "convicción"

    def resumen(self) -> str:
        return (f"{self.trades_por_dia:.0f} trades/día, mediana "
                f"${self.mediana_usdc:,.0f}, {self.pct_micro:.0%} bajo $50")


def perfil_operador(trades: Sequence[dict[str, Any]]) -> Perfil | None:
    """Perfil a partir de los trades crudos del backtest. None si no alcanzan."""
    if len(trades) < 5:
        return None
    stamps = [t["ts"] for t in trades]
    # Piso de medio día: una wallet con toda su actividad en una hora no debe
    # dar una frecuencia diaria astronómica por el divisor.
    dias = max((max(stamps) - min(stamps)) / SEGUNDOS_POR_DIA, 0.5)
    montos = [float(t.get("usdc") or 0.0) for t in trades]
    lados: dict[str, set[int]] = {}
    for t in trades:
        if t.get("side") == "BUY":
            lados.setdefault(t["condition_id"], set()).add(t.get("outcome_index"))
    ambos = sum(1 for s in lados.values() if len(s) > 1)
    return Perfil(
        n_trades=len(trades), dias=dias,
        trades_por_dia=len(trades) / dias,
        mediana_usdc=statistics.median(montos),
        pct_micro=sum(1 for m in montos if m < MICRO_USDC) / len(montos),
        mercados=len(lados),
        pct_ambos_lados=ambos / len(lados) if lados else 0.0)

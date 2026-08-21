"""backtest/: evaluación de estrategias contra histórico.

Fase actual: copy trading ("qué habría pasado copiando a la wallet X los
últimos N días"). Backtest de noticias: cuando intel/ acumule histórico
propio con el LLM activo.
"""
from .copy_backtest import BacktestReport, CopyBacktester, simulate_copy

__all__ = ["BacktestReport", "CopyBacktester", "simulate_copy"]

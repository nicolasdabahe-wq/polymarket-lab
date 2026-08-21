"""execution/: ejecución de órdenes.

Fase 2: PaperBroker — simula fills contra el order book real del CLOB.
Fase 3: broker real con py-clob-client detrás de la misma interfaz.

Toda orden pasa por RiskManager dentro de execute(); no hay otro camino.
"""
from .paper import Fill, PaperBroker, simulate_book_fill

__all__ = ["Fill", "PaperBroker", "simulate_book_fill"]

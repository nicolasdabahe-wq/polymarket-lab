"""execution/: ejecución de órdenes.

- PaperBroker: simula fills contra el order book real del CLOB.
- LiveBroker: órdenes reales vía py-clob-client (FAK). Solo se activa con
  LIVE_TRADING=I_UNDERSTAND_THE_RISKS + claves en .env.

Toda orden pasa por RiskManager dentro de execute(); no hay otro camino.
"""
from .live import LiveBroker
from .paper import Fill, PaperBroker, simulate_book_fill

__all__ = ["Fill", "LiveBroker", "PaperBroker", "simulate_book_fill"]

"""data/: acceso read-only a las APIs de Polymarket + cache SQLite.

- gamma.py    mercados y eventos (Gamma API)
- clob.py     order books y precios (CLOB, sin autenticación para lectura)
- data_api.py posiciones, actividad y leaderboard por wallet (Data API)
- store.py    persistencia de mercados en SQLite

Fase 1 usa polling; el websocket del CLOB se integra en la fase de ejecución.
"""
from .gamma import GammaClient, Market
from .clob import ClobClient
from .data_api import DataApiClient
from .store import MarketStore

__all__ = ["GammaClient", "Market", "ClobClient", "DataApiClient", "MarketStore"]

"""pmbot: bot de trading para Polymarket.

Módulos:
- data/        clientes read-only de Gamma / CLOB / Data API + cache SQLite
- intel/       monitoreo de noticias, análisis con LLM y briefing diario
- smart_money/ ranking de wallets y seguimiento de sus posiciones
- scheduler/   rutina diaria y loop 24/7
- monitor/     logging estructurado y notificaciones
- strategies/, risk/, execution/, backtest/  llegan en fases posteriores
"""

__version__ = "0.1.0"

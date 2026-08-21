"""intel/: monitoreo de noticias y contexto.

- sources.py   fetch de feeds RSS configurables, dedupe en SQLite
- analyzer.py  mapeo noticia -> mercados afectados (Claude API o keywords)
- briefing.py  briefing diario por categoría

Los eventos programados (debates, reportes económicos, partidos) se derivan
en fase 1 de los endDate/gameStartTime de los propios mercados; calendario
externo llega en fase 2.
"""
from .sources import NewsFetcher
from .analyzer import NewsAnalyzer
from .briefing import BriefingBuilder

__all__ = ["NewsFetcher", "NewsAnalyzer", "BriefingBuilder"]

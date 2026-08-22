"""strategies/: generación de señales de trading.

Cada estrategia produce OrderRequests que SIEMPRE pasan por risk/ dentro del
broker. Presupuesto por estrategia via strategy_budget_pct.

Fase 2: arbitrage (YES+NO < 1) y copy_trading (smart_money).
Fase 3: news_trading (intel validado por research/) y value vs. externos.
"""
from .arbitrage import ArbitrageStrategy
from .copy_trading import CopyTradingStrategy
from .crypto_value import CryptoValueStrategy
from .ladder_arb import LadderArbStrategy
from .sports_value import SportsValueStrategy

__all__ = ["ArbitrageStrategy", "CopyTradingStrategy", "CryptoValueStrategy", "LadderArbStrategy", "SportsValueStrategy"]

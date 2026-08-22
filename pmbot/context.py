"""Cableado central: construye clientes y módulos a partir del Config."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Config
from .data import (ClobClient, DataApiClient, GammaClient, MarketStore,
                   PriceFeed)
from .db import connect
from .execution import LiveBroker, PaperBroker
from .http import HttpClient
from .intel import BriefingBuilder, NewsAnalyzer, NewsFetcher
from .monitor import Notifier
from .risk import RiskManager
from .backtest import CopyBacktester
from .data.sports import MlbClient
from .smart_money.tape import TradeTape
from .smart_money import WalletScorer, WalletTracker, WalletValidator
from .strategies import (ArbitrageStrategy, CopyTradingStrategy,
                         CryptoValueStrategy, SportsValueStrategy)


@dataclass
class App:
    cfg: Config
    http: HttpClient
    conn: sqlite3.Connection
    gamma: GammaClient
    clob: ClobClient
    data_api: DataApiClient
    market_store: MarketStore
    wallet_scorer: WalletScorer
    wallet_tracker: WalletTracker
    tape: TradeTape
    wallet_validator: WalletValidator
    news_fetcher: NewsFetcher
    news_analyzer: NewsAnalyzer
    briefing: BriefingBuilder
    notifier: Notifier
    risk: RiskManager
    broker: PaperBroker
    arbitrage: ArbitrageStrategy
    copy_trading: CopyTradingStrategy
    crypto_value: CryptoValueStrategy
    sports_value: Any

    async def aclose(self) -> None:
        await self.http.aclose()
        self.conn.close()


def build_app(cfg: Config) -> App:
    http = HttpClient()
    conn = connect(cfg.db_path)
    data_api = DataApiClient(http)
    clob = ClobClient(http)
    risk = RiskManager(conn, cfg.section("risk"), cfg.var_dir)
    if cfg.live_trading:
        if not (cfg.polymarket_private_key and cfg.polymarket_proxy_address):
            raise RuntimeError(
                "LIVE_TRADING está activado pero faltan POLYMARKET_PRIVATE_KEY "
                "y/o POLYMARKET_PROXY_ADDRESS en .env")
        broker: PaperBroker = LiveBroker(
            conn, clob, risk, cfg.section("capital"), cfg.section("execution"),
            private_key=cfg.polymarket_private_key,
            proxy_address=cfg.polymarket_proxy_address,
            signature_type=cfg.polymarket_signature_type)
    else:
        broker = PaperBroker(conn, clob, risk, cfg.section("capital"),
                             cfg.section("execution"))
    strategies_cfg = cfg.section("strategies")
    return App(
        cfg=cfg,
        http=http,
        conn=conn,
        gamma=GammaClient(http),
        clob=clob,
        data_api=data_api,
        market_store=MarketStore(conn),
        wallet_scorer=WalletScorer(data_api, conn, cfg.section("smart_money")),
        wallet_tracker=WalletTracker(data_api, conn, cfg.section("smart_money")),
        tape=TradeTape(
            conn, http,
            min_usdc=float((cfg.section("strategies").get("copy_trading") or {})
                           .get("min_copy_usdc_of_wallet", 150)),
            candidate_min_usdc=float(
                ((cfg.section("smart_money").get("validation") or {})
                 .get("discovery") or {}).get("candidate_min_usdc", 500))),
        wallet_validator=WalletValidator(
            conn, CopyBacktester(data_api, GammaClient(http)),
            cfg.section("smart_money"), api=data_api),
        news_fetcher=NewsFetcher(http, conn, cfg.section("intel")),
        news_analyzer=NewsAnalyzer(conn, cfg.section("intel"),
                                   cfg.anthropic_api_key),
        briefing=BriefingBuilder(conn),
        notifier=Notifier(cfg.telegram_bot_token, cfg.telegram_chat_id,
                          bool(cfg.section("telegram").get("enabled"))),
        risk=risk,
        broker=broker,
        arbitrage=ArbitrageStrategy(conn, clob, broker,
                                    strategies_cfg.get("arbitrage") or {}),
        copy_trading=CopyTradingStrategy(
            conn, broker, strategies_cfg.get("copy_trading") or {},
            gamma=GammaClient(http), market_store=MarketStore(conn)),
        crypto_value=CryptoValueStrategy(conn, PriceFeed(http), broker,
                                         strategies_cfg.get("crypto_value") or {}),
        sports_value=SportsValueStrategy(
            conn, MlbClient(http), gamma, broker,
            strategies_cfg.get("sports_value") or {}, MarketStore(conn)),
    )

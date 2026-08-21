"""Cableado central: construye clientes y módulos a partir del Config."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Config
from .data import ClobClient, DataApiClient, GammaClient, MarketStore
from .db import connect
from .execution import PaperBroker
from .http import HttpClient
from .intel import BriefingBuilder, NewsAnalyzer, NewsFetcher
from .monitor import Notifier
from .risk import RiskManager
from .smart_money import WalletScorer, WalletTracker
from .strategies import ArbitrageStrategy, CopyTradingStrategy


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
    news_fetcher: NewsFetcher
    news_analyzer: NewsAnalyzer
    briefing: BriefingBuilder
    notifier: Notifier
    risk: RiskManager
    broker: PaperBroker
    arbitrage: ArbitrageStrategy
    copy_trading: CopyTradingStrategy

    async def aclose(self) -> None:
        await self.http.aclose()
        self.conn.close()


def build_app(cfg: Config) -> App:
    http = HttpClient()
    conn = connect(cfg.db_path)
    data_api = DataApiClient(http)
    clob = ClobClient(http)
    risk = RiskManager(conn, cfg.section("risk"), cfg.var_dir)
    # Fase 2: solo broker paper. El broker real (fase 3) exigirá además
    # cfg.live_trading y las claves de Polymarket.
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
        copy_trading=CopyTradingStrategy(conn, broker,
                                         strategies_cfg.get("copy_trading") or {}),
    )

"""Cableado central: construye clientes y módulos a partir del Config."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .config import Config
from .data import ClobClient, DataApiClient, GammaClient, MarketStore
from .db import connect
from .http import HttpClient
from .intel import BriefingBuilder, NewsAnalyzer, NewsFetcher
from .monitor import Notifier
from .smart_money import WalletScorer, WalletTracker


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

    async def aclose(self) -> None:
        await self.http.aclose()
        self.conn.close()


def build_app(cfg: Config) -> App:
    http = HttpClient()
    conn = connect(cfg.db_path)
    data_api = DataApiClient(http)
    return App(
        cfg=cfg,
        http=http,
        conn=conn,
        gamma=GammaClient(http),
        clob=ClobClient(http),
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
    )

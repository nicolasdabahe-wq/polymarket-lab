"""SQLite compartido: esquema y helpers.

Una sola base (var/pmbot.db) con WAL para que el scheduler y los comandos CLI
puedan convivir. Sin ORM: el esquema es chico y estable.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    condition_id   TEXT PRIMARY KEY,
    gamma_id       TEXT,
    slug           TEXT,
    question       TEXT NOT NULL,
    category       TEXT NOT NULL DEFAULT 'other',
    end_date       TEXT,
    liquidity      REAL,
    volume_24h     REAL,
    yes_price      REAL,
    best_bid       REAL,
    best_ask       REAL,
    clob_token_ids TEXT,          -- JSON [yes_token, no_token]
    active         INTEGER NOT NULL DEFAULT 1,
    raw            TEXT,          -- JSON completo de Gamma
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_category ON markets(category);
CREATE INDEX IF NOT EXISTS idx_markets_volume ON markets(volume_24h);

CREATE TABLE IF NOT EXISTS wallet_ranking (
    wallet        TEXT PRIMARY KEY,
    username      TEXT,
    score         REAL NOT NULL,
    pnl_week      REAL,
    pnl_month     REAL,
    pnl_all       REAL,
    vol_all       REAL,
    roi           REAL,
    trades        INTEGER,
    distinct_markets INTEGER,
    account_age_days REAL,
    passed_filters INTEGER NOT NULL,
    reject_reason TEXT,
    details       TEXT,           -- JSON con el desglose del score
    ranked_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wallet_positions (
    wallet       TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    title        TEXT,
    outcome      TEXT,
    size         REAL,
    avg_price    REAL,
    cur_price    REAL,
    value_usdc   REAL,
    cash_pnl     REAL,
    percent_pnl  REAL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (wallet, condition_id, outcome)
);

-- Última actividad vista por wallet monitoreada (para detectar entradas nuevas).
CREATE TABLE IF NOT EXISTS wallet_watermarks (
    wallet            TEXT PRIMARY KEY,
    last_activity_ts  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS news_items (
    id           TEXT PRIMARY KEY,  -- hash(link)
    feed         TEXT NOT NULL,
    category     TEXT NOT NULL,
    title        TEXT NOT NULL,
    link         TEXT,
    published_at TEXT,
    summary      TEXT,
    fetched_at   TEXT NOT NULL,
    analyzed     INTEGER NOT NULL DEFAULT 0,
    analysis     TEXT               -- JSON del análisis (LLM o keywords)
);
CREATE INDEX IF NOT EXISTS idx_news_analyzed ON news_items(analyzed, fetched_at);

CREATE TABLE IF NOT EXISTS briefings (
    briefing_date TEXT NOT NULL,   -- YYYY-MM-DD (UTC)
    category      TEXT NOT NULL,
    content       TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (briefing_date, category)
);

-- Portfolio paper: fase 1 solo lleva el saldo; las órdenes llegan en fase 2.
CREATE TABLE IF NOT EXISTS paper_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Señales informativas detectadas (fase 1: solo se registran, no se opera).
CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,      -- 'smart_money' | 'intel'
    kind       TEXT NOT NULL,
    condition_id TEXT,
    payload    TEXT NOT NULL,      -- JSON
    created_at TEXT NOT NULL
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def from_json(text: str | None) -> Any:
    return json.loads(text) if text else None

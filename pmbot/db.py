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

-- Señales detectadas. processed=1 cuando una estrategia ya las consumió.
CREATE TABLE IF NOT EXISTS signals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT NOT NULL,      -- 'smart_money' | 'intel'
    kind       TEXT NOT NULL,
    condition_id TEXT,
    payload    TEXT NOT NULL,      -- JSON
    created_at TEXT NOT NULL,
    processed  INTEGER NOT NULL DEFAULT 0
);

-- Cuenta paper: una sola fila (id=1).
CREATE TABLE IF NOT EXISTS paper_account (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    starting_usdc REAL NOT NULL,
    cash_usdc     REAL NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Órdenes (paper en fase 2; el broker real reutilizará el mismo esquema).
-- id es la clave de idempotencia: reintentos con el mismo id no duplican.
CREATE TABLE IF NOT EXISTS orders (
    id            TEXT PRIMARY KEY,
    strategy      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    token_id      TEXT,
    outcome       TEXT,
    outcome_index INTEGER,
    side          TEXT NOT NULL,       -- BUY | SELL | REDEEM
    req_size      REAL NOT NULL,       -- shares pedidas
    limit_price   REAL,
    status        TEXT NOT NULL,       -- FILLED | REJECTED | NO_LIQUIDITY
    fill_size     REAL,
    fill_price    REAL,                -- precio promedio del fill
    fill_usdc     REAL,
    fee_usdc      REAL,
    realized_pnl  REAL,                -- solo SELL/REDEEM
    reason        TEXT,                -- por qué se operó (para el reporte)
    reject_reason TEXT,
    sent          INTEGER NOT NULL DEFAULT 0,  -- llegó al exchange (sí/no)
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_orders_day ON orders(created_at);

-- Posiciones paper propias.
CREATE TABLE IF NOT EXISTS paper_positions (
    strategy      TEXT NOT NULL,
    condition_id  TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    outcome_index INTEGER,
    token_id      TEXT,
    question      TEXT,
    category      TEXT,
    size          REAL NOT NULL,
    avg_price     REAL NOT NULL,
    meta          TEXT,               -- JSON (p.ej. wallet copiada y su precio)
    opened_at     TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (strategy, condition_id, outcome)
);

-- Validación automática por backtest: qué pasa si copiamos a esta wallet.
CREATE TABLE IF NOT EXISTS wallet_backtest (
    wallet        TEXT PRIMARY KEY,
    roi           REAL,
    win_rate      REAL,
    n_copies      INTEGER,
    days_covered  REAL,
    verdict       TEXT NOT NULL,   -- 'copiable' | 'rechazada' | 'sin_datos'
    min_usdc      REAL,            -- umbral de tamaño óptimo para copiarla
    tested_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS equity_history (
    ts              TEXT PRIMARY KEY,
    cash_usdc       REAL NOT NULL,
    positions_usdc  REAL NOT NULL,
    equity_usdc     REAL NOT NULL
);
"""

# Migraciones idempotentes para bases creadas por versiones anteriores.
MIGRATIONS = [
    "ALTER TABLE signals ADD COLUMN processed INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE wallet_backtest ADD COLUMN min_usdc REAL",
    "ALTER TABLE orders ADD COLUMN sent INTEGER NOT NULL DEFAULT 0",
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    for migration in MIGRATIONS:
        try:
            conn.execute(migration)
        except sqlite3.OperationalError:
            pass  # columna ya existe
    return conn


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def from_json(text: str | None) -> Any:
    return json.loads(text) if text else None

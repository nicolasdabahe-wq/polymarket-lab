"""Cliente de la Data API (https://data-api.polymarket.com).

Posiciones y actividad son públicas por wallet (todo es on-chain).
Leaderboard: /v1/leaderboard?category=OVERALL&period=day|week|month|all.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..http import HttpClient

DATA_BASE = "https://data-api.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

LeaderboardPeriod = str  # "day" | "week" | "month" | "all"


@dataclass
class LeaderboardRow:
    rank: int
    wallet: str
    username: str
    volume: float
    pnl: float


@dataclass
class Position:
    wallet: str
    condition_id: str
    title: str
    outcome: str
    outcome_index: int
    size: float
    avg_price: float
    cur_price: float
    current_value: float
    cash_pnl: float
    percent_pnl: float
    redeemable: bool


@dataclass
class Activity:
    wallet: str
    timestamp: int
    type: str          # TRADE | REDEEM | SPLIT | MERGE | REWARD | CONVERSION
    side: str          # BUY | SELL (solo TRADE)
    condition_id: str
    title: str
    outcome: str
    outcome_index: int
    price: float
    usdc_size: float


class DataApiClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    async def leaderboard(self, period: LeaderboardPeriod = "month",
                          limit: int = 50) -> list[LeaderboardRow]:
        # OJO: el parámetro se llama timePeriod; "period" es aceptado pero
        # ignorado silenciosamente por la API (verificado 2026-08).
        rows = await self.http.get_json(
            f"{DATA_BASE}/v1/leaderboard",
            params={"category": "OVERALL", "timePeriod": period, "limit": limit},
        )
        out: list[LeaderboardRow] = []
        for r in rows or []:
            try:
                out.append(LeaderboardRow(
                    rank=int(r.get("rank", 0)),
                    wallet=(r.get("proxyWallet") or "").lower(),
                    username=r.get("userName") or "",
                    volume=float(r.get("vol") or 0),
                    pnl=float(r.get("pnl") or 0),
                ))
            except (TypeError, ValueError):
                continue
        return out

    async def profile_created_at(self, wallet: str) -> str | None:
        """Fecha de creación de la cuenta (Gamma public-profile), ISO o None."""
        try:
            data = await self.http.get_json(
                f"{GAMMA_BASE}/public-profile", params={"address": wallet})
            return data.get("createdAt")
        except Exception:
            return None

    async def positions(self, wallet: str, limit: int = 50) -> list[Position]:
        rows = await self.http.get_json(
            f"{DATA_BASE}/positions",
            params={"user": wallet, "limit": limit,
                    "sortBy": "CURRENT", "sortDirection": "DESC"},
        )
        out: list[Position] = []
        for r in rows or []:
            try:
                out.append(Position(
                    wallet=wallet.lower(),
                    condition_id=r.get("conditionId", ""),
                    title=r.get("title", ""),
                    outcome=r.get("outcome", ""),
                    outcome_index=int(r.get("outcomeIndex") or 0),
                    size=float(r.get("size") or 0),
                    avg_price=float(r.get("avgPrice") or 0),
                    cur_price=float(r.get("curPrice") or 0),
                    current_value=float(r.get("currentValue") or 0),
                    cash_pnl=float(r.get("cashPnl") or 0),
                    percent_pnl=float(r.get("percentPnl") or 0),
                    redeemable=bool(r.get("redeemable")),
                ))
            except (TypeError, ValueError):
                continue
        return out

    async def activity(self, wallet: str, limit: int = 500,
                       offset: int = 0) -> list[Activity]:
        rows = await self.http.get_json(
            f"{DATA_BASE}/activity",
            params={"user": wallet, "limit": limit, "offset": offset},
        )
        out: list[Activity] = []
        for r in rows or []:
            try:
                out.append(Activity(
                    wallet=wallet.lower(),
                    timestamp=int(r.get("timestamp") or 0),
                    type=r.get("type", ""),
                    side=r.get("side", "") or "",
                    condition_id=r.get("conditionId", ""),
                    title=r.get("title", ""),
                    outcome=r.get("outcome", "") or "",
                    outcome_index=int(r.get("outcomeIndex") or 0),
                    price=float(r.get("price") or 0),
                    usdc_size=float(r.get("usdcSize") or 0),
                ))
            except (TypeError, ValueError):
                continue
        return out

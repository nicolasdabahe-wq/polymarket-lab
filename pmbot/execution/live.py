"""Broker REAL contra el CLOB V2 de Polymarket (py-clob-client-v2).

Desde abril 2026 Polymarket opera CLOB V2: colateral pUSD (Polymarket USD),
contratos de exchange nuevos y SDK nuevo. El SDK v1 quedó incompatible.

Se activa SOLO si:
- LIVE_TRADING=I_UNDERSTAND_THE_RISKS (valor exacto), y
- POLYMARKET_PRIVATE_KEY y POLYMARKET_PROXY_ADDRESS están en .env.

Diseño:
- Hereda de PaperBroker: misma contabilidad local de posiciones (atribución
  por estrategia), mismos límites de risk/ y misma idempotencia. Solo cambia
  la ejecución (órdenes reales) y el cash (saldo USDC real del CLOB).
- Órdenes FAK (fill-and-kill): se llenan con lo que hay dentro del precio
  límite y el resto se cancela; nunca dejamos órdenes descansando.
- py-clob-client es sincrónico: las llamadas van a un thread para no
  bloquear el loop.

Al resolverse un mercado, el redeem en cadena se reclama desde la app de
Polymarket (Claim); acá solo se registra el PnL y se limpia la posición.
"""
from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from typing import Any

from ..data.clob import ClobClient as ReadOnlyClob
from ..risk import OrderRequest, RiskManager
from .paper import Fill, MIN_SHARES, PaperBroker

log = logging.getLogger("pmbot.execution.live")

CLOB_HOST = "https://clob.polymarket.com"
POLYGON_CHAIN_ID = 137
BALANCE_CACHE_SECONDS = 30.0


def round_to_tick(price: float, tick: float, side: str) -> float:
    """Redondea el precio límite al tick sin volverlo más agresivo:
    BUY hacia abajo, SELL hacia arriba. Pura."""
    if tick <= 0:
        return price
    steps = price / tick
    rounded = math.floor(steps) if side == "BUY" else math.ceil(steps)
    return round(rounded * tick, 6)


def parse_post_response(resp: dict[str, Any], side: str, req_size: float,
                        limit_price: float) -> tuple[float, float, str]:
    """(shares llenadas, precio promedio, error) desde la respuesta del CLOB.

    En una compra el maker asset es USDC (makingAmount=USDC gastado,
    takingAmount=shares); en una venta es al revés. Pura y defensiva.
    """
    if not resp or not resp.get("success"):
        return 0.0, 0.0, (resp or {}).get("errorMsg") or "respuesta inválida"
    try:
        making = float(resp.get("makingAmount") or 0)
        taking = float(resp.get("takingAmount") or 0)
    except (TypeError, ValueError):
        making = taking = 0.0
    if side == "BUY":
        shares, usdc = taking, making
    else:
        shares, usdc = making, taking
    if shares <= 0:
        # FAK sin fill (status live/unmatched): nada ejecutado.
        return 0.0, 0.0, f"sin fill (status {resp.get('status')})"
    return shares, usdc / shares if shares else limit_price, ""


class LiveBroker(PaperBroker):
    def __init__(self, conn: sqlite3.Connection, clob: ReadOnlyClob,
                 risk: RiskManager, capital_cfg: dict[str, Any],
                 exec_cfg: dict[str, Any] | None,
                 private_key: str, proxy_address: str,
                 signature_type: int = 1) -> None:
        super().__init__(conn, clob, risk, capital_cfg, exec_cfg)
        self._private_key = private_key
        self.proxy_address = proxy_address
        self._signature_type = signature_type
        self._client: Any = None
        self._balance_cache: tuple[float, float] | None = None  # (ts, usdc)

    # ---------- cliente autenticado ----------

    def client(self) -> Any:
        """Cliente V2 autenticado L2, creado la primera vez que se necesita."""
        if self._client is None:
            from py_clob_client_v2.client import ClobClient
            c = ClobClient(CLOB_HOST, chain_id=POLYGON_CHAIN_ID,
                           key=self._private_key,
                           signature_type=self._signature_type,
                           funder=self.proxy_address)
            c.set_api_creds(c.create_or_derive_api_key())
            self._client = c
            log.info("CLOB V2 autenticado como %s (funder %s)",
                     c.get_address(), self.proxy_address)
        return self._client

    # ---------- estado: cash real, posiciones locales ----------

    @property
    def cash(self) -> float:
        """Saldo pUSD real en el CLOB (cacheado unos segundos)."""
        now = time.monotonic()
        if self._balance_cache and now - self._balance_cache[0] < BALANCE_CACHE_SECONDS:
            return self._balance_cache[1]
        from py_clob_client_v2.clob_types import (AssetType,
                                                  BalanceAllowanceParams)
        raw = self.client().get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        usdc = float(raw.get("balance") or 0) / 1e6  # pUSD tiene 6 decimales
        self._balance_cache = (now, usdc)
        return usdc

    def _set_cash(self, value: float) -> None:
        # El cash real vive en la blockchain; solo invalidamos el cache.
        self._balance_cache = None

    # ---------- ejecución real ----------

    async def execute(self, order_id: str, request: OrderRequest) -> Fill:
        existing = self.conn.execute(
            "SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if existing:
            return Fill(order_id, "DUPLICATE",
                        detail=f"ya ejecutada ({existing['status']})")
        if request.side == "BUY" and request.size < MIN_SHARES:
            return self._record(order_id, request, Fill(
                order_id, "REJECTED", detail=f"mínimo {MIN_SHARES:.0f} shares"))

        decision = self.risk.check(request, self.portfolio_state())
        if not decision.approved:
            return self._record(order_id, request,
                                Fill(order_id, "REJECTED", detail=decision.reason))

        # negRisk se resuelve acá (el conn de SQLite no es thread-safe);
        # si no está en cache, el thread se lo pregunta al CLOB.
        neg_risk = self._neg_risk_from_cache(request)
        try:
            fill = await asyncio.to_thread(self._place_order, request, neg_risk)
        except Exception as exc:
            log.exception("orden real falló")
            fill = Fill(order_id, "REJECTED", detail=f"error del CLOB: {exc}")
            return self._record(order_id, request, fill)

        if fill.status == "FILLED":
            # Registrar en la contabilidad local (posiciones por estrategia).
            if request.side == "BUY":
                applied = self._apply_buy(request, fill.size, fill.price,
                                          fill.usdc, fill.fee)
            else:
                applied = self._apply_sell(request, fill.size, fill.price,
                                           fill.usdc, fill.fee)
            applied.order_id = order_id
            fill = applied
        fill.order_id = order_id
        result = self._record(order_id, request, fill)
        log.info("ORDEN REAL %s [%s] %s %.1f×%s → %s %s",
                 order_id[:24], request.strategy, request.side, fill.size,
                 request.outcome, fill.status, fill.detail)
        return result

    def _neg_risk_from_cache(self, request: OrderRequest) -> bool | None:
        """negRisk del mercado según el cache de Gamma (cambia el contrato
        que firma la orden). None si el mercado no está cacheado."""
        row = self.conn.execute(
            "SELECT raw FROM markets WHERE condition_id = ?",
            (request.condition_id,)).fetchone()
        if row and row["raw"]:
            import json as _json
            try:
                return bool(_json.loads(row["raw"]).get("negRisk"))
            except (ValueError, TypeError):
                pass
        return None

    def _place_order(self, request: OrderRequest,
                     neg_risk: bool | None = None) -> Fill:
        """Corre en thread: firma y postea la orden FAK al CLOB V2."""
        from py_clob_client_v2.clob_types import (OrderArgsV2, OrderType,
                                                  PartialCreateOrderOptions)
        from py_clob_client_v2.order_builder.constants import BUY, SELL

        client = self.client()
        tick_str = client.get_tick_size(request.token_id) or "0.01"
        tick = float(tick_str)
        price = round_to_tick(request.price, tick, request.side)
        if request.side == "SELL" and price <= 0:
            price = tick  # venta "a mercado": límite en el mínimo posible
        size = math.floor(request.size * 100) / 100
        if size < MIN_SHARES:
            return Fill("", "REJECTED", detail="tamaño < mínimo tras redondeo")

        if neg_risk is None:
            neg_risk = bool(client.get_neg_risk(request.token_id))
        order = client.create_order(
            OrderArgsV2(token_id=request.token_id, price=price, size=size,
                        side=BUY if request.side == "BUY" else SELL),
            PartialCreateOrderOptions(tick_size=tick_str, neg_risk=neg_risk))
        resp = client.post_order(order, OrderType.FAK)
        shares, avg_price, error = parse_post_response(
            resp, request.side, size, price)
        if shares <= 0:
            return Fill("", "NO_LIQUIDITY", detail=error)
        usdc = shares * avg_price
        return Fill("", "FILLED", shares, avg_price, usdc, fee=0.0)

    def redeem(self, position: sqlite3.Row, payout_price: float,
               reason: str) -> Fill:
        """En real, el claim del payout se hace en la app de Polymarket;
        acá se registra el PnL y se limpia la posición local."""
        fill = super().redeem(position, payout_price,
                              reason + " — reclamar en la app (Claim)")
        self._balance_cache = None
        return fill

    # ---------- diagnóstico ----------

    def check_connection(self) -> dict[str, Any]:
        """Para el comando live-check: auth, saldo y allowance."""
        from py_clob_client_v2.clob_types import (AssetType,
                                                  BalanceAllowanceParams)
        client = self.client()
        raw = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return {
            "signer_address": client.get_address(),
            "funder": self.proxy_address,
            "usdc_balance": float(raw.get("balance") or 0) / 1e6,
            "allowance_ok": any(float(v or 0) > 0 for v in
                                (raw.get("allowances") or {}).values())
                            or float(raw.get("allowance") or 0) > 0,
            "raw": raw,
        }

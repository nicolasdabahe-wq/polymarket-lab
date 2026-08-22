"""Arbitraje de escalera: contradicciones lógicas entre mercados hermanos.

Si "¿BTC llega a $150.000?" cuesta 0.035 y "¿BTC llega a $160.000?" cuesta
0.042 (mismo vencimiento), el mercado está roto: llegar a 160 EXIGE pasar
por 150, así que el de 150 nunca puede valer menos. Encontrado en vivo el
2026-08-22, también en XRP ($2,60 a 0.033 vs $2,80 a 0.041).

La jugada no necesita opinar sobre Bitcoin. Se compra el YES del evento
GRANDE (el que contiene al otro) y el NO del evento chico:

    costo = ask_yes_grande + ask_no_chico
    · pasa el chico  -> pasó el grande:  1 + 0 = $1
    · pasa solo el grande:               1 + 1 = $2
    · no pasa ninguno:                   0 + 1 = $1

Pagando menos de $1 no existe desenlace perdedor. Es la misma aritmética
del arbitraje YES+NO pero entre DOS mercados, y por eso nadie la barre:
hay que leer las preguntas para saber que son hermanos.

Cuál contiene a cuál:
  · touch/terminal ABOVE: el strike MENOR contiene al mayor
    (llegar a 160k implica haber tocado 150k)
  · touch/terminal BELOW: el strike MAYOR contiene al menor
    (caer a $60 implica haber pasado por $70)

Solo se emparejan mercados del mismo activo, mismo tipo de payoff y mismo
vencimiento EXACTO. Todo lo dudoso queda fuera: parse_crypto_question ya
descarta las carreras ("¿$1.000 o $3.000 primero?") y los formatos raros.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..risk import OrderRequest
from .crypto_value import parse_crypto_question

log = logging.getLogger("pmbot.strategies.ladder_arb")

KINDS_ABOVE = ("touch_above", "terminal_above")
KINDS_BELOW = ("touch_below", "terminal_below")


def superset_first(kind: str, strike_a: float, market_a: Any,
                   strike_b: float, market_b: Any) -> tuple[Any, Any] | None:
    """Ordena (grande, chico): el primero contiene al segundo. Pura.

    None si los strikes son iguales (mismo evento, no hay dominancia).
    """
    if strike_a == strike_b:
        return None
    if kind in KINDS_ABOVE:
        return (market_a, market_b) if strike_a < strike_b else (market_b, market_a)
    if kind in KINDS_BELOW:
        return (market_a, market_b) if strike_a > strike_b else (market_b, market_a)
    return None


def dominance_edge(ask_yes_superset: float | None,
                   ask_no_subset: float | None,
                   min_edge: float) -> float | None:
    """Ganancia garantizada por dólar del par, o None si no la hay. Pura."""
    if not ask_yes_superset or not ask_no_subset:
        return None
    if ask_yes_superset <= 0 or ask_no_subset <= 0:
        return None
    cost = ask_yes_superset + ask_no_subset
    edge = 1.0 - cost
    return edge if edge >= min_edge else None


@dataclass
class Escalera:
    product: str
    kind: str
    end_date: str
    # [(strike, fila_de_mercado)] ordenado por strike
    peldanos: list[tuple[float, sqlite3.Row]]


def agrupar_escaleras(rows: list[sqlite3.Row]) -> list[Escalera]:
    """Mercados hermanos: mismo activo, mismo payoff, mismo vencimiento."""
    grupos: dict[tuple[str, str, str], list[tuple[float, sqlite3.Row]]] = \
        defaultdict(list)
    for row in rows:
        parsed = parse_crypto_question(row["question"] or "")
        if not parsed or not row["end_date"]:
            continue
        grupos[(parsed.product, parsed.kind, row["end_date"])].append(
            (parsed.strike, row))
    out = []
    for (product, kind, end), peldanos in grupos.items():
        if len(peldanos) >= 2:
            peldanos.sort(key=lambda x: x[0])
            out.append(Escalera(product, kind, end, peldanos))
    return out


class LadderArbStrategy:
    name = "ladder_arb"

    def __init__(self, conn: sqlite3.Connection, clob: Any, gamma: Any,
                 broker: Any, cfg: dict[str, Any],
                 market_store: Any = None) -> None:
        self.conn = conn
        self.clob = clob
        self.gamma = gamma
        self.broker = broker
        self.market_store = market_store
        self.enabled = bool(cfg.get("enabled", True))
        self.budget_pct = float(cfg.get("budget_pct", 0.30))
        self.min_edge = float(cfg.get("min_edge", 0.015))
        self.max_usdc = float(cfg.get("max_usdc_per_pair", 60))
        self.min_usdc = float(cfg.get("min_usdc_per_pair", 12))

    async def scan_and_execute(self) -> list[str]:
        if not self.enabled:
            return []
        # Refrescar los mercados de cripto: muchos strikes lejanos no entran
        # al cache por volumen, y justo ahí viven las contradicciones.
        if self.market_store is not None and self.gamma is not None:
            try:
                frescos = await self.gamma.fetch_by_tag("crypto", limit=250)
                if frescos:
                    self.market_store.upsert_markets(frescos)
            except Exception as exc:
                log.debug("refresh de cripto falló: %s", exc)
        rows = self.conn.execute(
            """SELECT * FROM markets WHERE active = 1 AND category = 'crypto'
               AND yes_price IS NOT NULL""").fetchall()
        ejecutados: list[str] = []
        for escalera in agrupar_escaleras(rows):
            # Preselección barata con precios cacheados: solo pares vecinos
            # cuyo orden luce roto (o sospechosamente plano) van al book.
            for i in range(len(escalera.peldanos) - 1):
                (s1, m1), (s2, m2) = escalera.peldanos[i], escalera.peldanos[i + 1]
                par = superset_first(escalera.kind, s1, m1, s2, m2)
                if par is None:
                    continue
                grande, chico = par
                if (chico["yes_price"] or 0) < (grande["yes_price"] or 1) - 0.02:
                    continue  # el orden se respeta con margen: nada que ver
                try:
                    desc = await self._verificar_y_ejecutar(
                        escalera, grande, chico)
                except Exception as exc:
                    log.debug("par %s falló: %s", escalera.product, exc)
                    continue
                if desc:
                    ejecutados.append(desc)
        return ejecutados

    async def _verificar_y_ejecutar(self, escalera: Escalera,
                                    grande: sqlite3.Row,
                                    chico: sqlite3.Row) -> str | None:
        tok_g = json.loads(grande["clob_token_ids"] or "[]")
        tok_c = json.loads(chico["clob_token_ids"] or "[]")
        if len(tok_g) != 2 or len(tok_c) != 2:
            return None
        # Verificación EN VIVO: el cache puede tener minutos de atraso y una
        # contradicción vieja puede haberse cerrado ya.
        book_yes_g = await self.clob.order_book(tok_g[0])
        book_no_c = await self.clob.order_book(tok_c[1])
        edge = dominance_edge(book_yes_g.best_ask, book_no_c.best_ask,
                              self.min_edge)
        if edge is None:
            return None
        depth = min(book_yes_g.asks[0].size, book_no_c.asks[0].size)
        cost = book_yes_g.best_ask + book_no_c.best_ask
        size = min(depth, self.max_usdc / cost)
        if size * cost < self.min_usdc or size < 5:
            return None
        hoy = datetime.now(timezone.utc).date().isoformat()
        razon = (f"escalera {escalera.product}: «{grande['question'][:40]}» a "
                 f"{book_yes_g.best_ask:.3f} + NO de «{chico['question'][:40]}» "
                 f"a {book_no_c.best_ask:.3f} = {cost:.3f} < 1 "
                 f"(ganancia asegurada {edge:.1%})")
        patas = []
        for etiqueta, row, token, precio, outcome, idx in (
                ("g", grande, tok_g[0], book_yes_g.best_ask, "Yes", 0),
                ("c", chico, tok_c[1], book_no_c.best_ask, "No", 1)):
            fill = await self.broker.execute(
                f"ladder:{row['condition_id']}:{etiqueta}:{hoy}",
                OrderRequest(
                    strategy=self.name, condition_id=row["condition_id"],
                    category="crypto", token_id=token, outcome=outcome,
                    outcome_index=idx, side="BUY", size=size,
                    price=min(precio + edge / 2, 0.99),
                    reason=razon, strategy_budget_pct=self.budget_pct,
                    # El par vale $1 pase lo que pase: no inmoviliza capital
                    # en el sentido que limita la política de velocidad.
                    days_to_resolution=0.0,
                    meta={"question": row["question"], "pair": True}))
            patas.append((row, token, outcome, idx, fill))
        if all(f.status == "FILLED" for *_, f in patas):
            log.info("ESCALERA ejecutada: %s", razon)
            return razon
        # Una pata sin llenar deja posición direccional: deshacerla.
        for row, token, outcome, idx, fill in patas:
            if fill.status == "FILLED":
                await self.broker.execute(
                    f"ladder-unwind:{row['condition_id']}:{idx}:{hoy}",
                    OrderRequest(
                        strategy=self.name, condition_id=row["condition_id"],
                        category="crypto", token_id=token, outcome=outcome,
                        outcome_index=idx, side="SELL", size=fill.size,
                        price=0.0, days_to_resolution=0.0,
                        reason="unwind: la otra pata de la escalera no llenó"))
        return None

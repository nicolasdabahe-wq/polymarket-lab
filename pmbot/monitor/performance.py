"""Rendimiento por estrategia y por wallet copiada.

La pregunta que responde: ¿quién me hizo el dinero? Sin esto, el reporte
diario dice cuánto se ganó pero no quién lo ganó, y las decisiones de
escalar o apagar una estrategia se toman a ciegas.

Todo sale de la tabla orders (realizado) y de las posiciones abiertas
(no realizado). La atribución por wallet usa los ids de las órdenes de
copia ("copy:0xWALLET:...", "consensus:...:idx"): las ventas y redeems no
guardan la wallet, así que se hereda de la compra del mismo mercado.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Linea:
    nombre: str
    realizado: float = 0.0
    no_realizado: float = 0.0
    trades: int = 0          # cierres con PnL (ventas y redeems)
    ganados: int = 0
    abiertas: int = 0
    invertido_abierto: float = 0.0

    @property
    def total(self) -> float:
        return self.realizado + self.no_realizado

    @property
    def win_rate(self) -> float | None:
        return self.ganados / self.trades if self.trades else None


def _wallet_de_orden(order_id: str) -> str | None:
    """La wallet de una orden de copia, según el id ("copy:0xW:...")."""
    partes = (order_id or "").split(":")
    if len(partes) >= 2 and partes[0] in ("copy", "copy-exit"):
        return partes[1].lower()
    return None


def resumen_estrategias(conn: sqlite3.Connection,
                        positions: list[sqlite3.Row],
                        mark_price: Callable[[str, int, float], float],
                        desde: str | None = None) -> list[Linea]:
    """Una línea por estrategia, ordenadas por PnL total desc."""
    filtro, params = "", []
    if desde:
        filtro, params = " AND date(created_at) >= ?", [desde]
    lineas: dict[str, Linea] = {}

    def linea(nombre: str) -> Linea:
        return lineas.setdefault(nombre, Linea(nombre))

    for r in conn.execute(
            f"""SELECT strategy, realized_pnl FROM orders
                WHERE status = 'FILLED' AND realized_pnl IS NOT NULL{filtro}""",
            params):
        l = linea(r["strategy"])
        l.realizado += r["realized_pnl"]
        l.trades += 1
        if r["realized_pnl"] > 0:
            l.ganados += 1

    for p in positions:
        l = linea(p["strategy"])
        mark = mark_price(p["condition_id"], p["outcome_index"] or 0,
                          p["avg_price"])
        l.no_realizado += p["size"] * (mark - p["avg_price"])
        l.abiertas += 1
        l.invertido_abierto += p["size"] * p["avg_price"]

    return sorted(lineas.values(), key=lambda l: -l.total)


def resumen_wallets(conn: sqlite3.Connection,
                    positions: list[sqlite3.Row],
                    mark_price: Callable[[str, int, float], float],
                    desde: str | None = None) -> list[Linea]:
    """PnL por wallet copiada: el juicio en vivo que retroalimenta al
    backtest. La wallet de un cierre se hereda de la compra del mercado."""
    filtro, params = "", []
    if desde:
        filtro, params = " AND date(created_at) >= ?", [desde]
    # mercado -> wallet, según las compras de copia
    duenos: dict[str, str] = {}
    for r in conn.execute(
            """SELECT id, condition_id FROM orders
               WHERE side = 'BUY' AND (id LIKE 'copy:%')"""):
        w = _wallet_de_orden(r["id"])
        if w:
            duenos[r["condition_id"]] = w

    lineas: dict[str, Linea] = {}

    def linea(w: str) -> Linea:
        return lineas.setdefault(w, Linea(w))

    for r in conn.execute(
            f"""SELECT id, condition_id, realized_pnl FROM orders
                WHERE status = 'FILLED' AND realized_pnl IS NOT NULL
                AND strategy = 'copy_trading'{filtro}""", params):
        w = _wallet_de_orden(r["id"]) or duenos.get(r["condition_id"])
        if not w:
            continue
        l = linea(w)
        l.realizado += r["realized_pnl"]
        l.trades += 1
        if r["realized_pnl"] > 0:
            l.ganados += 1

    from ..db import from_json
    for p in positions:
        if p["strategy"] != "copy_trading":
            continue
        meta = from_json(p["meta"]) or {}
        w = (meta.get("copied_wallet") or "").lower()
        if not w:
            continue
        l = linea(w)
        mark = mark_price(p["condition_id"], p["outcome_index"] or 0,
                          p["avg_price"])
        l.no_realizado += p["size"] * (mark - p["avg_price"])
        l.abiertas += 1

    return sorted(lineas.values(), key=lambda l: -l.total)


def nombre_wallet(conn: sqlite3.Connection, wallet: str) -> str:
    row = conn.execute(
        "SELECT username FROM wallet_ranking WHERE wallet = ?",
        (wallet,)).fetchone()
    return (row["username"] if row and row["username"] else wallet[:10])


def formatear(lineas: list[Linea], titulo: str,
              nombres: Callable[[str], str] | None = None) -> str:
    """Bloque de texto listo para Telegram o terminal."""
    if not lineas:
        return f"{titulo}: sin operaciones todavía."
    filas = [titulo]
    for l in lineas:
        nombre = nombres(l.nombre) if nombres else l.nombre
        wr = f", gana {l.win_rate:.0%}" if l.win_rate is not None else ""
        abiertas = f", {l.abiertas} abiertas" if l.abiertas else ""
        filas.append(f"  {nombre[:22]:<22} {l.total:+8.2f}  "
                     f"(real {l.realizado:+.2f} / flot {l.no_realizado:+.2f}"
                     f"; {l.trades} cierres{wr}{abiertas})")
    return "\n".join(filas)

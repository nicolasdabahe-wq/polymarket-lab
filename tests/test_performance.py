"""Rendimiento por estrategia y por wallet: la tabla del lunes."""
import pytest

from pmbot.db import connect, to_json
from pmbot.monitor.performance import (formatear, resumen_estrategias,
                                       resumen_wallets)


def armar_db(tmp_path):
    conn = connect(tmp_path / "perf.db")
    ordenes = [
        # (id, strategy, cid, side, status, realized_pnl, created_at)
        ("copy:0xana:0xm1:0", "copy_trading", "0xm1", "BUY", "FILLED", None),
        ("redeem:copy_trading:0xm1:0", "copy_trading", "0xm1", "REDEEM",
         "FILLED", 5.0),
        ("copy:0xbeto:0xm2:0", "copy_trading", "0xm2", "BUY", "FILLED", None),
        ("copy-exit:0xbeto:0xm2:hoy", "copy_trading", "0xm2", "SELL",
         "FILLED", -3.0),
        ("cvalue:0xm3:0:hoy", "crypto_value", "0xm3", "BUY", "FILLED", None),
        ("redeem:crypto_value:0xm3:0", "crypto_value", "0xm3", "REDEEM",
         "FILLED", 8.0),
        ("arb:0xm4:0:hoy", "arbitrage", "0xm4", "BUY", "REJECTED", None),
    ]
    with conn:
        for oid, strat, cid, side, status, pnl in ordenes:
            conn.execute(
                """INSERT INTO orders (id, strategy, condition_id, side,
                   req_size, status, realized_pnl, created_at)
                   VALUES (?,?,?,?,1,?,?,'2026-08-22T05:00:00')""",
                (oid, strat, cid, side, status, pnl))
        conn.execute(
            """INSERT INTO paper_positions (strategy, condition_id, outcome,
               outcome_index, token_id, question, category, size, avg_price,
               meta, opened_at, updated_at)
               VALUES ('copy_trading','0xm5','Yes',0,'t','Q','sports',
                       10, 0.50, ?, 'x', 'x')""",
            (to_json({"copied_wallet": "0xana"}),))
    return conn


def marca(cid, idx, fallback):
    return 0.70 if cid == "0xm5" else fallback   # la abierta va ganando


def test_estrategias_ordenadas_por_pnl(tmp_path):
    conn = armar_db(tmp_path)
    pos = conn.execute("SELECT * FROM paper_positions").fetchall()
    lineas = resumen_estrategias(conn, pos, marca)
    assert [l.nombre for l in lineas] == ["crypto_value", "copy_trading"]
    cv, cp = lineas
    assert cv.realizado == pytest.approx(8.0) and cv.trades == 1
    # copia: +5 - 3 realizado, +2 flotando (10 x 0.20)
    assert cp.realizado == pytest.approx(2.0)
    assert cp.no_realizado == pytest.approx(2.0)
    assert cp.trades == 2 and cp.ganados == 1
    assert cp.win_rate == pytest.approx(0.5)
    assert cp.abiertas == 1


def test_rechazadas_no_cuentan(tmp_path):
    conn = armar_db(tmp_path)
    lineas = resumen_estrategias(conn, [], marca)
    assert all(l.nombre != "arbitrage" for l in lineas)


def test_wallets_heredan_el_pnl_de_sus_mercados(tmp_path):
    conn = armar_db(tmp_path)
    pos = conn.execute("SELECT * FROM paper_positions").fetchall()
    lineas = resumen_wallets(conn, pos, marca)
    por_nombre = {l.nombre: l for l in lineas}
    # ana: redeem de su mercado (+5, heredado por condition_id) y la
    # posición abierta que flota +2
    assert por_nombre["0xana"].realizado == pytest.approx(5.0)
    assert por_nombre["0xana"].no_realizado == pytest.approx(2.0)
    # beto: su salida directa con -3 (wallet en el id de la orden)
    assert por_nombre["0xbeto"].realizado == pytest.approx(-3.0)
    assert lineas[0].nombre == "0xana"   # ordenadas por total


def test_filtro_por_fecha(tmp_path):
    conn = armar_db(tmp_path)
    assert resumen_estrategias(conn, [], marca, desde="2026-08-23") == []
    assert len(resumen_estrategias(conn, [], marca, desde="2026-08-22")) == 2


def test_formato_legible(tmp_path):
    conn = armar_db(tmp_path)
    pos = conn.execute("SELECT * FROM paper_positions").fetchall()
    texto = formatear(resumen_estrategias(conn, pos, marca),
                      "PnL por estrategia")
    assert "crypto_value" in texto and "+8.00" in texto
    assert "gana 50%" in texto
    assert formatear([], "X").endswith("sin operaciones todavía.")

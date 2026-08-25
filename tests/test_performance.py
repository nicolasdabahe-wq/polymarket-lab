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


# --- Los dos fallos del reporte del 2026-08-25 -------------------------------

def test_extremos_muestra_ganadoras_y_perdedoras(tmp_path):
    """El reporte cortaba con `lineas[:6]` sobre una lista ordenada de mejor
    a peor: con copy_trading en -$228 enseñó seis wallets al 100% de acierto
    y escondió las 66 que perdían. Un resumen sesgado a favor es peor que
    ninguno, porque se lee como si todo fuera bien."""
    from pmbot.monitor.performance import Linea, extremos

    lineas = [Linea(f"w{i}", realizado=40.0 - i * 10) for i in range(10)]
    mejores, peores, ocultas = extremos(lineas, n=3)

    assert [l.nombre for l in mejores] == ["w0", "w1", "w2"]
    assert [l.nombre for l in peores] == ["w7", "w8", "w9"]
    assert ocultas == 4
    # Lo esencial: el bloque mostrado NO puede ser solo ganadoras.
    assert min(l.total for l in mejores + peores) < 0


def test_extremos_no_duplica_cuando_hay_pocas(tmp_path):
    """Con pocas wallets no debe repetirlas en los dos bloques."""
    from pmbot.monitor.performance import Linea, extremos

    lineas = [Linea(f"w{i}", realizado=-float(i)) for i in range(4)]
    mejores, peores, ocultas = extremos(lineas, n=3)
    assert [l.nombre for l in mejores] == ["w0", "w1", "w2", "w3"]
    assert peores == [] and ocultas == 0


def test_movimientos_del_dia_lee_de_la_base(tmp_path):
    """El reporte listaba solo lo devuelto por SU corrida, así que los fills
    del ciclo intradía no salían: la consulta a `orders` existía pero sus
    filas se tiraban, y la sección quedaba en blanco sin decir siquiera que
    no hubo operaciones."""
    from pmbot.monitor.performance import movimientos_del_dia

    conn = connect(tmp_path / "mov.db")
    with conn:
        conn.execute(
            """INSERT INTO markets (condition_id, question, category,
               updated_at) VALUES ('0xa','¿Gana Atlanta?','sports','x')""")
        for oid, side, status, dia, pnl in [
                ("o1", "BUY", "FILLED", "2026-08-25T10:00:00", None),
                ("o2", "SELL", "FILLED", "2026-08-25T20:00:00", 12.5),
                ("o3", "BUY", "REJECTED", "2026-08-25T11:00:00", None),
                ("o4", "BUY", "FILLED", "2026-08-24T10:00:00", None)]:
            conn.execute(
                """INSERT INTO orders (id, strategy, condition_id, outcome,
                   side, req_size, status, fill_size, fill_price, fill_usdc,
                   realized_pnl, created_at)
                   VALUES (?,'copy_trading','0xa','Yes',?,50,?,50,0.50,25.0,
                           ?,?)""",
                (oid, side, status, pnl, dia))

    filas = movimientos_del_dia(conn, "2026-08-25")

    assert len(filas) == 2                      # ni el rechazado ni el de ayer
    assert "BUY" in filas[0] and "SELL" in filas[1]
    assert "¿Gana Atlanta?" in filas[0]         # el LEFT JOIN trae la pregunta
    assert "PnL +12.50" in filas[1]
    assert movimientos_del_dia(conn, "2026-08-23") == []


def test_movimientos_del_dia_sin_mercado_en_cache(tmp_path):
    """Un mercado que no está en `markets` no puede tumbar el reporte."""
    from pmbot.monitor.performance import movimientos_del_dia

    conn = connect(tmp_path / "mov2.db")
    with conn:
        conn.execute(
            """INSERT INTO orders (id, strategy, condition_id, outcome, side,
               req_size, status, fill_size, fill_price, fill_usdc, created_at)
               VALUES ('o1','crypto_value','0xzz','No','BUY',10,'FILLED',
                       10,0.42,4.2,'2026-08-25T10:00:00')""")
    filas = movimientos_del_dia(conn, "2026-08-25")
    assert len(filas) == 1 and "crypto_value" in filas[0]

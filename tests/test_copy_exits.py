"""Las salidas de copy_trading: cuándo el bot puede afirmar que la wallet salió.

El 2026-08-25, sobre 65 posiciones cerradas, copy_trading acertó el 46%
comprando a 0.53 de media —un acierto compatible con ese precio— y aun así
perdió $213.42. La ganadora media dejó $8.76 cuando aguantar hasta la
resolución habría dado $14.91. Es decir: las ganadoras se cerraban a mitad
de camino. `check_exits` vendía en cuanto no encontraba la posición en el
snapshot de la wallet, sin preguntarse si el snapshot servía de prueba.
"""
from datetime import datetime, timedelta, timezone

import pytest

from pmbot.db import connect
from pmbot.strategies import CopyTradingStrategy


def _hace(horas: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(hours=horas)).isoformat(timespec="seconds")


def estrategia(tmp_path, limite=25, max_horas=12.0):
    conn = connect(tmp_path / "exits.db")
    return CopyTradingStrategy(
        conn, broker=None,
        cfg={"snapshot_positions_limit": limite,
             "snapshot_max_horas": max_horas}), conn


def poner(conn, wallet, n, edad_horas=1.0):
    with conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO wallet_positions (wallet, condition_id, title,
                   outcome, size, avg_price, cur_price, value_usdc, cash_pnl,
                   percent_pnl, fetched_at)
                   VALUES (?,?,'t','Yes',10,0.5,0.5,5,0,0,?)""",
                (wallet, f"0xm{i}", _hace(edad_horas)))


def test_foto_recortada_no_prueba_que_haya_salido(tmp_path):
    """El caso caro: la API devuelve como mucho 25 posiciones y las wallets
    grandes tienen muchas más (el ranking lista una con 118 mercados). Lo
    que copiamos casi nunca cabe en la foto, y ese hueco se leía como
    "cerró su posición"."""
    est, conn = estrategia(tmp_path, limite=25)
    poner(conn, "0xballena", 25)
    assert est._snapshot_prueba_salida("0xballena") is False


def test_foto_completa_y_fresca_si_prueba_la_salida(tmp_path):
    """Si no, el bot nunca vendería y la señal de salida no serviría."""
    est, conn = estrategia(tmp_path, limite=25)
    poner(conn, "0xchica", 4)
    assert est._snapshot_prueba_salida("0xchica") is True


def test_sin_filas_se_aguanta(tmp_path):
    """Cero filas puede ser 'salió de todo' o 'no la miramos' o 'la API
    devolvió vacío y el refresco borró las suyas'. No se distinguen, así que
    no se vende: si de verdad salió nos enteramos en el próximo refresco."""
    est, _ = estrategia(tmp_path)
    assert est._snapshot_prueba_salida("0xfantasma") is False


def test_foto_vieja_no_vale(tmp_path):
    est, conn = estrategia(tmp_path, max_horas=12.0)
    poner(conn, "0xvieja", 3, edad_horas=30)
    assert est._snapshot_prueba_salida("0xvieja") is False


def test_foto_al_limite_de_edad_todavia_vale(tmp_path):
    est, conn = estrategia(tmp_path, max_horas=12.0)
    poner(conn, "0xjusta", 3, edad_horas=11.5)
    assert est._snapshot_prueba_salida("0xjusta") is True


def test_fecha_corrupta_no_revienta_y_aguanta(tmp_path):
    est, conn = estrategia(tmp_path)
    with conn:
        conn.execute(
            """INSERT INTO wallet_positions (wallet, condition_id, title,
               outcome, size, avg_price, cur_price, value_usdc, cash_pnl,
               percent_pnl, fetched_at)
               VALUES ('0xraro','0xm','t','Yes',10,0.5,0.5,5,0,0,'ayer')""")
    assert est._snapshot_prueba_salida("0xraro") is False


def test_cada_wallet_se_juzga_por_su_propia_foto(tmp_path):
    """Una wallet grande no puede bloquear las salidas de una pequeña."""
    est, conn = estrategia(tmp_path, limite=25)
    poner(conn, "0xballena", 25)
    poner(conn, "0xchica", 2)
    assert est._snapshot_prueba_salida("0xballena") is False
    assert est._snapshot_prueba_salida("0xchica") is True


# --- Las salidas se pueden apagar, y por ahora lo están ---------------------

def test_con_salidas_apagadas_no_se_vende_nada(tmp_path):
    """Medición, no precaución. Sobre las 65 posiciones cerradas hasta el
    2026-08-26, aguantar habría dado -$141 y salir dio -$213: las salidas
    costaron $72. La ganadora media dejó $8.76 cuando aguantarla daba
    $14.91. Y el 27, sin nadie vendiendo, dos posiciones llegaron al final y
    pagaron +$31.70 y +$48.53."""
    import asyncio

    from pmbot.db import connect
    from pmbot.strategies import CopyTradingStrategy

    class BrokerQueGrita:
        async def execute(self, oid, req):
            raise AssertionError("con salidas_activas=false no se vende")

    conn = connect(tmp_path / "so.db")
    est = CopyTradingStrategy(conn, BrokerQueGrita(),
                              {"salidas_activas": False})
    with conn:
        conn.execute(
            """INSERT INTO paper_positions (strategy, condition_id, outcome,
               outcome_index, token_id, question, category, size, avg_price,
               meta, opened_at, updated_at)
               VALUES ('copy_trading','0xm','Yes',0,'t','Q','sports',10,0.50,
                       '{"copied_wallet":"0xw"}','x','x')""")
    assert asyncio.run(est.check_exits()) == []


def test_la_config_de_produccion_las_tiene_apagadas():
    """Si alguien las reactiva, que sea a sabiendas y con datos nuevos."""
    from pmbot.config import load_config
    cfg = load_config("config.yaml").section("strategies")["copy_trading"]
    assert cfg["enabled"] is True
    assert cfg["salidas_activas"] is False

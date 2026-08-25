"""Cada test reproduce un error de análisis real cometido en agosto 2026.

Si el cálculo vuelve a equivocarse igual, estos fallan.
"""
import pytest

from pmbot.db import connect
from pmbot.monitor.analisis import (Cerrada, agrupar, formatear,
                                    posiciones_cerradas, revisar)


def _db(tmp_path):
    return connect(tmp_path / "a.db")


def _orden(conn, oid, cid, outcome, side, usdc, shares, status="FILLED",
           strategy="copy_trading", category="sports", fecha="2026-08-24"):
    """La categoría vive en la tabla de mercados, no en la de órdenes."""
    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO markets (condition_id, gamma_id, slug,
               question, category, active, updated_at)
               VALUES (?,?,?,?,?,1,'2026-08-24')""",
            (cid, cid, cid, "pregunta", category))
        conn.execute(
            """INSERT INTO orders (id, strategy, condition_id,
               outcome, side, req_size, status, fill_size, fill_usdc,
               created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (oid, strategy, cid, outcome, side, shares, status,
             shares if status == "FILLED" else None,
             usdc if status == "FILLED" else None, fecha))


def _posicion(conn, cid, outcome, size):
    with conn:
        conn.execute(
            """INSERT INTO paper_positions (strategy, condition_id, outcome,
               outcome_index, size, avg_price, opened_at, updated_at)
               VALUES ('copy_trading',?,?,0,?,0.5,'2026-08-24','2026-08-24')""",
            (cid, outcome, size))


# --- ERROR 1: contar como compras las órdenes que nunca se ejecutaron ---

def test_las_ordenes_rechazadas_no_cuentan_como_apuestas(tmp_path):
    """Se informó de '5 compras en 2h' cuando las cinco fueron rechazadas y
    no entró ni una."""
    conn = _db(tmp_path)
    _orden(conn, "buena", "0xa", "Yes", "BUY", 25.0, 50.0)
    _orden(conn, "rech1", "0xb", "Yes", "BUY", 25.0, 50.0, status="REJECTED")
    _orden(conn, "rech2", "0xc", "Yes", "BUY", 25.0, 50.0, status="REJECTED")
    _orden(conn, "cobro", "0xa", "Yes", "REDEEM", 50.0, 50.0)
    cerradas = posiciones_cerradas(conn)
    assert len(cerradas) == 1, "las rechazadas se colaron como apuestas"
    assert cerradas[0].invertido == pytest.approx(25.0)


# --- ERROR 2: cruzar cada compra con cada venta multiplica los montos ---

def test_dos_compras_y_dos_cobros_no_se_multiplican(tmp_path):
    """El cruce fila a fila daba el doble de todo y produjo un ROI de -134%,
    que no puede existir."""
    conn = _db(tmp_path)
    _orden(conn, "c1", "0xa", "Yes", "BUY", 10.0, 20.0)
    _orden(conn, "c2", "0xa", "Yes", "BUY", 10.0, 20.0)
    _orden(conn, "v1", "0xa", "Yes", "SELL", 6.0, 20.0)
    _orden(conn, "v2", "0xa", "Yes", "SELL", 6.0, 20.0)
    [c] = posiciones_cerradas(conn)
    assert c.invertido == pytest.approx(20.0), "duplicó lo invertido"
    assert c.cobrado == pytest.approx(12.0), "duplicó lo cobrado"
    assert c.pnl == pytest.approx(-8.0)


# --- ERROR 3: medir el acierto sobre las posiciones abiertas ---

def test_las_ganadoras_ya_cobradas_cuentan_en_el_acierto(tmp_path):
    """El error más caro: las ganadoras se cobran y desaparecen de la lista
    de posiciones, las perdedoras se quedan en $0. Mirando ahí, esports
    parecía haber perdido 17 de 17 cuando había ganado 7.
    """
    conn = _db(tmp_path)
    # una ganadora: comprada, cobrada, ya no está en cartera
    _orden(conn, "g1", "0xgana", "Yes", "BUY", 13.0, 25.0)
    _orden(conn, "g2", "0xgana", "Yes", "REDEEM", 25.0, 25.0)
    # una perdedora: comprada, sin cobro, y SIGUE en cartera valiendo cero
    _orden(conn, "p1", "0xpierde", "Yes", "BUY", 13.0, 25.0)

    cerradas = posiciones_cerradas(conn)
    assert len(cerradas) == 2
    [g] = agrupar(cerradas, "categoria")
    assert g.ganadas == 1 and g.n == 2, (
        f"acierto mal calculado: {g.ganadas} de {g.n}")
    assert g.acierto == pytest.approx(0.5)


def test_una_posicion_todavia_abierta_no_se_cuenta(tmp_path):
    """Sin cerrar no hay resultado: contarla como pérdida sería inventar."""
    conn = _db(tmp_path)
    _orden(conn, "c1", "0xviva", "Yes", "BUY", 25.0, 50.0)
    _posicion(conn, "0xviva", "Yes", 50.0)
    assert posiciones_cerradas(conn) == []


# --- la red de seguridad: números que no pueden existir ---

def test_detecta_una_perdida_mayor_que_lo_invertido():
    mala = Cerrada("0xa", "Yes", "copy_trading", "sports",
                   invertido=24.01, cobrado=0.0, acciones=40.0)
    assert revisar([mala]) == []          # perder todo SÍ puede pasar
    peor = Cerrada("0xa", "Yes", "copy_trading", "sports",
                   invertido=24.01, cobrado=-8.0, acciones=40.0)
    assert revisar([peor]), "no detectó un cobro negativo"


def test_detecta_cobrar_sin_haber_comprado():
    fantasma = Cerrada("0xa", "Yes", "copy_trading", "sports",
                       invertido=0.0, cobrado=30.0, acciones=0.0)
    assert revisar([fantasma])


def test_los_numeros_creibles_pasan_la_revision():
    buena = Cerrada("0xa", "Yes", "crypto_value", "crypto",
                    invertido=25.0, cobrado=41.67, acciones=41.67)
    assert revisar([buena]) == []


# --- agrupaciones ---

def test_agrupa_por_precio_con_el_precio_medio_real(tmp_path):
    conn = _db(tmp_path)
    _orden(conn, "c1", "0xa", "Yes", "BUY", 25.0, 50.0)   # 0.50
    _orden(conn, "v1", "0xa", "Yes", "REDEEM", 50.0, 50.0)
    [g] = agrupar(posiciones_cerradas(conn), "precio")
    assert g.nombre.startswith("3 medio")
    assert g.roi == pytest.approx(1.0)


def test_el_formato_no_revienta_sin_datos():
    assert "TOTAL" not in formatear([], "vacío")


def test_los_esports_viejos_no_se_cuentan_como_deportes(tmp_path):
    """Un mercado ya resuelto conserva la categoría que tenía al cachearse,
    así que los esports anteriores al arreglo del clasificador figuraban
    como 'sports' e inflaban sus pérdidas. El texto de la pregunta no
    cambia nunca."""
    conn = _db(tmp_path)
    with conn:
        conn.execute(
            """INSERT INTO markets (condition_id, gamma_id, slug, question,
               category, active, updated_at)
               VALUES ('0xlol','1','s','LoL: Disguised vs Dignitas - Game 1',
                       'sports', 0, '2026-08-24')""")
        conn.execute(
            """INSERT INTO orders (id, strategy, condition_id, outcome, side,
               req_size, status, fill_size, fill_usdc, created_at)
               VALUES ('o1','copy_trading','0xlol','Dignitas','BUY',38.0,
                       'FILLED',38.0,19.84,'2026-08-22')""")
    [c] = posiciones_cerradas(conn)
    assert c.category == "esports", f"quedó como '{c.category}'"


def test_un_deporte_de_verdad_sigue_siendo_deporte(tmp_path):
    conn = _db(tmp_path)
    _orden(conn, "o1", "0xmlb", "Over", "BUY", 25.0, 50.0, category="sports")
    with conn:
        conn.execute("UPDATE markets SET question = ? WHERE condition_id = ?",
                     ("Cincinnati Reds vs. San Francisco Giants: O/U", "0xmlb"))
    [c] = posiciones_cerradas(conn)
    assert c.category == "sports"


def test_el_cruce_de_categoria_y_precio_separa_ambas_cosas(tmp_path):
    """Cripto perdía y casi todas sus entradas eran caras: sin cruzar las
    dos dimensiones no se sabe si el problema es la categoría o el precio,
    y vetar la categoría sería castigarla dos veces."""
    conn = _db(tmp_path)
    _orden(conn, "c1", "0xbtc", "No", "BUY", 12.0, 14.4, category="crypto")
    _orden(conn, "c2", "0xmlb", "Over", "BUY", 25.0, 50.0, category="sports")
    grupos = {g.nombre.strip(): g for g in
              agrupar(posiciones_cerradas(conn), "categoria+precio")}
    assert any(k.startswith("crypto") and "carisimo" in k for k in grupos), grupos.keys()
    assert any(k.startswith("sports") and "medio" in k for k in grupos), grupos.keys()


# --- Ganar más veces de las que se pierde y aun así perder dinero -----------

def _cerrada(invertido, cobrado, acciones, strategy="copy_trading"):
    from pmbot.monitor.analisis import Cerrada
    return Cerrada(condition_id=f"0x{invertido}{cobrado}{acciones}",
                   outcome="Yes", strategy=strategy, category="sports",
                   invertido=invertido, cobrado=cobrado, acciones=acciones)


def test_asimetria_detecta_cobrar_poco_y_pagar_entero():
    """El caso real del 2026-08-25: copy_trading acertó el 58% y perdió
    $86.58. No es contradicción — las ganadoras se cerraban por centavos y
    las perdedoras costaban la posición entera. Sin este corte la conclusión
    natural es culpar a las wallets copiadas, que eligieron bien."""
    from pmbot.monitor.analisis import asimetria

    # 6 ganadoras de +$0.50, 4 perdedoras de -$10. Acierto 60%, entrada 0.50.
    cerradas = ([_cerrada(10.0, 10.5, 20.0)] * 6
                + [_cerrada(10.0, 0.0, 20.0)] * 4)
    a = asimetria(cerradas)

    assert a.ganadoras == 6 and a.perdedoras == 4
    assert a.aciertos == 0.6
    assert round(a.precio_medio, 3) == 0.5
    assert round(a.esperado_por_accion, 3) == 0.1     # w - p, sería rentable
    assert round(a.media_ganadora, 2) == 0.50
    assert round(a.media_perdedora, 2) == 10.00
    assert "se cobran las buenas de a poco" in a.diagnostico


def test_asimetria_culpa_a_la_entrada_cuando_el_precio_es_alto():
    """Si el precio medio supera al acierto, pierde aunque se aguante hasta
    el final. Ahí el problema es comprar caro, no vender pronto — y decir lo
    contrario mandaría a tocar la parte equivocada del bot."""
    from pmbot.monitor.analisis import asimetria

    # 6 de 10 aciertos comprando a 0.80: 0.60 - 0.80 = -0.20 por acción.
    cerradas = ([_cerrada(8.0, 10.0, 10.0)] * 6
                + [_cerrada(8.0, 0.0, 10.0)] * 4)
    a = asimetria(cerradas)

    assert round(a.precio_medio, 2) == 0.80
    assert a.esperado_por_accion < 0
    assert "el problema es la entrada" in a.diagnostico


def test_asimetria_sana_no_inventa_problemas():
    from pmbot.monitor.analisis import asimetria

    cerradas = ([_cerrada(5.0, 10.0, 10.0)] * 6
                + [_cerrada(5.0, 0.0, 10.0)] * 4)
    a = asimetria(cerradas)
    assert round(a.precio_medio, 2) == 0.50 and a.aciertos == 0.6
    assert "sano" in a.diagnostico


def test_asimetria_sin_cierres_no_revienta():
    from pmbot.monitor.analisis import asimetria

    a = asimetria([])
    assert a.aciertos == 0.0 and a.precio_medio == 0.0
    assert a.diagnostico == "sin cierres que juzgar"

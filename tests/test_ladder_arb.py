"""Escaleras de strikes. Números reales del hallazgo del 2026-08-22:
BTC $150.000 a 0.035 vs $160.000 a 0.042, mismo vencimiento."""
import pytest

from pmbot.strategies.ladder_arb import (agrupar_escaleras, dominance_edge,
                                         superset_first)


# --- quién contiene a quién ---

def test_en_above_el_strike_menor_contiene():
    # Llegar a 160k implica haber tocado 150k.
    g, c = superset_first("touch_above", 150_000, "m150", 160_000, "m160")
    assert (g, c) == ("m150", "m160")
    g, c = superset_first("touch_above", 160_000, "m160", 150_000, "m150")
    assert (g, c) == ("m150", "m160")


def test_en_below_el_strike_mayor_contiene():
    # Caer a $60 implica haber pasado por $70.
    g, c = superset_first("touch_below", 60, "m60", 70, "m70")
    assert (g, c) == ("m70", "m60")


def test_strikes_iguales_no_forman_par():
    assert superset_first("touch_above", 100, "a", 100, "b") is None


# --- la aritmética del par ---

def test_el_hallazgo_real_de_btc():
    # YES 150k a 0.035 + NO 160k a ~0.960 = 0.995 -> 0.5% asegurado.
    edge = dominance_edge(0.035, 0.960, min_edge=0.004)
    assert edge == pytest.approx(0.005)


def test_par_caro_no_es_oportunidad():
    # Mercado sano: YES grande 0.08 + NO chico 0.97 = 1.05.
    assert dominance_edge(0.08, 0.97, min_edge=0.01) is None


def test_sin_book_no_hay_trato():
    assert dominance_edge(None, 0.9, 0.01) is None
    assert dominance_edge(0.05, None, 0.01) is None
    assert dominance_edge(0.0, 0.9, 0.01) is None


# --- agrupación de hermanos ---

def fila(question, end_date, yes=0.05):
    return {"question": question, "end_date": end_date, "yes_price": yes}


def test_solo_se_emparejan_hermanos_exactos():
    rows = [
        fila("Will Bitcoin reach $150,000 by December 31, 2026?", "2026-12-31"),
        fila("Will Bitcoin reach $160,000 by December 31, 2026?", "2026-12-31"),
        fila("Will Bitcoin reach $170,000 by June 30, 2027?", "2027-06-30"),
        fila("Will Ethereum reach $5,000 by December 31, 2026?", "2026-12-31"),
    ]
    escaleras = agrupar_escaleras(rows)
    assert len(escaleras) == 1
    e = escaleras[0]
    assert e.product == "BTC-USD"
    assert [s for s, _ in e.peldanos] == [150_000, 160_000]


def test_los_sufijos_k_se_emparejan_con_los_numeros_completos():
    # "$150k" y "$160,000" son la misma escalera: el lector ya normaliza.
    rows = [
        fila("Will Bitcoin hit $150k by December 31, 2026?", "2026-12-31"),
        fila("Will Bitcoin reach $160,000 by December 31, 2026?", "2026-12-31"),
    ]
    [e] = agrupar_escaleras(rows)
    assert [s for s, _ in e.peldanos] == [150_000, 160_000]


def test_las_carreras_no_contaminan_las_escaleras():
    rows = [
        fila("Will Ethereum hit $1,000 or $3,000 first?", "2026-12-31"),
        fila("Will Ethereum reach $2,750 by December 31, 2026?", "2026-12-31"),
    ]
    assert agrupar_escaleras(rows) == []

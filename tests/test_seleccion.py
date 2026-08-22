"""Casos reales medidos el 2026-08-22 sobre ballenas de Polymarket."""
from pmbot.smart_money.seleccion import Opcion, elegir_umbral

MIN_COPIAS, MIN_ROI = 20, 0.0


def elegir(datos):
    return elegir_umbral([Opcion(th, n, roi) for th, n, roi in datos],
                         MIN_COPIAS, MIN_ROI)


def test_breakthebank_es_copiable():
    # Gana en los cinco umbrales, con 34 a 58 copias.
    d = elegir([(500, 58, 0.045), (1000, 52, 0.063), (2000, 45, 0.151),
                (5000, 42, 0.020), (10000, 34, 0.050)])
    assert d.veredicto == "copiable" and d.umbral == 2000


def test_imjustken_es_copiable_ignorando_la_muestra_chica():
    # El umbral de $10.000 tiene 11 copias: no cuenta ni a favor ni en contra.
    d = elegir([(500, 77, 0.012), (1000, 58, 0.043), (2000, 43, 0.074),
                (5000, 26, 0.025), (10000, 11, 0.059)])
    assert d.veredicto == "copiable" and d.umbral == 2000


def test_swisstony_pierde_donde_hay_muestra():
    # -2.1% en 83 copias y -5.7% en 28; el +17.2% son 10 copias.
    d = elegir([(500, 83, -0.021), (1000, 28, -0.057), (2000, 10, 0.172)])
    assert d.veredicto == "rechazada" and "consistente" in d.motivo


def test_rn1_un_pico_no_alcanza():
    # Pierde 8% en 149 copias; el +23.8% es UNA sola apuesta.
    d = elegir([(500, 149, -0.080), (1000, 78, -0.043), (2000, 24, 0.014),
                (5000, 1, 0.238)])
    assert d.veredicto == "rechazada"


def test_sainttroplay_seis_copias_no_son_evidencia():
    # +77% en todos los umbrales, pero siempre con 6 copias.
    d = elegir([(th, 6, 0.775) for th in (500, 1000, 2000, 5000, 10000)])
    assert d.veredicto == "sin_datos" and "muestra insuficiente" in d.motivo


def test_empate_a_la_mitad_pasa():
    # La mayoría se cuenta como "no menos de la mitad": 2 de 4 alcanza.
    d = elegir([(500, 40, -0.02), (1000, 40, -0.01),
                (2000, 40, 0.05), (5000, 40, 0.08)])
    assert d.veredicto == "copiable" and d.umbral == 5000


def test_sin_backtest():
    assert elegir([]).veredicto == "sin_datos"

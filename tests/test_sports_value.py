"""Modelo de béisbol. Números reales de la MLB del 2026-08-22."""
import pytest

from pmbot.strategies.sports_value import (ajuste_pitcher, apodo, leer_pregunta,
                                           log5, pitagorica, prob_local)


# --- fuerza por carreras ---

def test_pitagorica_con_equipos_reales():
    # Brewers 644 CF / 484 CC -> .628 ; Athletics 557/746 -> .369
    assert pitagorica(644, 484) == pytest.approx(0.628, abs=0.002)
    assert pitagorica(557, 746) == pytest.approx(0.369, abs=0.002)


def test_pitagorica_castiga_al_que_gana_apretado():
    # Rockies: 50-78 de récord (.391) pero 612/742 de carreras -> .413.
    # El modelo dice que son mejores de lo que muestra el récord.
    assert pitagorica(612, 742) > 50 / 128


def test_pitagorica_sin_datos_es_moneda_al_aire():
    assert pitagorica(0, 0) == 0.5


# --- enfrentamiento ---

def test_log5_entre_iguales_es_mitad_y_mitad():
    assert log5(0.55, 0.55) == pytest.approx(0.5)


def test_log5_brewers_contra_athletics():
    # .628 vs .369: el mejor debe quedar bien favorito, pero no absurdo.
    p = log5(0.628, 0.369)
    assert 0.70 < p < 0.76


def test_log5_es_simetrico():
    assert log5(0.628, 0.369) == pytest.approx(1 - log5(0.369, 0.628))


# --- abridor ---

def test_un_as_suma_probabilidad():
    # Dylan Cease: ERA 2.42 en 137.2 entradas.
    assert ajuste_pitcher(2.42, 137.2) > 0.04


def test_un_pitcher_malo_resta():
    assert ajuste_pitcher(6.00, 120.0) < -0.02


def test_pitcher_promedio_no_mueve_nada():
    assert ajuste_pitcher(4.10, 100.0) == pytest.approx(0.0, abs=1e-9)


def test_sin_muestra_no_se_ajusta():
    # 12 entradas no dicen nada del nivel de un pitcher.
    assert ajuste_pitcher(1.20, 12.0) == 0.0
    assert ajuste_pitcher(None, 200.0) == 0.0


def test_el_ajuste_tiene_tope():
    # Ni un ERA de 0.00 puede mover el juego más de 10 puntos.
    assert ajuste_pitcher(0.0, 200.0) <= 0.10


# --- probabilidad final ---

def test_la_localia_suma():
    en_casa = prob_local(0.55, 0.55)
    assert en_casa > 0.5 and en_casa == pytest.approx(0.535, abs=0.001)


def test_el_abridor_puede_dar_vuelta_un_juego_parejo():
    # Equipos iguales, pero el visitante manda a su as y el local a un malo.
    p = prob_local(0.55, 0.55, ajuste_local=ajuste_pitcher(5.80, 120),
                   ajuste_visitante=ajuste_pitcher(2.42, 137))
    assert p < 0.5


def test_la_probabilidad_no_se_va_a_los_extremos():
    assert 0.02 <= prob_local(0.99, 0.01) <= 0.98


# --- lectura de las preguntas de Polymarket ---

def test_apodos_de_equipos():
    assert apodo("New York Yankees") == "yankees"
    assert apodo("Milwaukee Brewers") == "brewers"


def test_los_sox_no_se_confunden():
    assert apodo("Boston Red Sox") == "red sox"
    assert apodo("Chicago White Sox") == "white sox"
    assert apodo("Toronto Blue Jays") == "blue jays"


def test_lee_will_x_win():
    p = leer_pregunta("Will Milwaukee Brewers win on 2026-08-22?")
    assert p and p.equipo == "brewers"


def test_lee_el_formato_vs():
    p = leer_pregunta("Atlanta Braves vs. Milwaukee Brewers")
    assert p and p.equipo == "braves" and p.rival == "brewers"


def test_ignora_lo_que_el_modelo_no_cubre():
    # Estos tres fueron apuestas reales del bot que el modelo NO puede opinar.
    assert leer_pregunta("Spread: Baltimore Orioles (-1.5)") is None
    assert leer_pregunta("Washington Nationals vs. Miami Marlins: O/U 8.5") is None
    assert leer_pregunta("Exact Score: Ipswich Town FC 0 - 1 Sunderland AFC?") is None
    assert leer_pregunta("") is None

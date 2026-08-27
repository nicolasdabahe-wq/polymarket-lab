"""Reconocer un mercado de ganador de partido sin tragarse los derivados.

El 2026-08-27 había 1.915 mercados de tenis abiertos en Polymarket y el bot
miraba cero: `leer_pregunta` descartaba todo lo que llevara dos puntos, y el
tenis se titula «Torneo: Jugador A vs Jugador B». Levantar esa restricción a
lo bruto habría metido ganadores de set, hándicaps de juegos y props de
béisbol, que comparten exactamente la misma forma.

Todas las preguntas de aquí abajo son textuales de la API de Gamma.
"""
import pytest

from pmbot.strategies.sports_value import leer_partido

JUGADORES = ["Juan Manuel Cerundolo", "Sebastian Baez"]
EQUIPOS = ["Los Angeles Dodgers", "Atlanta Braves"]
SI_NO = ["Yes", "No"]


# --- lo que SÍ es un ganador de partido ---

def test_tenis_con_torneo_delante():
    assert leer_partido(
        "Winston-Salem Open: Juan Manuel Cerundolo vs Sebastian Baez",
        JUGADORES) == ("cerundolo", "baez")


def test_beisbol_sin_prefijo():
    assert leer_partido("Los Angeles Dodgers vs. Atlanta Braves",
                        EQUIPOS) == ("dodgers", "braves")


def test_apodos_ambiguos_siguen_funcionando():
    """'Red Sox' y 'White Sox' comparten última palabra."""
    salidas = ["Boston Red Sox", "Chicago White Sox"]
    assert leer_partido("Boston Red Sox vs. Chicago White Sox",
                        salidas) == ("red sox", "white sox")


# --- derivados del MISMO partido: misma forma, no se apuestan aquí ---

@pytest.mark.parametrize("pregunta", [
    "Set 1 Winner: Cerundolo vs Baez",
    "Set 2 Winner: Cerundolo vs Baez",
    "Set Handicap: Baez (-1.5) vs Cerundolo (+1.5)",
    "Game Spread: Baez (-1.5) vs Cerundolo (+1.5)",
])
def test_derivados_de_tenis_fuera(pregunta):
    """Los ganadores de set tienen a los dos jugadores como salidas, así que
    el filtro de salidas no los ve: los para el nombre del prefijo."""
    assert leer_partido(pregunta, JUGADORES) is None


@pytest.mark.parametrize("pregunta", [
    "Will there be a run scored in the first inning?: "
    "Los Angeles Dodgers vs. Atlanta Braves",
    "Will the game go to extra innings?: "
    "Los Angeles Dodgers vs. Atlanta Braves",
])
def test_props_de_beisbol_fuera(pregunta):
    """Estas se resuelven Yes/No: las descarta el filtro de salidas, que no
    depende de acertar cómo se titula la prop."""
    assert leer_partido(pregunta, SI_NO) is None


@pytest.mark.parametrize("pregunta", [
    "Cerundolo vs. Baez: Match O/U 21.5",
    "Juan Manuel Cerundolo vs. Sebastian Baez: Total Sets O/U 2.5",
])
def test_totales_con_el_vs_delante_fuera(pregunta):
    """Aquí el «vs» va ANTES de los dos puntos: la cola no es «A vs B»."""
    assert leer_partido(pregunta, JUGADORES) is None


@pytest.mark.parametrize("pregunta", [
    "Spread: Los Angeles Dodgers (-1.5)",
    "1st 5 Innings Spread: Los Angeles Dodgers (-1.5)",
    "Will Jannik Sinner win the 2026 Men's US Open?",
])
def test_otros_mercados_fuera(pregunta):
    assert leer_partido(pregunta, EQUIPOS) is None


# --- el filtro de salidas es el fuerte ---

def test_las_salidas_tienen_que_ser_los_participantes():
    """Aunque el título parezca un ganador, si las salidas no son los dos
    nombres es otro mercado. Sin esto entraría cualquier prop que mencione
    a los dos equipos."""
    assert leer_partido("Los Angeles Dodgers vs. Atlanta Braves",
                        SI_NO) is None


def test_sin_salidas_se_confia_solo_en_el_titulo():
    """Para poder usarlo también donde no tenemos las salidas a mano."""
    assert leer_partido(
        "Winston-Salem Open: Juan Manuel Cerundolo vs Sebastian Baez"
    ) == ("cerundolo", "baez")


def test_basura_no_revienta():
    for q in ("", None, ":", "vs", "A vs A", "Winner:"):
        assert leer_partido(q) is None

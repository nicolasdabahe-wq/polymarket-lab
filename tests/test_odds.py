"""Líneas de casas profesionales: de-vig y consenso."""
import pytest

from pmbot.data.odds import consenso, devig


def test_devig_recupera_probabilidades_reales():
    # Cuotas 1.91/1.91 (la clásica -110/-110): 52.4% bruto cada lado por el
    # margen; de-vigueado queda 50/50.
    p = devig([1.91, 1.91])
    assert p[0] == pytest.approx(0.5) and sum(p) == pytest.approx(1.0)


def test_devig_favorito_real():
    # 1.40 vs 3.10: el favorito queda ~68.9% tras quitar el margen.
    p = devig([1.40, 3.10])
    assert p[0] == pytest.approx(0.689, abs=0.002)
    assert sum(p) == pytest.approx(1.0)


def test_devig_cuotas_invalidas():
    assert devig([]) == []
    assert devig([1.0, 2.0]) == []
    assert devig([0.9, 2.0]) == []


def evento(casas):
    return {"home_team": "New York Yankees", "away_team": "Toronto Blue Jays",
            "commence_time": "2026-08-22T17:35:00Z",
            "bookmakers": [
                {"key": k, "markets": [{"key": "h2h", "outcomes": [
                    {"name": "New York Yankees", "price": pl},
                    {"name": "Toronto Blue Jays", "price": pv}]}]}
                for k, pl, pv in casas]}


def test_pinnacle_manda_sobre_las_recreativas():
    lineas = consenso([evento([("pinnacle", 1.60, 2.50),
                               ("bet365", 1.80, 2.10),
                               ("unibet", 1.85, 2.05)])])
    [l] = lineas
    assert l.sharp and l.casas == 3
    # de-vig de 1.60/2.50: 0.625/0.4 -> 61.0%/39.0%
    assert l.prob_local == pytest.approx(0.6098, abs=0.001)


def test_sin_sharp_se_usa_la_mediana():
    lineas = consenso([evento([("bet365", 1.80, 2.10),
                               ("unibet", 1.90, 2.00),
                               ("betsson", 1.85, 2.05)])])
    [l] = lineas
    assert not l.sharp
    assert 0.51 < l.prob_local < 0.54


def test_evento_sin_h2h_se_ignora():
    ev = {"home_team": "A", "away_team": "B", "bookmakers": [
        {"key": "x", "markets": [{"key": "spreads", "outcomes": []}]}]}
    assert consenso([ev]) == []


# --- fútbol: tres resultados y nombres ---

def evento3(local, visitante, pl, pv, pe):
    return {"home_team": local, "away_team": visitante,
            "commence_time": "2026-08-22T23:00:00Z",
            "bookmakers": [{"key": "pinnacle", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": local, "price": pl},
                    {"name": visitante, "price": pv},
                    {"name": "Draw", "price": pe}]}]}]}


def test_futbol_devig_a_tres_resultados():
    # 2.10 / 3.80 / 3.30: local ~47.5%, visitante ~26.2%, empate ~30.2%
    # sin de-vig; normalizado suman 1.
    [l] = consenso([evento3("Brentford", "Tottenham", 2.10, 3.80, 3.30)])
    assert l.prob_empate is not None
    total = l.prob_local + l.prob_visitante + l.prob_empate
    assert total == pytest.approx(1.0)
    assert l.prob_local == pytest.approx(0.458, abs=0.005)


def test_en_futbol_ganar_no_es_lo_contrario_de_perder():
    # P(local) + P(visitante) < 1 porque el empate existe: comprar NO del
    # "¿gana X?" incluye el empate y eso cambia todos los números.
    [l] = consenso([evento3("Brentford", "Tottenham", 2.10, 3.80, 3.30)])
    assert l.prob_local + l.prob_visitante < 0.75


def test_nombres_con_adornos():
    from pmbot.data.odds import nombre_coincide
    assert nombre_coincide("Brentford", "Will Brentford FC win on 2026-08-22?")
    assert nombre_coincide("Deportivo Toluca",
                           "Will Deportivo Toluca FC win on 2026-08-22?")
    assert nombre_coincide("CF América", "Will CF América win on 2026-08-21?")


def test_manchesters_no_se_confunden():
    from pmbot.data.odds import nombre_coincide
    q_united = "Will Manchester United FC win on 2026-08-22?"
    assert nombre_coincide("Manchester United", q_united)
    assert not nombre_coincide("Manchester City", q_united)


def test_equipos_distintos_no_matchean():
    from pmbot.data.odds import nombre_coincide
    assert not nombre_coincide("Brentford",
                               "Will Tottenham Hotspur win on 2026-08-22?")

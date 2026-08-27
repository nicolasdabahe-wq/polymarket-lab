"""Ligas cuyos mercados de Polymarket se titulan «A vs B».

Hasta el 2026-08-27 sports_value solo miraba béisbol y fútbol. Medido sobre
los mercados reales de Gamma ese día: 85 ganadores de partido en tenis y 72
en baloncesto que el bot no veía, contra los 35 de béisbol que sí. El fútbol
va por otra ruta porque allí Polymarket abre un mercado por equipo («Will X
win on FECHA?») en vez de uno con dos salidas.
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from pmbot.db import connect
from pmbot.strategies import SportsValueStrategy


def _dentro_de(horas):
    return (datetime.now(timezone.utc)
            + timedelta(hours=horas)).isoformat().replace("+00:00", "Z")


@dataclass
class Linea:
    local: str
    visitante: str
    prob_local: float
    inicio_utc: str
    casas: int = 20
    prob_visitante: float = 0.0
    prob_empate: float = None
    sharp: bool = True

    def __post_init__(self):
        self.prob_visitante = 1 - self.prob_local


class Odds:
    enabled = True
    def __init__(self, por_clave): self.por_clave = por_clave; self.pedidas = []
    async def lineas(self, clave):
        self.pedidas.append(clave)
        return self.por_clave.get(clave, [])


class Libro:
    def __init__(self, ask): self.best_ask = ask


class Clob:
    def __init__(self, asks): self.asks = asks
    async def order_book(self, token_id): return Libro(self.asks.get(token_id))


class Broker:
    def __init__(self, asks):
        self.clob = Clob(asks); self.ordenes = []
    def equity(self): return 500.0
    async def execute(self, oid, req):
        self.ordenes.append((req.outcome, round(req.price, 3), req.size))
        class F: status = "FILLED"; realized_pnl = 0.0
        return F()


class MlbMudo:
    async def equipos(self, t): return {}
    async def juegos(self, f): return []


def armar(tmp_path, odds, asks, ligas_vs):
    conn = connect(tmp_path / "v.db")
    cfg = {"enabled": True, "min_edge": 0.08, "max_entry_price": 0.60,
           "min_trade_usdc": 25.0, "min_minutos_antes": 15,
           "ligas_vs": ligas_vs}
    broker = Broker(asks)
    est = SportsValueStrategy(conn, MlbMudo(), None, broker, cfg, None,
                              odds=odds)
    return est, conn, broker


def mercado(conn, cid, pregunta, salidas):
    with conn:
        conn.execute(
            """INSERT INTO markets (condition_id, question, category, active,
               clob_token_ids, raw, updated_at)
               VALUES (?,?,'sports',1,?,?,'x')""",
            (cid, pregunta, json.dumps([f"{cid}-0", f"{cid}-1"]),
             json.dumps({"outcomes": json.dumps(salidas)})))


def test_tenis_con_torneo_delante_se_apuesta(tmp_path):
    """El mercado que el bot no veía: «Winston-Salem Open: A vs B»."""
    import asyncio

    linea = Linea("Sebastian Baez", "Juan Manuel Cerundolo", 0.70,
                  _dentro_de(5))
    odds = Odds({"tennis_wta_monterrey_open": [linea]})
    est, conn, broker = armar(
        tmp_path, odds, {"0xt-0": 0.55, "0xt-1": 0.42},
        [{"clave": "tennis_wta_monterrey_open", "tag": "tennis"}])
    mercado(conn, "0xt",
            "Winston-Salem Open: Juan Manuel Cerundolo vs Sebastian Baez",
            ["Juan Manuel Cerundolo", "Sebastian Baez"])

    traza = []
    asyncio.run(est.scan_and_execute(traza=traza))

    # La sharp da 70% a Baez y el libro lo pide a 0.55: 15 puntos.
    assert broker.ordenes, traza
    assert broker.ordenes[0][0] == "Sebastian Baez"


def test_no_se_apuesta_sin_ventaja(tmp_path):
    import asyncio

    linea = Linea("Sebastian Baez", "Juan Manuel Cerundolo", 0.56,
                  _dentro_de(5))
    odds = Odds({"tennis_wta_monterrey_open": [linea]})
    est, conn, broker = armar(
        tmp_path, odds, {"0xt-0": 0.45, "0xt-1": 0.55},
        [{"clave": "tennis_wta_monterrey_open", "tag": "tennis"}])
    mercado(conn, "0xt",
            "Winston-Salem Open: Juan Manuel Cerundolo vs Sebastian Baez",
            ["Juan Manuel Cerundolo", "Sebastian Baez"])
    traza = []
    asyncio.run(est.scan_and_execute(traza=traza))
    assert broker.ordenes == []
    assert any("por debajo de 8%" in t for t in traza), traza


def test_los_derivados_del_mismo_partido_no_se_tocan(tmp_path):
    """«Set 1 Winner: A vs B» tiene los mismos dos jugadores como salidas.
    Si entrara aquí, apostaríamos la probabilidad del PARTIDO al ganador de
    un set, que no es lo mismo ni de lejos."""
    import asyncio

    linea = Linea("Sebastian Baez", "Juan Manuel Cerundolo", 0.70,
                  _dentro_de(5))
    odds = Odds({"tennis_wta_monterrey_open": [linea]})
    est, conn, broker = armar(
        tmp_path, odds, {"0xs-0": 0.40, "0xs-1": 0.55},
        [{"clave": "tennis_wta_monterrey_open", "tag": "tennis"}])
    mercado(conn, "0xs", "Set 1 Winner: Cerundolo vs Baez",
            ["Juan Manuel Cerundolo", "Sebastian Baez"])
    asyncio.run(est.scan_and_execute())
    assert broker.ordenes == []


def test_partido_ya_empezado_fuera(tmp_path):
    import asyncio

    linea = Linea("Sebastian Baez", "Juan Manuel Cerundolo", 0.70,
                  _dentro_de(-1))
    odds = Odds({"tennis_wta_monterrey_open": [linea]})
    est, conn, broker = armar(
        tmp_path, odds, {"0xt-0": 0.55, "0xt-1": 0.42},
        [{"clave": "tennis_wta_monterrey_open", "tag": "tennis"}])
    mercado(conn, "0xt",
            "Winston-Salem Open: Juan Manuel Cerundolo vs Sebastian Baez",
            ["Juan Manuel Cerundolo", "Sebastian Baez"])
    asyncio.run(est.scan_and_execute())
    assert broker.ordenes == []


def test_una_liga_rota_no_calla_a_las_demas(tmp_path):
    """El error que ya nos costó días de silencio, ahora entre ligas."""
    import asyncio

    class OddsMixto(Odds):
        async def lineas(self, clave):
            self.pedidas.append(clave)
            if clave == "basketball_wnba":
                raise RuntimeError("503 del proveedor")
            return self.por_clave.get(clave, [])

    linea = Linea("Sebastian Baez", "Juan Manuel Cerundolo", 0.70,
                  _dentro_de(5))
    odds = OddsMixto({"tennis_wta_monterrey_open": [linea]})
    est, conn, broker = armar(
        tmp_path, odds, {"0xt-0": 0.55, "0xt-1": 0.42},
        [{"clave": "basketball_wnba", "tag": "basketball"},
         {"clave": "tennis_wta_monterrey_open", "tag": "tennis"}])
    mercado(conn, "0xt",
            "Winston-Salem Open: Juan Manuel Cerundolo vs Sebastian Baez",
            ["Juan Manuel Cerundolo", "Sebastian Baez"])
    asyncio.run(est.scan_and_execute())
    assert "basketball_wnba" in odds.pedidas
    assert broker.ordenes, "el tenis tenía que apostarse igual"


def test_sin_llave_no_se_consulta_nada(tmp_path):
    import asyncio

    class Apagado:
        enabled = False
        async def lineas(self, clave): raise AssertionError("no debe pedir")

    est, conn, broker = armar(
        tmp_path, Apagado(), {},
        [{"clave": "tennis_wta_monterrey_open", "tag": "tennis"}])
    asyncio.run(est.scan_and_execute())
    assert broker.ordenes == []


def test_el_colchon_de_creditos_corta_las_consultas():
    """20.000 créditos al mes se acaban. Sin colchón, el plan se vacía y los
    deportes dejan de operar en silencio — que es exactamente como
    sports_value pasó días muda por un 406 sin que nadie lo notara."""
    import asyncio

    from pmbot.data.odds import OddsClient

    class HttpCuenta:
        def __init__(self): self.llamadas = 0
        async def get_json_con_cabeceras(self, url, params=None, headers=None):
            self.llamadas += 1
            return [], {"x-requests-remaining": "900",
                        "x-requests-used": "19100"}

    http = HttpCuenta()
    c = OddsClient(http, "llave", cache_segundos=0, reserva_creditos=1000)
    asyncio.run(c.lineas("soccer_epl"))          # primera: consulta y aprende
    assert http.llamadas == 1
    assert c.creditos_restantes == 900
    asyncio.run(c.lineas("soccer_spain_la_liga"))   # ya por debajo: no pide
    assert http.llamadas == 1


def test_con_saldo_de_sobra_no_corta():
    import asyncio

    from pmbot.data.odds import OddsClient

    class Http:
        def __init__(self): self.llamadas = 0
        async def get_json_con_cabeceras(self, url, params=None, headers=None):
            self.llamadas += 1
            return [], {"x-requests-remaining": "18000"}

    http = Http()
    c = OddsClient(http, "llave", cache_segundos=0, reserva_creditos=1000)
    asyncio.run(c.lineas("soccer_epl"))
    asyncio.run(c.lineas("soccer_spain_la_liga"))
    assert http.llamadas == 2


def test_el_cache_ahorra_creditos():
    import asyncio

    from pmbot.data.odds import OddsClient

    class Http:
        def __init__(self): self.llamadas = 0
        async def get_json_con_cabeceras(self, url, params=None, headers=None):
            self.llamadas += 1
            return [], {"x-requests-remaining": "18000"}

    http = Http()
    c = OddsClient(http, "llave", cache_segundos=7200)
    for _ in range(5):
        asyncio.run(c.lineas("soccer_epl"))
    assert http.llamadas == 1

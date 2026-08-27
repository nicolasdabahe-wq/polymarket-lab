"""El diagnóstico deportivo recorre el MISMO camino que el bot.

sports_value llevaba días encendida sin haber creado ni una orden —ni
siquiera una rechazada— y no había forma de saber en qué paso se caía. Un
diagnóstico con su propia copia de la lógica no habría probado nada sobre
la lógica que opera; por eso la traza va dentro de `scan_and_execute`.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from pmbot.db import connect
from pmbot.strategies import SportsValueStrategy


@dataclass
class PitcherFalso:
    nombre: str = "P"
    era: float | None = 4.0
    entradas: float = 100.0


@dataclass
class JuegoFalso:
    visitante: str
    local: str
    inicio_utc: str
    pitcher_visitante: object = None
    pitcher_local: object = None


class MlbFalso:
    def __init__(self, juegos): self._juegos = juegos
    async def equipos(self, temporada):
        @dataclass
        class E:
            nombre: str; victorias: int; derrotas: int
            carreras_a_favor: int; carreras_en_contra: int
        return {"Tampa Bay Rays": E("Rays", 80, 50, 700, 550),
                "New York Yankees": E("Yankees", 70, 60, 600, 600)}
    async def juegos(self, fecha): return self._juegos


class LibroFalso:
    def __init__(self, ask): self.best_ask = ask


class ClobFalso:
    def __init__(self, ask=0.40): self.ask = ask
    async def order_book(self, token_id): return LibroFalso(self.ask)


class BrokerFalso:
    def __init__(self, ask=0.40):
        self.clob = ClobFalso(ask); self.ordenes = []
    def equity(self): return 500.0
    async def execute(self, oid, req):
        self.ordenes.append(oid)
        raise AssertionError("simular=True no debe mandar órdenes")


def armar(tmp_path, ask=0.40, juegos=None):
    conn = connect(tmp_path / "d.db")
    cfg = {"enabled": True, "min_edge": 0.08, "max_entry_price": 0.60,
           "min_trade_usdc": 25.0, "min_minutos_antes": 15}
    broker = BrokerFalso(ask)
    est = SportsValueStrategy(conn, MlbFalso(juegos or []), None, broker, cfg,
                              None, odds=None)
    return est, conn, broker


def _dentro_de(horas):
    return (datetime.now(timezone.utc)
            + timedelta(hours=horas)).isoformat().replace("+00:00", "Z")


def test_simular_nunca_manda_una_orden(tmp_path):
    """Lo crítico: el diagnóstico corre con dinero real en la cuenta."""
    juegos = [JuegoFalso("New York Yankees", "Tampa Bay Rays", _dentro_de(5),
                         PitcherFalso(), PitcherFalso())]
    est, conn, broker = armar(tmp_path, ask=0.30, juegos=juegos)
    with conn:                       # mercado que sí empareja
        conn.execute(
            """INSERT INTO markets (condition_id, question, category, active,
               clob_token_ids, raw, updated_at)
               VALUES ('0xg','New York Yankees vs Tampa Bay Rays','sports',1,
                       '["t0","t1"]',
                       '{"outcomes":"[\\"New York Yankees\\", \\"Tampa Bay Rays\\"]"}',
                       'x')""")
    traza = []
    import asyncio
    asyncio.run(est.scan_and_execute(traza=traza, simular=True))
    assert broker.ordenes == []
    assert any("APOSTARÍA" in t for t in traza), traza


def test_la_traza_dice_por_que_no_hay_ventaja(tmp_path):
    """El caso que teníamos delante: el modelo opina, pero la diferencia con
    el precio no llega al umbral. Sin este renglón parecía que la estrategia
    no se ejecutaba."""
    juegos = [JuegoFalso("New York Yankees", "Tampa Bay Rays", _dentro_de(5),
                         PitcherFalso(), PitcherFalso())]
    est, conn, broker = armar(tmp_path, ask=0.58, juegos=juegos)
    with conn:
        conn.execute(
            """INSERT INTO markets (condition_id, question, category, active,
               clob_token_ids, raw, updated_at)
               VALUES ('0xg','New York Yankees vs Tampa Bay Rays','sports',1,
                       '["t0","t1"]',
                       '{"outcomes":"[\\"New York Yankees\\", \\"Tampa Bay Rays\\"]"}',
                       'x')""")
    traza = []
    import asyncio
    asyncio.run(est.scan_and_execute(traza=traza, simular=True))
    assert any("por debajo de 8%" in t for t in traza), traza


def test_la_traza_avisa_del_partido_ya_empezado(tmp_path):
    juegos = [JuegoFalso("New York Yankees", "Tampa Bay Rays",
                         _dentro_de(-1), PitcherFalso(), PitcherFalso())]
    est, _, _ = armar(tmp_path, juegos=juegos)
    traza = []
    import asyncio
    asyncio.run(est.scan_and_execute(traza=traza, simular=True))
    assert any("ya empezó" in t for t in traza), traza


def test_la_traza_avisa_de_que_no_hay_mercado(tmp_path):
    juegos = [JuegoFalso("New York Yankees", "Tampa Bay Rays", _dentro_de(5),
                         PitcherFalso(), PitcherFalso())]
    est, _, _ = armar(tmp_path, juegos=juegos)     # base sin mercados
    traza = []
    import asyncio
    asyncio.run(est.scan_and_execute(traza=traza, simular=True))
    assert any("no tiene mercado" in t for t in traza), traza


def test_sin_traza_el_comportamiento_no_cambia(tmp_path):
    """La traza es opcional: el bot de verdad la llama sin ella."""
    est, _, _ = armar(tmp_path, juegos=[])
    import asyncio
    assert asyncio.run(est.scan_and_execute()) == []


def test_apagada_lo_dice(tmp_path):
    est, _, _ = armar(tmp_path, juegos=[])
    est.enabled = False
    traza = []
    import asyncio
    asyncio.run(est.scan_and_execute(traza=traza, simular=True))
    assert any("apagada" in t for t in traza)


# --- Un deporte caído no puede callar al otro -------------------------------

class MlbRoto:
    """La API de la MLB devolviendo 406, como en el droplet el 2026-08-27."""
    async def equipos(self, temporada):
        raise RuntimeError("Client error '406 Not Acceptable'")
    async def juegos(self, fecha):
        raise RuntimeError("Client error '406 Not Acceptable'")


class OddsFalso:
    enabled = True
    def __init__(self): self.consultada = []
    async def lineas(self, deporte):
        self.consultada.append(deporte)
        return []


def test_el_futbol_corre_aunque_la_mlb_este_caida(tmp_path):
    """El fallo real: `juegos()` lanzaba, el `except` devolvía [] y con eso
    se saltaba también el escaneo de fútbol, que no necesita la MLB para
    nada. Un 406 de una API dejó la estrategia entera muda durante días."""
    import asyncio

    conn = connect(tmp_path / "r.db")
    odds = OddsFalso()
    cfg = {"enabled": True, "min_edge": 0.08, "max_entry_price": 0.60,
           "min_trade_usdc": 25.0, "soccer_leagues": ["soccer_epl"]}
    est = SportsValueStrategy(conn, MlbRoto(), None, BrokerFalso(), cfg,
                              None, odds=odds)
    traza = []
    asyncio.run(est.scan_and_execute(traza=traza, simular=True))

    assert any("béisbol fuera de juego" in t for t in traza), traza
    # Lo que importa: se llegó a mirar el fútbol pese al fallo del béisbol.
    assert any("fútbol" in t for t in traza), traza


def test_la_mlb_caida_no_revienta_la_estrategia(tmp_path):
    """Sin traza tampoco puede lanzar: el scheduler la llama cada 20 min."""
    import asyncio

    conn = connect(tmp_path / "r2.db")
    est = SportsValueStrategy(conn, MlbRoto(), None, BrokerFalso(),
                              {"enabled": True}, None, odds=None)
    assert asyncio.run(est.scan_and_execute()) == []


# --- El ERA es un extra, no un requisito ------------------------------------

class HttpRoto:
    """statsapi respondiendo 406 solo en /people, como el droplet."""
    def __init__(self): self.pedidos = []
    async def get_json(self, url, params=None, headers=None):
        self.pedidos.append(url)
        if "people" in url:
            raise RuntimeError("Client error '406 Not Acceptable'")
        if "schedule" in url:
            return {"dates": [{"games": [{
                "gameDate": "2026-08-28T22:40:00Z",
                "teams": {
                    "away": {"team": {"name": "New York Yankees"},
                             "probablePitcher": {"id": 1, "fullName": "A"}},
                    "home": {"team": {"name": "Tampa Bay Rays"},
                             "probablePitcher": {"id": 2, "fullName": "B"}}}}]}]}
        return {}


def test_los_juegos_salen_aunque_falle_el_era():
    """El 406 del droplet se movió de /schedule a /people. El ERA del
    abridor solo afina el modelo —sin él se opina con la pitagórica—, así
    que no puede tumbar el calendario entero. Es el mismo fallo de esta
    semana un nivel más abajo: una API secundaria callando a toda una
    estrategia."""
    import asyncio

    from pmbot.data.sports import MlbClient

    http = HttpRoto()
    juegos = asyncio.run(MlbClient(http).juegos("2026-08-28"))

    assert len(juegos) == 1
    assert juegos[0].local == "Tampa Bay Rays"
    assert juegos[0].pitcher_local is not None
    assert juegos[0].pitcher_local.era is None      # sin ERA, pero existe


def test_el_era_se_pide_en_lotes_chicos():
    """13 IDs de una vez daban 406 y 5 pasaban."""
    import asyncio

    from pmbot.data.sports import MlbClient

    class HttpCuenta:
        def __init__(self): self.lotes = []
        async def get_json(self, url, params=None, headers=None):
            self.lotes.append((params or {}).get("personIds", "").split(","))
            return {"people": []}

    http = HttpCuenta()
    asyncio.run(MlbClient(http).eras([str(i) for i in range(13)]))
    assert len(http.lotes) == 3
    assert all(len(l) <= 6 for l in http.lotes), http.lotes

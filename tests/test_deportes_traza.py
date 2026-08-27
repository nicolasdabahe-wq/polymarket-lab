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

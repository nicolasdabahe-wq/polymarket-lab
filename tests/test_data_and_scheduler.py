from datetime import datetime, timezone

from pmbot.data.gamma import categorize_tags
from pmbot.scheduler.daily import next_daily_run


def tag(label: str) -> dict:
    return {"label": label}


def test_specific_tag_wins_over_general():
    # Los eventos de la Fed traen tags específicos primero y Politics después.
    tags = [tag("fomc"), tag("Economic Policy"), tag("Politics")]
    assert categorize_tags(tags) == "economy"


def test_politics_tag():
    assert categorize_tags([tag("Politics")]) == "politics"


def test_unknown_tags_are_other():
    assert categorize_tags([tag("Something Odd")]) == "other"
    assert categorize_tags([]) == "other"
    assert categorize_tags(None) == "other"


def test_next_daily_run_future_today():
    now = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
    run = next_daily_run(now, "11:00")
    assert run == datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)


def test_next_daily_run_rolls_to_tomorrow():
    now = datetime(2026, 8, 21, 12, 30, tzinfo=timezone.utc)
    run = next_daily_run(now, "11:00")
    assert run == datetime(2026, 8, 22, 11, 0, tzinfo=timezone.utc)


def test_next_daily_run_exact_time_goes_tomorrow():
    now = datetime(2026, 8, 21, 11, 0, tzinfo=timezone.utc)
    assert next_daily_run(now, "11:00").day == 22


# --- la cinta de trades (vigilancia global) ---

def test_tape_parsea_y_filtra_lo_que_importa(tmp_path):
    import asyncio

    from pmbot.db import connect
    from pmbot.smart_money.tape import TradeTape

    crudo = [
        # compra grande de una wallet vigilada -> señal
        {"proxyWallet": "0xAAA", "side": "BUY", "conditionId": "0xm1",
         "title": "Partido X", "outcome": "Yes", "outcomeIndex": 0,
         "size": "100", "price": "0.60", "timestamp": 200},
        # venta: no se copia
        {"proxyWallet": "0xAAA", "side": "SELL", "conditionId": "0xm2",
         "title": "Partido Y", "outcome": "No", "outcomeIndex": 1,
         "size": "50", "price": "0.40", "timestamp": 201},
        # wallet que no seguimos
        {"proxyWallet": "0xZZZ", "side": "BUY", "conditionId": "0xm3",
         "title": "Partido Z", "outcome": "Yes", "outcomeIndex": 0,
         "size": "80", "price": "0.30", "timestamp": 202},
        # anterior al watermark: ya la vimos
        {"proxyWallet": "0xAAA", "side": "BUY", "conditionId": "0xm4",
         "title": "Vieja", "outcome": "Yes", "outcomeIndex": 0,
         "size": "10", "price": "0.50", "timestamp": 50},
    ]

    class FakeHttp:
        async def get_json(self, url, params=None):
            return crudo

    conn = connect(tmp_path / "tape.db")
    tape = TradeTape(conn, FakeHttp(), min_usdc=150)

    # primera vuelta: solo fija el watermark, no inunda de señales viejas
    assert asyncio.run(tape.poll({"0xaaa"})) == []
    assert conn.execute(
        "SELECT COUNT(*) c FROM signals").fetchone()["c"] == 0

    # se retrocede el watermark para simular trades nuevos
    conn.execute("UPDATE paper_state SET value = '100' "
                 "WHERE key = 'tape_watermark'")
    conn.commit()
    nuevos = asyncio.run(tape.poll({"0xaaa"}))
    assert [t.condition_id for t in nuevos] == ["0xm1"]   # compra, vigilada, nueva
    assert conn.execute(
        "SELECT COUNT(*) c FROM signals WHERE kind='new_trade'"
    ).fetchone()["c"] == 1


def test_tape_calcula_el_usdc_del_trade(tmp_path):
    import asyncio

    from pmbot.db import connect
    from pmbot.smart_money.tape import TradeTape

    class FakeHttp:
        async def get_json(self, url, params=None):
            return [{"proxyWallet": "0xAAA", "side": "BUY", "conditionId": "0xm1",
                     "title": "T", "outcome": "Yes", "outcomeIndex": 0,
                     "size": "250", "price": "0.64", "timestamp": 900}]

    tape = TradeTape(connect(tmp_path / "t2.db"), FakeHttp(), min_usdc=150)
    [t] = asyncio.run(tape.fetch())
    assert t.usdc == 160.0 and t.wallet == "0xaaa"


def test_tape_registra_el_universo_de_candidatas(tmp_path):
    """Toda wallet que opere en grande entra a la cola de evaluación, sea
    conocida o no. Filtrar acá sería descartar sin mirar números."""
    import asyncio

    from pmbot.db import connect
    from pmbot.smart_money.tape import TradeTape

    crudo = [
        {"proxyWallet": "0xBALLENA", "side": "BUY", "conditionId": "0xm1",
         "title": "T", "outcome": "Yes", "outcomeIndex": 0,
         "size": "4000", "price": "0.50", "timestamp": 100},   # $2000
        {"proxyWallet": "0xCHICA", "side": "BUY", "conditionId": "0xm2",
         "title": "T", "outcome": "Yes", "outcomeIndex": 0,
         "size": "200", "price": "0.50", "timestamp": 101},    # $100
        {"proxyWallet": "0xBALLENA", "side": "SELL", "conditionId": "0xm3",
         "title": "T", "outcome": "No", "outcomeIndex": 1,
         "size": "2000", "price": "0.60", "timestamp": 102},   # $1200
    ]

    class FakeHttp:
        async def get_json(self, url, params=None):
            return crudo

    conn = connect(tmp_path / "u.db")
    tape = TradeTape(conn, FakeHttp(), min_usdc=150, candidate_min_usdc=500)
    trades = asyncio.run(tape.fetch())
    tape.registrar_candidatas(trades, 500)

    filas = {r["wallet"]: r for r in conn.execute(
        "SELECT * FROM wallet_candidates")}
    assert set(filas) == {"0xballena"}          # la de $100 no llega al corte
    assert filas["0xballena"]["trades_grandes"] == 2   # compra y venta cuentan
    assert filas["0xballena"]["max_usdc"] == 2000.0
    assert filas["0xballena"]["fuente"] == "cinta"


def test_tape_acumula_apariciones_entre_corridas(tmp_path):
    import asyncio

    from pmbot.db import connect
    from pmbot.smart_money.tape import TradeTape

    class FakeHttp:
        async def get_json(self, url, params=None):
            return [{"proxyWallet": "0xW", "side": "BUY", "conditionId": "0xm",
                     "title": "T", "outcome": "Yes", "outcomeIndex": 0,
                     "size": "2000", "price": "0.50", "timestamp": 1}]

    conn = connect(tmp_path / "u2.db")
    tape = TradeTape(conn, FakeHttp(), min_usdc=150, candidate_min_usdc=500)
    for _ in range(3):
        tape.registrar_candidatas(asyncio.run(tape.fetch()), 500)
    fila = conn.execute("SELECT * FROM wallet_candidates").fetchone()
    assert fila["trades_grandes"] == 3


def test_los_esports_tienen_su_propia_categoria():
    """Bug del 2026-08-22 al 24: 'esport' estaba dentro del grupo de deportes,
    así que estos mercados se clasificaban como 'sports' y el freno que los
    bloqueaba —que comprueba category=='esports'— nunca llegó a actuar.
    Costó $120.56: siete posiciones, siete perdidas.
    """
    from pmbot.data.gamma import categorize_tags

    for etiqueta in ("Esports", "LoL", "League of Legends", "Counter-Strike",
                     "CS2", "Valorant", "Dota 2", "Overwatch"):
        assert categorize_tags([{"label": etiqueta}]) == "esports", etiqueta


def test_los_deportes_de_verdad_siguen_siendo_deportes():
    """El cambio de arriba no puede llevarse por delante al béisbol."""
    from pmbot.data.gamma import categorize_tags

    for etiqueta in ("MLB", "NBA", "NFL", "Soccer", "Tennis", "UFC", "Golf"):
        assert categorize_tags([{"label": etiqueta}]) == "sports", etiqueta


def test_un_evento_de_esports_no_se_cuela_como_otro():
    """Los tags con el nombre del juego caían en 'other' y escapaban incluso
    de los filtros de deportes."""
    from pmbot.data.gamma import categorize_tags

    assert categorize_tags([{"label": "LoL"}, {"label": "Sports"}]) == "esports"

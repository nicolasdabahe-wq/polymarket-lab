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

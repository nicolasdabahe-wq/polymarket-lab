"""Tests del broker real con el cliente CLOB mockeado (sin red, sin claves)."""
import asyncio

import pytest

from pmbot.db import connect
from pmbot.execution.live import (LiveBroker, parse_post_response,
                                  round_to_tick)
from pmbot.risk import OrderRequest, RiskManager


# --- funciones puras ---

def test_round_to_tick_buy_never_rounds_up():
    assert round_to_tick(0.567, 0.01, "BUY") == pytest.approx(0.56)
    assert round_to_tick(0.56, 0.01, "BUY") == pytest.approx(0.56)


def test_round_to_tick_sell_never_rounds_down():
    assert round_to_tick(0.561, 0.01, "SELL") == pytest.approx(0.57)


def test_round_to_tick_fine_tick():
    assert round_to_tick(0.5678, 0.001, "BUY") == pytest.approx(0.567)


def test_parse_buy_response():
    resp = {"success": True, "status": "matched",
            "makingAmount": "10.0", "takingAmount": "20.0"}
    shares, price, err = parse_post_response(resp, "BUY", 20, 0.55)
    assert shares == 20 and price == pytest.approx(0.50) and not err


def test_parse_sell_response_swaps_amounts():
    resp = {"success": True, "status": "matched",
            "makingAmount": "20.0", "takingAmount": "9.0"}
    shares, price, err = parse_post_response(resp, "SELL", 20, 0.40)
    assert shares == 20 and price == pytest.approx(0.45)


def test_parse_failed_response():
    shares, _, err = parse_post_response(
        {"success": False, "errorMsg": "not enough balance"}, "BUY", 20, 0.5)
    assert shares == 0 and "balance" in err


def test_parse_unfilled_fak():
    resp = {"success": True, "status": "live", "makingAmount": "0",
            "takingAmount": "0"}
    shares, _, err = parse_post_response(resp, "BUY", 20, 0.5)
    assert shares == 0 and "sin fill" in err


# --- LiveBroker con cliente mockeado ---

class FakeSignedOrder:
    pass


class FakeClobClient:
    """Simula py_clob_client.ClobClient: fill total a 0.50."""
    def __init__(self):
        self.posted = []

    def get_tick_size(self, token_id):
        return 0.01

    def create_order(self, args):
        self.args = args
        return FakeSignedOrder()

    def post_order(self, order, order_type):
        self.posted.append((self.args, order_type))
        usdc = self.args.size * 0.50
        return {"success": True, "status": "matched",
                "makingAmount": str(usdc), "takingAmount": str(self.args.size)}

    def get_address(self):
        return "0xsigner"

    def get_balance_allowance(self, params=None):
        return {"balance": "100000000", "allowance": "1"}  # 100 USDC


def make_live(tmp_path) -> tuple[LiveBroker, FakeClobClient]:
    conn = connect(tmp_path / "live.db")
    risk = RiskManager(conn, {"min_order_usdc": 1.0}, tmp_path)
    broker = LiveBroker(conn, clob=None, risk=risk,
                        capital_cfg={"paper_starting_usdc": 270},
                        exec_cfg={}, private_key="0xkey",
                        proxy_address="0xfunder")
    fake = FakeClobClient()
    broker._client = fake  # inyectar el mock: no hay red ni firma real
    return broker, fake


def req(**kw):
    # 15 shares a 0.55 = $8.25: entra en el 10% por mercado del saldo de $100.
    base = dict(strategy="copy_trading", condition_id="0xm1",
                category="politics", token_id="t1", outcome="Yes",
                outcome_index=0, side="BUY", size=15.0, price=0.55,
                reason="test", strategy_budget_pct=0.5)
    base.update(kw)
    return OrderRequest(**base)


def test_live_buy_places_fak_and_records(tmp_path):
    broker, fake = make_live(tmp_path)
    fill = asyncio.run(broker.execute("o1", req()))
    assert fill.status == "FILLED"
    assert fill.price == pytest.approx(0.50)
    [(args, order_type)] = fake.posted
    assert str(order_type) in ("FAK", "OrderType.FAK")
    assert args.price == pytest.approx(0.55)  # ya en tick
    [pos] = broker.positions()
    assert pos["size"] == pytest.approx(15)


def test_live_cash_reads_real_balance(tmp_path):
    broker, _ = make_live(tmp_path)
    assert broker.cash == pytest.approx(100.0)


def test_live_idempotency(tmp_path):
    broker, fake = make_live(tmp_path)
    asyncio.run(broker.execute("o1", req()))
    dup = asyncio.run(broker.execute("o1", req()))
    assert dup.status == "DUPLICATE"
    assert len(fake.posted) == 1  # la segunda nunca llegó al CLOB


def test_live_risk_rejection_blocks_clob_call(tmp_path):
    broker, fake = make_live(tmp_path)
    # 100 USDC de saldo: una orden de 200 USDC no pasa el check de cash.
    fill = asyncio.run(broker.execute("o1", req(size=400, price=0.5)))
    assert fill.status == "REJECTED"
    assert fake.posted == []  # ninguna orden salió al exchange


def test_live_clob_error_recorded_not_raised(tmp_path):
    broker, fake = make_live(tmp_path)

    def boom(order, order_type):
        raise RuntimeError("CLOB caído")

    fake.post_order = boom
    fill = asyncio.run(broker.execute("o1", req()))
    assert fill.status == "REJECTED" and "CLOB" in fill.detail

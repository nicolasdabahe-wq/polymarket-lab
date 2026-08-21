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
    """Simula py_clob_client_v2.ClobClient: fill total a 0.50."""
    def __init__(self):
        self.posted = []

    def get_tick_size(self, token_id):
        return "0.01"  # V2 devuelve string

    def get_neg_risk(self, token_id):
        return False

    def create_order(self, args, options=None):
        self.args = args
        self.options = options
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


# --- cuantización de tamaño (el CLOB exige montos con <=2 decimales) ---

from pmbot.execution.live import quantize_size, tick_decimals


def test_tick_decimals():
    assert tick_decimals(0.01) == 2
    assert tick_decimals(0.001) == 3
    assert tick_decimals(0.0001) == 4


def test_quantize_with_cent_tick_gives_whole_shares():
    # 19.3 x 0.51 = 9.843 -> rechazado por el CLOB; 19 x 0.51 = 9.69 -> OK
    size = quantize_size(19.3, 0.01, 5.0)
    assert size == 19.0
    assert round(size * 0.51, 10) == 9.69


def test_quantize_with_milli_tick_needs_multiples_of_ten():
    size = quantize_size(97.0, 0.001, 5.0)
    assert size == 90.0
    assert round(size * 0.512, 10) == 46.08   # 2 decimales exactos


def test_quantize_below_minimum_returns_zero():
    assert quantize_size(4.9, 0.01, 5.0) == 0.0
    assert quantize_size(9.0, 0.001, 5.0) == 0.0  # 9 -> 0 tras cuantizar


def test_quantized_amounts_always_two_decimals():
    for size, tick, price in ((19.3, 0.01, 0.51), (6.8, 0.01, 0.99),
                              (7.84, 0.01, 0.96), (123.4, 0.001, 0.425)):
        q = quantize_size(size, tick, 5.0)
        if q <= 0:
            continue
        amount = q * price
        assert abs(amount * 100 - round(amount * 100)) < 1e-6, (size, tick, price)


# --- reconciliación con la blockchain ---

class FakeDataApi:
    """Devuelve una posición on-chain fija (simula un fill tardío)."""
    def __init__(self, positions):
        self._positions = positions

    async def positions(self, wallet, limit=50):
        return self._positions


def onchain_pos(**kw):
    from pmbot.data.data_api import Position
    base = dict(wallet="0xfunder", condition_id="0xm1", title="Fritz vs Naka",
                outcome="Yes", outcome_index=0, size=15.0, avg_price=0.54,
                cur_price=0.001, current_value=0.01, cash_pnl=-8.0,
                percent_pnl=-1.0, redeemable=False)
    base.update(kw)
    return Position(**base)


def test_reconcile_adopts_late_fill(tmp_path):
    broker, fake = make_live(tmp_path)
    # El bot intentó comprar (queda la orden) pero la dio por muerta.
    fake.post_order = lambda order, order_type: {
        "success": True, "status": "live", "makingAmount": "0",
        "takingAmount": "0"}
    asyncio.run(broker.execute("o1", req()))
    assert broker.positions() == []
    notes = asyncio.run(broker.reconcile_positions(FakeDataApi([onchain_pos()])))
    assert notes and broker.positions()[0]["size"] == pytest.approx(15.0)


def test_reconcile_ignores_foreign_positions(tmp_path):
    broker, _ = make_live(tmp_path)
    api = FakeDataApi([onchain_pos(condition_id="0xajeno")])
    assert asyncio.run(broker.reconcile_positions(api)) == []
    assert broker.positions() == []


def test_reconcile_does_not_readopt_settled_position(tmp_path):
    """Tras liquidar, los tokens siguen on-chain hasta el Claim del dueño:
    la posición NO debe volver a adoptarse en cada reconciliación."""
    broker, _ = make_live(tmp_path)
    asyncio.run(broker.execute("o1", req()))
    [pos] = broker.positions()
    broker.redeem(pos, 0.0, "de facto resuelto")
    assert broker.positions() == []
    notes = asyncio.run(broker.reconcile_positions(FakeDataApi([onchain_pos()])))
    assert notes == [] and broker.positions() == []


# --- convivencia con las apuestas manuales del dueño ---

def test_manual_position_counts_in_equity(tmp_path):
    """Una apuesta del dueño baja el saldo; si no se contara su valor, el
    bot la leería como pérdida y podría dispararse el stop diario solo."""
    broker, _ = make_live(tmp_path)
    assert broker.equity() == pytest.approx(100.0)   # solo saldo
    api = FakeDataApi([onchain_pos(condition_id="0xajeno", size=50.0,
                                   cur_price=0.50)])
    asyncio.run(broker.reconcile_positions(api))
    assert broker.positions() == []                  # no la gestiona
    assert broker.equity() == pytest.approx(125.0)   # pero sí la cuenta


def test_manual_add_on_bot_market_is_not_adopted(tmp_path):
    """El dueño compra en el mismo mercado que el bot: el excedente sigue
    siendo suyo, el bot no puede venderlo al salir de la copia."""
    broker, _ = make_live(tmp_path)
    asyncio.run(broker.execute("o1", req()))          # el bot compra 15
    api = FakeDataApi([onchain_pos(size=40.0, cur_price=0.50)])  # +25 del dueño
    asyncio.run(broker.reconcile_positions(api))
    [pos] = broker.positions()
    assert pos["size"] == pytest.approx(15.0)
    # el equity sí incluye las 25 shares del dueño (25 x 0.50)
    assert broker.equity() == pytest.approx(100.0 + 15 * 0.50 + 12.5)


def test_risk_rejected_order_gives_no_claim_on_shares(tmp_path):
    """Una orden que risk/ frenó nunca salió al exchange: no puede
    justificar la adopción de shares que compró el dueño."""
    broker, fake = make_live(tmp_path)
    fill = asyncio.run(broker.execute("o1", req(size=400, price=0.5)))
    assert fill.status == "REJECTED" and fake.posted == []
    api = FakeDataApi([onchain_pos(size=30.0, cur_price=0.50)])
    assert asyncio.run(broker.reconcile_positions(api)) == []
    assert broker.positions() == []

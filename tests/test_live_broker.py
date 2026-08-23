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


def test_el_monto_en_usdc_siempre_cae_en_centavos_enteros():
    """La regla que el CLOB aplica a las compras a mercado: el monto en USDC
    admite 2 decimales como máximo ('invalid amounts, the market buy orders
    maker amount supports a max accuracy of 2 decimals'). Rechazó 18 órdenes
    reales antes de existir quantize_size. Se reproduce la aritmética exacta
    de la librería sobre todo el rango de precios y tamaños que usamos.
    """
    from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG
    from py_clob_client_v2.order_builder.helpers import (
        decimal_places, round_down, round_normal, round_up, to_token_decimals)

    from pmbot.execution.live import quantize_size, round_to_tick, tick_decimals

    def maker_amount_wei(size: float, price: float, tick_str: str) -> int:
        rc = ROUNDING_CONFIG[tick_str]
        raw_price = round_normal(price, rc.price)
        taker = round_down(size, rc.size)
        maker = taker * raw_price
        if decimal_places(maker) > rc.amount:
            maker = round_up(maker, rc.amount + 4)
            if decimal_places(maker) > rc.amount:
                maker = round_down(maker, rc.amount)
        return to_token_decimals(maker)

    probadas = 0
    for tick_str in ["0.01", "0.001", "0.005", "0.0001"]:
        tick = float(tick_str)
        for usdc in (12.0, 13.2, 15.0, 18.0, 23.5):
            for milesimas in range(30, 760, 7):
                price = round(round_to_tick(milesimas / 1000, tick, "BUY"),
                              tick_decimals(tick))
                if price <= 0:
                    continue
                size = quantize_size(usdc / price, tick, 5.0)
                if size <= 0:
                    continue
                probadas += 1
                wei = maker_amount_wei(size, price, tick_str)
                # 1 centavo = 10.000 en unidades de 6 decimales.
                assert wei % 10_000 == 0, (
                    f"tick {tick_str}, {size} shares @ {price} deja "
                    f"{wei / 1e6:.6f} USDC, que el CLOB rechaza")
    assert probadas > 1000, f"solo se probaron {probadas} combinaciones"


def test_una_posicion_chica_siempre_se_puede_vender():
    """Las tres posiciones que quedaron atrapadas el 2026-08-22: la regla de
    la COMPRA (múltiplos de 10 con tick 0.001) se les aplicaba también al
    vender y las cuantizaba a cero, así que no había forma de salir de ellas
    nunca. Vendiendo, el 'maker amount' son las shares y basta con 2
    decimales."""
    from pmbot.execution.live import quantize_size

    for shares, tick in [(2.93, 0.001), (0.52, 0.01), (3.68, 0.001)]:
        assert quantize_size(shares, tick, 5.0, "SELL") == shares, (shares, tick)
        # comprando esa misma cantidad sí se rechaza: es menos del mínimo
        assert quantize_size(shares, tick, 5.0, "BUY") == 0.0


def test_vender_recorta_a_dos_decimales_de_shares():
    from pmbot.execution.live import quantize_size

    assert quantize_size(41.5678, 0.001, 5.0, "SELL") == 41.56
    assert quantize_size(7.0, 0.001, 5.0, "SELL") == 7.0


def test_comprar_no_cambia_de_comportamiento():
    """La regla de la compra se queda igual: es la que evita que el CLOB
    rechace por 'invalid amounts'."""
    from pmbot.execution.live import quantize_size

    assert quantize_size(19.3, 0.01, 5.0) == quantize_size(19.3, 0.01, 5.0, "BUY")
    assert quantize_size(97.0, 0.001, 5.0, "BUY") == 90.0


def _fila(conn, oid):
    return conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()


def test_un_rechazo_que_no_salio_al_exchange_se_puede_reintentar(tmp_path):
    """El 2026-08-22 tres ventas quedaron bloqueadas: el intento fallido se
    guardaba y el guard de duplicados lo trataba como ya ejecutado, así que
    la orden mala impedía su propio arreglo hasta el día siguiente."""
    from pmbot.db import connect
    from pmbot.execution.paper import Fill, PaperBroker
    from pmbot.risk import OrderRequest, RiskManager

    conn = connect(tmp_path / "r.db")
    b = PaperBroker(conn, None,
                    RiskManager(conn, {"min_order_usdc": 1.0}, tmp_path),
                    {"paper_starting_usdc": 200.0})
    req = OrderRequest(strategy="s", condition_id="0xc", category="crypto",
                       token_id="t", outcome="Yes", outcome_index=0,
                       side="SELL", size=2.93, price=0.35, reason="salir")
    b._record("liberar:0xc:0", req, Fill("liberar:0xc:0", "REJECTED",
                                         detail="tamaño < mínimo"))
    assert _fila(conn, "liberar:0xc:0")["status"] == "REJECTED"

    # el reintento entra y la fila queda con el resultado nuevo
    b._record("liberar:0xc:0", req,
              Fill("liberar:0xc:0", "FILLED", 2.93, 0.35, 1.03, sent=True))
    fila = _fila(conn, "liberar:0xc:0")
    assert fila["status"] == "FILLED" and fila["sent"] == 1
    assert fila["reject_reason"] is None


def test_una_orden_que_llego_al_exchange_no_se_pisa_jamas(tmp_path):
    """La garantía que sostiene todo lo anterior: si la orden salió, repetirla
    podría ejecutarla dos veces con dinero real."""
    from pmbot.db import connect
    from pmbot.execution.paper import Fill, PaperBroker
    from pmbot.risk import OrderRequest, RiskManager

    conn = connect(tmp_path / "r2.db")
    b = PaperBroker(conn, None,
                    RiskManager(conn, {"min_order_usdc": 1.0}, tmp_path),
                    {"paper_starting_usdc": 200.0})
    req = OrderRequest(strategy="s", condition_id="0xc", category="crypto",
                       token_id="t", outcome="Yes", outcome_index=0,
                       side="BUY", size=40.0, price=0.50, reason="entrar")
    b._record("copy:0xw:0xc:0", req,
              Fill("copy:0xw:0xc:0", "FILLED", 40.0, 0.50, 20.0, sent=True))
    # un segundo registro con datos distintos NO debe alterar la fila
    b._record("copy:0xw:0xc:0", req,
              Fill("copy:0xw:0xc:0", "REJECTED", detail="lo que sea"))
    fila = _fila(conn, "copy:0xw:0xc:0")
    assert fila["status"] == "FILLED" and fila["fill_size"] == 40.0


def test_un_rechazo_que_si_salio_al_exchange_tampoco_se_pisa(tmp_path):
    """NO_LIQUIDITY llega al CLOB: pudo haber llenado parcialmente."""
    from pmbot.db import connect
    from pmbot.execution.paper import Fill, PaperBroker
    from pmbot.risk import OrderRequest, RiskManager

    conn = connect(tmp_path / "r3.db")
    b = PaperBroker(conn, None,
                    RiskManager(conn, {"min_order_usdc": 1.0}, tmp_path),
                    {"paper_starting_usdc": 200.0})
    req = OrderRequest(strategy="s", condition_id="0xc", category="crypto",
                       token_id="t", outcome="Yes", outcome_index=0,
                       side="BUY", size=40.0, price=0.50, reason="entrar")
    b._record("x:1", req, Fill("x:1", "NO_LIQUIDITY", detail="sin book",
                               sent=True))
    b._record("x:1", req, Fill("x:1", "FILLED", 40.0, 0.50, 20.0, sent=True))
    assert _fila(conn, "x:1")["status"] == "NO_LIQUIDITY"


def test_lee_el_saldo_real_del_error_del_clob():
    """El exchange dice cuántas shares tenemos de verdad dentro de su propio
    mensaje de error. Es el dato autoritativo: más fiable que nuestros libros.
    Los tres casos son los reales del 2026-08-22."""
    from pmbot.execution.live import saldo_real_del_error

    msg = ("PolyApiException[status_code=400, error_message={'error': 'not "
           "enough balance / allowance: the balance is not enough -> "
           "balance: 1033, order amount: 2930000'}]")
    assert saldo_real_del_error(msg) == pytest.approx(0.001033)
    assert saldo_real_del_error(
        "not enough balance -> balance: 8831, order amount: 3670000"
    ) == pytest.approx(0.008831)


def test_no_inventa_un_saldo_si_el_mensaje_es_otro():
    """Ante cualquier duda, None: corregir la contabilidad con un número mal
    leído sería peor que no corregirla."""
    from pmbot.execution.live import saldo_real_del_error

    assert saldo_real_del_error("invalid amounts, max accuracy 2 decimals") is None
    assert saldo_real_del_error("not enough balance / allowance") is None
    assert saldo_real_del_error("") is None

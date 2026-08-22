from pmbot.risk import Limits, OrderRequest, PortfolioState, evaluate

LIMITS = Limits(max_pct_per_market=0.10, max_pct_per_category=0.40,
                max_pct_per_copied_wallet=0.15, max_total_exposure_pct=0.80,
                daily_stop_loss_pct=0.05, min_order_usdc=1.0)


def state(**kw) -> PortfolioState:
    base = dict(equity=500.0, cash=400.0, day_start_equity=500.0,
                exposure_total=100.0, exposure_by_market={},
                exposure_by_category={}, exposure_by_wallet={},
                exposure_by_strategy={})
    base.update(kw)
    return PortfolioState(**base)


def order(**kw) -> OrderRequest:
    base = dict(strategy="copy_trading", condition_id="0xm1",
                category="politics", token_id="t1", outcome="Yes",
                outcome_index=0, side="BUY", size=20.0, price=0.50,
                reason="test", strategy_budget_pct=0.50)
    base.update(kw)
    return OrderRequest(**base)


def test_ok_order_approved():
    assert evaluate(order(), state(), LIMITS).approved


def test_sells_always_approved():
    # Vender reduce riesgo: pasa aunque el stop diario esté activado.
    s = state(equity=100.0, day_start_equity=500.0)
    assert evaluate(order(side="SELL"), s, LIMITS).approved


def test_min_order_size():
    d = evaluate(order(size=1.0, price=0.5), state(), LIMITS)
    assert not d.approved and "chica" in d.reason


def test_insufficient_cash():
    d = evaluate(order(size=1000, price=0.9), state(cash=10.0), LIMITS)
    assert not d.approved


def test_market_limit():
    # 10% de 500 = 50; ya hay 45 en el mercado, comprar 10 más lo pasa.
    s = state(exposure_by_market={"0xm1": 45.0})
    d = evaluate(order(size=20, price=0.5), s, LIMITS)
    assert not d.approved and "mercado" in d.reason


def test_category_limit():
    s = state(exposure_by_category={"politics": 195.0})
    d = evaluate(order(size=20, price=0.5), s, LIMITS)  # 195+10 > 200
    assert not d.approved and "categoría" in d.reason


def test_copied_wallet_limit():
    s = state(exposure_by_wallet={"0xw": 70.0})
    d = evaluate(order(copied_wallet="0xw", size=20, price=0.5), s, LIMITS)
    assert not d.approved and "wallet" in d.reason


def test_strategy_budget():
    s = state(exposure_by_strategy={"copy_trading": 245.0})
    d = evaluate(order(size=20, price=0.5), s, LIMITS)  # 245+10 > 50%*500
    assert not d.approved and "estrategia" in d.reason


def test_total_exposure_limit():
    s = state(exposure_total=395.0)
    d = evaluate(order(size=20, price=0.5), s, LIMITS)  # 395+10 > 400
    assert not d.approved and "total" in d.reason


def test_daily_stop_blocks_buys():
    # Cayó 6% desde el inicio del día (> 5% de stop): no más compras.
    s = state(equity=470.0, day_start_equity=500.0)
    d = evaluate(order(), s, LIMITS)
    assert not d.approved and "stop" in d.reason


def test_zero_equity_blocks():
    assert not evaluate(order(), state(equity=0.0), LIMITS).approved


# --- freno total (drawdown desde el capital inicial) ---

LIMITS_DD = Limits(max_pct_per_market=0.20, max_pct_per_category=0.50,
                   max_pct_per_copied_wallet=0.25, max_total_exposure_pct=0.85,
                   daily_stop_loss_pct=0.10, min_order_usdc=10.0,
                   max_drawdown_pct=0.25)


def test_total_drawdown_blocks_buys():
    # Capital inicial 278; equity 200 = -28% -> freno total
    s = state(equity=200.0, cash=200.0, day_start_equity=205.0,
              starting_equity=278.0, exposure_total=0.0)
    d = evaluate(order(size=25, price=0.5), s, LIMITS_DD)
    assert not d.approved and "FRENO TOTAL" in d.reason


def test_within_drawdown_allows_buys():
    s = state(equity=250.0, cash=250.0, day_start_equity=255.0,
              starting_equity=278.0, exposure_total=0.0)
    assert evaluate(order(size=25, price=0.5), s, LIMITS_DD).approved


def test_drawdown_never_blocks_sells():
    s = state(equity=100.0, cash=100.0, day_start_equity=250.0,
              starting_equity=278.0, exposure_total=0.0)
    assert evaluate(order(side="SELL"), s, LIMITS_DD).approved


def test_min_order_floor_ten_dollars():
    s = state(starting_equity=278.0)
    d = evaluate(order(size=10, price=0.5), s, LIMITS_DD)   # $5
    assert not d.approved and "chica" in d.reason
    assert evaluate(order(size=24, price=0.5), s, LIMITS_DD).approved  # $12


# --- los dos lados del mismo evento (casos reales del 2026-08-22) ---

def test_no_pagar_mas_de_un_dolar_entre_los_dos_lados():
    """Musetti: compró Musetti a 0.50 y después Tiafoe a 0.69. El par cuesta
    1.19 y solo paga 1.00: pierde gane quien gane."""
    s = state(held_outcomes={"0xm1": {0: 0.50}})
    d = evaluate(order(outcome_index=1, outcome="No", price=0.69), s, LIMITS)
    assert not d.approved and "el par cuesta" in d.reason


def test_el_otro_lado_si_asegura_ganancia_pasa():
    # Musetti a 0.50 y el rival se desploma a 0.25: el par cuesta 0.75 y
    # paga 1.00 gane quien gane. Eso hay que dejarlo entrar.
    s = state(held_outcomes={"0xm1": {0: 0.50}})
    assert evaluate(order(outcome_index=1, outcome="No", price=0.25),
                    s, LIMITS).approved


def test_recargar_el_mismo_lado_esta_permitido():
    """Convicción, no contradicción: entrar a 0.50 y sumar a 0.60 (o a 0.20
    si cae) es una tesis, y el dueño la quiere habilitada."""
    s = state(held_outcomes={"0xm1": {0: 0.50}})
    assert evaluate(order(outcome_index=0, price=0.60), s, LIMITS).approved
    assert evaluate(order(outcome_index=0, price=0.20), s, LIMITS).approved


def test_primera_entrada_en_el_mercado_pasa():
    assert evaluate(order(), state(held_outcomes={"0xotro": {0: 0.4}}),
                    LIMITS).approved


def test_vender_no_se_bloquea_por_tener_posicion():
    s = state(held_outcomes={"0xm1": {0: 0.50}})
    assert evaluate(order(side="SELL"), s, LIMITS).approved


# --- velocidad del capital (pedido del dueño, 2026-08-22) ---

LIMITS_VEL = Limits(max_pct_per_market=0.20, max_pct_per_category=0.50,
                    max_pct_per_copied_wallet=0.25, max_total_exposure_pct=0.85,
                    daily_stop_loss_pct=0.20, min_order_usdc=10.0,
                    max_days_to_resolution=21, slow_days=7, max_pct_slow=0.20)


def test_rechaza_lo_que_tarda_demasiado():
    """La alcaldía de LA se decide en noviembre: tres meses de capital
    secuestrado con una cuenta de $237."""
    d = evaluate(order(size=30, price=0.5, days_to_resolution=85),
                 state(), LIMITS_VEL)
    assert not d.approved and "parado" in d.reason


def test_acepta_lo_que_se_resuelve_pronto():
    # Un partido de hoy o un mercado de cripto de esta semana.
    assert evaluate(order(size=30, price=0.5, days_to_resolution=0.4),
                    state(), LIMITS_VEL).approved
    assert evaluate(order(size=30, price=0.5, days_to_resolution=6),
                    state(), LIMITS_VEL).approved


def test_limita_cuanto_capital_puede_estar_dormido():
    # 20% de $500 = $100 en mercados lentos; ya hay $95, esta suma $15.
    s = state(exposure_slow=95.0)
    d = evaluate(order(size=30, price=0.5, days_to_resolution=14), s, LIMITS_VEL)
    assert not d.approved and "lentos" in d.reason


def test_una_apuesta_rapida_no_toca_el_cupo_de_lentas():
    s = state(exposure_slow=95.0)
    assert evaluate(order(size=30, price=0.5, days_to_resolution=1),
                    s, LIMITS_VEL).approved


def test_sin_fecha_conocida_no_se_bloquea():
    # Mejor no frenar por ignorancia: si no sabemos cuándo resuelve, pasa.
    assert evaluate(order(size=30, price=0.5, days_to_resolution=None),
                    state(exposure_slow=95.0), LIMITS_VEL).approved


def test_vender_una_lenta_siempre_se_puede():
    # Salir de una posición dormida libera capital: nunca se bloquea.
    s = state(exposure_slow=200.0)
    assert evaluate(order(side="SELL", days_to_resolution=90),
                    s, LIMITS_VEL).approved


def test_mercado_en_limbo_no_se_compra():
    """Nordone: endDate vencido hace 11 días, sin resolución (segunda
    vuelta). El bot la recompró por consenso; los días negativos pasaban
    todos los filtros de velocidad."""
    d = evaluate(order(size=30, price=0.5, days_to_resolution=-11),
                 state(), LIMITS_VEL)
    assert not d.approved and "limbo" in d.reason


def test_recien_vencido_no_cuenta_como_limbo():
    # Un partido que terminó hace horas aún no liquidado no es limbo.
    assert evaluate(order(side="SELL", days_to_resolution=-0.3),
                    state(), LIMITS_VEL).approved

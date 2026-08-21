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

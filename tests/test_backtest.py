import pytest

from pmbot.backtest import simulate_copy


def trade(ts, side, price, usdc=1000, cid="0xm1", idx=0, title="t"):
    return {"ts": ts, "side": side, "price": price, "usdc": usdc,
            "condition_id": cid, "outcome_index": idx, "title": title}


def test_follow_exit_uses_wallet_sell_price():
    trades = [trade(1, "BUY", 0.40), trade(2, "SELL", 0.60)]
    [t] = simulate_copy(trades, {}, stake_usdc=10, min_copy_usdc=500,
                        slippage=0.0)
    assert t.status == "closed_follow"
    assert t.pnl == pytest.approx((10 / 0.40) * (0.60 - 0.40))


def test_slippage_penalizes_both_ends():
    trades = [trade(1, "BUY", 0.50), trade(2, "SELL", 0.50)]
    [t] = simulate_copy(trades, {}, 10, 500, slippage=0.02)
    # entramos a 0.51, salimos a 0.49: perdemos aunque la wallet empató
    assert t.pnl < 0


def test_resolution_pays_zero_or_one():
    trades = [trade(1, "BUY", 0.40)]
    outcomes = {"0xm1": {"closed": True, "outcome_prices": [1.0, 0.0]}}
    [t] = simulate_copy(trades, outcomes, 10, 500, slippage=0.0)
    assert t.status == "resolved"
    assert t.pnl == pytest.approx((10 / 0.40) * (1.0 - 0.40))


def test_losing_resolution():
    trades = [trade(1, "BUY", 0.40)]
    outcomes = {"0xm1": {"closed": True, "outcome_prices": [0.0, 1.0]}}
    [t] = simulate_copy(trades, outcomes, 10, 500, slippage=0.0)
    assert t.pnl == pytest.approx(-10.0)  # perdemos el stake completo


def test_de_facto_resolved_counts_as_resolved():
    # Gamma a veces demora el flag closed; precio 0.9995 = ya se sabe el
    # resultado y debe contarse como realizado.
    trades = [trade(1, "BUY", 0.40)]
    outcomes = {"0xm1": {"closed": False, "outcome_prices": [0.9995, 0.0005]}}
    [t] = simulate_copy(trades, outcomes, 10, 500, slippage=0.0)
    assert t.status == "resolved"
    assert t.exit_price == 1.0


def test_open_position_marks_to_market():
    trades = [trade(1, "BUY", 0.40)]
    outcomes = {"0xm1": {"closed": False, "outcome_prices": [0.55, 0.45]}}
    [t] = simulate_copy(trades, outcomes, 10, 500, slippage=0.0)
    assert t.status == "open"
    assert t.pnl == pytest.approx((10 / 0.40) * (0.55 - 0.40))


def test_small_trades_not_copied():
    trades = [trade(1, "BUY", 0.40, usdc=100)]
    assert simulate_copy(trades, {}, 10, 500) == []


def test_no_double_copy_same_market():
    trades = [trade(1, "BUY", 0.40), trade(2, "BUY", 0.45)]
    result = simulate_copy(trades, {}, 10, 500)
    assert len(result) == 1


def test_high_price_entries_skipped():
    trades = [trade(1, "BUY", 0.97)]
    assert simulate_copy(trades, {}, 10, 500) == []


def test_sell_without_open_position_ignored():
    trades = [trade(1, "SELL", 0.40)]
    assert simulate_copy(trades, {}, 10, 500) == []


def test_different_outcomes_tracked_separately():
    trades = [trade(1, "BUY", 0.40, idx=0), trade(2, "BUY", 0.30, idx=1),
              trade(3, "SELL", 0.50, idx=0)]
    result = simulate_copy(trades, {}, 10, 500, slippage=0.0)
    by_idx = {t.outcome_index: t for t in result}
    assert by_idx[0].status == "closed_follow"
    assert by_idx[1].status == "open"

from pmbot.smart_money.ranking import (WalletStats, normalize_positive,
                                       score_wallets)

WEIGHTS = {"pnl_week": 0.15, "pnl_month": 0.35, "pnl_all": 0.20,
           "roi": 0.15, "diversification": 0.15}
FILTERS = {"min_trades": 20, "min_distinct_markets": 5,
           "min_account_age_days": 30}


def make_stats(**kw) -> WalletStats:
    base = dict(wallet="0xabc", username="test", pnl_week=1000,
                pnl_month=5000, pnl_all=20000, vol_all=100000, trades=100,
                distinct_markets=25, account_age_days=200,
                top_market_value_share=0.2)
    base.update(kw)
    return WalletStats(**base)


def test_solid_wallet_passes_filters():
    [w] = score_wallets([make_stats()], WEIGHTS, FILTERS)
    assert w.passed_filters
    assert 0 < w.score <= 1


def test_one_trick_pony_rejected():
    # Una wallet con una sola gran apuesta (patrón insider) no pasa.
    [w] = score_wallets([make_stats(trades=3, distinct_markets=1,
                                    top_market_value_share=1.0)],
                        WEIGHTS, FILTERS)
    assert not w.passed_filters
    assert "trades" in w.reject_reason


def test_new_account_rejected():
    [w] = score_wallets([make_stats(account_age_days=5)], WEIGHTS, FILTERS)
    assert not w.passed_filters
    assert "nueva" in w.reject_reason


def test_negative_pnl_rejected():
    [w] = score_wallets([make_stats(pnl_all=-500)], WEIGHTS, FILTERS)
    assert not w.passed_filters


def test_better_wallet_ranks_first():
    weak = make_stats(wallet="0xweak", pnl_week=100, pnl_month=500,
                      pnl_all=2000, distinct_markets=6)
    strong = make_stats(wallet="0xstrong")
    ranked = score_wallets([weak, strong], WEIGHTS, FILTERS)
    assert ranked[0].wallet == "0xstrong"


def test_passed_filters_rank_above_rejected():
    rich_but_new = make_stats(wallet="0xnew", pnl_month=10**7,
                              pnl_all=10**7, account_age_days=1)
    modest = make_stats(wallet="0xok")
    ranked = score_wallets([rich_but_new, modest], WEIGHTS, FILTERS)
    assert ranked[0].wallet == "0xok"


def test_concentration_penalizes_diversification():
    spread = make_stats(wallet="0xspread", top_market_value_share=0.1)
    concentrated = make_stats(wallet="0xconc", top_market_value_share=1.0)
    ranked = {w.wallet: w for w in score_wallets([spread, concentrated],
                                                 WEIGHTS, FILTERS)}
    assert (ranked["0xspread"].components["diversification"]
            > ranked["0xconc"].components["diversification"])


def test_normalize_positive_bounds():
    assert normalize_positive(0, 100) == 0.0
    assert normalize_positive(-5, 100) == 0.0
    assert normalize_positive(100, 100) == 1.0
    assert 0 < normalize_positive(50, 100) < 1


def test_empty_input():
    assert score_wallets([], WEIGHTS, FILTERS) == []

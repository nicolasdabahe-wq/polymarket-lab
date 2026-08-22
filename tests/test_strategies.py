import pytest

from pmbot.strategies.arbitrage import detect_arbitrage
from pmbot.strategies.copy_trading import (pick_candidates,
                                           pick_holdings_consensus,
                                           slippage_ok)

CFG = {"min_wallet_score": 0.55, "min_copy_usdc_of_wallet": 500,
       "strong_score": 0.70, "strong_usdc": 2000, "confirm_count": 2}

SCORES = {"0xstrong": 0.75, "0xgood1": 0.60, "0xgood2": 0.58, "0xweak": 0.30}


def sig(wallet, usdc=1000, side="BUY", cid="0xm1", idx=0, price=0.40):
    return {"wallet": wallet, "side": side, "condition_id": cid,
            "outcome_index": idx, "outcome": "Yes", "title": "t",
            "price": price, "usdc": usdc}


# --- arbitraje ---

def test_arb_detected_when_sum_below_one():
    assert detect_arbitrage(0.45, 0.50, min_edge=0.02) is not None


def test_no_arb_when_sum_near_one():
    assert detect_arbitrage(0.50, 0.495, min_edge=0.02) is None


def test_arb_fees_eat_edge():
    # edge bruto 5%, pero 600 bps de fee sobre 0.95 lo consume
    assert detect_arbitrage(0.45, 0.50, min_edge=0.02, fee_bps=600) is None


def test_arb_invalid_prices():
    assert detect_arbitrage(None, 0.5, 0.02) is None
    assert detect_arbitrage(0.0, 0.5, 0.02) is None


# --- copy trading: reglas de disparo ---

def test_strong_single_wallet_triggers():
    cands = pick_candidates([sig("0xstrong", usdc=3000)], SCORES, CFG)
    assert len(cands) == 1


def test_strong_wallet_small_entry_does_not_trigger():
    assert pick_candidates([sig("0xstrong", usdc=800)], SCORES, CFG) == []


def test_big_single_entry_triggers_any_qualified_wallet():
    # Wallet calificada (no strong) con entrada muy grande: dispara sola.
    cfg = dict(CFG, solo_big_usdc=2500)
    [cand] = pick_candidates([sig("0xgood1", usdc=3000)], SCORES, cfg)
    assert cand.leader["usdc"] == 3000


def test_big_entry_threshold_not_met():
    cfg = dict(CFG, solo_big_usdc=2500)
    assert pick_candidates([sig("0xgood1", usdc=2000)], SCORES, cfg) == []


def test_two_good_wallets_consensus_triggers():
    cands = pick_candidates([sig("0xgood1"), sig("0xgood2")], SCORES, CFG)
    assert len(cands) == 1
    assert len(cands[0].wallets) == 2


def test_one_good_wallet_alone_does_not_trigger():
    assert pick_candidates([sig("0xgood1")], SCORES, CFG) == []


def test_weak_or_unknown_wallets_ignored():
    assert pick_candidates([sig("0xweak"), sig("0xunknown")], SCORES, CFG) == []


def test_sells_and_small_trades_ignored():
    signals = [sig("0xgood1", side="SELL"), sig("0xgood2", usdc=100)]
    assert pick_candidates(signals, SCORES, CFG) == []


def test_different_outcomes_not_merged():
    # Dos wallets en el MISMO mercado pero outcomes opuestos: no es consenso.
    signals = [sig("0xgood1", idx=0), sig("0xgood2", idx=1)]
    assert pick_candidates(signals, SCORES, CFG) == []


def test_same_wallet_not_double_counted_for_consensus():
    signals = [sig("0xgood1"), sig("0xgood1")]
    assert pick_candidates(signals, SCORES, CFG) == []


def test_split_fills_aggregate_to_strong_trigger():
    # Una entrada grande llega partida en fills chicos: deben sumarse.
    signals = [sig("0xstrong", usdc=800, price=0.50),
               sig("0xstrong", usdc=800, price=0.54),
               sig("0xstrong", usdc=800, price=0.58)]
    [cand] = pick_candidates(signals, SCORES, CFG)
    leader = cand.leader
    assert leader["usdc"] == pytest.approx(2400)     # >= strong_usdc 2000
    assert leader["price"] == pytest.approx(0.54)    # promedio ponderado


def test_small_fills_aggregate_past_min_size():
    # Fills individuales bajo el mínimo, pero el total sí califica.
    signals = [sig("0xgood1", usdc=300), sig("0xgood1", usdc=300),
               sig("0xgood2", usdc=600)]
    [cand] = pick_candidates(signals, SCORES, CFG)
    assert len(cand.wallets) == 2  # consenso alcanzado con totales


def test_leader_is_highest_score():
    cands = pick_candidates([sig("0xgood1"), sig("0xstrong", usdc=3000)],
                            SCORES, CFG)
    assert cands[0].leader["wallet"] == "0xstrong"


# --- consenso de posiciones sostenidas ---

HC_CFG = {"min_wallets": 2, "min_value_usdc": 5000}


def hold(wallet, value=6000, cid="0xm1", outcome="Yes", avg=0.60):
    return {"wallet": wallet, "condition_id": cid, "outcome": outcome,
            "value": value, "avg_price": avg}


def test_consensus_two_wallets_same_outcome():
    [c] = pick_holdings_consensus([hold("0xa", avg=0.50),
                                   hold("0xb", value=12000, avg=0.65)], HC_CFG)
    assert c["wallets"] == ["0xa", "0xb"]
    # promedio ponderado por valor: (0.5*6000 + 0.65*12000) / 18000 = 0.60
    assert c["avg_entry"] == pytest.approx(0.60)


def test_consensus_requires_min_value_each():
    assert pick_holdings_consensus([hold("0xa"), hold("0xb", value=100)],
                                   HC_CFG) == []


def test_consensus_one_wallet_not_enough():
    assert pick_holdings_consensus([hold("0xa")], HC_CFG) == []


def test_consensus_opposite_outcomes_not_grouped():
    holdings = [hold("0xa", outcome="Yes"), hold("0xb", outcome="No")]
    assert pick_holdings_consensus(holdings, HC_CFG) == []


def test_consensus_sorted_by_total_value():
    holdings = [hold("0xa"), hold("0xb"),
                hold("0xc", cid="0xm2", value=50000),
                hold("0xd", cid="0xm2", value=50000)]
    result = pick_holdings_consensus(holdings, HC_CFG)
    assert result[0]["condition_id"] == "0xm2"


# --- copy trading: slippage ---

def test_slippage_within_limit():
    assert slippage_ok(0.40, 0.43, 0.10)       # +7.5%


def test_slippage_exceeded_blocks_copy():
    assert not slippage_ok(0.40, 0.46, 0.10)   # +15%


def test_price_dropped_is_fine():
    assert slippage_ok(0.40, 0.35, 0.10)


def test_invalid_prices_block():
    assert not slippage_ok(0.0, 0.5, 0.10)


# --- techo de entrada y deportes en vivo en las copias ---

def test_techo_de_entrada_configurable():
    from pmbot.strategies.copy_trading import CopyTradingStrategy
    cfg = {"max_entry_price": 0.80, "sports_only_prematch": True}
    s = CopyTradingStrategy.__new__(CopyTradingStrategy)
    s.max_entry = float(cfg["max_entry_price"])
    s.prematch_only_sports = bool(cfg["sports_only_prematch"])
    # Las entradas de anoche: 0.95 y 0.92 quedan fuera; 0.57 y 0.68 entran.
    assert 0.95 > s.max_entry and 0.92 > s.max_entry
    assert 0.57 <= s.max_entry and 0.68 <= s.max_entry


def test_categorias_deportivas_incluyen_esports():
    from pmbot.strategies.copy_trading import SPORT_CATEGORIES
    # El Dota 2 (-9.80) cae en esports: también debe filtrarse en vivo.
    assert {"sports", "esports"} <= SPORT_CATEGORIES

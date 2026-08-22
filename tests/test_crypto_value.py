import pytest

from pmbot.strategies.crypto_value import (model_probability,
                                           parse_crypto_question,
                                           prob_terminal_above, prob_touch,
                                           ParsedMarket)


# --- parser ---

def test_parse_reach_is_touch_above():
    p = parse_crypto_question("Will Bitcoin reach $100,000 in August?")
    assert p and p.product == "BTC-USD" and p.strike == 100000
    assert p.kind == "touch_above"


def test_parse_dip_is_touch_below():
    p = parse_crypto_question("Will Bitcoin dip to $60,000 in August?")
    assert p and p.kind == "touch_below" and p.strike == 60000


def test_parse_price_above_is_terminal():
    p = parse_crypto_question(
        "Will the price of Ethereum be above $2,500 on August 25?")
    assert p and p.product == "ETH-USD" and p.kind == "terminal_above"
    assert p.strike == 2500


def test_parse_below_terminal():
    p = parse_crypto_question(
        "Will the price of Bitcoin be below $70,000 on August 30?")
    assert p and p.kind == "terminal_below"


def test_parse_ambiguous_returns_none():
    assert parse_crypto_question("Will Bitcoin outperform Ethereum?") is None
    assert parse_crypto_question("Will Solana flip XRP this year?") is None


def test_parse_non_crypto_returns_none():
    assert parse_crypto_question(
        "Will WTI Crude Oil hit (LOW) $70 in August?") is None


# --- modelo: propiedades que DEBEN cumplirse ---

SPOT, VOL = 76000.0, 0.03  # BTC, ~3% de vol diaria


def test_terminal_at_the_money_near_half():
    p = prob_terminal_above(SPOT, SPOT, VOL, 10)
    assert 0.4 < p < 0.55  # ~50% menos el ajuste -s²/2


def test_terminal_monotonic_in_strike():
    p_low = prob_terminal_above(SPOT, 70000, VOL, 10)
    p_high = prob_terminal_above(SPOT, 90000, VOL, 10)
    assert p_low > 0.6 > 0.4 > p_high


def test_touch_geq_terminal():
    # Tocar una barrera es siempre más probable que terminar arriba de ella.
    for strike in (80000, 90000, 110000):
        assert (prob_touch(SPOT, strike, VOL, 15)
                >= prob_terminal_above(SPOT, strike, VOL, 15) - 1e-9)


def test_touch_below_symmetric_behaviour():
    near = prob_touch(SPOT, 74000, VOL, 10)   # barrera cercana abajo
    far = prob_touch(SPOT, 50000, VOL, 10)    # barrera lejana abajo
    assert near > far
    assert near > 0.5 and far < 0.05


def test_touch_more_time_more_likely():
    assert (prob_touch(SPOT, 90000, VOL, 30)
            > prob_touch(SPOT, 90000, VOL, 5))


def test_touch_at_spot_is_certain():
    assert prob_touch(SPOT, SPOT, VOL, 5) == 1.0


def test_model_probability_touch_already_crossed():
    p = ParsedMarket("BTC-USD", 70000, "touch_above")
    assert model_probability(p, spot=76000, vol_daily=VOL, days=10) == 1.0


def test_model_probability_terminal_below():
    p = ParsedMarket("BTC-USD", 90000, "terminal_below")
    assert model_probability(p, SPOT, VOL, 10) > 0.9


def test_degenerate_inputs():
    assert prob_terminal_above(SPOT, 50000, 0.0, 10) == 1.0
    assert prob_terminal_above(SPOT, 90000, 0.0, 10) == 0.0
    assert prob_touch(SPOT, 90000, VOL, 0) == 0.0


# --- sufijos y carreras (errores reales del escáner del 2026-08-22) ---

def test_150k_son_ciento_cincuenta_mil():
    # "Will Bitcoin hit $150k..." se leía como strike $150: con el spot en
    # seis cifras el modelo regalaba probabilidad 1.0 a un mercado de 3¢.
    p = parse_crypto_question("Will Bitcoin hit $150k by December 31, 2026?")
    assert p and p.strike == 150_000 and p.kind == "touch_above"


def test_6k_son_seis_mil():
    p = parse_crypto_question("Will Ethereum hit $6k by December 31, 2026?")
    assert p and p.strike == 6_000


def test_sin_sufijo_sigue_igual():
    p = parse_crypto_question("Will Bitcoin reach $160,000 by December 31, 2026?")
    assert p and p.strike == 160_000


def test_las_carreras_entre_barreras_no_se_leen():
    # "o $3.000 primero" es otro payoff (carrera), no un one-touch.
    assert parse_crypto_question(
        "Will Ethereum hit $1,000 or $3,000 first?") is None
    assert parse_crypto_question(
        "Will Solana hit $60 or $140 first?") is None

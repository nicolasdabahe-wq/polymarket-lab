"""Casos tomados de posiciones reales (2026-08-21)."""
from pmbot.scheduler.settlement import decide_settlement


def decide(**kw):
    base = dict(gamma_closed=False, gamma_prices=None, outcome_index=0,
                uma_status=None, onchain_price=None, onchain_redeemable=False)
    base.update(kw)
    return decide_settlement(**base)


def test_gamma_closed_paga_el_precio_oficial():
    assert decide(gamma_closed=True, gamma_prices=[0.0, 1.0],
                  outcome_index=1).payout == 1.0


def test_redimible_onchain_liquida_sin_esperar():
    assert decide(onchain_price=0.001, onchain_redeemable=True).payout == 0.0
    assert decide(onchain_price=1.0, onchain_redeemable=True).payout == 1.0


def test_fritz_perdido_con_uma_propuesto():
    # Partido terminado: closed=false, uma='proposed', precio 0.001.
    d = decide(uma_status="proposed", gamma_prices=[0.0005, 0.9995],
               outcome_index=0, onchain_price=0.001)
    assert d.payout == 0.0 and "oráculo" in d.reason


def test_brewers_ganado_con_uma_propuesto():
    # Payout ya cobrado on-chain: la posición no aparece, manda Gamma.
    d = decide(uma_status="proposed", gamma_prices=[0.0005, 0.9995],
               outcome_index=1, onchain_price=None)
    assert d.payout == 1.0


def test_mercado_vivo_barato_no_se_liquida():
    # "¿La Fed baja 50+ bps?" cotiza a 0.003 y vence dentro de un mes:
    # sin oráculo NO se toca, aunque el precio parezca resuelto.
    d = decide(uma_status=None, gamma_prices=[0.0025, 0.9975],
               outcome_index=0, onchain_price=0.003)
    assert d.payout is None and d.reason == "abierto"


def test_uma_disputado_no_liquida():
    assert decide(uma_status="disputed", onchain_price=0.001).payout is None


def test_uma_propuesto_pero_precio_sin_definir_espera():
    # Propuesto y aún cotizando 0.60: algo no cuadra, mejor no liquidar.
    assert decide(uma_status="proposed", onchain_price=0.60).payout is None


def test_partido_en_curso_no_se_liquida():
    d = decide(gamma_prices=[0.355, 0.645], outcome_index=1,
               onchain_price=0.645)
    assert d.payout is None


def test_sin_datos_no_hace_nada():
    assert decide().payout is None
    assert decide(gamma_closed=True, gamma_prices=[]).payout is None

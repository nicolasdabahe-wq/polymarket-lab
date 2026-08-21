from datetime import datetime, timedelta, timezone

from pmbot.scheduler.settlement import decide_settlement

NOW = datetime(2026, 8, 21, 20, 0, tzinfo=timezone.utc)


def decide(**kw):
    base = dict(gamma_closed=False, gamma_prices=None, outcome_index=0,
                onchain_price=None, onchain_redeemable=False,
                pinned_since=None, now=NOW, confirm_minutes=10)
    base.update(kw)
    return decide_settlement(**base)


def test_gamma_closed_paga_el_precio_oficial():
    d = decide(gamma_closed=True, gamma_prices=[0.0, 1.0], outcome_index=1)
    assert d.payout == 1.0 and d.pinned_since is None


def test_redimible_onchain_liquida_sin_esperar():
    d = decide(onchain_price=0.001, onchain_redeemable=True)
    assert d.payout == 0.0
    d = decide(onchain_price=1.0, onchain_redeemable=True)
    assert d.payout == 1.0


def test_precio_clavado_primero_marca_y_no_liquida():
    d = decide(onchain_price=0.001)
    assert d.payout is None and d.pinned_since == NOW


def test_precio_clavado_liquida_tras_la_ventana():
    d = decide(onchain_price=0.001, pinned_since=NOW - timedelta(minutes=11))
    assert d.payout == 0.0 and "de facto" in d.reason
    d = decide(onchain_price=0.999, pinned_since=NOW - timedelta(minutes=30))
    assert d.payout == 1.0


def test_precio_clavado_dentro_de_la_ventana_espera():
    d = decide(onchain_price=0.999, pinned_since=NOW - timedelta(minutes=4))
    assert d.payout is None and d.pinned_since == NOW - timedelta(minutes=4)


def test_precio_que_se_despega_borra_la_marca():
    # Un pico de 0.996 que vuelve a 0.80 no debe liquidar nada.
    d = decide(onchain_price=0.80, pinned_since=NOW - timedelta(minutes=30))
    assert d.payout is None and d.pinned_since is None


def test_gamma_es_el_respaldo_cuando_no_hay_onchain():
    # Modo paper: sin posiciones on-chain, se usa el precio de Gamma.
    d = decide(gamma_prices=[0.997, 0.003], outcome_index=0,
               pinned_since=NOW - timedelta(minutes=20))
    assert d.payout == 1.0


def test_sin_datos_no_hace_nada():
    assert decide().payout is None
    assert decide(gamma_closed=True, gamma_prices=[]).payout is None

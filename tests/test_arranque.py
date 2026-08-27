"""El bot tiene que poder ARRANCAR.

Esta prueba nace de un fallo real: 203 tests en verde y el contenedor en
bucle de reinicio, porque build_app usaba una variable que no existía en
ese punto. Ningún test tocaba build_app, así que nadie lo vio.

Arma la aplicación completa con el config.yaml de verdad, igual que en
producción, y le pide las operaciones del primer ciclo. Sin red: solo
comprueba que todo se construye y que el cableado entre módulos existe.
"""
import asyncio

import pytest

from pmbot.config import load_config
from pmbot.context import build_app
from pmbot.scheduler.daily import DailyRoutine


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("PMBOT_VAR_DIR", str(tmp_path))
    monkeypatch.delenv("PMBOT_LIVE_TRADING", raising=False)
    aplicacion = build_app(load_config("config.yaml"))
    yield aplicacion
    asyncio.run(aplicacion.aclose())


def test_la_app_se_construye_con_el_config_real(app):
    # Todas las piezas que el scheduler usa en el primer ciclo.
    for pieza in ("broker", "risk", "gamma", "data_api", "tape",
                  "copy_trading", "arbitrage", "crypto_value", "sports_value",
                  "ladder_arb",
                  "wallet_scorer", "wallet_tracker", "wallet_validator",
                  "notifier", "market_store"):
        assert getattr(app, pieza, None) is not None, f"falta {pieza}"


def test_el_primer_ciclo_no_revienta(app):
    """Las consultas locales que corren apenas arranca, sin tocar la red."""
    rutina = DailyRoutine(app)
    assert rutina._fast_lane_wallets(12) == []
    assert rutina._watched_wallets() == set()
    assert asyncio.run(rutina.settle_resolved()) == []


def test_el_estado_del_portfolio_se_calcula(app):
    estado = app.broker.portfolio_state()
    assert estado.equity > 0
    assert estado.exposure_slow == 0.0
    assert estado.held_outcomes == {}


def test_los_limites_de_velocidad_llegan_del_config(app):
    """Política del dueño: el dinero se cobra el mismo día.

    El 2026-08-22 el plazo eran tres días, sin excepciones — hubo una por
    "oportunidad dorada" y se eliminó porque no era lo pedido. El 2026-08-27
    lo apretó a 24 horas: quiere que el capital dé varias vueltas al día en
    vez de quedarse retenido hasta mañana.

    Si alguien estira ese plazo, este test tiene que fallar."""
    limites = app.risk.limits
    assert limites.max_days_to_resolution == 1.0
    assert not hasattr(limites, "golden_edge")
    # Lo dormido se mide con la misma vara: si `slow_days` se quedara en 3,
    # una posición de dos días no contaría como capital parado y `capital
    # --vender` no la vería.
    assert limites.slow_days <= 1.0
    cripto = app.cfg.section("strategies")["crypto_value"]
    assert cripto["max_days_to_resolution"] <= 1.0
    # Y la ventana tiene que seguir siendo habitable: con el mínimo en medio
    # día y el máximo en uno, casi nada entraba.
    assert cripto["min_days_to_resolution"] < cripto["max_days_to_resolution"] / 2


def test_no_queda_ningun_freno_por_perdidas(app):
    """El dueño los quitó (2026-08-22). 1.0 es el valor que los apaga; si
    alguno vuelve a bajar, el bot dejaría de comprar solo tras una mala
    racha y nadie sabría por qué."""
    limites = app.risk.limits
    assert limites.daily_stop_loss_pct == 1.0
    assert limites.max_drawdown_pct == 1.0


def test_la_cartera_queda_abierta_sin_techos(app):
    """Sin techos de concentración. Si esto vuelve a bajar de 1.0 sin que
    el dueño lo pida, el bot se autobloquea al caer el equity: los techos se
    miden contra el equity de hoy y bajan con él (pasó el 2026-08-22)."""
    limites = app.risk.limits
    assert limites.max_total_exposure_pct == 1.0
    assert limites.max_pct_per_market == 1.0
    assert limites.max_pct_per_category == 1.0
    assert limites.max_pct_slow == 1.0
    estrategias = app.cfg.section("strategies")
    for nombre, scfg in estrategias.items():
        pct = (scfg or {}).get("budget_pct")
        if pct is not None:
            assert pct == 1.0, f"{nombre} todavía tiene techo {pct}"

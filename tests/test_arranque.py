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
    limites = app.risk.limits
    assert limites.max_days_to_resolution == 21
    assert limites.slow_days == 12
    assert limites.max_pct_slow == pytest.approx(0.20)

import pytest

from pmbot.strategies.sizing import MAX_ROI, kelly_usdc, retorno_esperado


def test_sin_ventaja_no_se_apuesta():
    assert kelly_usdc(240, 0.60, 0.0, 0.25, 12, 0.12) == 0.0
    assert kelly_usdc(240, 0.60, -0.1, 0.25, 12, 0.12) == 0.0


def test_mas_ventaja_mas_tamano():
    chico = kelly_usdc(240, 0.60, 0.05, 0.25, 12, 0.30)
    grande = kelly_usdc(240, 0.60, 0.20, 0.25, 12, 0.30)
    assert grande > chico


def test_el_piso_se_respeta_si_hay_ventaja():
    # Ventaja mínima: apuesta el piso, no $2.
    assert kelly_usdc(240, 0.60, 0.005, 0.25, 12, 0.12) == pytest.approx(12.0)


def test_el_techo_por_apuesta_se_respeta():
    # Ventaja enorme: se corta en max_pct del equity.
    assert kelly_usdc(240, 0.60, 5.0, 0.25, 12, 0.12) == pytest.approx(28.8)


def test_precio_invalido_devuelve_cero():
    for p in (0.0, 1.0, -0.2, 1.5):
        assert kelly_usdc(240, p, 0.10, 0.25, 12, 0.12) == 0.0


def test_roi_se_recorta_para_no_sobreapostar():
    # Un backtest con pocas copias puede dar +80% por casualidad.
    assert retorno_esperado(5.0, 1, 0.0) == pytest.approx(MAX_ROI)


def test_consenso_sube_la_ventaja():
    una = retorno_esperado(0.10, 1, 0.0)
    dos = retorno_esperado(0.10, 2, 0.0)
    tres = retorno_esperado(0.10, 3, 0.0)
    assert una < dos < tres
    assert tres <= una * 2  # con tope


def test_slippage_come_la_ventaja():
    limpia = retorno_esperado(0.10, 1, 0.0)
    a_medias = retorno_esperado(0.10, 1, 0.5)
    quemada = retorno_esperado(0.10, 1, 1.0)
    assert a_medias == pytest.approx(limpia / 2)
    assert quemada == 0.0


def test_roi_negativo_no_apuesta():
    assert retorno_esperado(-0.20, 3, 0.0) == 0.0

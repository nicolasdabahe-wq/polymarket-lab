"""Perfiles medidos contra wallets reales el 2026-08-22."""
import pytest

from pmbot.smart_money.behavior import perfil_operador

DIA = 86_400


def trades(n, usdc, dias=1.0, mercados=None, ambos=0):
    """n trades de tamaño usdc repartidos en 'dias'."""
    out = []
    for i in range(n):
        cid = f"0xm{i % (mercados or n)}"
        out.append({"ts": int(i * dias * DIA / max(n - 1, 1)),
                    "condition_id": cid, "outcome_index": 0,
                    "side": "BUY", "usdc": usdc, "price": 0.5})
    for i in range(ambos):   # los mismos mercados, el otro lado
        out.append({"ts": 10, "condition_id": f"0xm{i}", "outcome_index": 1,
                    "side": "BUY", "usdc": usdc, "price": 0.5})
    return out


def test_creador_de_mercado_de_la_vida_real():
    # 0x204f72f3: 1755 trades en un día, mediana $15.59
    p = perfil_operador(trades(1755, 15.59, dias=1.0, mercados=351))
    assert p.es_creador_de_mercado and p.etiqueta == "creador_de_mercado"


def test_metralleta_de_micro_ordenes():
    # 0x86e99fae: 339 trades/día, mediana $13
    p = perfil_operador(trades(339, 13.0, dias=1.0, mercados=71))
    assert p.es_creador_de_mercado


def test_wallet_de_conviccion_pasa():
    # 0xb340ecd9: 15 trades/día, mediana $812
    p = perfil_operador(trades(443, 812.0, dias=29.5, mercados=105))
    assert not p.es_creador_de_mercado and p.etiqueta == "convicción"


def test_wallet_grande_aunque_opere_seguido():
    # 0x818be2b3: 30 trades/día pero mediana $1014: es convicción, no mercado.
    p = perfil_operador(trades(644, 1014.0, dias=21.5, mercados=110))
    assert not p.es_creador_de_mercado


def test_comprar_ambos_lados_no_condena_por_si_solo():
    # Una wallet grande cubre posiciones: 36% de sus mercados con los dos
    # lados y aun así es de convicción.
    p = perfil_operador(trades(300, 1000.0, dias=20, mercados=100, ambos=36))
    assert p.pct_ambos_lados == pytest.approx(0.36)
    assert not p.es_creador_de_mercado


def test_actividad_concentrada_no_infla_la_frecuencia():
    # 20 trades en 10 minutos: el piso de medio día evita 2880 trades/día.
    p = perfil_operador(trades(20, 500.0, dias=10 / 1440))
    assert p.trades_por_dia == pytest.approx(40.0)


def test_muestra_insuficiente_no_opina():
    assert perfil_operador(trades(3, 100.0)) is None

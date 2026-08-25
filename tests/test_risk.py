from pmbot.risk import Limits, OrderRequest, PortfolioState, evaluate

LIMITS = Limits(max_pct_per_market=0.10, max_pct_per_category=0.40,
                max_pct_per_copied_wallet=0.15, max_total_exposure_pct=0.80,
                daily_stop_loss_pct=0.05, min_order_usdc=1.0)


def state(**kw) -> PortfolioState:
    base = dict(equity=500.0, cash=400.0, day_start_equity=500.0,
                exposure_total=100.0, exposure_by_market={},
                exposure_by_category={}, exposure_by_wallet={},
                exposure_by_strategy={})
    base.update(kw)
    return PortfolioState(**base)


def order(**kw) -> OrderRequest:
    base = dict(strategy="copy_trading", condition_id="0xm1",
                category="politics", token_id="t1", outcome="Yes",
                outcome_index=0, side="BUY", size=20.0, price=0.50,
                reason="test", strategy_budget_pct=0.50)
    base.update(kw)
    return OrderRequest(**base)


def test_ok_order_approved():
    assert evaluate(order(), state(), LIMITS).approved


def test_sells_always_approved():
    # Vender reduce riesgo: pasa aunque el stop diario esté activado.
    s = state(equity=100.0, day_start_equity=500.0)
    assert evaluate(order(side="SELL"), s, LIMITS).approved


def test_min_order_size():
    d = evaluate(order(size=1.0, price=0.5), state(), LIMITS)
    assert not d.approved and "chica" in d.reason


def test_insufficient_cash():
    d = evaluate(order(size=1000, price=0.9), state(cash=10.0), LIMITS)
    assert not d.approved


def test_market_limit():
    # 10% de 500 = 50; ya hay 45 en el mercado, comprar 10 más lo pasa.
    s = state(exposure_by_market={"0xm1": 45.0})
    d = evaluate(order(size=20, price=0.5), s, LIMITS)
    assert not d.approved and "mercado" in d.reason


def test_category_limit():
    s = state(exposure_by_category={"politics": 195.0})
    d = evaluate(order(size=20, price=0.5), s, LIMITS)  # 195+10 > 200
    assert not d.approved and "categoría" in d.reason


def test_copied_wallet_limit():
    s = state(exposure_by_wallet={"0xw": 70.0})
    d = evaluate(order(copied_wallet="0xw", size=20, price=0.5), s, LIMITS)
    assert not d.approved and "wallet" in d.reason


def test_strategy_budget():
    s = state(exposure_by_strategy={"copy_trading": 245.0})
    d = evaluate(order(size=20, price=0.5), s, LIMITS)  # 245+10 > 50%*500
    assert not d.approved and "estrategia" in d.reason


def test_total_exposure_limit():
    s = state(exposure_total=395.0)
    d = evaluate(order(size=20, price=0.5), s, LIMITS)  # 395+10 > 400
    assert not d.approved and "total" in d.reason


def test_daily_stop_blocks_buys():
    # Cayó 6% desde el inicio del día (> 5% de stop): no más compras.
    s = state(equity=470.0, day_start_equity=500.0)
    d = evaluate(order(), s, LIMITS)
    assert not d.approved and "stop" in d.reason


def test_zero_equity_blocks():
    assert not evaluate(order(), state(equity=0.0), LIMITS).approved


# --- freno total (drawdown desde el capital inicial) ---

LIMITS_DD = Limits(max_pct_per_market=0.20, max_pct_per_category=0.50,
                   max_pct_per_copied_wallet=0.25, max_total_exposure_pct=0.85,
                   daily_stop_loss_pct=0.10, min_order_usdc=10.0,
                   max_drawdown_pct=0.25)


def test_total_drawdown_blocks_buys():
    # Capital inicial 278; equity 200 = -28% -> freno total
    s = state(equity=200.0, cash=200.0, day_start_equity=205.0,
              starting_equity=278.0, exposure_total=0.0)
    d = evaluate(order(size=25, price=0.5), s, LIMITS_DD)
    assert not d.approved and "FRENO TOTAL" in d.reason


def test_within_drawdown_allows_buys():
    s = state(equity=250.0, cash=250.0, day_start_equity=255.0,
              starting_equity=278.0, exposure_total=0.0)
    assert evaluate(order(size=25, price=0.5), s, LIMITS_DD).approved


def test_drawdown_never_blocks_sells():
    s = state(equity=100.0, cash=100.0, day_start_equity=250.0,
              starting_equity=278.0, exposure_total=0.0)
    assert evaluate(order(side="SELL"), s, LIMITS_DD).approved


def test_min_order_floor_ten_dollars():
    s = state(starting_equity=278.0)
    d = evaluate(order(size=10, price=0.5), s, LIMITS_DD)   # $5
    assert not d.approved and "chica" in d.reason
    assert evaluate(order(size=24, price=0.5), s, LIMITS_DD).approved  # $12


# --- los dos lados del mismo evento (casos reales del 2026-08-22) ---

def test_no_pagar_mas_de_un_dolar_entre_los_dos_lados():
    """Musetti: compró 20 de Musetti a 0.50 ($10) y después 20 de Tiafoe a
    0.69 ($13.80). Pone $23.80 y gane quien gane cobra 20: pierde siempre."""
    s = state(held_outcomes={"0xm1": {0: 0.50}},
              held_shares={"0xm1": {0: 20.0}})
    d = evaluate(order(outcome_index=1, outcome="No", size=20.0, price=0.69),
                 s, LIMITS)
    assert not d.approved and "No es arbitraje" in d.reason


def test_el_otro_lado_si_asegura_ganancia_pasa():
    """Musetti a 0.50 y el rival se desploma a 0.25: 20 acciones de cada
    lado cuestan $15 en total y cobran $20 gane quien gane."""
    s = state(held_outcomes={"0xm1": {0: 0.50}},
              held_shares={"0xm1": {0: 20.0}})
    assert evaluate(order(outcome_index=1, outcome="No", size=20.0,
                          price=0.25), s, LIMITS).approved


def test_el_par_desigual_no_es_arbitraje_aunque_los_precios_sumen_menos_de_uno():
    """El caso real del 2026-08-24, Atlanta Dream vs Los Angeles Sparks.

    La regla vieja solo sumaba precios: 0.80 + 0.16 = 0.96 < 1, luego "es
    arbitraje, adelante". Pero eran 28.3 acciones de un lado y 171.1 del
    otro. Se pusieron $49.99 entre los dos lados y si Atlanta ganaba
    ajustado se cobraban $28.27: -$21.72 asegurados en esa rama.
    """
    s = state(cash=400.0, held_outcomes={"0xatl": {0: 0.80}},
              held_shares={"0xatl": {0: 28.3}})
    d = evaluate(order(condition_id="0xatl", outcome_index=1,
                       outcome="Los Angeles Sparks", size=171.1, price=0.16),
                 s, LIMITS)
    assert not d.approved and "No es arbitraje" in d.reason


def test_el_par_desigual_pasa_si_aun_asi_asegura_ganancia():
    """Desiguales no significa malo: lo que manda es si el lado más chico
    cubre lo gastado. 60 acciones a 0.20 ($12) contra 100 ya compradas a
    0.30 ($30): se ponen $42 y en el peor caso se cobran 60."""
    s = state(cash=400.0, held_outcomes={"0xm2": {0: 0.30}},
              held_shares={"0xm2": {0: 100.0}})
    d = evaluate(order(condition_id="0xm2", outcome_index=1, outcome="No",
                       size=60.0, price=0.20), s, LIMITS)
    assert d.approved, d.reason


def test_recargar_el_mismo_lado_esta_permitido():
    """Convicción, no contradicción: entrar a 0.50 y sumar a 0.60 (o a 0.20
    si cae) es una tesis, y el dueño la quiere habilitada."""
    s = state(held_outcomes={"0xm1": {0: 0.50}})
    assert evaluate(order(outcome_index=0, price=0.60), s, LIMITS).approved
    assert evaluate(order(outcome_index=0, price=0.20), s, LIMITS).approved


def test_primera_entrada_en_el_mercado_pasa():
    assert evaluate(order(), state(held_outcomes={"0xotro": {0: 0.4}}),
                    LIMITS).approved


def test_vender_no_se_bloquea_por_tener_posicion():
    s = state(held_outcomes={"0xm1": {0: 0.50}})
    assert evaluate(order(side="SELL"), s, LIMITS).approved


# --- velocidad del capital (pedido del dueño, 2026-08-22) ---

LIMITS_VEL = Limits(max_pct_per_market=0.20, max_pct_per_category=0.50,
                    max_pct_per_copied_wallet=0.25, max_total_exposure_pct=0.85,
                    daily_stop_loss_pct=0.20, min_order_usdc=10.0,
                    max_days_to_resolution=21, slow_days=7, max_pct_slow=0.20)


def test_rechaza_lo_que_tarda_demasiado():
    """La alcaldía de LA se decide en noviembre: tres meses de capital
    secuestrado con una cuenta de $237."""
    d = evaluate(order(size=30, price=0.5, days_to_resolution=85),
                 state(), LIMITS_VEL)
    assert not d.approved and "parado" in d.reason


def test_acepta_lo_que_se_resuelve_pronto():
    # Un partido de hoy o un mercado de cripto de esta semana.
    assert evaluate(order(size=30, price=0.5, days_to_resolution=0.4),
                    state(), LIMITS_VEL).approved
    assert evaluate(order(size=30, price=0.5, days_to_resolution=6),
                    state(), LIMITS_VEL).approved


def test_limita_cuanto_capital_puede_estar_dormido():
    # 20% de $500 = $100 en mercados lentos; ya hay $95, esta suma $15.
    s = state(exposure_slow=95.0)
    d = evaluate(order(size=30, price=0.5, days_to_resolution=14), s, LIMITS_VEL)
    assert not d.approved and "lentos" in d.reason


def test_una_apuesta_rapida_no_toca_el_cupo_de_lentas():
    s = state(exposure_slow=95.0)
    assert evaluate(order(size=30, price=0.5, days_to_resolution=1),
                    s, LIMITS_VEL).approved


def test_sin_fecha_conocida_no_se_bloquea():
    # Mejor no frenar por ignorancia: si no sabemos cuándo resuelve, pasa.
    assert evaluate(order(size=30, price=0.5, days_to_resolution=None),
                    state(exposure_slow=95.0), LIMITS_VEL).approved


def test_vender_una_lenta_siempre_se_puede():
    # Salir de una posición dormida libera capital: nunca se bloquea.
    s = state(exposure_slow=200.0)
    assert evaluate(order(side="SELL", days_to_resolution=90),
                    s, LIMITS_VEL).approved


def test_mercado_en_limbo_no_se_compra():
    """Nordone: endDate vencido hace 11 días, sin resolución (segunda
    vuelta). El bot la recompró por consenso; los días negativos pasaban
    todos los filtros de velocidad."""
    d = evaluate(order(size=30, price=0.5, days_to_resolution=-11),
                 state(), LIMITS_VEL)
    assert not d.approved and "limbo" in d.reason


def test_recien_vencido_no_cuenta_como_limbo():
    # Un partido que terminó hace horas aún no liquidado no es limbo.
    assert evaluate(order(side="SELL", days_to_resolution=-0.3),
                    state(), LIMITS_VEL).approved


# --- tope de plazo sin excepciones (pedido del dueño, 2026-08-22) ---
# "NO quiero que metas ninguna apuesta que se resuelva en más de 3 días."

LIMITS_TRES = Limits(max_pct_per_market=1.0, max_pct_per_category=1.0,
                     max_pct_per_copied_wallet=1.0, max_total_exposure_pct=1.0,
                     daily_stop_loss_pct=1.0, min_order_usdc=10.0,
                     max_days_to_resolution=3, slow_days=3, max_pct_slow=1.0)


def test_ocho_dias_se_rechaza_por_buena_que_parezca():
    """El BTC de fin de mes. Antes había una excepción por "oportunidad
    dorada" que lo dejaba pasar; el dueño la eliminó."""
    d = evaluate(order(size=30, price=0.5, days_to_resolution=8),
                 state(), LIMITS_TRES)
    assert not d.approved and "parado" in d.reason


def test_ni_la_mejor_apuesta_del_mundo_pasa_los_tres_dias():
    d = evaluate(order(size=30, price=0.5, days_to_resolution=4),
                 state(), LIMITS_TRES)
    assert not d.approved and "parado" in d.reason


def test_lo_que_se_resuelve_hoy_o_en_tres_dias_pasa():
    for dias in (0.2, 1.0, 3.0):
        assert evaluate(order(size=30, price=0.5, days_to_resolution=dias),
                        state(), LIMITS_TRES).approved, dias


def test_cartera_abierta_no_frena_por_concentracion():
    """Sin techos: una sola apuesta puede llevarse casi todo el capital."""
    s = state(exposure_by_market={"0xm1": 300.0}, exposure_total=300.0,
              exposure_by_strategy={"copy_trading": 300.0})
    d = evaluate(order(size=100, price=0.5, days_to_resolution=1,
                       strategy_budget_pct=1.0), s, LIMITS_TRES)
    assert d.approved, d.reason


# --- frenos de capital apagados (pedido del dueño, 2026-08-22) ---
# "Quita todo tipo de stop o freno."

LIMITS_SIN_FRENO = Limits(
    max_pct_per_market=1.0, max_pct_per_category=1.0,
    max_pct_per_copied_wallet=1.0, max_total_exposure_pct=1.0,
    daily_stop_loss_pct=1.0, min_order_usdc=10.0, max_drawdown_pct=1.0,
    max_days_to_resolution=3, slow_days=3, max_pct_slow=1.0)


def test_el_stop_diario_apagado_no_frena_ni_perdiendo_casi_todo():
    """El día va en -80%, cuatro veces peor que el viejo stop del 20%, y el
    bot sigue comprando. Es exactamente lo que el dueño pidió."""
    s = state(cash=100.0, equity=100.0, day_start_equity=500.0,
              starting_equity=500.0, exposure_total=0.0)
    assert evaluate(order(size=30, price=0.5, days_to_resolution=1),
                    s, LIMITS_SIN_FRENO).approved


def test_el_freno_total_apagado_no_frena_ni_con_la_cuenta_deshecha():
    """Caída del 80% desde el capital inicial: el viejo freno total saltaba
    al 60% y ya no queda nada que detenga al bot."""
    s = state(cash=100.0, equity=100.0, day_start_equity=100.0,
              starting_equity=500.0, exposure_total=0.0)
    assert evaluate(order(size=30, price=0.5, days_to_resolution=1),
                    s, LIMITS_SIN_FRENO).approved


def test_cero_no_apaga_el_stop_lo_vuelve_permanente():
    """Trampa que hay que dejar documentada: el porcentaje que apaga es 1.0.
    Con 0.0 el stop saltaría ante la primera pérdida, no se desactivaría."""
    limites = Limits(
        max_pct_per_market=1.0, max_pct_per_category=1.0,
        max_pct_per_copied_wallet=1.0, max_total_exposure_pct=1.0,
        daily_stop_loss_pct=0.0, min_order_usdc=10.0,
        max_days_to_resolution=3, slow_days=3, max_pct_slow=1.0)
    s = state(cash=300.0, equity=499.0, day_start_equity=500.0,
              starting_equity=500.0)
    d = evaluate(order(size=30, price=0.5, days_to_resolution=1), s, limites)
    assert not d.approved and "stop diario" in d.reason


def test_sin_frenos_el_kill_switch_sigue_siendo_del_dueno(tmp_path):
    """Lo único que puede parar al bot ahora es la mano del dueño."""
    from pmbot.db import connect
    from pmbot.risk.manager import RiskManager
    conn = connect(tmp_path / "k.db")
    rm = RiskManager(conn, {"daily_stop_loss_pct": 1.0,
                            "max_drawdown_pct": 1.0}, tmp_path)
    assert not rm.kill_switch_on()
    rm.kill_file.touch()
    assert rm.kill_switch_on()


# --- premio mínimo (pedido del dueño, 2026-08-24) ---
# "No quiero ganar 2 dólares, mínimo 15 o 20."

LIMITS_PREMIO = Limits(
    max_pct_per_market=1.0, max_pct_per_category=1.0,
    max_pct_per_copied_wallet=1.0, max_total_exposure_pct=1.0,
    daily_stop_loss_pct=1.0, min_order_usdc=10.0, max_drawdown_pct=1.0,
    max_days_to_resolution=3, slow_days=3, max_pct_slow=1.0,
    min_ganancia_usdc=15.0)


def test_la_apuesta_cara_de_premio_flaco_se_rechaza():
    """El caso que trajo el dueño: pagar 0.83 para ganar unos centavos por
    dólar. Con $12 en juego se cobrarían $14.46: $2.46 de ganancia."""
    d = evaluate(order(size=12.0 / 0.83, price=0.83, days_to_resolution=1),
                 state(cash=200.0), LIMITS_PREMIO)
    assert not d.approved and "premio flaco" in d.reason


def test_ni_agrandando_la_apuesta_pasa_un_precio_muy_alto():
    """A 0.83 harían falta $73 para ganar $15: el resto de límites lo corta
    antes, pero la regla también lo ve."""
    d = evaluate(order(size=25 / 0.83, price=0.83, days_to_resolution=1),
                 state(cash=200.0), LIMITS_PREMIO)
    assert not d.approved and "premio flaco" in d.reason


def test_una_apuesta_de_verdad_a_precio_medio_pasa():
    # $25 a 0.60 -> se cobran $41.67, o sea $16.67 de ganancia.
    d = evaluate(order(size=25 / 0.60, price=0.60, days_to_resolution=1),
                 state(cash=200.0), LIMITS_PREMIO)
    assert d.approved, d.reason


def test_apuesta_chica_a_precio_bueno_tambien_se_rechaza():
    """La regla no es solo sobre el precio: $10 a 0.50 solo ganan $10."""
    d = evaluate(order(size=20.0, price=0.50, days_to_resolution=1),
                 state(cash=200.0), LIMITS_PREMIO)
    assert not d.approved and "premio flaco" in d.reason


def test_cuanto_mas_barato_menos_hace_falta_arriesgar():
    """A 0.40 bastan $10 para ganar $15; a 0.60 hacen falta $22.5."""
    assert evaluate(order(size=25.0, price=0.40, days_to_resolution=1),
                    state(cash=200.0), LIMITS_PREMIO).approved
    d = evaluate(order(size=30.0 / 0.60, price=0.60, days_to_resolution=1),
                 state(cash=200.0), LIMITS_PREMIO)
    assert d.approved, d.reason


def test_la_regla_apagada_no_estorba():
    """Con 0 la regla no existe: las ventas y el modo viejo siguen igual."""
    assert evaluate(order(size=20.0, price=0.90, days_to_resolution=1),
                    state(cash=200.0), LIMITS_TRES).approved


def test_vender_nunca_se_bloquea_por_premio():
    assert evaluate(order(side="SELL", size=40.0, price=0.95),
                    state(cash=200.0), LIMITS_PREMIO).approved


# --- categorías vetadas (evidencia del 2026-08-24) ---

LIMITS_VETO = Limits(
    max_pct_per_market=1.0, max_pct_per_category=1.0,
    max_pct_per_copied_wallet=1.0, max_total_exposure_pct=1.0,
    daily_stop_loss_pct=1.0, min_order_usdc=10.0, max_drawdown_pct=1.0,
    max_days_to_resolution=3, slow_days=3, max_pct_slow=1.0,
    categorias_vetadas=frozenset({"esports"}))


def test_no_se_compra_nada_de_una_categoria_vetada():
    """Esports perdió 7 de 7 posiciones: $120.56 invertidos, $0.00 de vuelta."""
    d = evaluate(order(category="esports", size=50.0, price=0.50,
                       days_to_resolution=1), state(cash=200.0), LIMITS_VETO)
    assert not d.approved and "vetada" in d.reason


def test_el_veto_no_distingue_mayusculas():
    d = evaluate(order(category="Esports", size=50.0, price=0.50,
                       days_to_resolution=1), state(cash=200.0), LIMITS_VETO)
    assert not d.approved and "vetada" in d.reason


def test_el_veto_no_toca_a_los_deportes_de_verdad():
    assert evaluate(order(category="sports", size=50.0, price=0.50,
                          days_to_resolution=1),
                    state(cash=200.0), LIMITS_VETO).approved


def test_vender_lo_vetado_siempre_se_permite():
    """De una posición vetada hay que poder salir."""
    assert evaluate(order(category="esports", side="SELL", size=40.0,
                          price=0.30), state(cash=200.0), LIMITS_VETO).approved


# --- piso de precio (evidencia del 2026-08-25) ---

LIMITS_PISO = Limits(
    max_pct_per_market=1.0, max_pct_per_category=1.0,
    max_pct_per_copied_wallet=1.0, max_total_exposure_pct=1.0,
    daily_stop_loss_pct=1.0, min_order_usdc=10.0, max_drawdown_pct=1.0,
    max_days_to_resolution=3, slow_days=3, max_pct_slow=1.0,
    min_precio_entrada=0.35,
    estrategias_sin_piso=frozenset({"arbitrage", "ladder_arb"}))


def test_no_se_compran_tapados():
    """Por debajo de 0.35: 8 posiciones cerradas, 1 acierto, -63% de ROI.
    El caso concreto fue las Sparks a 0.16."""
    d = evaluate(order(size=178.0, price=0.16, days_to_resolution=1),
                 state(cash=200.0), LIMITS_PISO)
    assert not d.approved and "tapado" in d.reason


def test_la_franja_que_gana_si_pasa():
    """0.35-0.50 es la única franja rentable del historial (+8.1%)."""
    assert evaluate(order(size=60.0, price=0.42, days_to_resolution=1),
                    state(cash=200.0), LIMITS_PISO).approved


def test_el_arbitraje_no_tiene_piso():
    """En un arbitraje el precio suelto no dice nada: lo que manda es que
    los dos lados juntos cuesten menos de lo que pagan."""
    assert evaluate(order(strategy="ladder_arb", size=178.0, price=0.16,
                          days_to_resolution=0), state(cash=200.0),
                    LIMITS_PISO).approved


def test_el_piso_apagado_no_estorba():
    assert evaluate(order(size=178.0, price=0.16, days_to_resolution=1),
                    state(cash=200.0), LIMITS_TRES).approved

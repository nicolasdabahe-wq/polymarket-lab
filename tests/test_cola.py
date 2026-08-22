from pmbot.smart_money.cola import Candidata, ordenar_cola


def test_las_nunca_testeadas_van_primero():
    cola = ordenar_cola([
        Candidata("0xvieja", testeada_en="2026-08-01"),
        Candidata("0xnueva", actividad=3),
        Candidata("0xreciente", testeada_en="2026-08-22"),
    ], cupo=10)
    assert [c.wallet for c in cola] == ["0xnueva", "0xvieja", "0xreciente"]


def test_entre_nuevas_manda_la_actividad():
    cola = ordenar_cola([
        Candidata("0xpoca", actividad=1),
        Candidata("0xmucha", actividad=50),
        Candidata("0xmedia", actividad=10),
    ], cupo=10)
    assert [c.wallet for c in cola] == ["0xmucha", "0xmedia", "0xpoca"]


def test_el_cupo_corta():
    cola = ordenar_cola([Candidata(f"0x{i}", actividad=i) for i in range(10)],
                        cupo=3)
    assert len(cola) == 3


def test_deduplica_y_fusiona_fuentes():
    cola = ordenar_cola([
        Candidata("0xw", username="", actividad=5, fuentes={"cinta"}),
        Candidata("0xw", username="Fulano", actividad=2,
                  fuentes={"leaderboard"}),
    ], cupo=10)
    assert len(cola) == 1
    assert cola[0].username == "Fulano"          # se queda el nombre conocido
    assert cola[0].actividad == 5                # y la mejor señal
    assert cola[0].fuentes == {"cinta", "leaderboard"}


def test_una_wallet_conocida_por_dos_lados_no_pierde_su_turno():
    # Si una fuente la trae sin veredicto y otra con uno viejo, la wallet
    # no debe pasar por nueva ni quedar al final: manda el dato que existe.
    cola = ordenar_cola([
        Candidata("0xw", testeada_en=None, actividad=1),
        Candidata("0xw", testeada_en="2026-08-02"),
        Candidata("0xotra", testeada_en="2026-08-01"),
    ], cupo=10)
    assert [c.wallet for c in cola] == ["0xotra", "0xw"]


def test_cupo_cero_no_devuelve_nada():
    assert ordenar_cola([Candidata("0xw")], cupo=0) == []

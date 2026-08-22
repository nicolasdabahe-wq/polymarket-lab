"""A quién le toca backtest en esta corrida.

El universo es más grande de lo que se puede testear de una: cada wallet
cuesta varias llamadas a la API. Entonces se rota, con dos principios:

1. NADIE se descarta sin números. Ninguna wallet queda fuera de la cola
   por su antigüedad, su cantidad de trades ni su forma de operar. Lo
   único que decide es el backtest, y para eso primero hay que correrlo.

2. Prioridad a quien no tiene veredicto. Primero las nunca testeadas
   (ordenadas por cuánto se las ve operar en grande), después las más
   viejas de revisar. Así el universo se cubre entero con el tiempo en
   vez de re-testear siempre a las mismas.

Pura: recibe candidatas con su último test y devuelve el orden.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candidata:
    wallet: str
    username: str = ""
    # None = nunca testeada. Si no, ISO del último backtest.
    testeada_en: str | None = None
    # Señal de actividad: cuántas veces se la vio operar en grande.
    actividad: int = 0
    fuentes: set[str] = field(default_factory=set)


def ordenar_cola(candidatas: list[Candidata], cupo: int) -> list[Candidata]:
    """Las que entran a esta corrida, en orden. Deduplica por wallet."""
    unicas: dict[str, Candidata] = {}
    for c in candidatas:
        previa = unicas.get(c.wallet)
        if previa is None:
            unicas[c.wallet] = c
            continue
        # Fusionar lo que sepamos de cada fuente sobre la misma wallet.
        previa.username = previa.username or c.username
        previa.actividad = max(previa.actividad, c.actividad)
        previa.fuentes |= c.fuentes
        if previa.testeada_en is None or (
                c.testeada_en and c.testeada_en < previa.testeada_en):
            previa.testeada_en = previa.testeada_en or c.testeada_en

    def clave(c: Candidata) -> tuple:
        # 0 = nunca testeada (primero). Entre esas, más actividad primero.
        # Entre las testeadas, la más vieja primero.
        if c.testeada_en is None:
            return (0, -c.actividad, c.wallet)
        return (1, c.testeada_en, c.wallet)

    return sorted(unicas.values(), key=clave)[:max(cupo, 0)]

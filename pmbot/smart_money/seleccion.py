"""¿Con qué umbral copiar a una wallet, y conviene copiarla?

El backtest simula copiarla con varios umbrales de tamaño. Elegir sin más
el umbral de mejor ROI premia la casualidad: una wallet con seis copias y
+77% parece la mejor del mundo y no ha demostrado nada.

Dos exigencias, sacadas de medir ballenas reales el 2026-08-22:

1. MUESTRA. Solo cuentan los umbrales con al menos min_copies copias.
   sainttroplay daba +77.5% con 6 copias en todos los umbrales; rn1 daba
   +23.8% con UNA. Nada de eso es evidencia.

2. CONSISTENCIA. Entre los umbrales con muestra suficiente, la mayoría
   tiene que ser rentable. Una wallet buena gana en casi todos:
   BreakTheBank dio +4.5%, +6.3%, +15.1%, +2.0% y +5.0% con 34 a 58
   copias. Una mala muestra un pico suelto entre pérdidas: swisstony
   perdía -2.1% en 83 copias y -5.7% en 28, con un +17.2% en apenas 10.

Lo que NO decide: cuántos trades por día haga. Un creador de mercado con
apuestas grandes rentables es copiable en esas apuestas grandes, y eso lo
dicen estas dos reglas mejor que cualquier perfil de comportamiento.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Opcion:
    umbral: float
    copias: int
    roi: float


@dataclass
class Eleccion:
    umbral: float
    roi: float
    veredicto: str      # copiable | rechazada | sin_datos
    motivo: str


def elegir_umbral(opciones: list[Opcion], min_copias: int,
                  min_roi: float) -> Eleccion:
    """Umbral de copia y veredicto para una wallet. Pura."""
    if not opciones:
        return Eleccion(0.0, 0.0, "sin_datos", "sin backtest")

    validas = [o for o in opciones if o.copias >= min_copias]
    if not validas:
        mejor_muestra = max(opciones, key=lambda o: o.copias)
        return Eleccion(mejor_muestra.umbral, 0.0, "sin_datos",
                        f"muestra insuficiente (máx {mejor_muestra.copias} "
                        f"copias, hacen falta {min_copias})")

    positivas = [o for o in validas if o.roi >= min_roi]
    if len(positivas) * 2 < len(validas):
        mejor = max(validas, key=lambda o: o.roi)
        return Eleccion(mejor.umbral, mejor.roi, "rechazada",
                        f"gana en {len(positivas)} de {len(validas)} umbrales "
                        f"con muestra: no es consistente")

    elegida = max(positivas, key=lambda o: o.roi)
    return Eleccion(elegida.umbral, elegida.roi, "copiable",
                    f"rentable en {len(positivas)} de {len(validas)} umbrales "
                    f"({elegida.copias} copias sobre ${elegida.umbral:,.0f})")

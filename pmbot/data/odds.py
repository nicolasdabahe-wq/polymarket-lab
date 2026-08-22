"""Líneas de las casas de apuestas profesionales, vía The Odds API.

Para qué: nuestro modelo de béisbol es matemática decente, pero Pinnacle
tiene equipos enteros dedicados a poner el precio correcto. Comparar SU
línea contra Polymarket es el detector de valor más honesto que existe:
donde difieren, casi siempre el equivocado es Polymarket.

Presupuesto: el tier gratis da 500 créditos/mes y cada consulta de un
deporte cuesta 1 crédito por región. Con cache de 2 horas son ~12
consultas al día: sobra margen. El header x-requests-remaining se loguea
para vigilar el consumo.

De-vig: las cuotas traen el margen de la casa (la suma de probabilidades
implícitas pasa de 1). Se normaliza para recuperar la probabilidad real:
    p_i = (1/cuota_i) / Σ(1/cuota_j)
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field

from ..http import HttpClient

log = logging.getLogger("pmbot.data.odds")

ODDS_BASE = "https://api.the-odds-api.com/v4"
CACHE_SEGUNDOS = 2 * 3600
# Las casas "sharp" (línea afilada, límites altos) pesan más que las
# recreativas. Pinnacle es la referencia mundial.
CASAS_SHARP = ("pinnacle", "betonlineag", "lowvig")


def devig(cuotas: list[float]) -> list[float]:
    """Probabilidades reales a partir de cuotas decimales. Pura."""
    if not cuotas or any(c <= 1.0 for c in cuotas):
        return []
    brutas = [1.0 / c for c in cuotas]
    total = sum(brutas)
    return [b / total for b in brutas]


@dataclass
class LineaJuego:
    local: str
    visitante: str
    inicio_utc: str
    prob_local: float       # ya sin el margen de la casa
    prob_visitante: float
    casas: int              # cuántas casas respaldan la línea
    sharp: bool             # True si viene de una casa sharp


def consenso(eventos: list[dict]) -> list[LineaJuego]:
    """Convierte la respuesta cruda de la API en líneas de-vigueadas. Pura.

    Prefiere la casa sharp si está; si no, la mediana de todas las casas.
    """
    fuera: list[LineaJuego] = []
    for ev in eventos or []:
        local = ev.get("home_team") or ""
        visitante = ev.get("away_team") or ""
        if not local or not visitante:
            continue
        por_casa: dict[str, tuple[float, float]] = {}
        for casa in ev.get("bookmakers") or []:
            for mercado in casa.get("markets") or []:
                if mercado.get("key") != "h2h":
                    continue
                precios = {o.get("name"): float(o.get("price") or 0)
                           for o in mercado.get("outcomes") or []}
                if local in precios and visitante in precios:
                    probs = devig([precios[local], precios[visitante]])
                    if probs:
                        por_casa[casa.get("key") or ""] = (probs[0], probs[1])
        if not por_casa:
            continue
        sharp = next((c for c in CASAS_SHARP if c in por_casa), None)
        if sharp:
            p_local, p_visit = por_casa[sharp]
        else:
            p_local = statistics.median(v[0] for v in por_casa.values())
            p_visit = statistics.median(v[1] for v in por_casa.values())
        fuera.append(LineaJuego(
            local=local, visitante=visitante,
            inicio_utc=ev.get("commence_time") or "",
            prob_local=p_local, prob_visitante=p_visit,
            casas=len(por_casa), sharp=sharp is not None))
    return fuera


class OddsClient:
    def __init__(self, http: HttpClient, api_key: str | None) -> None:
        self.http = http
        self.api_key = api_key
        self._cache: dict[str, tuple[float, list[LineaJuego]]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def lineas(self, deporte: str = "baseball_mlb") -> list[LineaJuego]:
        """Líneas del deporte, con cache de 2h para cuidar los créditos."""
        if not self.enabled:
            return []
        hit = self._cache.get(deporte)
        if hit and time.monotonic() - hit[0] < CACHE_SEGUNDOS:
            return hit[1]
        try:
            crudo = await self.http.get_json(
                f"{ODDS_BASE}/sports/{deporte}/odds",
                params={"apiKey": self.api_key, "regions": "eu",
                        "markets": "h2h", "oddsFormat": "decimal"})
        except Exception as exc:
            log.warning("The Odds API falló (%s); sigo con el modelo propio",
                        exc)
            return hit[1] if hit else []
        resultado = consenso(crudo if isinstance(crudo, list) else [])
        self._cache[deporte] = (time.monotonic(), resultado)
        log.info("odds: %d juegos de %s con línea sharp/consenso",
                 len(resultado), deporte)
        return resultado

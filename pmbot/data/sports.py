"""Datos deportivos oficiales, gratis y sin llave: MLB statsapi.

statsapi.mlb.com es la API pública de la MLB. De ahí salen los tres
insumos que el modelo necesita:

  - carreras anotadas y recibidas por equipo (mejor predictor que el
    récord: ganar juegos cerrados es en buena parte suerte y no se
    sostiene, las carreras sí)
  - el pitcher abridor probable de cada juego, con su ERA
  - quién juega de local

Se cachea en memoria por unos minutos: los datos cambian una vez al día.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..http import HttpClient

log = logging.getLogger("pmbot.data.sports")

MLB_BASE = "https://statsapi.mlb.com/api/v1"

# statsapi.mlb.com está detrás de un cortafuegos que responde 406 Not
# Acceptable a los clientes que no parecen un navegador. Con el User-Agent
# por defecto ("pmbot/0.1") la API funciona desde una IP doméstica y falla
# desde un datacenter: el 2026-08-27 el droplet llevaba días recibiendo 406
# en /schedule, `juegos()` lanzaba, y sports_value no había hecho ni una
# apuesta desde que se encendió. No hay nada que burlar aquí — la API es
# pública y gratuita — solo hace falta pedir en el formato que espera.
CABECERAS_MLB = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
CACHE_SEGUNDOS = 900


@dataclass
class EquipoMLB:
    nombre: str
    victorias: int
    derrotas: int
    carreras_a_favor: int
    carreras_en_contra: int


@dataclass
class PitcherMLB:
    nombre: str
    era: float | None = None
    entradas: float = 0.0


@dataclass
class JuegoMLB:
    fecha: str
    visitante: str
    local: str
    inicio_utc: str
    pitcher_visitante: PitcherMLB | None = None
    pitcher_local: PitcherMLB | None = None
    equipos: dict[str, EquipoMLB] = field(default_factory=dict)


class MlbClient:
    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        clave = f"{path}:{sorted(params.items())}"
        hit = self._cache.get(clave)
        if hit and time.monotonic() - hit[0] < CACHE_SEGUNDOS:
            return hit[1]
        data = await self.http.get_json(f"{MLB_BASE}/{path}", params=params,
                                        headers=CABECERAS_MLB)
        self._cache[clave] = (time.monotonic(), data)
        return data

    async def equipos(self, temporada: int) -> dict[str, EquipoMLB]:
        """Todos los equipos con sus carreras, indexados por nombre."""
        data = await self._get("standings", {
            "leagueId": "103,104", "season": temporada,
            "standingsTypes": "regularSeason"})
        fuera: dict[str, EquipoMLB] = {}
        for division in (data or {}).get("records", []):
            for t in division.get("teamRecords", []):
                nombre = (t.get("team") or {}).get("name") or ""
                rec = t.get("leagueRecord") or {}
                if not nombre:
                    continue
                fuera[nombre] = EquipoMLB(
                    nombre=nombre,
                    victorias=int(rec.get("wins") or 0),
                    derrotas=int(rec.get("losses") or 0),
                    carreras_a_favor=int(t.get("runsScored") or 0),
                    carreras_en_contra=int(t.get("runsAllowed") or 0))
        return fuera

    async def juegos(self, fecha: str) -> list[JuegoMLB]:
        """Juegos de una fecha (YYYY-MM-DD) con abridores probables."""
        data = await self._get("schedule", {
            "sportId": 1, "date": fecha,
            "hydrate": "probablePitcher,team"})
        ids: set[str] = set()
        crudos = []
        for dia in (data or {}).get("dates", []):
            for g in dia.get("games", []):
                crudos.append(g)
                for lado in ("away", "home"):
                    p = (g.get("teams", {}).get(lado, {})
                         .get("probablePitcher") or {})
                    if p.get("id"):
                        ids.add(str(p["id"]))
        eras = await self.eras(sorted(ids)) if ids else {}

        fuera: list[JuegoMLB] = []
        for g in crudos:
            def abridor(lado: str) -> PitcherMLB | None:
                p = (g.get("teams", {}).get(lado, {})
                     .get("probablePitcher") or {})
                if not p.get("id"):
                    return None
                era, ip = eras.get(str(p["id"]), (None, 0.0))
                return PitcherMLB(p.get("fullName", ""), era, ip)

            fuera.append(JuegoMLB(
                fecha=fecha,
                visitante=(g.get("teams", {}).get("away", {})
                           .get("team", {}).get("name") or ""),
                local=(g.get("teams", {}).get("home", {})
                       .get("team", {}).get("name") or ""),
                inicio_utc=g.get("gameDate") or "",
                pitcher_visitante=abridor("away"),
                pitcher_local=abridor("home")))
        return fuera

    async def eras(self, ids: list[str]) -> dict[str, tuple[float | None, float]]:
        """ERA y entradas lanzadas de varios pitchers, en una sola llamada."""
        if not ids:
            return {}
        data = await self._get("people", {
            "personIds": ",".join(ids),
            "hydrate": "stats(group=pitching,type=season)"})
        fuera: dict[str, tuple[float | None, float]] = {}
        for p in (data or {}).get("people", []):
            era, ip = None, 0.0
            for s in p.get("stats", []):
                sp = (s.get("splits") or [{}])[0].get("stat", {})
                try:
                    if sp.get("era") not in (None, "-", "-.--"):
                        era = float(sp["era"])
                    ip = float(sp.get("inningsPitched") or 0)
                except (TypeError, ValueError):
                    pass
            fuera[str(p.get("id"))] = (era, ip)
        return fuera

"""Nuestra propia opinión sobre béisbol, contra el precio de Polymarket.

No se trata de adivinar mejor que las casas de apuestas — ellas tienen
equipos de matemáticos y Polymarket sigue sus líneas. Se trata de tener un
número propio y apostar SOLO cuando el mercado se aleja mucho de él, que
es donde suele haber error de precio: juegos de poco volumen, líneas que
tardan en moverse, mercados que nadie está mirando.

El modelo, con tres piezas que en béisbol están bien documentadas:

1. FUERZA REAL DEL EQUIPO — expectativa pitagórica de Bill James:
       p = CF^1.83 / (CF^1.83 + CC^1.83)
   Se usa carreras anotadas y recibidas, no el récord. Ganar juegos
   cerrados es en buena parte suerte y no se sostiene; las carreras sí.
   Medido el 2026-08-22: Brewers 80-49 con .628 pitagórica, Rockies 50-78
   con .413 — el récord exagera la diferencia, las carreras la ordenan.

2. ENFRENTAMIENTO — fórmula Log5:
       P(A gana a B) = (pA - pA·pB) / (pA + pB - 2·pA·pB)
   Convierte dos fuerzas absolutas en una probabilidad de duelo.

3. ABRIDOR Y LOCALÍA — el pitcher que abre decide buena parte del juego.
   Su ERA contra el promedio de la liga se traduce a carreras evitadas en
   una apertura típica, y de ahí a probabilidad. Se aplica a media fuerza
   porque la pitagórica del equipo YA incluye a ese pitcher en su
   temporada: contarlo entero sería contarlo dos veces. La ventaja de
   local en MLB ronda el 54%.

Todo puro y testeable. El límite honesto: esto NO sabe de lesiones de
última hora, clima ni bullpen quemado, así que solo opera antes del
partido y solo cuando la discrepancia es grande.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

log = logging.getLogger("pmbot.strategies.sports_value")

EXPONENTE_PITAGORICO = 1.83
ERA_LIGA = 4.10          # promedio MLB moderno
ENTRADAS_APERTURA = 5.5  # duración típica de una apertura
CARRERA_A_PROB = 0.095   # una carrera vale ~9,5 puntos de probabilidad
IP_MINIMAS = 30.0        # menos que esto, el ERA es ruido
TOPE_AJUSTE_PITCHER = 0.10


def pitagorica(carreras_a_favor: float, carreras_en_contra: float) -> float:
    """Fuerza del equipo según sus carreras. Pura."""
    if carreras_a_favor <= 0 or carreras_en_contra <= 0:
        return 0.5
    cf = carreras_a_favor ** EXPONENTE_PITAGORICO
    cc = carreras_en_contra ** EXPONENTE_PITAGORICO
    return cf / (cf + cc)


def log5(fuerza_a: float, fuerza_b: float) -> float:
    """P(A le gana a B) a partir de dos fuerzas absolutas. Pura."""
    a = min(max(fuerza_a, 1e-6), 1 - 1e-6)
    b = min(max(fuerza_b, 1e-6), 1 - 1e-6)
    denom = a + b - 2 * a * b
    if denom <= 0:
        return 0.5
    return (a - a * b) / denom


def ajuste_pitcher(era: float | None, entradas: float,
                   peso: float = 0.5) -> float:
    """Puntos de probabilidad que suma (o resta) el abridor. Puro.

    Se mide contra el promedio de la liga y se aplica a `peso` porque la
    pitagórica del equipo ya lo incluye. Sin muestra suficiente, cero.
    """
    if era is None or entradas < IP_MINIMAS:
        return 0.0
    carreras_evitadas = (ERA_LIGA - era) * (ENTRADAS_APERTURA / 9.0)
    ajuste = carreras_evitadas * CARRERA_A_PROB * peso
    return max(min(ajuste, TOPE_AJUSTE_PITCHER), -TOPE_AJUSTE_PITCHER)


def prob_local(fuerza_local: float, fuerza_visitante: float,
               ajuste_local: float = 0.0, ajuste_visitante: float = 0.0,
               ventaja_local: float = 0.035) -> float:
    """Probabilidad de que gane el local, con todo aplicado. Pura."""
    base = log5(fuerza_local, fuerza_visitante)
    p = base + ventaja_local + ajuste_local - ajuste_visitante
    return min(max(p, 0.02), 0.98)


# --- lectura de las preguntas de Polymarket ---

# Apodos que necesitan dos palabras para no confundirse entre sí.
AMBIGUOS = ("red sox", "white sox", "blue jays")


def apodo(nombre_equipo: str) -> str:
    """Clave corta y comparable de un equipo ('New York Yankees' -> 'yankees',
    'Boston Red Sox' -> 'red sox'). Pura."""
    limpio = re.sub(r"[^a-z ]", "", nombre_equipo.lower()).strip()
    for amb in AMBIGUOS:
        if limpio.endswith(amb):
            return amb
    partes = limpio.split()
    return partes[-1] if partes else ""


@dataclass
class PreguntaMLB:
    equipo: str          # apodo del equipo por el que se pregunta
    rival: str = ""      # apodo del rival, si la pregunta lo nombra


# "Will Deportivo Toluca FC win on 2026-08-22?" / "Atlanta Braves vs. Milwaukee Brewers"
GANA_RE = re.compile(r"^will\s+(?P<equipo>.+?)\s+win\s+on\s+\d{4}-\d{2}-\d{2}\??$",
                     re.IGNORECASE)
VS_RE = re.compile(r"^(?P<a>[^:]+?)\s+vs\.?\s+(?P<b>[^:]+?)$", re.IGNORECASE)


def leer_pregunta(pregunta: str) -> PreguntaMLB | None:
    """Extrae de qué equipo habla el mercado. None si no es un ganador
    simple (spreads, over/under, carreras exactas: el modelo no los cubre).
    """
    texto = (pregunta or "").strip()
    if not texto or ":" in texto:
        return None            # 'Spread:', 'O/U', 'Exact Score:' fuera
    m = GANA_RE.match(texto)
    if m:
        return PreguntaMLB(equipo=apodo(m.group("equipo")))
    m = VS_RE.match(texto)
    if m:
        return PreguntaMLB(equipo=apodo(m.group("a")), rival=apodo(m.group("b")))
    return None


# --- estrategia ---

import json as _json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from ..risk import OrderRequest
from .sizing import kelly_usdc


class SportsValueStrategy:
    """Compara nuestro modelo de béisbol contra el precio de Polymarket."""

    name = "sports_value"

    def __init__(self, conn: sqlite3.Connection, mlb: Any, gamma: Any,
                 broker: Any, cfg: dict[str, Any],
                 market_store: Any = None, odds: Any = None) -> None:
        self.conn = conn
        self.mlb = mlb
        self.gamma = gamma
        self.broker = broker
        self.market_store = market_store
        self.odds = odds
        self.enabled = bool(cfg.get("enabled", True))
        self.budget_pct = float(cfg.get("budget_pct", 0.25))
        self.min_edge = float(cfg.get("min_edge", 0.08))
        self.max_entry = float(cfg.get("max_entry_price", 0.75))
        self.min_trade_usdc = float(cfg.get("min_trade_usdc", 12.0))
        self.kelly_fraction = float(cfg.get("kelly_fraction", 0.25))
        self.max_trade_pct = float(cfg.get("max_trade_pct", 0.12))
        self.peso_pitcher = float(cfg.get("peso_pitcher", 0.5))
        self.ventaja_local = float(cfg.get("ventaja_local", 0.035))
        # Antelación mínima: el modelo no sabe del partido en curso.
        self.minutos_antes = float(cfg.get("min_minutos_antes", 15))
        # Ligas de fútbol con línea sharp. Cada liga consultada cuesta 1
        # crédito de The Odds API por llamada: con cache de 6h y tres ligas
        # más MLB son ~480/mes, dentro del tier gratis de 500.
        self.ligas_futbol = list(cfg.get("soccer_leagues") or [])

    async def _fuerzas(self) -> dict[str, float]:
        temporada = datetime.now(timezone.utc).year
        equipos = await self.mlb.equipos(temporada)
        return {apodo(n): pitagorica(e.carreras_a_favor, e.carreras_en_contra)
                for n, e in equipos.items()}

    async def scan_and_execute(self) -> list[str]:
        if not self.enabled:
            return []
        ahora = datetime.now(timezone.utc)
        # Traer los mercados de béisbol al cache: los juegos del día no
        # entran en el top de volumen hasta que la gente apuesta, y son
        # justamente los que el modelo quiere mirar.
        if self.market_store is not None:
            try:
                mercados = await self.gamma.fetch_by_tag("baseball", limit=200)
                if mercados:
                    self.market_store.upsert_markets(mercados)
            except Exception as exc:
                log.warning("no pude refrescar mercados de béisbol: %s", exc)
        # Líneas de las casas profesionales: cuando existen, mandan sobre
        # nuestro modelo (Pinnacle pone precios mejor que nuestra pitagórica).
        lineas = []
        if self.odds is not None and getattr(self.odds, "enabled", False):
            try:
                lineas = await self.odds.lineas("baseball_mlb")
            except Exception as exc:
                log.debug("odds no disponibles: %s", exc)
        self._lineas_por_par = {
            frozenset((apodo(l.local), apodo(l.visitante))): l for l in lineas}
        try:
            fuerzas = await self._fuerzas()
            juegos = []
            for delta in (0, 1):     # hoy y mañana (los mercados abren antes)
                fecha = (ahora + timedelta(days=delta)).date().isoformat()
                juegos.extend(await self.mlb.juegos(fecha))
        except Exception as exc:
            log.warning("datos de MLB no disponibles: %s", exc)
            return []
        if not juegos:
            return []

        ejecutadas: list[str] = []
        for juego in juegos:
            try:
                desc = await self._evaluar(juego, fuerzas, ahora)
            except Exception as exc:
                log.debug("juego %s falló: %s", juego.local, exc)
                continue
            if desc:
                ejecutadas.append(desc)
        ejecutadas.extend(await self._escanear_futbol(ahora))
        return ejecutadas

    # ---------- fútbol con línea sharp ----------

    def _mercados_ganador_futbol(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM markets WHERE active = 1 AND category = 'sports'
               AND question LIKE 'Will % win on ____-__-__?'""").fetchall()

    async def _escanear_futbol(self, ahora: datetime) -> list[str]:
        """Compara la línea sharp de cada liga configurada contra los
        mercados "Will X win on FECHA?" de Polymarket.

        En fútbol NO hay modelo propio de respaldo: sin línea sharp no se
        opina (el empate hace que cualquier heurística barata mienta).
        """
        if not (self.ligas_futbol and self.odds is not None
                and getattr(self.odds, "enabled", False)):
            return []
        mercados = self._mercados_ganador_futbol()
        if not mercados:
            return []
        from ..data.odds import nombre_coincide
        hechas: list[str] = []
        for liga in self.ligas_futbol:
            try:
                lineas = await self.odds.lineas(liga)
            except Exception as exc:
                log.debug("liga %s sin líneas: %s", liga, exc)
                continue
            for linea in lineas:
                try:
                    inicio = datetime.fromisoformat(
                        linea.inicio_utc.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if inicio - ahora < timedelta(minutes=self.minutos_antes):
                    continue
                for equipo, prob in ((linea.local, linea.prob_local),
                                     (linea.visitante, linea.prob_visitante)):
                    fila = self._mercado_del_equipo(
                        mercados, equipo, inicio, nombre_coincide)
                    if fila is None:
                        continue
                    desc = await self._apostar_binario(
                        fila, prob,
                        f"línea sharp {liga} ({linea.casas} casas)", inicio)
                    if desc:
                        hechas.append(desc)
        return hechas

    def _guardar_sharp(self, condition_id: str, prob_first: float,
                       fuente: str) -> None:
        """Registra el precio justo del mercado aunque no haya apuesta:
        las copias lo usan de escudo contra pagar de más."""
        with self.conn:
            self.conn.execute(
                """INSERT INTO sharp_lines (condition_id, prob_first, fuente,
                   updated_at) VALUES (?,?,?,?)
                   ON CONFLICT(condition_id) DO UPDATE SET
                     prob_first = excluded.prob_first,
                     fuente = excluded.fuente,
                     updated_at = excluded.updated_at""",
                (condition_id, prob_first, fuente,
                 datetime.now(timezone.utc).isoformat(timespec="seconds")))

    @staticmethod
    def _mercado_del_equipo(mercados: list[sqlite3.Row], equipo: str,
                            inicio: datetime, coincide: Any
                            ) -> sqlite3.Row | None:
        """El mercado "Will X win on FECHA?" de ESE equipo y ESE partido.
        La fecha de la pregunta puede diferir un día del arranque UTC
        (partidos nocturnos de América), se tolera ±1 día."""
        import re as _re
        for fila in mercados:
            q = fila["question"] or ""
            if not coincide(equipo, q):
                continue
            m = _re.search(r"win on (\d{4}-\d{2}-\d{2})", q)
            if not m:
                continue
            try:
                fecha = datetime.fromisoformat(m.group(1) + "T00:00:00+00:00")
            except ValueError:
                continue
            if abs((inicio - fecha).total_seconds()) <= 36 * 3600:
                return fila
        return None

    async def _apostar_binario(self, fila: sqlite3.Row, prob_yes: float,
                               fuente: str, inicio: datetime) -> str | None:
        """Evalúa YES y NO de un mercado binario contra la probabilidad
        sharp y ejecuta el lado con ventaja, si la hay."""
        tokens = _json.loads(fila["clob_token_ids"] or "[]")
        if len(tokens) != 2:
            return None
        self._guardar_sharp(fila["condition_id"], prob_yes, fuente)
        mejor = None
        for idx, (outcome, prob) in enumerate(
                (("Yes", prob_yes), ("No", 1.0 - prob_yes))):
            libro = await self.broker.clob.order_book(tokens[idx])
            ask = libro.best_ask
            if ask is None or ask <= 0.02 or ask > self.max_entry:
                continue
            ventaja = prob - ask
            if mejor is None or ventaja > mejor[0]:
                mejor = (ventaja, idx, outcome, prob, ask)
        if not mejor or mejor[0] < self.min_edge:
            return None
        ventaja, idx, outcome, prob, ask = mejor
        usdc = kelly_usdc(self.broker.equity(), ask, prob / ask - 1.0,
                          self.kelly_fraction, self.min_trade_usdc,
                          self.max_trade_pct)
        if usdc <= 0:
            return None
        razon = (f"{fuente}: {outcome} de «{fila['question'][:45]}» vale "
                 f"{prob:.0%} y el libro pide {ask:.0%} "
                 f"(ventaja {ventaja:+.0%})")
        fill = await self.broker.execute(
            f"soccer:{fila['condition_id']}:{idx}",
            OrderRequest(
                strategy=self.name, condition_id=fila["condition_id"],
                category="sports", token_id=tokens[idx], outcome=outcome,
                outcome_index=idx, side="BUY", size=usdc / ask,
                price=min(ask * 1.02, 0.99), reason=razon,
                strategy_budget_pct=self.budget_pct,
                days_to_resolution=max(
                    (inicio - datetime.now(timezone.utc)).total_seconds()
                    / 86400, 0.0),
                meta={"question": fila["question"], "sharp": prob,
                      "ask": ask}))
        if fill.status == "FILLED":
            log.info("SPORTS SHARP: %s", razon)
            return razon
        return None

    async def _evaluar(self, juego: Any, fuerzas: dict[str, float],
                       ahora: datetime) -> str | None:
        visitante, local = apodo(juego.visitante), apodo(juego.local)
        if visitante not in fuerzas or local not in fuerzas:
            return None
        # El partido tiene que estar por empezar, no en curso.
        try:
            inicio = datetime.fromisoformat(
                juego.inicio_utc.replace("Z", "+00:00"))
        except ValueError:
            return None
        if inicio - ahora < timedelta(minutes=self.minutos_antes):
            return None

        linea = getattr(self, "_lineas_por_par", {}).get(
            frozenset((local, visitante)))
        if linea is not None:
            # La línea sharp de-vigueada ES la probabilidad: nadie pone
            # este precio mejor que Pinnacle y compañía.
            p_local = (linea.prob_local if apodo(linea.local) == local
                       else linea.prob_visitante)
            fuente = f"línea sharp ({linea.casas} casas)"
        else:
            aj_local = ajuste_pitcher(
                juego.pitcher_local.era, juego.pitcher_local.entradas,
                self.peso_pitcher) if juego.pitcher_local else 0.0
            aj_visitante = ajuste_pitcher(
                juego.pitcher_visitante.era, juego.pitcher_visitante.entradas,
                self.peso_pitcher) if juego.pitcher_visitante else 0.0
            p_local = prob_local(fuerzas[local], fuerzas[visitante],
                                 aj_local, aj_visitante, self.ventaja_local)
            fuente = "modelo propio"

        mercado = self._buscar_mercado(visitante, local, inicio)
        if not mercado:
            return None
        tokens = _json.loads(mercado["clob_token_ids"] or "[]")
        salidas = _json.loads(
            (_json.loads(mercado["raw"] or "{}") or {}).get("outcomes") or "[]")
        if len(tokens) != 2 or len(salidas) != 2:
            return None

        # Evaluar los dos lados y quedarse con el de más ventaja.
        self._guardar_sharp(mercado["condition_id"],
                            p_local if apodo(salidas[0]) == local
                            else 1.0 - p_local, fuente)
        mejor = None
        for idx, salida in enumerate(salidas):
            clave = apodo(salida)
            if clave == local:
                prob = p_local
            elif clave == visitante:
                prob = 1.0 - p_local
            else:
                return None      # los outcomes no son los equipos: no opinar
            libro = await self.broker.clob.order_book(tokens[idx])
            ask = libro.best_ask
            if ask is None or ask <= 0.02 or ask > self.max_entry:
                continue
            ventaja = prob - ask
            if mejor is None or ventaja > mejor[0]:
                mejor = (ventaja, idx, salida, prob, ask)
        if not mejor or mejor[0] < self.min_edge:
            return None
        ventaja, idx, salida, prob, ask = mejor

        # Retorno esperado por dólar = cuánto vale lo que compramos sobre
        # lo que pagamos. Con eso Kelly dimensiona (ver sizing.py).
        retorno = prob / ask - 1.0
        usdc = kelly_usdc(self.broker.equity(), ask, retorno,
                          self.kelly_fraction, self.min_trade_usdc,
                          self.max_trade_pct)
        if usdc <= 0:
            return None

        pit = ""
        if juego.pitcher_local and juego.pitcher_local.era:
            pit = (f"; abridores {juego.pitcher_visitante.nombre} "
                   f"({juego.pitcher_visitante.era}) vs "
                   f"{juego.pitcher_local.nombre} ({juego.pitcher_local.era})")
        razon = (f"{fuente}: {salida} vale {prob:.0%} y el libro pide "
                 f"{ask:.0%} (ventaja {ventaja:+.0%}; pitagórica "
                 f"{fuerzas[local]:.3f} local vs {fuerzas[visitante]:.3f} "
                 f"visitante{pit})")
        fill = await self.broker.execute(
            f"sports:{mercado['condition_id']}:{idx}",
            OrderRequest(
                strategy=self.name, condition_id=mercado["condition_id"],
                category="sports", token_id=tokens[idx], outcome=salida,
                outcome_index=idx, side="BUY", size=usdc / ask,
                price=min(ask * 1.02, 0.99), reason=razon,
                strategy_budget_pct=self.budget_pct,
                days_to_resolution=max(
                    (inicio - datetime.now(timezone.utc)).total_seconds()
                    / 86400, 0.0),
                meta={"question": mercado["question"], "modelo": prob,
                      "ask": ask}))
        if fill.status == "FILLED":
            log.info("SPORTS VALUE: %s", razon)
            return f"{mercado['question'][:60]} — {razon}"
        return None

    def _buscar_mercado(self, visitante: str, local: str,
                        inicio: datetime) -> sqlite3.Row | None:
        """El mercado de ganador simple de ese juego, verificando la fecha:
        dos equipos se enfrentan varias veces en una serie."""
        filas = self.conn.execute(
            """SELECT * FROM markets WHERE active = 1 AND category = 'sports'
               AND question LIKE '%vs%' AND question NOT LIKE '%:%'""").fetchall()
        for fila in filas:
            leida = leer_pregunta(fila["question"])
            if not leida or {leida.equipo, leida.rival} != {visitante, local}:
                continue
            crudo = _json.loads(fila["raw"] or "{}") or {}
            arranque = crudo.get("gameStartTime") or crudo.get("eventStartTime")
            if arranque:
                try:
                    cuando = datetime.fromisoformat(
                        str(arranque).replace("Z", "+00:00").replace(" ", "T", 1))
                    if abs((cuando - inicio).total_seconds()) > 6 * 3600:
                        continue      # es otro juego de la misma serie
                except ValueError:
                    pass
            return fila
        return None

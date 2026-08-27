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


# Mercados derivados: se juegan sobre el mismo partido pero no sobre quién
# gana. Comparten la forma «algo: A vs B» con el ganador de tenis, así que la
# lista es lo que los separa.
_DERIVADO = re.compile(
    r"\bset\b|\bspread\b|handicap|hándicap|\bo/u\b|over/under|\btotal|"
    r"\bwinner\b|inning|\bquarter\b|\bhalf\b|\bperiod\b|\bgame\b|"
    r"\bmap\b|\bcorners?\b|\bcards?\b",
    re.IGNORECASE)


def leer_partido(pregunta: str,
                 salidas: list[str] | None = None) -> tuple[str, str] | None:
    """Los dos participantes de un mercado de ganador de partido, o None.

    Existe por el tenis: Polymarket titula esos mercados «Winston-Salem Open:
    Juan Manuel Cerundolo vs Sebastian Baez», con el torneo delante, y
    `leer_pregunta` descarta de plano todo lo que lleve dos puntos. Resultado
    medido el 2026-08-27: 1.915 mercados de tenis abiertos y el bot miraba
    cero.

    Levantar esa restricción sin más metería basura, porque la misma forma la
    tienen los derivados: «Set 1 Winner: Cerundolo vs Baez», «Game Spread:
    Baez (-1.5) vs Cerundolo (+1.5)» y, en béisbol, «Will there be a run
    scored in the first inning?: Dodgers vs. Braves». Dos filtros los separan:

    · Las SALIDAS tienen que ser los dos participantes. Las props de béisbol
      se resuelven Yes/No, así que caen aquí solas — es el filtro fuerte,
      porque no depende de adivinar cómo se titula un torneo.
    · El prefijo no puede nombrar un derivado (set, spread, total, game...).
      Esto es lo que descarta al ganador de set, que sí tiene a los dos
      jugadores como salidas.

    Pura.
    """
    texto = (pregunta or "").strip()
    if not texto:
        return None
    prefijo, _, cola = texto.rpartition(":")
    if prefijo and (_DERIVADO.search(prefijo) or "?" in prefijo
                    or prefijo.lower().startswith("will ")):
        return None
    m = VS_RE.match(cola.strip())
    if not m:
        return None
    a, b = apodo(m.group("a")), apodo(m.group("b"))
    if not a or not b or a == b:
        return None
    if salidas is not None:
        if {apodo(x) for x in salidas} != {a, b}:
            return None
    return a, b


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
        # Apostar solo donde hay línea de casas profesionales con la que
        # contrastarse. Ver la nota en `_evaluar`.
        self.solo_linea_sharp = bool(cfg.get("solo_linea_sharp", True))
        # Ligas de fútbol con línea sharp. Cada liga consultada cuesta 1
        # crédito de The Odds API por llamada: con cache de 6h y tres ligas
        # más MLB son ~480/mes, dentro del tier gratis de 500.
        self.ligas_futbol = list(cfg.get("soccer_leagues") or [])
        # Ligas cuyos mercados de Polymarket se titulan «A vs B»: cada una
        # es {clave: <clave de The Odds API>, tag: <tag de Polymarket>}.
        self.ligas_vs = [dict(x) for x in (cfg.get("ligas_vs") or [])]

    async def _fuerzas(self) -> dict[str, float]:
        temporada = datetime.now(timezone.utc).year
        equipos = await self.mlb.equipos(temporada)
        return {apodo(n): pitagorica(e.carreras_a_favor, e.carreras_en_contra)
                for n, e in equipos.items()}

    async def scan_and_execute(self, traza: list[str] | None = None,
                               simular: bool = False) -> list[str]:
        """Con `traza` va apuntando por qué cada juego termina sin apuesta;
        con `simular` no manda nada al broker. Es el MISMO camino que usa el
        bot de verdad: un diagnóstico que recorriera su propia copia del
        código no probaría nada sobre el código que opera."""
        self._traza = traza
        self._simular = simular
        self._tags_frescos: set[str] = set()
        if not self.enabled:
            self._nota("la estrategia está apagada en config.yaml")
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
        if self.odds is None or not getattr(self.odds, "enabled", False):
            self._nota("líneas sharp: cliente apagado (falta ODDS_API_KEY)")
        else:
            self._nota(f"líneas sharp de MLB: {len(lineas)} partidos")
        # El béisbol y el fútbol son independientes: hasta el 2026-08-27 un
        # fallo de la API de la MLB devolvía [] y se llevaba por delante el
        # escaneo de fútbol, que no la necesita para nada. Con el 406 del
        # droplet eso dejó la estrategia entera muda durante días.
        ejecutadas: list[str] = []
        fuerzas: dict[str, float] = {}
        juegos: list[Any] = []
        try:
            fuerzas = await self._fuerzas()
            for delta in (0, 1):     # hoy y mañana (los mercados abren antes)
                fecha = (ahora + timedelta(days=delta)).date().isoformat()
                juegos.extend(await self.mlb.juegos(fecha))
            self._nota(f"juegos de MLB (hoy y mañana): {len(juegos)}")
        except Exception as exc:
            log.warning("datos de MLB no disponibles: %s", exc)
            self._nota(f"béisbol fuera de juego: {exc}")

        for juego in juegos:
            try:
                desc = await self._evaluar(juego, fuerzas, ahora)
            except Exception as exc:
                log.debug("juego %s falló: %s", juego.local, exc)
                continue
            if desc:
                ejecutadas.append(desc)
        try:
            ejecutadas.extend(await self._escanear_futbol(ahora))
        except Exception as exc:
            log.warning("fútbol no disponible: %s", exc)
            self._nota(f"fútbol fuera de juego: {exc}")
        # Cada liga se cae sola: un tenis sin datos no puede callar al
        # baloncesto, que es el error que ya nos costó días de silencio.
        for liga in self.ligas_vs:
            try:
                ejecutadas.extend(await self._escanear_liga_vs(
                    liga.get("clave", ""), liga.get("tag", ""), ahora))
            except Exception as exc:
                log.warning("liga %s no disponible: %s",
                            liga.get("clave"), exc)
                self._nota(f"{liga.get('clave')} fuera de juego: {exc}")
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
            self._nota("fútbol: sin ligas configuradas o sin ODDS_API_KEY")
            return []
        # Traer los mercados de fútbol al cache. Nadie lo hacía: el escaneo
        # diario guarda los 600 de más volumen y un partido de liga no entra
        # ni de lejos, así que la consulta de abajo siempre encontraba cero
        # por mucho que hubiera línea sharp que comparar.
        if self.market_store is not None:
            try:
                frescos = await self.gamma.fetch_by_tag("soccer", limit=300)
                if frescos:
                    self.market_store.upsert_markets(frescos)
            except Exception as exc:
                log.warning("no pude refrescar mercados de fútbol: %s", exc)
        mercados = self._mercados_ganador_futbol()
        self._nota(f"fútbol: {len(mercados)} mercados «Will X win on FECHA?» "
                   f"en el cache de Polymarket")
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
                        f"línea sharp {liga} ({linea.casas} casas)", inicio,
                        etq=f"{liga}: {equipo}")
                    if desc:
                        hechas.append(desc)
        return hechas

    async def _refrescar_tag(self, tag: str) -> None:
        """Trae los mercados de un tag al cache, una sola vez por corrida.

        El escaneo diario guarda los 600 de más volumen y un partido suelto
        no entra: sin esto la consulta local encuentra cero por muchas
        líneas que haya al lado."""
        if not tag or self.market_store is None:
            return
        if tag in getattr(self, "_tags_frescos", set()):
            return
        try:
            frescos = await self.gamma.fetch_by_tag(tag, limit=300)
            if frescos:
                self.market_store.upsert_markets(frescos)
        except Exception as exc:
            log.warning("no pude refrescar mercados de '%s': %s", tag, exc)
        self._tags_frescos = getattr(self, "_tags_frescos", set()) | {tag}

    async def _escanear_liga_vs(self, clave: str, tag: str,
                                ahora: datetime) -> list[str]:
        """Ligas cuyos mercados de Polymarket se titulan «A vs B».

        Es la ruta de tenis, baloncesto, hockey y rugby: hay línea de casas
        profesionales y el mercado nombra a los dos participantes. El fútbol
        no pasa por aquí porque allí Polymarket abre un mercado por equipo
        («Will X win on FECHA?») en vez de uno con dos salidas.
        """
        if not (clave and self.odds is not None
                and getattr(self.odds, "enabled", False)):
            return []
        try:
            lineas = await self.odds.lineas(clave)
        except Exception as exc:
            log.debug("liga %s sin líneas: %s", clave, exc)
            return []
        if not lineas:
            self._nota(f"{clave}: la casa no tiene partidos ahora")
            return []
        await self._refrescar_tag(tag)
        from ..data.odds import nombre_coincide

        filas = self.conn.execute(
            """SELECT * FROM markets WHERE active = 1 AND category = 'sports'
               AND question LIKE '%vs%'""").fetchall()
        # Candidatos: mercados de ganador de partido, sin indexar por apodo.
        #
        # Indexarlos por `frozenset((apodo(a), apodo(b)))` fue un error caro:
        # `apodo` se queda con la última palabra, y en béisbol asiático eso
        # choca de frente. Hanshin Tigers (Japón), Kia Tigers (Corea) y
        # Detroit Tigers (MLB) son todos 'tigers'; Yomiuri, Lotte y San
        # Francisco Giants son todos 'giants'. El 2026-08-27 el bot emparejó
        # la línea de «Yomiuri Giants @ Hanshin Tigers» con un mercado de los
        # Kia Tigers y sacó de ahí una ventaja de +5.3% que no existía. Todos
        # los "despegues" que encontró estaban en KBO y NPB, que es justo
        # donde chocan los nombres.
        candidatos: list[sqlite3.Row] = []
        for fila in filas:
            try:
                salidas = _json.loads(
                    (_json.loads(fila["raw"] or "{}") or {}).get("outcomes")
                    or "[]")
            except (ValueError, TypeError):
                salidas = []
            if leer_partido(fila["question"], salidas or None):
                candidatos.append(fila)
        hechas: list[str] = []
        emparejados = 0
        for linea in lineas:
            try:
                inicio = datetime.fromisoformat(
                    linea.inicio_utc.replace("Z", "+00:00"))
            except ValueError:
                continue
            if inicio - ahora < timedelta(minutes=self.minutos_antes):
                continue
            # Los DOS nombres completos tienen que aparecer en la
            # pregunta, no solo su última palabra.
            fila = next(
                (f for f in candidatos
                 if nombre_coincide(linea.local, f["question"])
                 and nombre_coincide(linea.visitante, f["question"])), None)
            if fila is None:
                continue
            local, visitante = apodo(linea.local), apodo(linea.visitante)
            emparejados += 1
            desc = await self._apostar_vs(
                fila, {local: linea.prob_local,
                       visitante: linea.prob_visitante},
                f"línea sharp {clave} ({linea.casas} casas)", inicio,
                etq=f"{clave}: {linea.visitante} @ {linea.local}")
            if desc:
                hechas.append(desc)
        self._nota(f"{clave}: {len(lineas)} partidos con línea, "
                   f"{emparejados} emparejados con Polymarket")
        return hechas

    async def _apostar_vs(self, fila: sqlite3.Row, probs: dict[str, float],
                          fuente: str, inicio: datetime,
                          etq: str = "") -> str | None:
        """Apuesta el lado con ventaja de un mercado «A vs B»."""
        tokens = _json.loads(fila["clob_token_ids"] or "[]")
        try:
            salidas = _json.loads(
                (_json.loads(fila["raw"] or "{}") or {}).get("outcomes") or "[]")
        except (ValueError, TypeError):
            return None
        if len(tokens) != 2 or len(salidas) != 2:
            return None
        # Escudo contra pagar de más en las copias, haya apuesta o no.
        primera = probs.get(apodo(salidas[0]))
        if primera is not None:
            self._guardar_sharp(fila["condition_id"], primera, fuente)
        mejor = await self._mejor_lado(tokens, salidas, probs)
        if mejor is False or not mejor:
            self._nota(f"{etq}: sin precio usable en el libro")
            return None
        self._anotar_comparacion(fuente, fila["condition_id"], mejor[2],
                                 mejor[3], mejor[4])
        if mejor[0] < self.min_edge:
            self._nota(f"{etq}: {mejor[2]} vale {mejor[3]:.0%} y pide "
                       f"{mejor[4]:.0%} -> ventaja {mejor[0]:+.1%}, "
                       f"por debajo de {self.min_edge:.0%} [{fuente}]")
            return None
        ventaja, idx, salida, prob, ask = mejor
        usdc = kelly_usdc(self.broker.equity(), ask, prob / ask - 1.0,
                          self.kelly_fraction, self.min_trade_usdc,
                          self.max_trade_pct)
        if usdc <= 0:
            self._nota(f"{etq}: ventaja {ventaja:+.1%} pero Kelly no llega "
                       f"al mínimo de ${self.min_trade_usdc:.0f}")
            return None
        razon = (f"{fuente}: {salida} vale {prob:.0%} y el libro pide "
                 f"{ask:.0%} (ventaja {ventaja:+.0%})")
        if getattr(self, "_simular", False):
            self._nota(f"{etq}: APOSTARÍA {salida} @ {ask:.3f} ${usdc:.2f} "
                       f"(ventaja {ventaja:+.1%}) [{fuente}]")
            return None
        fill = await self.broker.execute(
            f"sharp:{fila['condition_id']}:{idx}",
            OrderRequest(
                strategy=self.name, condition_id=fila["condition_id"],
                category="sports", token_id=tokens[idx], outcome=salida,
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

    def _anotar_comparacion(self, liga: str, condition_id: str,
                            salida: str, prob: float, ask: float) -> None:
        """Deja constancia de cada comparación línea-vs-Polymarket.

        Un barrido suelto no dice si el negocio existe: los despegues duran
        minutos, así que lo que cuenta es la serie a lo largo del día. Sin
        registrarlos, la única forma de saberlo sería mirar la pantalla en el
        momento justo.
        """
        if getattr(self, "_simular", False):
            return
        try:
            with self.conn:
                self.conn.execute(
                    """INSERT INTO comparaciones_sharp (created_at, liga,
                       condition_id, outcome, prob_sharp, ask, ventaja)
                       VALUES (?,?,?,?,?,?,?)""",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     liga, condition_id, salida, prob, ask, prob - ask))
        except Exception as exc:      # nunca por un registro se deja de operar
            log.debug("no pude anotar la comparación: %s", exc)

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
                               fuente: str, inicio: datetime,
                               etq: str = "") -> str | None:
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
        if not mejor:
            self._nota(f"{etq}: sin precio usable en el libro")
            return None
        self._anotar_comparacion(fuente, fila["condition_id"], mejor[2],
                                 mejor[3], mejor[4])
        if mejor[0] < self.min_edge:
            self._nota(f"{etq}: {mejor[2]} vale {mejor[3]:.0%} y pide "
                       f"{mejor[4]:.0%} -> ventaja {mejor[0]:+.1%}, "
                       f"por debajo de {self.min_edge:.0%} [{fuente}]")
            return None
        ventaja, idx, outcome, prob, ask = mejor
        usdc = kelly_usdc(self.broker.equity(), ask, prob / ask - 1.0,
                          self.kelly_fraction, self.min_trade_usdc,
                          self.max_trade_pct)
        if usdc <= 0:
            self._nota(f"{etq}: ventaja {ventaja:+.1%} pero Kelly no llega "
                       f"al mínimo de ${self.min_trade_usdc:.0f}")
            return None
        if getattr(self, "_simular", False):
            self._nota(f"{etq}: APOSTARÍA {outcome} de "
                       f"«{fila['question'][:40]}» @ {ask:.3f} ${usdc:.2f} "
                       f"(ventaja {ventaja:+.1%}) [{fuente}]")
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

    def _nota(self, texto: str) -> None:
        if getattr(self, "_traza", None) is not None:
            self._traza.append(texto)

    async def _mejor_lado(self, tokens: list[str], salidas: list[str],
                          probs: dict[str, float]):
        """El lado con más ventaja de un mercado de dos salidas.

        Devuelve (ventaja, idx, salida, prob, ask), None si ningún lado tiene
        precio usable, o False si las salidas no son los participantes que
        esperábamos — que no es lo mismo y no se puede tratar igual: sin
        saber de quién habla cada salida, cualquier apuesta sería a ciegas.
        """
        mejor = None
        for idx, salida in enumerate(salidas):
            prob = probs.get(apodo(salida))
            if prob is None:
                return False
            libro = await self.broker.clob.order_book(tokens[idx])
            ask = libro.best_ask
            if ask is None or ask <= 0.02 or ask > self.max_entry:
                continue
            ventaja = prob - ask
            if mejor is None or ventaja > mejor[0]:
                mejor = (ventaja, idx, salida, prob, ask)
        return mejor

    async def _evaluar(self, juego: Any, fuerzas: dict[str, float],
                       ahora: datetime) -> str | None:
        visitante, local = apodo(juego.visitante), apodo(juego.local)
        etq = f"{juego.visitante} @ {juego.local}"
        if visitante not in fuerzas or local not in fuerzas:
            self._nota(f"{etq}: sin fuerza para "
                       f"{visitante if visitante not in fuerzas else local}")
            return None
        # El partido tiene que estar por empezar, no en curso.
        try:
            inicio = datetime.fromisoformat(
                juego.inicio_utc.replace("Z", "+00:00"))
        except ValueError:
            return None
        if inicio - ahora < timedelta(minutes=self.minutos_antes):
            self._nota(f"{etq}: ya empezó o le faltan menos de "
                       f"{self.minutos_antes:.0f} min")
            return None

        # El modelo propio se calcula SIEMPRE, incluso cuando hay línea
        # sharp que lo va a sustituir: comparar los dos números en los
        # partidos que sí tienen línea es la única forma de saber cuánto se
        # equivoca el modelo en los que no la tienen. Sin esa medida, sus
        # "ventajas" de 8-11 puntos en partidos sin línea no se distinguen
        # de un error suyo de 8-11 puntos.
        aj_local = ajuste_pitcher(
            juego.pitcher_local.era, juego.pitcher_local.entradas,
            self.peso_pitcher) if juego.pitcher_local else 0.0
        aj_visitante = ajuste_pitcher(
            juego.pitcher_visitante.era, juego.pitcher_visitante.entradas,
            self.peso_pitcher) if juego.pitcher_visitante else 0.0
        p_modelo = prob_local(fuerzas[local], fuerzas[visitante],
                              aj_local, aj_visitante, self.ventaja_local)

        linea = getattr(self, "_lineas_por_par", {}).get(
            frozenset((local, visitante)))
        if linea is not None:
            # La línea sharp de-vigueada ES la probabilidad: nadie pone
            # este precio mejor que Pinnacle y compañía.
            p_local = (linea.prob_local if apodo(linea.local) == local
                       else linea.prob_visitante)
            fuente = f"línea sharp ({linea.casas} casas)"
            self._nota(f"CALIBRACIÓN {etq}: mi modelo dice "
                       f"{p_modelo:.1%} para el local, la sharp dice "
                       f"{p_local:.1%} -> me equivoco "
                       f"{abs(p_modelo - p_local)*100:.1f} puntos")
        else:
            p_local = p_modelo
            fuente = "modelo propio"

        mercado = self._buscar_mercado(visitante, local, inicio)
        if not mercado:
            self._nota(f"{etq}: Polymarket no tiene mercado de ganador simple")
            return None
        if linea is None and self.solo_linea_sharp:
            # Medido el 2026-08-27 sobre los 6 partidos que sí tenían línea:
            # el modelo propio se desvía 5.7 puntos de media y hasta 11.9,
            # con una típica de ~6.6. Una "ventaja" de 9 puntos es 1.4 sigmas
            # del cero: indistinguible de su propio error. Ese día habría
            # apostado $75 en tres partidos sin línea con ventajas de +8.5%,
            # +9.4% y +11.2%, las tres dentro del ruido.
            #
            # El modelo se sigue calculando y guardando como escudo (evita
            # que las copias paguen de más), pero no abre posiciones: para
            # eso haría falta un umbral de ~13 puntos, y a ese nivel no
            # aparece nada. Se apuesta donde hay con qué comprobarse.
            self._nota(f"{etq}: sin línea sharp, no se apuesta "
                       f"(el modelo opina pero se desvía ~6 puntos)")
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
        mejor = await self._mejor_lado(
            tokens, salidas, {local: p_local, visitante: 1.0 - p_local})
        if mejor is False:
            return None          # los outcomes no son los equipos: no opinar
        if not mejor:
            self._nota(f"{etq}: sin precio usable (libro vacío o por encima "
                       f"de {self.max_entry:.2f}) [{fuente}]")
            return None
        self._anotar_comparacion(fuente, mercado["condition_id"], mejor[2],
                                 mejor[3], mejor[4])
        if mejor[0] < self.min_edge:
            self._nota(f"{etq}: {mejor[2]} vale {mejor[3]:.0%} y pide "
                       f"{mejor[4]:.0%} -> ventaja {mejor[0]:+.1%}, "
                       f"por debajo de {self.min_edge:.0%} [{fuente}]")
            return None
        ventaja, idx, salida, prob, ask = mejor

        # Retorno esperado por dólar = cuánto vale lo que compramos sobre
        # lo que pagamos. Con eso Kelly dimensiona (ver sizing.py).
        retorno = prob / ask - 1.0
        usdc = kelly_usdc(self.broker.equity(), ask, retorno,
                          self.kelly_fraction, self.min_trade_usdc,
                          self.max_trade_pct)
        if usdc <= 0:
            self._nota(f"{etq}: ventaja {ventaja:+.1%} pero Kelly no llega "
                       f"al mínimo de ${self.min_trade_usdc:.0f} "
                       f"(equity ${self.broker.equity():.2f})")
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
        if getattr(self, "_simular", False):
            self._nota(f"{etq}: APOSTARÍA {salida} @ {ask:.3f} "
                       f"${usdc:.2f} (ventaja {ventaja:+.1%}) [{fuente}]")
            return None
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

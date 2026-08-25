"""Rendimiento cerrado: compras contra cobros, sobre el historial COMPLETO.

Existe porque los informes que se improvisan salen mal. Tres errores reales
de análisis, todos en agosto de 2026, y los tres evitables:

1. Contar como "compras" las órdenes que el riesgo rechazó. Decía que el bot
   operaba cuando en realidad se estaba estrellando contra sus propios
   límites. -> aquí solo cuentan las FILLED.

2. Cruzar cada compra con cada venta del mismo mercado. Con dos compras y
   dos ventas las filas se multiplican y los montos se duplican; salió un
   ROI de -134%, que no puede existir. -> aquí se agrupa por posición antes
   de sumar, y `_imposible` rechaza cualquier resultado peor que perderlo
   todo.

3. Sacar el porcentaje de acierto de las posiciones ABIERTAS. Es la peor de
   las tres porque parece razonable: las ganadoras se cobran y desaparecen
   de esa lista, las perdedoras se quedan en $0 esperando. Mirando ahí,
   esports parecía haber perdido 17 de 17 cuando había ganado 7. -> aquí la
   fuente son las órdenes, que no se borran nunca.

Todo puro salvo `posiciones_cerradas`, que solo lee.
"""
from __future__ import annotations

import sqlite3
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Cerrada:
    """Una posición que ya terminó: lo que se puso y lo que se cobró."""
    condition_id: str
    outcome: str
    strategy: str
    category: str
    invertido: float          # USDC gastado comprando
    cobrado: float            # USDC recibido vendiendo o al resolverse
    acciones: float

    @property
    def pnl(self) -> float:
        return self.cobrado - self.invertido

    @property
    def precio_medio(self) -> float:
        return self.invertido / self.acciones if self.acciones > 0 else 0.0

    @property
    def gano(self) -> bool:
        return self.pnl > 0


def _imposible(c: Cerrada) -> str | None:
    """Comprobación de cordura: ¿este resultado puede existir?

    Una compra no puede perder más de lo que costó ni cobrar de la nada.
    Si un cálculo produce algo así, el cálculo está mal y hay que verlo
    antes de enseñárselo a nadie.
    """
    if c.invertido < 0 or c.cobrado < 0:
        return f"montos negativos ({c.invertido:.2f} / {c.cobrado:.2f})"
    if c.pnl < -c.invertido - 0.01:
        return (f"pierde ${-c.pnl:.2f} habiendo puesto solo "
                f"${c.invertido:.2f}")
    if c.invertido == 0 and c.cobrado > 0:
        return f"cobra ${c.cobrado:.2f} sin haber comprado"
    return None


def posiciones_cerradas(conn: sqlite3.Connection,
                        desde: str | None = None) -> list[Cerrada]:
    """Posiciones terminadas, agrupadas por (mercado, outcome).

    Se agrupa ANTES de sumar: si se cruzan compras con ventas fila a fila,
    dos de cada multiplican los montos por dos.
    """
    filtro = "AND o.created_at >= ?" if desde else ""
    args = (desde,) if desde else ()
    compras = {
        (r["condition_id"], r["outcome"]): r for r in conn.execute(
            f"""SELECT o.condition_id, o.outcome,
                       MIN(o.strategy) strategy,
                       COALESCE(MIN(m.category), 'other') category,
                       COALESCE(MIN(m.question), '') pregunta,
                       SUM(o.fill_usdc) usdc, SUM(o.fill_size) shares
                FROM orders o
                LEFT JOIN markets m ON m.condition_id = o.condition_id
                WHERE o.side = 'BUY' AND o.status = 'FILLED'
                      AND o.fill_size > 0 {filtro}
                GROUP BY o.condition_id, o.outcome""", args)}
    cobros: dict[tuple[str, str], float] = {}
    for r in conn.execute(
            """SELECT condition_id, outcome, SUM(fill_usdc) usdc
               FROM orders
               WHERE side IN ('SELL', 'REDEEM') AND status = 'FILLED'
               GROUP BY condition_id, outcome"""):
        cobros[(r["condition_id"], r["outcome"])] = float(r["usdc"] or 0.0)

    # Una posición está cerrada si ya no queda nada de ella en cartera.
    vivas = {(r["condition_id"], r["outcome"]) for r in conn.execute(
        "SELECT condition_id, outcome FROM paper_positions WHERE size > 0.01")}

    fuera: list[Cerrada] = []
    for clave, r in compras.items():
        if clave in vivas:
            continue
        c = Cerrada(condition_id=clave[0], outcome=clave[1],
                    strategy=r["strategy"] or "?",
                    category=categoria_real(r["category"],
                                             r["pregunta"]),
                    invertido=float(r["usdc"] or 0.0),
                    cobrado=cobros.get(clave, 0.0),
                    acciones=float(r["shares"] or 0.0))
        fuera.append(c)
    return fuera


# Los mercados ya resueltos conservan en la tabla la categoría que tenían
# cuando se cachearon, así que los esports anteriores al arreglo del
# clasificador siguen figurando como 'sports' y contaminan sus números. El
# texto de la pregunta no cambia nunca: es la fuente fiable para lo viejo.
_ESPORTS = re.compile(
    r"\blol:|\bcounter-strike|\bvalorant|\besports?\b|\bdota|\bcs2\b|"
    r"\boverwatch|\bmap handicap|\bstarcraft|\brainbow six",
    re.IGNORECASE)


def categoria_real(categoria: str, pregunta: str) -> str:
    """Categoría de una posición, corrigiendo la que quedó vieja en la tabla."""
    if _ESPORTS.search(pregunta or ""):
        return "esports"
    return categoria or "other"


def etiqueta_precio(p: float) -> str:
    """Franja de precio de entrada. Pura."""
    if p < 0.35: return "1 muy barato <0.35"
    if p < 0.50: return "2 barato .35-.50"
    if p < 0.65: return "3 medio  .50-.65"
    if p < 0.80: return "4 caro   .65-.80"
    return "5 carisimo >0.80"


@dataclass(frozen=True)
class Grupo:
    nombre: str
    n: int
    ganadas: int
    invertido: float
    pnl: float

    @property
    def acierto(self) -> float:
        return self.ganadas / self.n if self.n else 0.0

    @property
    def roi(self) -> float:
        return self.pnl / self.invertido if self.invertido > 0 else 0.0


def agrupar(cerradas: list[Cerrada], por: str) -> list[Grupo]:
    """Agrupa por 'categoria', 'estrategia' o 'precio'. Pura."""
    def etiqueta(c: Cerrada) -> str:
        if por == "categoria":
            return c.category
        if por == "estrategia":
            return c.strategy
        if por == "categoria+precio":
            return f"{c.category:<9} {etiqueta_precio(c.precio_medio)}"
        if por == "precio":
            return etiqueta_precio(c.precio_medio)
        raise ValueError(f"no sé agrupar por '{por}'")

    acc: dict[str, list[float]] = {}
    for c in cerradas:
        a = acc.setdefault(etiqueta(c), [0, 0, 0.0, 0.0])
        a[0] += 1
        a[1] += 1 if c.gano else 0
        a[2] += c.invertido
        a[3] += c.pnl
    return sorted((Grupo(k, int(v[0]), int(v[1]), v[2], v[3])
                   for k, v in acc.items()), key=lambda g: g.nombre)


def revisar(cerradas: list[Cerrada]) -> list[str]:
    """Resultados que no pueden existir. Vacío = los números son creíbles."""
    return [f"{c.condition_id[:12]} [{c.outcome}]: {m}"
            for c in cerradas if (m := _imposible(c))]


def formatear(grupos: list[Grupo], titulo: str) -> str:
    lineas = [f"\n{titulo}",
              f"{'':<28} {'cerradas':>9} {'acierto':>8} {'invertido':>10} "
              f"{'PnL':>9} {'ROI':>8}"]
    tn = tg = 0
    ti = tp = 0.0
    for g in grupos:
        lineas.append(f"{g.nombre:<28} {g.n:>9} {g.acierto:>7.0%} "
                      f"{g.invertido:>10.2f} {g.pnl:>+9.2f} {g.roi:>7.1%}")
        tn += g.n; tg += g.ganadas; ti += g.invertido; tp += g.pnl
    if grupos:
        roi = tp / ti if ti else 0.0
        lineas.append(f"{'TOTAL':<28} {tn:>9} {tg/tn if tn else 0:>7.0%} "
                      f"{ti:>10.2f} {tp:>+9.2f} {roi:>7.1%}")
    return "\n".join(lineas)

"""CLI del bot.

  python -m pmbot markets            # refresca y muestra mercados por categoría
  python -m pmbot rank-wallets       # ranking de wallets (leaderboard + score)
  python -m pmbot positions [wallet] # posiciones de una wallet (o de las top)
  python -m pmbot news               # baja y analiza noticias pendientes
  python -m pmbot briefing           # briefing diario por categoría
  python -m pmbot daily              # rutina diaria completa (una vez)
  python -m pmbot trade-cycle        # un ciclo de trading paper (settle/exits/entradas)
  python -m pmbot portfolio          # equity, posiciones y PnL por estrategia
  python -m pmbot trades             # últimas órdenes (llenadas y rechazadas)
  python -m pmbot kill on|off        # kill switch manual (bloquea compras)
  python -m pmbot backtest-wallet W  # qué habría pasado copiando a W (o 'top')
  python -m pmbot live-check         # verificar conexión/saldo del modo real
  python -m pmbot notify-test        # mandar mensaje de prueba por Telegram
  python -m pmbot test-trade         # compra y vende ~$1-2 real: valida el circuito
  python -m pmbot analisis [--dias N] # qué funciona y qué no, sobre lo ya cerrado
  python -m pmbot deportes          # por qué el modelo deportivo apuesta o no
  python -m pmbot set-baseline N     # fija el capital inicial contra el que se mide el PnL
  python -m pmbot validate-wallets [--force]  # backtestea el ranking y habilita a quién copiar
  python -m pmbot wallets            # a quién copia el bot y en qué orden las vigila
  python -m pmbot mlb                # qué opina nuestro modelo de béisbol vs el mercado
  python -m pmbot capital [--vender] # cuánto capital está dormido (y liberarlo)
  python -m pmbot rendimiento [--dias N]  # quién hizo el dinero: por estrategia y wallet
  python -m pmbot diagnose           # embudo: por qué se opera (o no) ahora mismo
  python -m pmbot run                # loop 24/7 (scheduler)
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_config
from .context import App, build_app
from .db import from_json
from .monitor import setup_logging
from .scheduler import DailyRoutine, run_forever


async def cmd_markets(app: App) -> None:
    routine = DailyRoutine(app)
    n = await routine.refresh_markets()
    print(f"\nMercados activos cacheados: {n}\n")
    print(f"{'categoría':<14} {'#':>4} {'vol 24h':>16}")
    for row in app.market_store.category_summary():
        print(f"{row['category']:<14} {row['n']:>4} ${row['vol24h']:>14,.0f}")
    print("\nTop 10 por volumen 24h:")
    for row in app.market_store.active_markets(limit=10):
        print(f"  [{row['category']:<11}] YES={row['yes_price']} "
              f"vol24h=${row['volume_24h']:,.0f}  {row['question'][:70]}")


async def cmd_rank_wallets(app: App) -> None:
    scored = await app.wallet_scorer.refresh_ranking()
    passed = [w for w in scored if w.passed_filters]
    rejected = [w for w in scored if not w.passed_filters]
    print(f"\nRanking de wallets ({len(passed)} aprobadas, "
          f"{len(rejected)} rechazadas por filtros)\n")
    print(f"{'#':>2} {'wallet/usuario':<22} {'score':>6} {'PnL 7d':>12} "
          f"{'PnL 30d':>12} {'PnL total':>13} {'ROI':>6} {'trades':>6} {'mkts':>5} {'edad':>6}")
    for i, w in enumerate(passed[:20], 1):
        name = (w.username or w.wallet[:12])[:22]
        print(f"{i:>2} {name:<22} {w.score:>6.3f} ${w.stats.pnl_week:>11,.0f} "
              f"${w.stats.pnl_month:>11,.0f} ${w.stats.pnl_all:>12,.0f} "
              f"{w.components.get('roi', 0):>6.2f} {w.stats.trades:>6} "
              f"{w.stats.distinct_markets:>5} {w.stats.account_age_days:>5.0f}d")
    if rejected:
        print("\nRechazadas (muestra):")
        for w in rejected[:8]:
            name = (w.username or w.wallet[:12])[:22]
            print(f"   {name:<22} -> {w.reject_reason}")


async def cmd_positions(app: App, wallet: str | None) -> None:
    if wallet:
        wallets = [wallet.lower()]
    else:
        wallets = [r["wallet"] for r in app.wallet_scorer.top_wallets(5)]
        if not wallets:
            print("No hay ranking todavía; corré primero: python -m pmbot rank-wallets")
            return
    await app.wallet_tracker.refresh_positions(wallets)
    for w in wallets:
        rows = app.wallet_tracker.positions_of(w)
        rank = app.conn.execute(
            "SELECT username, score FROM wallet_ranking WHERE wallet = ?",
            (w,)).fetchone()
        label = (rank["username"] if rank and rank["username"] else w)
        print(f"\n💼 {label} ({len(rows)} posiciones)"
              + (f" — score {rank['score']:.3f}" if rank else ""))
        for r in rows[:12]:
            print(f"   {r['outcome']:<4} {r['size']:>10,.0f} u @ {r['avg_price']:.3f}"
                  f" → {r['cur_price']:.3f} | ${r['value_usdc']:>10,.0f}"
                  f" | PnL ${r['cash_pnl']:>9,.0f}  {r['title'][:55]}")


async def cmd_news(app: App) -> None:
    routine = DailyRoutine(app)
    await routine.poll_intel()
    rows = app.conn.execute(
        """SELECT * FROM news_items WHERE analyzed = 1
           ORDER BY fetched_at DESC LIMIT 15""").fetchall()
    print(f"\nÚltimas noticias analizadas:")
    for r in rows:
        analysis = from_json(r["analysis"]) or {}
        mark = "🎯" if analysis.get("relevant") and analysis.get("markets") else "  "
        print(f" {mark} [{r['category']:<11}] {r['title'][:75]}")
        for m in (analysis.get("markets") or [])[:2]:
            print(f"      → {m.get('direction', '?'):<7} impacto={m.get('impact', '?'):<7}"
                  f" {m.get('question', '')[:60]}")


async def cmd_briefing(app: App) -> None:
    routine = DailyRoutine(app)
    await routine.poll_intel()
    briefings = app.briefing.build_daily()
    print()
    for category in sorted(briefings):
        print(briefings[category])
        print()


async def cmd_daily(app: App) -> None:
    report = await DailyRoutine(app).run_daily()
    print("\n" + report)


async def cmd_trade_cycle(app: App) -> None:
    moves = await DailyRoutine(app).trade_cycle()
    print()
    any_move = False
    for kind, items in moves.items():
        for item in items:
            any_move = True
            print(f"[{kind}] {item}")
    if not any_move:
        print("Sin oportunidades que superen los umbrales; no se operó.")
    await cmd_portfolio(app)


async def cmd_portfolio(app: App) -> None:
    # Sincronizar con la blockchain antes de mostrar (fills tardíos).
    # Vía DailyRoutine para que la adopción también avise por Telegram.
    for note in await DailyRoutine(app).reconcile():
        print(f"🔄 {note}")
    state = app.broker.portfolio_state()
    starting = app.broker.starting_capital()
    pnl = state.equity - starting
    external = app.broker.external_value()
    detalle = (f"cash ${state.cash:.2f} + bot ${state.exposure_total:.2f}"
               + (f" + tuyo ${external:.2f}" if external > 0.01 else ""))
    print(f"\n💰 Equity: ${state.equity:.2f}  ({detalle})")
    print(f"   PnL total: {pnl:+.2f} USDC ({pnl / starting:+.2%} sobre "
          f"${starting:.0f} iniciales)")
    if app.risk.kill_switch_on():
        print("   ⛔ KILL SWITCH ACTIVADO: compras bloqueadas")
    positions = app.broker.positions()
    print(f"\nPosiciones abiertas ({len(positions)}):")
    for p in positions:
        mark = app.broker.mark_price(p["condition_id"],
                                     p["outcome_index"] or 0, p["avg_price"])
        unreal = p["size"] * (mark - p["avg_price"])
        print(f"  [{p['strategy']:<12}] {p['outcome']:<4} {p['size']:>8.0f} u "
              f"@ {p['avg_price']:.3f} → {mark:.3f} | PnL {unreal:+8.2f}  "
              f"{(p['question'] or '')[:50]}")
    if not positions:
        print("  (ninguna)")
    if external > 0.01:
        print(f"\nTuyo, fuera del bot: ${external:.2f} — posiciones que "
              f"abriste vos y premios ganados sin cobrar.\n"
              f"  El bot las cuenta en el equity pero no las toca.")


def cmd_rendimiento(app: App, dias: int | None = None) -> None:
    """¿Quién me hizo el dinero? PnL por estrategia y por wallet copiada."""
    from datetime import datetime, timedelta, timezone

    from .monitor.performance import (formatear, nombre_wallet,
                                      resumen_estrategias, resumen_wallets)

    desde = None
    if dias is not None:
        desde = (datetime.now(timezone.utc) - timedelta(days=dias)
                 ).date().isoformat()
    posiciones = app.broker.positions()
    marca = app.broker.mark_price
    periodo = f"últimos {dias} días" if dias else "desde el inicio"
    print()
    print(formatear(resumen_estrategias(app.conn, posiciones, marca, desde),
                    f"🧮 PnL por estrategia ({periodo}):"))
    print()
    print(formatear(resumen_wallets(app.conn, posiciones, marca, desde),
                    f"👛 PnL por wallet copiada ({periodo}):",
                    nombres=lambda w: nombre_wallet(app.conn, w)))
    print("\n(real = cerrado y cobrado; flot = posiciones aún abiertas)")


def _plazo(dias: float) -> str:
    """Días u horas, lo que se lea mejor. Con cortes por debajo de un día,
    "{:.0f} días" imprimía "0 días" y el aviso no decía nada."""
    if dias < 0:
        return "fecha vencida (limbo)"
    if dias < 1:
        return f"{dias * 24:.0f} horas"
    return f"{dias:.0f} días"


async def cmd_capital(app: App, vender: bool = False,
                      dias_min: float | None = None) -> None:
    """Cuánto capital está dormido y en qué. Con --vender lo libera.

    Con una cuenta chica el dinero parado no compone: una apuesta que se
    resuelve en tres meses secuestra munición que en ese tiempo podría dar
    varias vueltas.

    OJO con qué se vende: --vender solo suelta lo que ya no cumple la
    política actual (más de max_days_to_resolution), no todo lo que figura
    como lento. Una posición de diez días que está ganando no es capital
    muerto, es capital trabajando. Con --dias N se elige otro corte.
    """
    from datetime import datetime, timezone

    from .risk import OrderRequest
    from .strategies.sizing import dias_hasta

    lentas, rapido, total = [], 0.0, 0.0
    for p in app.broker.positions():
        marca = app.broker.mark_price(p["condition_id"],
                                      p["outcome_index"] or 0, p["avg_price"])
        valor = p["size"] * marca
        total += valor
        fila = app.conn.execute(
            "SELECT end_date FROM markets WHERE condition_id = ?",
            (p["condition_id"],)).fetchone()
        dias = dias_hasta(fila["end_date"]) if fila else None
        # Con --dias N el corte manda también aquí: si no, una posición que
        # se resuelve mañana ni siquiera se listaba (el umbral fijo eran los
        # `slow_days` de la política) y `--dias 0.5` no tenía nada que vender.
        umbral = (dias_min if dias_min is not None
                  else app.risk.limits.slow_days)
        # dias negativo = el endDate ya pasó y el mercado sigue abierto:
        # está en limbo (ej. una primaria que se fue a segunda vuelta).
        # Es el peor capital dormido, el que no tiene fecha.
        if dias is not None and (dias >= umbral or dias < -1):
            lentas.append((dias, valor, marca, p))
        else:
            rapido += valor
    print(f"\n💰 En posiciones: ${total:.2f}  "
          f"(rápido ${rapido:.2f} | dormido ${total - rapido:.2f})")
    if not lentas:
        print("\nNada dormido: todo el capital se resuelve pronto. 👌")
        return
    lentas.sort(reverse=True)
    print(f"\n{'días':>6} {'valor':>9} {'PnL':>9}  mercado")
    for dias, valor, marca, p in lentas:
        pnl = p["size"] * (marca - p["avg_price"])
        etiqueta = "LIMBO" if dias < 0 else f"{dias:.0f}"
        print(f"{etiqueta:>6} {valor:9.2f} {pnl:+9.2f}  "
              f"{(p['question'] or '')[:52]}")
    corte = (dias_min if dias_min is not None
             else app.risk.limits.max_days_to_resolution)
    # Se libera lo que supera el corte Y lo que está en limbo (fecha
    # vencida sin resolución: puede tardar semanas más, nadie lo sabe).
    a_vender = [x for x in lentas if x[0] > corte or x[0] < -1]
    if not vender:
        if a_vender:
            libera = sum(v for _, v, _, _ in a_vender)
            print(f"\nDe eso, ${libera:.2f} está más allá de "
                  f"{_plazo(corte)}, que es lo que hoy permite la política.")
            print("Para liberar SOLO eso:  python -m pmbot capital --vender")
            print("Para elegir otro corte: python -m pmbot capital --vender "
                  "--dias 7")
        else:
            print(f"\nNada supera {_plazo(corte)}: lo dormido está dentro "
                  f"de la política y varias posiciones están trabajando. "
                  f"No hay nada que liberar.")
        return
    if not a_vender:
        print(f"\nNada supera {_plazo(corte)}. No se vende nada.")
        return
    hoy = datetime.now(timezone.utc).date().isoformat()
    for dias, valor, marca, p in a_vender:
        fill = await app.broker.execute(
            f"liberar:{p['condition_id']}:{p['outcome_index'] or 0}:{hoy}",
            OrderRequest(
                strategy=p["strategy"], condition_id=p["condition_id"],
                category=p["category"] or "other", token_id=p["token_id"],
                outcome=p["outcome"], outcome_index=p["outcome_index"] or 0,
                side="SELL", size=p["size"], price=0.0,
                reason=f"liberar capital: se resolvía en {_plazo(dias)}"))
        estado = "✅" if fill.status == "FILLED" else "⚠️"
        print(f"{estado} {(p['question'] or '')[:46]:48} {fill.status} "
              f"{fill.detail[:40]}")


async def cmd_mlb(app: App) -> None:
    """Qué opina nuestro modelo de béisbol y dónde discrepa del mercado.

    No opera: sirve para auditar el modelo antes y después de que apueste.
    """
    import json as _json
    from datetime import datetime, timedelta, timezone

    from .strategies.sports_value import (ajuste_pitcher, apodo, leer_pregunta,
                                          pitagorica, prob_local)

    s = app.sports_value
    mercados = await app.gamma.fetch_by_tag("baseball", limit=200)
    if mercados:
        app.market_store.upsert_markets(mercados)
    ahora = datetime.now(timezone.utc)
    fuerzas = await s._fuerzas()
    juegos = []
    for delta in (0, 1):
        juegos.extend(await app.sports_value.mlb.juegos(
            (ahora + timedelta(days=delta)).date().isoformat()))
    print(f"\n⚾ {len(juegos)} juegos de MLB en las próximas 48h\n")
    print(f"{'juego':36} {'falta':>7} {'modelo':>8} {'libro':>7} {'ventaja':>8}  abridores")
    apuestas = 0
    for g in juegos:
        av, lo = apodo(g.visitante), apodo(g.local)
        if av not in fuerzas or lo not in fuerzas:
            continue
        aj_l = (ajuste_pitcher(g.pitcher_local.era, g.pitcher_local.entradas,
                               s.peso_pitcher) if g.pitcher_local else 0.0)
        aj_v = (ajuste_pitcher(g.pitcher_visitante.era,
                               g.pitcher_visitante.entradas,
                               s.peso_pitcher) if g.pitcher_visitante else 0.0)
        p = prob_local(fuerzas[lo], fuerzas[av], aj_l, aj_v, s.ventaja_local)
        try:
            inicio = datetime.fromisoformat(g.inicio_utc.replace("Z", "+00:00"))
        except ValueError:
            continue
        horas = (inicio - ahora).total_seconds() / 3600
        fila = s._buscar_mercado(av, lo, inicio)
        if not fila:
            continue
        crudo = _json.loads(fila["raw"] or "{}") or {}
        salidas = _json.loads(crudo.get("outcomes") or "[]")
        i_local = next((i for i, o in enumerate(salidas) if apodo(o) == lo), None)
        if i_local is None or fila["yes_price"] is None:
            continue
        libro = fila["yes_price"] if i_local == 0 else 1 - fila["yes_price"]
        v = p - libro
        marca = "  <<<" if abs(v) >= s.min_edge and horas > 0.25 else ""
        apuestas += 1 if marca else 0
        pits = ""
        if g.pitcher_visitante and g.pitcher_local:
            pits = (f"{g.pitcher_visitante.nombre.split()[-1][:9]}"
                    f"({g.pitcher_visitante.era or '—'}) vs "
                    f"{g.pitcher_local.nombre.split()[-1][:9]}"
                    f"({g.pitcher_local.era or '—'})")
        print(f"{g.visitante[:16]:17}@{g.local[:16]:18} {horas:6.1f}h {p:8.1%} "
              f"{libro:7.1%} {v:+8.1%}{marca:6} {pits}")
    print(f"\nDiscrepancias sobre el umbral de {s.min_edge:.0%}: {apuestas}")
    print("El modelo usa carreras (pitagórica), Log5, ERA del abridor y "
          "localía.\nSolo apuesta antes del primer lanzamiento.")


def cmd_wallets(app: App) -> None:
    """Quiénes están habilitadas para copia y en qué orden las vigila el bot."""
    rows = app.conn.execute(
        """SELECT b.wallet, b.roi, b.win_rate, b.n_copies, b.min_usdc,
                  b.trades_por_dia, b.mediana_usdc, r.username, r.score
           FROM wallet_backtest b
           LEFT JOIN wallet_ranking r ON r.wallet = b.wallet
           WHERE b.verdict = 'copiable' ORDER BY b.roi DESC""").fetchall()
    if not rows:
        print("Ninguna wallet habilitada. Corré: python -m pmbot validate-wallets")
        return
    n_fast = int(app.cfg.section("scheduler").get("fast_lane_wallets", 12))
    print(f"\n{len(rows)} wallets habilitadas para copia "
          f"(las primeras {n_fast} van en el carril rápido, cada 15s)\n")
    print(f"{'':3} {'wallet':<24} {'ROI':>7} {'aciertos':>9} {'copias':>7} "
          f"{'mín $':>7} {'trades/día':>11} {'mediana':>9}")
    for i, r in enumerate(rows, 1):
        marca = "⚡" if i <= n_fast else "  "
        nombre = (r["username"] or r["wallet"][:16])[:24]
        wr = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
        xdia = f"{r['trades_por_dia']:.0f}" if r["trades_por_dia"] else "—"
        med = f"${r['mediana_usdc']:,.0f}" if r["mediana_usdc"] else "—"
        print(f"{marca}{i:>2} {nombre:<24} {r['roi']:>+6.1%} {wr:>9} "
              f"{r['n_copies']:>7} {r['min_usdc'] or 0:>7.0f} {xdia:>11} {med:>9}")
    print(f"\n⚡ = carril rápido. El resto se vigila con la cinta (60s) "
          f"y el barrido (5 min).")
    rech = app.conn.execute(
        "SELECT COUNT(*) c FROM wallet_backtest WHERE verdict = 'rechazada'"
    ).fetchone()["c"]
    sin = app.conn.execute(
        "SELECT COUNT(*) c FROM wallet_backtest WHERE verdict = 'sin_datos'"
    ).fetchone()["c"]
    print(f"Rechazadas por no ser rentables o consistentes: {rech}")
    print(f"Sin muestra suficiente para opinar: {sin}")
    universo = app.conn.execute(
        "SELECT COUNT(*) c FROM wallet_candidates").fetchone()["c"]
    rank = app.conn.execute(
        "SELECT COUNT(*) c FROM wallet_ranking").fetchone()["c"]
    testeadas = rech + sin + len(rows)
    print(f"\nUniverso conocido: {universo} wallets vistas operando en grande "
          f"+ {rank} del leaderboard.")
    print(f"Con backtest corrido: {testeadas}. El resto entra en las "
          f"próximas corridas (la cola prioriza a las que no tienen "
          f"veredicto).")


async def cmd_validate_wallets(app: App, force: bool = False,
                               wallets: list[str] | None = None) -> None:
    if wallets:
        results = await app.wallet_validator.validar_lista(wallets)
    else:
        results = await app.wallet_validator.validate_ranked(force=force)
    if not results:
        print("Nada que validar (ya testeadas recientemente o ranking vacío).")
    for r in sorted(results, key=lambda x: -(x["roi"] or 0)):
        wr = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
        mark = {"copiable": "✅", "rechazada": "❌"}.get(r["verdict"], "⚪")
        motivo = f"  ({r.get('motivo', '')})" if r.get("motivo") else ""
        print(f"{mark} {(r['username'] or r['wallet'][:12]):<22} "
              f"ROI {r['roi']:+7.1%} | WR {wr:>4} | {r['n']:>3} copias "
              f"→ {r['verdict']}{motivo}")
    rows = app.conn.execute(
        "SELECT COUNT(*) c FROM wallet_backtest WHERE verdict='copiable'").fetchone()
    print(f"\nWallets habilitadas para copia: {rows['c']}")


def cmd_analisis(app: App, dias: int | None = None) -> None:
    """Rendimiento cerrado por categoría, estrategia y precio de entrada.

    Sale de posiciones CERRADAS del historial de órdenes, no de la cartera
    actual: las ganadoras se cobran y desaparecen de la cartera, así que
    medir ahí infla las pérdidas (pasó el 2026-08-24 con los esports).
    """
    from datetime import datetime, timedelta, timezone

    from .monitor.analisis import (agrupar, asimetria, formatear,
                                   posiciones_cerradas, revisar)

    desde = None
    if dias:
        desde = (datetime.now(timezone.utc)
                 - timedelta(days=dias)).isoformat(timespec="seconds")
    cerradas = posiciones_cerradas(app.conn, desde)
    if not cerradas:
        print("Todavía no hay posiciones cerradas en esa ventana.")
        return
    ventana = f"últimos {dias} días" if dias else "todo el historial"
    print(f"\n📊 RENDIMIENTO DE LO YA CERRADO ({ventana}) — "
          f"{len(cerradas)} posiciones")

    # Primero la revisión de cordura: si algún número no puede existir, se
    # avisa ANTES de las tablas en vez de presentarlo como si fuera cierto.
    problemas = revisar(cerradas)
    if problemas:
        print("\n⚠️  NÚMEROS IMPOSIBLES — no te fíes de las tablas de abajo:")
        for p in problemas[:8]:
            print(f"   {p}")

    for por, titulo in (("categoria", "POR CATEGORÍA"),
                        ("estrategia", "POR ESTRATEGIA"),
                        ("precio", "POR PRECIO DE ENTRADA"),
                        ("categoria+precio", "CATEGORÍA × PRECIO "
                         "(¿el problema es la categoría o el precio?)")):
        print(formatear(agrupar(cerradas, por), titulo))

    # El acierto no dice el tamaño del acierto: sin esto, "gana 58% y pierde
    # $86" parece una contradicción y se culpa a quien no es.
    for estrategia in sorted({c.strategy for c in cerradas}):
        a = asimetria([c for c in cerradas if c.strategy == estrategia])
        print(f"\n⚖️  {estrategia}: {a.ganadoras} ganadas / "
              f"{a.perdedoras} perdidas — ganadora media "
              f"${a.media_ganadora:.2f}, perdedora media "
              f"${a.media_perdedora:.2f}")
        print(f"   {a.diagnostico}")
    print("\n(cerradas = ya resueltas o vendidas; las abiertas no cuentan)")


def cmd_set_baseline(app: App, amount: float) -> None:
    """Fija el capital inicial de referencia para el PnL.

    El baseline se auto-fija la primera vez que arranca el modo real, pero
    si en ese momento había posiciones sin registrar queda subvaluado.
    """
    with app.conn:
        app.conn.execute(
            """INSERT INTO paper_state (key, value)
               VALUES ('live_starting_equity', ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (str(amount),))
    print(f"Capital inicial de referencia fijado en ${amount:.2f}. "
          "El PnL se mide desde ahí.")


async def cmd_trades(app: App, n: int = 25,
                     buscar: str | None = None) -> None:
    # Con `buscar` se filtra por el texto del mercado: las órdenes que
    # interesan casi nunca están entre las 25 últimas.
    if buscar:
        rows = app.conn.execute(
            """SELECT o.* FROM orders o
               LEFT JOIN markets m ON m.condition_id = o.condition_id
               WHERE m.question LIKE ? OR o.reason LIKE ?
               ORDER BY o.created_at DESC LIMIT ?""",
            (f"%{buscar}%", f"%{buscar}%", n)).fetchall()
    else:
        rows = app.conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
            (n,)).fetchall()
    titulo = f" que contienen '{buscar}'" if buscar else ""
    print(f"\nÚltimas órdenes{titulo} ({len(rows)}):")
    for r in rows:
        if r["status"] == "FILLED":
            detail = (f"{r['fill_size']:.0f} u @ {r['fill_price']:.3f} "
                      f"(${r['fill_usdc']:.2f})")
            if r["realized_pnl"] is not None:
                detail += f" PnL {r['realized_pnl']:+.2f}"
            # El riesgo juzga el precio LÍMITE, que es una estimación previa;
            # el CLOB llena a lo que haya en el libro. Si el hueco entre los
            # dos es grande, la orden que se aprobó no es la que se ejecutó
            # — y ninguna regla de precio miró la de verdad.
            lim = r["limit_price"]
            if (lim and r["side"] == "BUY" and r["fill_price"]
                    and abs(r["fill_price"] - lim) > 0.02):
                detail += (f"  ⚠️ pedido a {lim:.3f}, llenó a "
                           f"{r['fill_price']:.3f}")
        else:
            detail = r["reject_reason"] or ""
        print(f"  {r['created_at'][5:16]} [{r['strategy']:<12}] {r['side']:<6} "
              f"{r['status']:<12} {detail}")
        if r["reason"]:
            print(f"      motivo: {r['reason'][:90]}")


async def cmd_deportes(app: App) -> None:
    """Por qué el modelo deportivo apuesta o no, juego por juego.

    Recorre el MISMO camino que usa el bot (scan_and_execute con traza), sin
    mandar nada al broker. Un diagnóstico con su propia copia de la lógica
    no probaría nada sobre la lógica que opera de verdad.
    """
    traza: list[str] = []
    await app.sports_value.scan_and_execute(traza=traza, simular=True)
    est = app.sports_value
    print(f"\n⚾ MODELO DEPORTIVO — umbrales: ventaja mínima "
          f"{est.min_edge:.0%}, precio máximo {est.max_entry:.2f}, "
          f"apuesta mínima ${est.min_trade_usdc:.0f}, "
          f"antelación {est.minutos_antes:.0f} min")
    print(f"   equity para dimensionar: ${app.broker.equity():.2f}\n")
    if not traza:
        print("   (sin nada que contar: ni juegos ni datos)")
        return
    apuestas = [t for t in traza if "APOSTARÍA" in t]
    for linea in traza:
        print(f"   {'>>> ' if 'APOSTARÍA' in linea else ''}{linea}")
    print(f"\n   {len(apuestas)} apuesta(s) pasarían el modelo. "
          f"El riesgo todavía puede frenarlas aparte.")

    # Cuánto se equivoca el modelo propio donde SÍ se le puede tomar la
    # lección. Sin esta cuenta, sus ventajas en los partidos sin línea no se
    # distinguen de su propio error.
    import re as _re
    errores = [float(m.group(1)) for t in traza
               if (m := _re.search(r"me equivoco ([\d.]+) puntos", t))]
    if errores:
        errores.sort()
        media = sum(errores) / len(errores)
        print(f"\n   📏 CALIBRACIÓN sobre {len(errores)} partidos con línea "
              f"sharp: el modelo propio se desvía {media:.1f} puntos de "
              f"media (mediana {errores[len(errores)//2]:.1f}, "
              f"máximo {errores[-1]:.1f}).")
        print(f"      Umbral para apostar: {est.min_edge:.0%}. Si el error "
              f"típico es igual o mayor que el umbral, las 'ventajas' de "
              f"los partidos SIN línea son ruido del modelo, no dinero.")
    else:
        print("\n   📏 Sin partidos con línea sharp: no hay forma de saber "
              "cuánto se equivoca el modelo propio.")

    # La pregunta del negocio: ¿se despega Polymarket de las casas? Un
    # barrido suelto no la responde — los despegues duran minutos. Esto es
    # la serie acumulada de todo lo que el bot lleva comparando.
    from .monitor.analisis import resumen_despegues
    r = resumen_despegues(app.conn)
    print()
    if not r["n"]:
        print("   📊 Todavía no hay comparaciones registradas. El bot las va "
              "anotando cada 20 minutos; mañana habrá con qué juzgar.")
        return
    print(f"   📊 DESPEGUES — {r['n']} comparaciones acumuladas contra las "
          f"casas profesionales")
    print(f"      desviación media {r['media_abs']:.2%} | máxima "
          f"{r['maxima']:+.2%} | p95 {r['p95']:+.2%}")
    print(f"      por encima de 3%: {r['sobre_3']}   de 5%: {r['sobre_5']}   "
          f"de 8% (umbral): {r['sobre_8']}")
    if r["sobre_3"] == 0 and r["n"] >= 200:
        print("      Con esta muestra, Polymarket no se despega lo bastante "
              "como para que haya negocio en el ganador de partido.")
    flojas = [f"{k} ({n}, máx {mx:+.1%})"
              for k, (n, mx) in r["ligas"].items() if mx >= 0.03]
    if flojas:
        print("      Ligas con algún despegue de 3%+: " + ", ".join(flojas))


async def cmd_live_check(app: App) -> None:
    from .execution import LiveBroker
    if not isinstance(app.broker, LiveBroker):
        print("Modo PAPER activo. Para el modo real: LIVE_TRADING="
              "I_UNDERSTAND_THE_RISKS + claves de Polymarket en .env")
        return
    import asyncio as _asyncio
    info = await _asyncio.to_thread(app.broker.check_connection)
    print("\n✅ Conexión al CLOB OK")
    print(f"   Firmante:  {info['signer_address']}")
    print(f"   Funder:    {info['funder']}")
    print(f"   Saldo:     {info['usdc_balance']:.2f} USDC")
    print(f"   Allowance: {'OK' if info['allowance_ok'] else '⚠️ SIN ALLOWANCE'}")
    if not info["allowance_ok"]:
        print("   → Depositá desde la app de Polymarket (configura los "
              "permisos automáticamente) antes de operar.")
    positions = await app.data_api.positions(app.broker.proxy_address, limit=20)
    print(f"   Posiciones on-chain: {len(positions)}")
    for p in positions[:10]:
        print(f"     {p.outcome:<4} {p.size:>8.1f} u @ {p.avg_price:.3f} "
              f"| ${p.current_value:.2f} | {p.title[:45]}")


async def cmd_notify_test(app: App) -> None:
    if not app.notifier.enabled:
        print("Telegram no configurado: faltan TELEGRAM_BOT_TOKEN y/o "
              "TELEGRAM_CHAT_ID en .env (o telegram.enabled en config.yaml).")
        return
    await app.notifier.send("✅ pmbot conectado a Telegram. Por acá van a "
                            "llegar los trades y el reporte diario.")
    print("Mensaje enviado — revisá tu Telegram.")


async def cmd_test_trade(app: App) -> None:
    """Compra 5 shares en un mercado líquido y las vende al instante.
    Valida el circuito completo de órdenes reales; costo ~= el spread."""
    import json as _json
    from datetime import datetime, timezone
    from .risk import OrderRequest

    print(f"Modo: {app.cfg.mode} — orden de prueba mínima (5 shares)")

    # Si quedó una posición abierta de una prueba anterior, cerrarla primero.
    stamp0 = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    for pos in app.conn.execute(
            "SELECT * FROM paper_positions WHERE strategy = 'live_test'").fetchall():
        print(f"Cerrando prueba anterior: {pos['size']:.1f} u de "
              f"'{(pos['question'] or '')[:50]}'")
        sell_prev = await app.broker.execute(
            f"livetest:{stamp0}:cleanup",
            OrderRequest(strategy="live_test", condition_id=pos["condition_id"],
                         category=pos["category"] or "other",
                         token_id=pos["token_id"], outcome=pos["outcome"],
                         outcome_index=pos["outcome_index"] or 0, side="SELL",
                         size=pos["size"], price=0.0,
                         reason="prueba de circuito: cierre pendiente"))
        print(f"VENTA  → {sell_prev.status} {sell_prev.size:.1f} u @ "
              f"{sell_prev.price:.3f} (${sell_prev.usdc:.2f}) {sell_prev.detail}")
        if sell_prev.status == "FILLED":
            print(f"\n✅ CIRCUITO COMPLETO VERIFICADO (compra previa + esta "
                  f"venta). PnL de la prueba: {sell_prev.realized_pnl:+.2f} USDC")
            await app.notifier.send(
                "🧪 Prueba de circuito OK: compra y venta reales verificadas "
                f"(PnL {sell_prev.realized_pnl:+.2f} USDC)")
            return
        print("La venta de limpieza no llenó; sigo con una prueba nueva.\n")

    # Sin deportes: los mercados en vivo tienen delay de matcheo y la
    # prueba debe ser instantánea.
    row = app.conn.execute(
        """SELECT * FROM markets WHERE active = 1 AND category != 'sports'
           AND yes_price BETWEEN 0.10 AND 0.60 AND volume_24h > 50000
           AND clob_token_ids IS NOT NULL
           ORDER BY volume_24h DESC LIMIT 1""").fetchone()
    if not row:
        print("No hay mercado apto en cache; corré primero: python -m pmbot markets")
        return
    tokens = _json.loads(row["clob_token_ids"])
    book = await app.clob.order_book(tokens[0])
    if not book.best_ask or not book.best_bid:
        print("Book vacío, reintentá en un rato.")
        return
    print(f"Mercado: {row['question'][:60]}")
    print(f"Book real: bid {book.best_bid:.3f} / ask {book.best_ask:.3f}")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    buy = await app.broker.execute(
        f"livetest:{stamp}:buy",
        OrderRequest(strategy="live_test", condition_id=row["condition_id"],
                     category=row["category"], token_id=tokens[0],
                     outcome="Yes", outcome_index=0, side="BUY", size=5.0,
                     price=min(book.best_ask * 1.03, 0.99),
                     reason="prueba de circuito: compra mínima"))
    print(f"COMPRA → {buy.status} {buy.size:.1f} u @ {buy.price:.3f} "
          f"(${buy.usdc:.2f}) {buy.detail}")
    if buy.status != "FILLED":
        return
    sell = await app.broker.execute(
        f"livetest:{stamp}:sell",
        OrderRequest(strategy="live_test", condition_id=row["condition_id"],
                     category=row["category"], token_id=tokens[0],
                     outcome="Yes", outcome_index=0, side="SELL",
                     size=buy.size, price=0.0,
                     reason="prueba de circuito: venta inmediata"))
    print(f"VENTA  → {sell.status} {sell.size:.1f} u @ {sell.price:.3f} "
          f"(${sell.usdc:.2f}) {sell.detail}")
    if sell.status == "FILLED" and sell.realized_pnl is not None:
        print(f"\n✅ CIRCUITO COMPLETO VERIFICADO. Costo de la prueba "
              f"(spread): {sell.realized_pnl:+.2f} USDC")
        await app.notifier.send(
            f"🧪 Prueba de circuito OK: compra y venta reales ejecutadas en "
            f"'{row['question'][:50]}' (costo {sell.realized_pnl:+.2f} USDC)")
    else:
        print("\n⚠️ La compra funcionó pero la venta no llenó — la posición "
              "de 5 shares queda abierta (podés venderla desde la app).")


async def cmd_backtest(app: App, wallet: str, days: int, stake: float,
                       min_copy: float) -> None:
    from .backtest import CopyBacktester
    from .backtest.copy_backtest import format_report
    tester = CopyBacktester(app.data_api, app.gamma)
    if wallet == "top":
        rows = app.wallet_scorer.top_wallets(5)
        if not rows:
            print("No hay ranking; corré primero: python -m pmbot rank-wallets")
            return
        targets = [(r["wallet"], r["username"]) for r in rows]
    else:
        targets = [(wallet, "")]
    for addr, username in targets:
        report = await tester.run(addr, days=days, stake_usdc=stake,
                                  min_copy_usdc=min_copy)
        print("\n" + format_report(report, username))


def cmd_kill(app: App, mode: str) -> None:
    if mode == "on":
        app.risk.kill_file.touch()
        print("⛔ Kill switch ACTIVADO: no se abrirán posiciones nuevas "
              "(las ventas siguen permitidas).")
    else:
        app.risk.kill_file.unlink(missing_ok=True)
        print("✅ Kill switch desactivado.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="pmbot", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("markets")
    sub.add_parser("rank-wallets")
    p_pos = sub.add_parser("positions")
    p_pos.add_argument("wallet", nargs="?", default=None)
    sub.add_parser("news")
    sub.add_parser("briefing")
    sub.add_parser("daily")
    sub.add_parser("trade-cycle")
    sub.add_parser("portfolio")
    sub.add_parser("deportes")
    p_trades = sub.add_parser("trades")
    p_trades.add_argument("--n", type=int, default=25,
                          help="cuántas órdenes listar")
    p_trades.add_argument("--buscar", default=None,
                          help="filtrar por texto de la pregunta del mercado")
    p_kill = sub.add_parser("kill")
    p_kill.add_argument("mode", choices=["on", "off"])
    p_bt = sub.add_parser("backtest-wallet")
    p_bt.add_argument("wallet", help="dirección 0x… o 'top' (las 5 mejores)")
    p_bt.add_argument("--days", type=int, default=90)
    p_bt.add_argument("--stake", type=float, default=8.0,
                      help="USDC apostados por copia (default 8 ≈ 3%% de 270)")
    p_bt.add_argument("--min-copy", type=float, default=500.0,
                      help="tamaño mínimo del trade de la wallet para copiarlo")
    sub.add_parser("live-check")
    sub.add_parser("notify-test")
    sub.add_parser("test-trade")
    p_val = sub.add_parser("validate-wallets")
    p_val.add_argument("--force", action="store_true",
                       help="reevaluar a todas, sin esperar la ventana de 24h")
    p_val.add_argument("--wallet", action="append", default=None,
                       help="backtestear solo esta wallet (0x… o alias "
                            "público); repetible")
    sub.add_parser("diagnose")
    sub.add_parser("wallets")
    sub.add_parser("mlb")
    p_rend = sub.add_parser("rendimiento")
    p_rend.add_argument("--dias", type=int, default=None,
                        help="limitar a los últimos N días")
    p_cap = sub.add_parser("capital")
    p_cap.add_argument("--vender", action="store_true",
                       help="liberar las posiciones que superan el corte")
    p_cap.add_argument("--dias", type=float, default=None,
                       help="corte en días (por defecto, el máximo que "
                            "permite la política de riesgo)")
    p_ana = sub.add_parser("analisis")
    p_ana.add_argument("--dias", type=int, default=None,
                       help="limitar a los últimos N días")
    p_base = sub.add_parser("set-baseline")
    p_base.add_argument("amount", type=float)
    sub.add_parser("run")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()
    if cfg.live_trading:
        print("🔴 MODO REAL ACTIVADO: las órdenes se envían al CLOB con "
              "dinero real. Kill switch: python -m pmbot kill on",
              file=sys.stderr)

    async def dispatch() -> None:
        app = build_app(cfg)
        try:
            if args.command == "markets":
                await cmd_markets(app)
            elif args.command == "rank-wallets":
                await cmd_rank_wallets(app)
            elif args.command == "positions":
                await cmd_positions(app, args.wallet)
            elif args.command == "news":
                await cmd_news(app)
            elif args.command == "briefing":
                await cmd_briefing(app)
            elif args.command == "daily":
                await cmd_daily(app)
            elif args.command == "trade-cycle":
                await cmd_trade_cycle(app)
            elif args.command == "portfolio":
                await cmd_portfolio(app)
            elif args.command == "deportes":
                await cmd_deportes(app)
            elif args.command == "trades":
                await cmd_trades(app, n=args.n, buscar=args.buscar)
            elif args.command == "kill":
                cmd_kill(app, args.mode)
            elif args.command == "backtest-wallet":
                await cmd_backtest(app, args.wallet, args.days, args.stake,
                                   args.min_copy)
            elif args.command == "live-check":
                await cmd_live_check(app)
            elif args.command == "notify-test":
                await cmd_notify_test(app)
            elif args.command == "test-trade":
                await cmd_test_trade(app)
            elif args.command == "analisis":
                cmd_analisis(app, dias=args.dias)
            elif args.command == "diagnose":
                from .diagnose import diagnose
                await diagnose()
            elif args.command == "validate-wallets":
                await cmd_validate_wallets(app, force=args.force,
                                           wallets=args.wallet)
            elif args.command == "wallets":
                cmd_wallets(app)
            elif args.command == "mlb":
                await cmd_mlb(app)
            elif args.command == "rendimiento":
                cmd_rendimiento(app, dias=args.dias)
            elif args.command == "capital":
                await cmd_capital(app, vender=args.vender,
                                  dias_min=args.dias)
            elif args.command == "set-baseline":
                cmd_set_baseline(app, args.amount)
            elif args.command == "run":
                await run_forever(app)
        finally:
            await app.aclose()

    asyncio.run(dispatch())


if __name__ == "__main__":
    main()

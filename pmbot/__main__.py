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
  python -m pmbot set-baseline N     # fija el capital inicial contra el que se mide el PnL
  python -m pmbot validate-wallets   # backtestea el ranking y habilita a quién copiar
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
    print(f"\n💰 Equity: ${state.equity:.2f}  (cash ${state.cash:.2f} + "
          f"posiciones ${state.exposure_total:.2f})")
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


async def cmd_validate_wallets(app: App) -> None:
    results = await app.wallet_validator.validate_ranked()
    if not results:
        print("Nada que validar (ya testeadas recientemente o ranking vacío).")
    for r in sorted(results, key=lambda x: -(x["roi"] or 0)):
        wr = f"{r['win_rate']:.0%}" if r["win_rate"] is not None else "—"
        mark = {"copiable": "✅", "rechazada": "❌"}.get(r["verdict"], "⚪")
        print(f"{mark} {(r['username'] or r['wallet'][:12]):<22} "
              f"ROI {r['roi']:+7.1%} | WR {wr:>4} | {r['n']:>3} copias "
              f"→ {r['verdict']}")
    rows = app.conn.execute(
        "SELECT COUNT(*) c FROM wallet_backtest WHERE verdict='copiable'").fetchone()
    print(f"\nWallets habilitadas para copia: {rows['c']}")


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


async def cmd_trades(app: App) -> None:
    rows = app.conn.execute(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT 25").fetchall()
    print(f"\nÚltimas órdenes ({len(rows)}):")
    for r in rows:
        if r["status"] == "FILLED":
            detail = (f"{r['fill_size']:.0f} u @ {r['fill_price']:.3f} "
                      f"(${r['fill_usdc']:.2f})")
            if r["realized_pnl"] is not None:
                detail += f" PnL {r['realized_pnl']:+.2f}"
        else:
            detail = r["reject_reason"] or ""
        print(f"  {r['created_at'][5:16]} [{r['strategy']:<12}] {r['side']:<6} "
              f"{r['status']:<12} {detail}")
        if r["reason"]:
            print(f"      motivo: {r['reason'][:90]}")


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
    sub.add_parser("trades")
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
    sub.add_parser("validate-wallets")
    sub.add_parser("diagnose")
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
            elif args.command == "trades":
                await cmd_trades(app)
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
            elif args.command == "diagnose":
                from .diagnose import diagnose
                await diagnose()
            elif args.command == "validate-wallets":
                await cmd_validate_wallets(app)
            elif args.command == "set-baseline":
                cmd_set_baseline(app, args.amount)
            elif args.command == "run":
                await run_forever(app)
        finally:
            await app.aclose()

    asyncio.run(dispatch())


if __name__ == "__main__":
    main()

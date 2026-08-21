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
    state = app.broker.portfolio_state()
    row = app.conn.execute(
        "SELECT starting_usdc FROM paper_account WHERE id=1").fetchone()
    starting = row["starting_usdc"]
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
    sub.add_parser("run")
    args = parser.parse_args()

    setup_logging()
    cfg = load_config()
    if cfg.live_trading:
        print("⚠️  LIVE_TRADING activado — pero la fase actual es solo lectura;"
              " ninguna orden se envía todavía.", file=sys.stderr)

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
            elif args.command == "run":
                await run_forever(app)
        finally:
            await app.aclose()

    asyncio.run(dispatch())


if __name__ == "__main__":
    main()

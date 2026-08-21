"""Rutina diaria y loop 24/7.

Fase 2 (paper trading):
- Rutina diaria a hora fija (scheduler.daily_run_utc):
    1. refrescar mercados activos
    2. refrescar ranking de wallets y posiciones (top + copiadas)
    3. bajar y analizar noticias; briefing por categoría
    4. liquidar posiciones de mercados resueltos (redeem a 0/1)
    5. rebalancear: salir de copias cuya wallet salió (tesis rota)
    6. tomar oportunidades nuevas (copy + arbitraje) si pasan risk/
       — si no hay ninguna que califique, NO se opera y se reporta
    7. snapshot de equity y reporte -> reports/YYYY-MM-DD.md + Telegram
- Intradía: polls de intel y smart_money; las señales de smart_money se
  copian en tiempo real (con límite de slippage) y el arbitraje se escanea
  en cada refresh de mercados.

Toda orden pasa por risk/ dentro del broker; el modo real no existe aún.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from ..context import App
from ..db import from_json

log = logging.getLogger("pmbot.scheduler")


def next_daily_run(now: datetime, daily_utc: str) -> datetime:
    """Próxima ocurrencia de HH:MM UTC estrictamente en el futuro."""
    hour, minute = (int(x) for x in daily_utc.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


class DailyRoutine:
    def __init__(self, app: App) -> None:
        self.app = app

    async def refresh_markets(self) -> int:
        cfg = self.app.cfg.section("data")
        markets = await self.app.gamma.fetch_active_markets(
            max_markets=int(cfg.get("max_markets", 600)),
            page_size=int(cfg.get("page_size", 100)),
            min_liquidity=float(cfg.get("min_liquidity", 0)),
        )
        return self.app.market_store.upsert_markets(markets)

    async def refresh_smart_money(self) -> list[str]:
        await self.app.wallet_scorer.refresh_ranking()
        top = self.app.wallet_scorer.top_wallets()
        wallets = {r["wallet"] for r in top}
        # También refrescar las wallets que estamos copiando aunque hayan
        # salido del top: la señal de salida depende de su snapshot.
        for row in self.app.conn.execute(
                "SELECT meta FROM paper_positions WHERE strategy='copy_trading'"):
            meta = from_json(row["meta"]) or {}
            if meta.get("copied_wallet"):
                wallets.add(meta["copied_wallet"])
        await self.app.wallet_tracker.refresh_positions(sorted(wallets))
        return sorted(wallets)

    async def settle_resolved(self) -> list[str]:
        """Liquida posiciones de mercados ya resueltos."""
        settled: list[str] = []
        for pos in list(self.app.broker.positions()):
            try:
                status = await self.app.gamma.market_status(pos["condition_id"])
            except Exception as exc:
                log.warning("status de %s falló: %s",
                            pos["condition_id"][:10], exc)
                continue
            if not status or not status["closed"]:
                continue
            prices = status["outcome_prices"]
            idx = pos["outcome_index"] or 0
            if idx >= len(prices):
                continue
            payout = prices[idx]
            fill = self.app.broker.redeem(
                pos, payout, f"mercado resuelto (payout {payout:g})")
            if fill.status == "FILLED":
                settled.append(f"{(pos['question'] or '')[:60]} — resuelto a "
                               f"{payout:g}, PnL {fill.realized_pnl:+.2f}")
        return settled

    async def trade_cycle(self) -> dict[str, list[str]]:
        """Rebalanceo + oportunidades nuevas. Devuelve movimientos por tipo."""
        moves: dict[str, list[str]] = {}
        moves["liquidadas"] = await self.settle_resolved()
        moves["salidas"] = await self.app.copy_trading.check_exits()
        moves["copias"] = await self.app.copy_trading.process_signals()
        moves["consenso"] = await self.app.copy_trading.check_holdings_consensus()
        moves["arbitrajes"] = await self.app.arbitrage.scan_and_execute()
        self.app.broker.snapshot_equity()
        return moves

    async def refresh_intel(self) -> dict[str, str]:
        fetcher = self.app.news_fetcher
        await fetcher.fetch_all()
        pending = fetcher.pending_analysis(limit=200)
        await self.app.news_analyzer.analyze_pending(pending)
        return self.app.briefing.build_daily()

    async def run_daily(self) -> str:
        """Rutina completa. Devuelve el reporte en texto."""
        log.info("=== rutina diaria (%s) ===", self.app.cfg.mode)
        n_markets = await self.refresh_markets()
        wallets = await self.refresh_smart_money()
        briefings = await self.refresh_intel()
        moves = await self.trade_cycle()
        report = self._build_report(n_markets, wallets, briefings, moves)
        self._write_report(report)
        await self.app.notifier.send(report)
        return report

    def _build_report(self, n_markets: int, wallets: list[str],
                      briefings: dict[str, str],
                      moves: dict[str, list[str]]) -> str:
        conn = self.app.conn
        today = datetime.now(timezone.utc).date().isoformat()
        lines = [f"📊 pmbot — reporte diario {today} [{self.app.cfg.mode}]", ""]

        state = self.app.broker.portfolio_state()
        starting = float(conn.execute(
            "SELECT starting_usdc FROM paper_account WHERE id=1"
        ).fetchone()["starting_usdc"])
        total_pnl = state.equity - starting
        lines.append(
            f"💰 Equity: ${state.equity:.2f} (cash ${state.cash:.2f} + "
            f"posiciones ${state.exposure_total:.2f}) | "
            f"PnL total {total_pnl:+.2f} ({total_pnl / starting:+.1%})")
        pnl_by_strategy = self._pnl_by_strategy(state)
        if pnl_by_strategy:
            lines.append("PnL por estrategia: " + " | ".join(
                f"{s}: realizado {r:+.2f}, no realizado {u:+.2f}"
                for s, (r, u) in sorted(pnl_by_strategy.items())))
        lines.append("")

        lines.append("🔁 Movimientos de hoy:")
        any_move = False
        for kind, items in moves.items():
            for item in items:
                any_move = True
                lines.append(f"  [{kind}] {item}")
        todays_orders = conn.execute(
            """SELECT * FROM orders WHERE date(created_at)=? AND status='FILLED'
               ORDER BY created_at""", (today,)).fetchall()
        if not any_move and not todays_orders:
            lines.append("  (sin oportunidades que superen los umbrales: hoy no se opera)")
        lines.append("")

        open_positions = self.app.broker.positions()
        lines.append(f"📈 Posiciones abiertas ({len(open_positions)}):")
        for p in open_positions[:15]:
            mark = self.app.broker.mark_price(
                p["condition_id"], p["outcome_index"] or 0, p["avg_price"])
            unreal = p["size"] * (mark - p["avg_price"])
            lines.append(
                f"  [{p['strategy']}] {p['outcome']:<4} {p['size']:.0f} u "
                f"@ {p['avg_price']:.3f} → {mark:.3f} (PnL {unreal:+.2f})  "
                f"{(p['question'] or '')[:50]}")
        if not open_positions:
            lines.append("  (ninguna)")
        lines.append("")
        lines.append(f"Mercados activos cacheados: {n_markets}")

        cats = self.app.market_store.category_summary()
        if cats:
            lines.append("Volumen 24h por categoría: " + ", ".join(
                f"{c['category']} ${c['vol24h']:,.0f}" for c in cats[:6]))
        lines.append("")

        lines.append(f"🏆 Wallets top monitoreadas ({len(wallets)}):")
        for row in conn.execute(
                """SELECT * FROM wallet_ranking WHERE passed_filters = 1
                   ORDER BY score DESC LIMIT 10"""):
            name = row["username"] or row["wallet"][:10]
            lines.append(
                f"  {name:<20} score {row['score']:.3f} | "
                f"PnL 30d ${row['pnl_month']:,.0f} | total ${row['pnl_all']:,.0f} | "
                f"{row['trades']} trades / {row['distinct_markets']} mercados")
        lines.append("")

        new_signals = conn.execute(
            """SELECT COUNT(*) AS n FROM signals
               WHERE date(created_at) = ?""", (today,)).fetchone()["n"]
        lines.append(f"Señales registradas hoy: {new_signals}")
        lines.append("")

        lines.append("📰 Briefing por categoría:")
        for category in sorted(briefings):
            lines.append(briefings[category])
            lines.append("")
        if not briefings:
            lines.append("(sin noticias analizadas hoy)")
        return "\n".join(lines)

    def _pnl_by_strategy(self, state) -> dict[str, tuple[float, float]]:
        """{estrategia: (PnL realizado, PnL no realizado)}."""
        out: dict[str, tuple[float, float]] = {}
        for row in self.app.conn.execute(
                """SELECT strategy, COALESCE(SUM(realized_pnl), 0) AS r
                   FROM orders WHERE realized_pnl IS NOT NULL
                   GROUP BY strategy"""):
            out[row["strategy"]] = (row["r"], 0.0)
        for p in self.app.broker.positions():
            mark = self.app.broker.mark_price(
                p["condition_id"], p["outcome_index"] or 0, p["avg_price"])
            unreal = p["size"] * (mark - p["avg_price"])
            r, u = out.get(p["strategy"], (0.0, 0.0))
            out[p["strategy"]] = (r, u + unreal)
        return out

    def _write_report(self, report: str) -> Path:
        reports_dir = Path("reports")
        reports_dir.mkdir(exist_ok=True)
        path = reports_dir / f"{datetime.now(timezone.utc).date().isoformat()}.md"
        path.write_text(report, encoding="utf-8")
        log.info("reporte escrito en %s", path)
        return path

    async def poll_intel(self) -> None:
        new = await self.app.news_fetcher.fetch_all()
        if new:
            pending = self.app.news_fetcher.pending_analysis(limit=50)
            await self.app.news_analyzer.analyze_pending(pending)

    async def poll_smart_money(self) -> None:
        top = self.app.wallet_scorer.top_wallets()
        wallets = [r["wallet"] for r in top]
        if not wallets:
            return
        trades = await self.app.wallet_tracker.poll_new_activity(wallets)
        for t in trades:
            log.info("señal smart_money: %s %s '%s' @%.2f (%.0f USDC)",
                     t.wallet[:10], t.side, t.title[:50], t.price, t.usdc_size)
        if trades:
            # Copia en tiempo real: el precio se mueve rápido tras la entrada
            # de una wallet grande, no esperamos a la rutina diaria.
            copied = await self.app.copy_trading.process_signals()
            for desc in copied:
                log.info("COPIA intradía: %s", desc)
            if copied:
                await self.app.notifier.send(
                    "🤖 Copia ejecutada:\n" + "\n".join(f"• {d}" for d in copied))

    async def poll_arbitrage(self) -> None:
        executed = await self.app.arbitrage.scan_and_execute()
        for desc in executed:
            log.info("ARB intradía: %s", desc)
        if executed:
            await self.app.notifier.send(
                "♻️ Arbitraje ejecutado:\n" + "\n".join(f"• {d}" for d in executed))


async def run_forever(app: App) -> None:
    """Loop principal 24/7: rutina diaria a hora fija + polls intradía."""
    routine = DailyRoutine(app)
    sched = app.cfg.section("scheduler")
    daily_utc = str(sched.get("daily_run_utc", "11:00"))
    intel_every = timedelta(minutes=float(sched.get("intel_poll_minutes", 30)))
    sm_every = timedelta(minutes=float(sched.get("smart_money_poll_minutes", 15)))
    markets_every = timedelta(minutes=float(sched.get("markets_refresh_minutes", 30)))

    now = datetime.now(timezone.utc)
    next_daily = next_daily_run(now, daily_utc)
    next_intel = now  # primer poll inmediato
    next_sm = now
    next_markets = now
    log.info("scheduler iniciado [%s]. Próxima rutina diaria: %s UTC",
             app.cfg.mode, next_daily.isoformat(timespec="minutes"))

    while True:
        now = datetime.now(timezone.utc)
        try:
            if now >= next_daily:
                next_daily = next_daily_run(now, daily_utc)
                await routine.run_daily()
            if now >= next_markets:
                next_markets = now + markets_every
                await routine.refresh_markets()
                await routine.poll_arbitrage()
            if now >= next_intel:
                next_intel = now + intel_every
                await routine.poll_intel()
            if now >= next_sm:
                next_sm = now + sm_every
                await routine.poll_smart_money()
        except Exception:
            # Ningún fallo transitorio debe tumbar el loop 24/7.
            log.exception("error en el ciclo del scheduler; sigo")
        wake = min(next_daily, next_intel, next_sm, next_markets)
        await asyncio.sleep(max((wake - datetime.now(timezone.utc)).total_seconds(), 1.0))

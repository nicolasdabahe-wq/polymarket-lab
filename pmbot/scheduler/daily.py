"""Rutina diaria y loop 24/7.

Fase 1 (solo lectura, paper):
- Rutina diaria a hora fija (scheduler.daily_run_utc):
    1. refrescar mercados activos
    2. refrescar ranking de wallets
    3. snapshot de posiciones de las wallets top
    4. bajar y analizar noticias
    5. briefing diario por categoría
    6. reporte del día -> reports/YYYY-MM-DD.md + Telegram (si está activo)
- Intradía: polls de intel (noticias nuevas) y smart_money (trades nuevos de
  wallets top). Solo registran señales; nadie opera todavía.

Las fases siguientes insertan aquí: research/, rebalanceo de posiciones y
toma de oportunidades vía risk/ + execution/.
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
        wallets = [r["wallet"] for r in top]
        await self.app.wallet_tracker.refresh_positions(wallets)
        return wallets

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
        report = self._build_report(n_markets, wallets, briefings)
        self._write_report(report)
        await self.app.notifier.send(report)
        return report

    def _build_report(self, n_markets: int, wallets: list[str],
                      briefings: dict[str, str]) -> str:
        conn = self.app.conn
        today = datetime.now(timezone.utc).date().isoformat()
        lines = [f"📊 pmbot — reporte diario {today} [{self.app.cfg.mode}]", ""]

        paper = float(self.app.cfg.section("capital").get("paper_starting_usdc", 0))
        lines.append(f"Capital paper: {paper:.2f} USDC (sin posiciones: fase 1 es solo lectura)")
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
        lines.append(f"Señales registradas hoy: {new_signals} (informativas; aún no se opera)")
        lines.append("")

        lines.append("📰 Briefing por categoría:")
        for category in sorted(briefings):
            lines.append(briefings[category])
            lines.append("")
        if not briefings:
            lines.append("(sin noticias analizadas hoy)")
        return "\n".join(lines)

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

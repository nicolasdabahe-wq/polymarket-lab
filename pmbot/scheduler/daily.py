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
from .settlement import decide_settlement

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
        # Copiables validadas por backtest (incluidas las descubiertas fuera
        # del leaderboard): hay que vigilarlas para ver sus trades nuevos.
        wallets |= {r["wallet"] for r in self.app.conn.execute(
            "SELECT wallet FROM wallet_backtest WHERE verdict = 'copiable'")}
        # También refrescar las wallets que estamos copiando aunque hayan
        # salido del top: la señal de salida depende de su snapshot.
        for row in self.app.conn.execute(
                "SELECT meta FROM paper_positions WHERE strategy='copy_trading'"):
            meta = from_json(row["meta"]) or {}
            if meta.get("copied_wallet"):
                wallets.add(meta["copied_wallet"])
        await self.app.wallet_tracker.refresh_positions(sorted(wallets))
        # Validar por backtest a quién conviene copiar (crece el universo
        # de copiables con evidencia, sin lista negra manual).
        try:
            results = await self.app.wallet_validator.validate_ranked()
            if results:
                ok = [r for r in results if r["verdict"] == "copiable"]
                log.info("validación: %d/%d wallets habilitadas para copia",
                         len(ok), len(results))
        except Exception:
            log.exception("validación de wallets falló; sigo")
        return sorted(wallets)

    async def reconcile(self) -> list[str]:
        """Adopta posiciones on-chain que el bot no registró (fills tardíos
        en mercados con delay). Solo aplica al broker real."""
        reconcile = getattr(self.app.broker, "reconcile_positions", None)
        if reconcile is None:
            return []
        notes = await reconcile(self.app.data_api)
        if notes:
            await self.app.notifier.send(
                "🎯 TRADE detectado (fill tardío en mercado con delay):\n"
                + "\n".join(f"• {n}" for n in notes)
                + "\n\nLa posición ya está registrada y el bot la gestiona.")
        return notes

    # ---------- liquidación ----------

    async def _onchain_prices(self) -> dict[tuple[str, int], tuple[float, bool]]:
        """Precio actual y flag redimible de las posiciones on-chain.

        Solo aplica al broker real; en paper devuelve vacío y la liquidación
        usa únicamente Gamma.
        """
        wallet = getattr(self.app.broker, "proxy_address", None)
        if not wallet:
            return {}
        try:
            rows = await self.app.data_api.positions(wallet, limit=100)
        except Exception as exc:
            log.warning("liquidación: no se pudieron leer posiciones "
                        "on-chain: %s", exc)
            return {}
        return {(r.condition_id, r.outcome_index): (r.cur_price, r.redeemable)
                for r in rows}

    def _pinned_since(self, key: str) -> datetime | None:
        row = self.app.conn.execute(
            "SELECT value FROM paper_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        try:
            return datetime.fromisoformat(row["value"])
        except ValueError:
            return None

    def _save_pinned(self, key: str, value: datetime | None) -> None:
        with self.app.conn:
            if value is None:
                self.app.conn.execute(
                    "DELETE FROM paper_state WHERE key = ?", (key,))
            else:
                self.app.conn.execute(
                    """INSERT INTO paper_state (key, value) VALUES (?, ?)
                       ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                    (key, value.isoformat()))

    async def settle_resolved(self) -> list[str]:
        """Liquida posiciones de mercados ya resueltos.

        No basta con esperar a que Gamma marque closed: la resolución UMA
        puede tardar horas y en deportes el resultado se sabe al instante.
        Por eso también se liquida cuando el payout ya es redimible on-chain
        o cuando el precio quedó clavado en ~0/~1 (ver settlement.py).
        """
        positions = list(self.app.broker.positions())
        if not positions:
            return []
        onchain = await self._onchain_prices()
        confirm = float(self.app.cfg.section("scheduler").get(
            "settle_confirm_minutes", 10))
        now = datetime.now(timezone.utc)
        settled: list[str] = []
        won = False
        for pos in positions:
            cid = pos["condition_id"]
            idx = pos["outcome_index"] or 0
            try:
                status = await self.app.gamma.market_status(cid)
            except Exception as exc:
                log.warning("status de %s falló: %s", cid[:10], exc)
                status = None
            price, redeemable = onchain.get((cid, idx), (None, False))
            key = f"pinned:{cid}:{idx}"
            decision = decide_settlement(
                gamma_closed=bool(status and status["closed"]),
                gamma_prices=(status or {}).get("outcome_prices"),
                outcome_index=idx,
                onchain_price=price,
                onchain_redeemable=redeemable,
                pinned_since=self._pinned_since(key),
                now=now, confirm_minutes=confirm)
            self._save_pinned(key, decision.pinned_since)
            if decision.payout is None:
                continue
            fill = self.app.broker.redeem(
                pos, decision.payout,
                f"{decision.reason} (payout {decision.payout:g})")
            if fill.status == "FILLED":
                won = won or decision.payout > 0
                settled.append(f"{(pos['question'] or '')[:60]} — "
                               f"{decision.reason}, PnL {fill.realized_pnl:+.2f}")
        if settled:
            emoji = "🏆" if won else "📕"
            msg = (f"{emoji} Mercado(s) resuelto(s):\n"
                   + "\n".join(f"• {s}" for s in settled))
            if won:
                # El payout se cobra manualmente: el bot no puede reclamarlo.
                msg += ("\n\n💵 Cobrá el premio en la app de Polymarket "
                        "(botón Claim): hasta entonces no vuelve al saldo.")
            await self.app.notifier.send(msg)
        return settled

    async def trade_cycle(self) -> dict[str, list[str]]:
        """Rebalanceo + oportunidades nuevas. Devuelve movimientos por tipo."""
        moves: dict[str, list[str]] = {}
        moves["reconciliadas"] = await self.reconcile()
        moves["liquidadas"] = await self.settle_resolved()
        moves["salidas"] = await self.app.copy_trading.check_exits()
        moves["copias"] = await self.app.copy_trading.process_signals()
        moves["consenso"] = await self.app.copy_trading.check_holdings_consensus()
        moves["arbitrajes"] = await self.app.arbitrage.scan_and_execute()
        moves["valor_cripto"] = await self.app.crypto_value.scan_and_execute()
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
        starting = self.app.broker.starting_capital()
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
        wallets = sorted({r["wallet"] for r in self.app.wallet_scorer.top_wallets()}
                         | {r["wallet"] for r in self.app.conn.execute(
                             "SELECT wallet FROM wallet_backtest "
                             "WHERE verdict = 'copiable'")})
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

    async def poll_reconcile(self) -> None:
        try:
            await self.reconcile()
            # Liquidar apenas resuelve el mercado (no esperar al día siguiente).
            await self.settle_resolved()
        except Exception:
            log.exception("reconciliación falló; sigo")

    async def poll_holdings_consensus(self) -> None:
        """Refresca las carteras de las wallets vigiladas y busca consensos."""
        wallets = sorted({r["wallet"] for r in self.app.wallet_scorer.top_wallets()}
                         | {r["wallet"] for r in self.app.conn.execute(
                             "SELECT wallet FROM wallet_backtest "
                             "WHERE verdict = 'copiable'")})
        if not wallets:
            return
        await self.app.wallet_tracker.refresh_positions(wallets)
        entries = await self.app.copy_trading.check_holdings_consensus()
        for desc in entries:
            log.info("CONSENSO intradía: %s", desc)
        if entries:
            await self.app.notifier.send(
                "🤝 Consenso de posiciones:\n" + "\n".join(f"• {d}" for d in entries))

    async def poll_arbitrage(self) -> None:
        executed = await self.app.arbitrage.scan_and_execute()
        for desc in executed:
            log.info("ARB intradía: %s", desc)
        if executed:
            await self.app.notifier.send(
                "♻️ Arbitraje ejecutado:\n" + "\n".join(f"• {d}" for d in executed))
        value_trades = await self.app.crypto_value.scan_and_execute()
        if value_trades:
            await self.app.notifier.send(
                "📐 Valor cripto (modelo vs mercado):\n"
                + "\n".join(f"• {d}" for d in value_trades))


async def run_forever(app: App) -> None:
    """Loop principal 24/7: rutina diaria a hora fija + polls intradía."""
    routine = DailyRoutine(app)
    sched = app.cfg.section("scheduler")
    daily_utc = str(sched.get("daily_run_utc", "11:00"))
    intel_every = timedelta(minutes=float(sched.get("intel_poll_minutes", 30)))
    sm_every = timedelta(minutes=float(sched.get("smart_money_poll_minutes", 15)))
    markets_every = timedelta(minutes=float(sched.get("markets_refresh_minutes", 30)))
    consensus_every = timedelta(
        minutes=float(sched.get("holdings_consensus_minutes", 30)))
    reconcile_every = timedelta(
        minutes=float(sched.get("reconcile_minutes", 5)))

    now = datetime.now(timezone.utc)
    next_daily = next_daily_run(now, daily_utc)
    next_intel = now  # primer poll inmediato
    next_sm = now
    next_markets = now
    next_consensus = now
    next_reconcile = now
    log.info("scheduler iniciado [%s]. Próxima rutina diaria: %s UTC",
             app.cfg.mode, next_daily.isoformat(timespec="minutes"))

    # Bootstrap: en una instalación fresca el ranking de wallets está vacío
    # y sin él la copia queda ciega hasta la rutina diaria. Se genera ya.
    empty = app.conn.execute(
        "SELECT COUNT(*) AS c FROM wallet_ranking").fetchone()["c"] == 0
    if empty:
        try:
            log.info("ranking vacío: bootstrap inicial de mercados y wallets")
            await routine.refresh_markets()
            await routine.refresh_smart_money()
        except Exception:
            log.exception("bootstrap falló; la rutina diaria lo reintentará")

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
            if now >= next_consensus:
                next_consensus = now + consensus_every
                await routine.poll_holdings_consensus()
            if now >= next_reconcile:
                next_reconcile = now + reconcile_every
                await routine.poll_reconcile()
        except Exception:
            # Ningún fallo transitorio debe tumbar el loop 24/7.
            log.exception("error en el ciclo del scheduler; sigo")
        wake = min(next_daily, next_intel, next_sm, next_markets,
                   next_consensus, next_reconcile)
        await asyncio.sleep(max((wake - datetime.now(timezone.utc)).total_seconds(), 1.0))

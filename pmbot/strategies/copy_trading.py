"""Copy trading sobre las señales de smart_money/.

Entrada (sobre señales new_trade no procesadas):
- Solo BUYs de wallets con score >= min_wallet_score y tamaño >= min_copy_usdc.
- Dispara si: una wallet de score muy alto entra fuerte (strong_score /
  strong_usdc), o >= confirm_count wallets calificadas coinciden en el mismo
  mercado y outcome.
- No copiar si el precio ya se movió más de max_slippage_pct desde su entrada.
- Tamaño: equity * base_pct_per_trade * confianza (score de la wallet líder).

Salida:
- Cuando la wallet copiada ya no tiene la posición (o la redujo >50%), vender.
- Si el mercado resuelve, lo liquida la rutina de settlement del scheduler.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..db import from_json, to_json
from ..execution import PaperBroker
from ..risk import OrderRequest

log = logging.getLogger("pmbot.strategies.copy")


@dataclass
class CopyCandidate:
    condition_id: str
    outcome_index: int
    outcome: str
    title: str
    wallets: list[dict[str, Any]]   # [{wallet, score, price, usdc}]

    @property
    def leader(self) -> dict[str, Any]:
        return max(self.wallets, key=lambda w: w["score"])


def slippage_ok(entry_price: float, current_price: float,
                max_slippage_pct: float) -> bool:
    """True si el precio no subió más de max_slippage_pct desde la entrada
    de la wallet copiada. Pura."""
    if entry_price <= 0 or current_price <= 0:
        return False
    return (current_price - entry_price) / entry_price <= max_slippage_pct


def pick_candidates(signals: list[dict[str, Any]], scores: dict[str, float],
                    cfg: dict[str, Any]) -> list[CopyCandidate]:
    """Agrupa señales calificadas y aplica las reglas de disparo. Pura.

    signals: payloads de señales new_trade (wallet, side, outcome,
    outcome_index, price, usdc, title, condition_id).
    """
    min_score = float(cfg.get("min_wallet_score", 0.55))
    min_usdc = float(cfg.get("min_copy_usdc_of_wallet", 500))
    strong_score = float(cfg.get("strong_score", 0.70))
    strong_usdc = float(cfg.get("strong_usdc", 2000))
    confirm_count = int(cfg.get("confirm_count", 2))

    grouped: dict[tuple[str, int], CopyCandidate] = {}
    for s in signals:
        score = scores.get(s.get("wallet", ""), 0.0)
        if (s.get("side") != "BUY" or score < min_score
                or float(s.get("usdc", 0)) < min_usdc):
            continue
        key = (s["condition_id"], int(s.get("outcome_index", 0)))
        cand = grouped.setdefault(key, CopyCandidate(
            condition_id=s["condition_id"],
            outcome_index=int(s.get("outcome_index", 0)),
            outcome=s.get("outcome", ""), title=s.get("title", ""),
            wallets=[]))
        if all(w["wallet"] != s["wallet"] for w in cand.wallets):
            cand.wallets.append({"wallet": s["wallet"], "score": score,
                                 "price": float(s.get("price", 0)),
                                 "usdc": float(s.get("usdc", 0))})

    out = []
    for cand in grouped.values():
        leader = cand.leader
        strong_single = (leader["score"] >= strong_score
                         and leader["usdc"] >= strong_usdc)
        consensus = len(cand.wallets) >= confirm_count
        if strong_single or consensus:
            out.append(cand)
    return out


class CopyTradingStrategy:
    name = "copy_trading"

    def __init__(self, conn: sqlite3.Connection, broker: PaperBroker,
                 cfg: dict[str, Any]) -> None:
        self.conn = conn
        self.broker = broker
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.budget_pct = float(cfg.get("budget_pct", 0.50))
        self.base_pct = float(cfg.get("base_pct_per_trade", 0.03))
        self.max_slippage = float(cfg.get("max_slippage_pct", 0.10))

    def _wallet_scores(self) -> dict[str, float]:
        return {r["wallet"]: r["score"] for r in self.conn.execute(
            "SELECT wallet, score FROM wallet_ranking WHERE passed_filters = 1")}

    def _unprocessed_signals(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM signals WHERE source = 'smart_money'
               AND kind = 'new_trade' AND processed = 0
               ORDER BY created_at""").fetchall()

    async def process_signals(self) -> list[str]:
        """Consume señales nuevas y ejecuta copias. Devuelve descripciones."""
        if not self.enabled:
            return []
        rows = self._unprocessed_signals()
        if not rows:
            return []
        payloads = []
        for r in rows:
            p = from_json(r["payload"]) or {}
            p["condition_id"] = p.get("condition_id") or r["condition_id"]
            payloads.append(p)
        candidates = pick_candidates(payloads, self._wallet_scores(), self.cfg)
        with self.conn:
            self.conn.execute(
                """UPDATE signals SET processed = 1 WHERE source='smart_money'
                   AND kind='new_trade' AND processed = 0""")
        executed = []
        for cand in candidates:
            desc = await self._try_copy(cand)
            if desc:
                executed.append(desc)
        return executed

    async def _try_copy(self, cand: CopyCandidate) -> str | None:
        market = self.conn.execute(
            "SELECT * FROM markets WHERE condition_id = ?",
            (cand.condition_id,)).fetchone()
        if not market or not market["active"]:
            return None
        leader = cand.leader
        cur_price = self.broker.mark_price(
            cand.condition_id, cand.outcome_index, leader["price"])
        if not slippage_ok(leader["price"], cur_price, self.max_slippage):
            log.info("no copio '%s': precio ya movió %.3f→%.3f (>%.0f%%)",
                     cand.title[:40], leader["price"], cur_price,
                     self.max_slippage * 100)
            return None

        import json as _json
        tokens = _json.loads(market["clob_token_ids"] or "[]")
        if cand.outcome_index >= len(tokens):
            return None
        equity = self.broker.equity()
        confidence = min(leader["score"], 1.0)
        usdc_target = equity * self.base_pct * confidence
        size = usdc_target / max(cur_price, 0.01)
        names = ", ".join(w["wallet"][:8] for w in cand.wallets)
        reason = (f"copy: {len(cand.wallets)} wallet(s) top [{names}] "
                  f"compraron {cand.outcome} @ {leader['price']:.3f} "
                  f"(score líder {leader['score']:.2f})")
        fill = await self.broker.execute(
            f"copy:{leader['wallet']}:{cand.condition_id}:{cand.outcome_index}",
            OrderRequest(
                strategy=self.name, condition_id=cand.condition_id,
                category=market["category"], token_id=tokens[cand.outcome_index],
                outcome=cand.outcome, outcome_index=cand.outcome_index,
                side="BUY", size=size,
                price=min(cur_price * (1 + self.max_slippage), 0.99),
                reason=reason, strategy_budget_pct=self.budget_pct,
                copied_wallet=leader["wallet"],
                meta={"question": market["question"],
                      "copied_wallet": leader["wallet"],
                      "copied_entry_price": leader["price"]}))
        if fill.status == "FILLED":
            return f"{cand.title[:60]} — {reason}"
        return None

    async def check_exits(self) -> list[str]:
        """Vende posiciones cuya wallet copiada salió o redujo >50%."""
        if not self.enabled:
            return []
        exits = []
        today = datetime.now(timezone.utc).date().isoformat()
        for pos in self.conn.execute(
                "SELECT * FROM paper_positions WHERE strategy = ?",
                (self.name,)).fetchall():
            meta = from_json(pos["meta"]) or {}
            wallet = meta.get("copied_wallet")
            if not wallet:
                continue
            held = self.conn.execute(
                """SELECT size FROM wallet_positions
                   WHERE wallet = ? AND condition_id = ?""",
                (wallet, pos["condition_id"])).fetchone()
            original = meta.get("copied_size")
            if held and (original is None or held["size"] > 0.5 * original):
                if original is None:
                    # primera vez que la vemos: registrar tamaño de referencia
                    meta["copied_size"] = held["size"]
                    with self.conn:
                        self.conn.execute(
                            """UPDATE paper_positions SET meta = ?
                               WHERE strategy=? AND condition_id=? AND outcome=?""",
                            (to_json(meta), self.name, pos["condition_id"],
                             pos["outcome"]))
                continue
            reason = (f"salida: wallet copiada {wallet[:8]} "
                      + ("redujo su posición >50%" if held else "cerró su posición"))
            fill = await self.broker.execute(
                f"copy-exit:{wallet}:{pos['condition_id']}:{today}",
                OrderRequest(
                    strategy=self.name, condition_id=pos["condition_id"],
                    category=pos["category"] or "other",
                    token_id=pos["token_id"], outcome=pos["outcome"],
                    outcome_index=pos["outcome_index"] or 0, side="SELL",
                    size=pos["size"], price=0.0, reason=reason,
                    copied_wallet=wallet))
            if fill.status == "FILLED":
                exits.append(f"{(pos['question'] or '')[:60]} — {reason} "
                             f"(PnL {fill.realized_pnl:+.2f})")
        return exits

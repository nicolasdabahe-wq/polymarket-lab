"""Copy trading sobre las señales de smart_money/.

Entrada (sobre señales new_trade no procesadas):
- Solo BUYs de wallets con score >= min_wallet_score y tamaño >= min_copy_usdc.
- Dispara si: una wallet de score muy alto entra fuerte (strong_score /
  strong_usdc), o >= confirm_count wallets calificadas coinciden en el mismo
  mercado y outcome.
- No copiar si el precio ya se movió más de max_slippage_pct desde su entrada.
- Tamaño: equity * base_pct_per_trade * confianza (score de la wallet líder).

Consenso de posiciones (rutina diaria):
- Si >=N wallets top SOSTIENEN la misma posición grande en un mercado lento
  (política/economía/geo/cripto), entrar aunque el trade original no se haya
  visto en vivo. Mismo control de slippage contra su precio promedio.

Lista negra:
- Wallets cuyo backtest de copia dio negativo se excluyen siempre
  (config: copy_trading.wallet_blacklist), pase lo que pase con su score.

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
from .sizing import dias_hasta, kelly_usdc, retorno_esperado

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


# Categorías donde "en vivo" existe y hace incopiable la señal.
SPORT_CATEGORIES = {"sports", "esports", "games"}


def market_not_started(market_row: sqlite3.Row,
                       buffer_minutes: float = 20.0) -> bool:
    """True si el evento todavía no empezó (con margen). Los mercados sin
    hora de inicio se consideran no-eventos y pasan."""
    import json as _json
    from datetime import datetime, timedelta, timezone as _tz
    try:
        raw = _json.loads(market_row["raw"] or "{}")
    except (ValueError, TypeError):
        return True
    start = raw.get("gameStartTime") or raw.get("eventStartTime")
    if not start:
        return True
    text = str(start).strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text = text[:-3] + "+00:00"
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=_tz.utc)
    return datetime.now(_tz.utc) + timedelta(minutes=buffer_minutes) < when


def _outcome_index(market_row: sqlite3.Row, outcome: str) -> int | None:
    """Índice del outcome según la lista de outcomes del mercado (raw Gamma)."""
    import json as _json
    try:
        raw = _json.loads(market_row["raw"] or "{}")
        outcomes = _json.loads(raw.get("outcomes") or "[]")
    except (ValueError, TypeError):
        return None
    for i, name in enumerate(outcomes):
        if str(name).strip().lower() == outcome.strip().lower():
            return i
    return None


def slippage_ok(entry_price: float, current_price: float,
                max_slippage_pct: float) -> bool:
    """True si el precio no subió más de max_slippage_pct desde la entrada
    de la wallet copiada. Pura."""
    if entry_price <= 0 or current_price <= 0:
        return False
    return (current_price - entry_price) / entry_price <= max_slippage_pct


def pick_holdings_consensus(holdings: list[dict[str, Any]],
                            cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Mercados donde >=min_wallets wallets sostienen la misma posición con
    valor >= min_value_usdc cada una. Pura.

    holdings: [{wallet, condition_id, outcome, value, avg_price}]
    Devuelve [{condition_id, outcome, wallets, avg_entry, total_value}].
    """
    min_wallets = int(cfg.get("min_wallets", 2))
    min_value = float(cfg.get("min_value_usdc", 5000))
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for h in holdings:
        if float(h.get("value", 0)) < min_value:
            continue
        groups.setdefault((h["condition_id"], h["outcome"]), []).append(h)
    out = []
    for (cid, outcome), hs in groups.items():
        wallets = sorted({h["wallet"] for h in hs})
        if len(wallets) < min_wallets:
            continue
        total = sum(h["value"] for h in hs)
        avg_entry = (sum(h["avg_price"] * h["value"] for h in hs) / total
                     if total > 0 else 0.0)
        out.append({"condition_id": cid, "outcome": outcome,
                    "wallets": wallets, "avg_entry": avg_entry,
                    "total_value": total})
    out.sort(key=lambda c: -c["total_value"])
    return out


def pick_candidates(signals: list[dict[str, Any]], scores: dict[str, float],
                    cfg: dict[str, Any],
                    min_usdc_by_wallet: dict[str, float] | None = None
                    ) -> list[CopyCandidate]:
    """Agrupa señales calificadas y aplica las reglas de disparo. Pura.

    signals: payloads de señales new_trade (wallet, side, outcome,
    outcome_index, price, usdc, title, condition_id).
    """
    min_score = float(cfg.get("min_wallet_score", 0.55))
    min_usdc = float(cfg.get("min_copy_usdc_of_wallet", 500))
    strong_score = float(cfg.get("strong_score", 0.70))
    strong_usdc = float(cfg.get("strong_usdc", 2000))
    solo_big_usdc = float(cfg.get("solo_big_usdc", 999999))
    confirm_count = int(cfg.get("confirm_count", 2))

    grouped: dict[tuple[str, int], CopyCandidate] = {}
    for s in signals:
        score = scores.get(s.get("wallet", ""), 0.0)
        if s.get("side") != "BUY" or score < min_score:
            continue
        usdc = float(s.get("usdc", 0))
        price = float(s.get("price", 0))
        key = (s["condition_id"], int(s.get("outcome_index", 0)))
        cand = grouped.setdefault(key, CopyCandidate(
            condition_id=s["condition_id"],
            outcome_index=int(s.get("outcome_index", 0)),
            outcome=s.get("outcome", ""), title=s.get("title", ""),
            wallets=[]))
        existing = next((w for w in cand.wallets
                         if w["wallet"] == s["wallet"]), None)
        if existing:
            # Las órdenes grandes llegan partidas en varios fills: se SUMAN
            # (precio promedio ponderado) para no subestimar la entrada real.
            total = existing["usdc"] + usdc
            if total > 0:
                existing["price"] = ((existing["price"] * existing["usdc"]
                                      + price * usdc) / total)
            existing["usdc"] = total
        else:
            cand.wallets.append({"wallet": s["wallet"], "score": score,
                                 "price": price, "usdc": usdc})

    by_wallet = min_usdc_by_wallet or {}
    out = []
    for cand in grouped.values():
        # El tamaño mínimo se evalúa sobre el TOTAL agregado por wallet, con
        # el umbral que el backtest encontró óptimo para esa wallet.
        cand.wallets = [w for w in cand.wallets
                        if w["usdc"] >= by_wallet.get(w["wallet"], min_usdc)]
        if not cand.wallets:
            continue
        leader = cand.leader
        strong_single = (leader["score"] >= strong_score
                         and leader["usdc"] >= strong_usdc)
        # Entrada muy grande de cualquier wallet calificada: también dispara.
        big_single = any(w["usdc"] >= solo_big_usdc for w in cand.wallets)
        consensus = len(cand.wallets) >= confirm_count
        if strong_single or big_single or consensus:
            out.append(cand)
    return out


class CopyTradingStrategy:
    name = "copy_trading"

    def __init__(self, conn: sqlite3.Connection, broker: PaperBroker,
                 cfg: dict[str, Any], gamma: Any = None,
                 market_store: Any = None) -> None:
        self.conn = conn
        self.broker = broker
        self.gamma = gamma
        self.market_store = market_store
        self.cfg = cfg
        self.enabled = bool(cfg.get("enabled", True))
        self.budget_pct = float(cfg.get("budget_pct", 0.50))
        self.base_pct = float(cfg.get("base_pct_per_trade", 0.03))
        self.max_slippage = float(cfg.get("max_slippage_pct", 0.10))
        # Piso por apuesta: por debajo de esto no vale la pena el riesgo
        # operativo (fees de red, spread, mínimos del exchange).
        self.min_trade_usdc = float(cfg.get("min_trade_usdc", 10.0))
        self.max_entry = float(cfg.get("max_entry_price", 0.80))
        # Dimensionamiento por ventaja (ver sizing.py).
        self.kelly_fraction = float(cfg.get("kelly_fraction", 0.25))
        self.max_trade_pct = float(cfg.get("max_trade_pct", 0.12))
        self.default_roi = float(cfg.get("default_roi_untested", 0.05))
        self.consensus_boost = float(cfg.get("consensus_boost", 0.4))
        self.prematch_only_sports = bool(
            cfg.get("sports_only_prematch", True))
        # Escudo sharp: no copiar pagando más que el precio justo de las
        # casas profesionales + esta tolerancia. La primera noche real se
        # compró NO de Brentford a 0.58 cuando la línea decía 0.535.
        self.sharp_tolerance = float(cfg.get("sharp_tolerance", 0.04))
        self.sharp_max_age_h = float(cfg.get("sharp_max_age_hours", 8))
        # Freno por juicio EN VIVO: una wallet que ya nos costó esto en
        # dinero real deja de copiarse hasta que la validación diaria la
        # rehabilite con datos nuevos. El backtest simula copiar los
        # precios de la wallet, pero un creador de mercado llena DENTRO
        # del spread y nosotros compramos al ask: su ROI simulado no es el
        # nuestro (2026-08-22: 0xf03044eb +24% simulado, -$23.61 real).
        self.live_stop_usdc = float(cfg.get("live_stop_usdc", 15.0))
        # Esports SOLO antes del arranque: los "Game N Winner" se
        # resuelven en media hora y son de scalpers (4 de 5 en vivo
        # perdieron el 2026-08-22).
        self.esports_prematch_only = bool(
            cfg.get("esports_prematch_only", True))

    @property
    def blacklist(self) -> set[str]:
        return {w.lower() for w in self.cfg.get("wallet_blacklist") or []}

    def _wallet_scores(self) -> dict[str, float]:
        """Wallets copiables: pasan filtros, no están en lista negra y el
        backtest no las rechazó. Las no testeadas se permiten solo si su
        score supera trust_without_backtest (para no frenar rachas nuevas)."""
        rejected = {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_backtest WHERE verdict = 'rechazada'")}
        validated = {r["wallet"] for r in self.conn.execute(
            "SELECT wallet FROM wallet_backtest WHERE verdict = 'copiable'")}
        trust_score = float(self.cfg.get("trust_without_backtest", 0.60))
        frenadas = self._wallets_frenadas_en_vivo()
        out: dict[str, float] = {}
        # Sin filtrar por passed_filters: los filtros duros del ranking
        # (antigüedad, nº de trades) descartaban wallets sin mirarles un
        # número. Acá el veredicto del backtest ya mandó; el score solo
        # habilita a las que todavía no tienen veredicto.
        for r in self.conn.execute(
                "SELECT wallet, score FROM wallet_ranking"):
            w, score = r["wallet"], r["score"]
            if w in self.blacklist or w in rejected or w in frenadas:
                continue
            if w in validated or score >= trust_score:
                out[w] = score
        # Wallets descubiertas fuera del leaderboard: el backtest es su aval,
        # así que entran con un score sintético según su ROI simulado.
        for r in self.conn.execute(
                "SELECT wallet, roi FROM wallet_backtest WHERE verdict = 'copiable'"):
            w = r["wallet"]
            if w in self.blacklist or w in out or w in frenadas:
                continue
            out[w] = min(0.50 + max(r["roi"] or 0.0, 0.0), 0.95)
        return out

    def _wallet_rois(self) -> dict[str, float]:
        """ROI por operación que dio copiar a cada wallet en el backtest.
        Es la evidencia con la que se dimensiona: sin backtest se usa un
        valor conservador de config."""
        return {r["wallet"]: (r["roi"] or 0.0) for r in self.conn.execute(
            "SELECT wallet, roi FROM wallet_backtest WHERE verdict = 'copiable'")}

    def _size_usdc(self, wallets: list[str], entry_price: float,
                   cur_price: float, price_pagado: float) -> float:
        """USDC a apostar según la ventaja estimada (Kelly fraccionado)."""
        rois = self._wallet_rois()
        mejor_roi = max((rois.get(w, self.default_roi) for w in wallets),
                        default=self.default_roi)
        # Cuánto del slippage tolerado ya se comió el precio.
        movido = ((cur_price - entry_price) / entry_price
                  if entry_price > 0 else 0.0)
        usado = movido / self.max_slippage if self.max_slippage > 0 else 0.0
        exp_r = retorno_esperado(mejor_roi, len(wallets), usado,
                                 self.consensus_boost)
        return kelly_usdc(self.broker.equity(), price_pagado, exp_r,
                          self.kelly_fraction, self.min_trade_usdc,
                          self.max_trade_pct)

    def _precio_justo_sharp(self, condition_id: str,
                            outcome_index: int) -> float | None:
        """Probabilidad sharp del outcome, si hay línea fresca. None si no."""
        row = self.conn.execute(
            "SELECT prob_first, updated_at FROM sharp_lines "
            "WHERE condition_id = ?", (condition_id,)).fetchone()
        if not row:
            return None
        from datetime import datetime, timedelta, timezone
        try:
            edad = datetime.now(timezone.utc) - datetime.fromisoformat(
                row["updated_at"])
        except ValueError:
            return None
        if edad > timedelta(hours=self.sharp_max_age_h):
            return None
        p = float(row["prob_first"])
        return p if outcome_index == 0 else 1.0 - p

    def _wallets_frenadas_en_vivo(self) -> set[str]:
        """Wallets cuyo PnL realizado con NUESTRO dinero cruzó el freno."""
        if self.live_stop_usdc <= 0:
            return set()
        duenos: dict[str, str] = {}
        acumulado: dict[str, float] = {}
        for r in self.conn.execute(
                """SELECT id, condition_id FROM orders
                   WHERE side = 'BUY' AND id LIKE 'copy:%'"""):
            partes = r["id"].split(":")
            if len(partes) >= 2:
                duenos[r["condition_id"]] = partes[1].lower()
        for r in self.conn.execute(
                """SELECT id, condition_id, realized_pnl FROM orders
                   WHERE status = 'FILLED' AND realized_pnl IS NOT NULL
                   AND strategy = 'copy_trading'"""):
            partes = (r["id"] or "").split(":")
            w = (partes[1].lower() if len(partes) >= 2
                 and partes[0] in ("copy", "copy-exit")
                 else duenos.get(r["condition_id"]))
            if w:
                acumulado[w] = acumulado.get(w, 0.0) + r["realized_pnl"]
        return {w for w, pnl in acumulado.items()
                if pnl <= -self.live_stop_usdc}

    def _min_usdc_by_wallet(self) -> dict[str, float]:
        """Umbral de tamaño óptimo por wallet según su backtest."""
        return {r["wallet"]: r["min_usdc"] for r in self.conn.execute(
            """SELECT wallet, min_usdc FROM wallet_backtest
               WHERE verdict = 'copiable' AND min_usdc IS NOT NULL""")}

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
        candidates = pick_candidates(payloads, self._wallet_scores(), self.cfg,
                                     self._min_usdc_by_wallet())
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
        # Techo de entrada: a 0.95 se arriesga todo para ganar 5 centavos por
        # dólar. Hace falta acertar ~19 de cada 20 solo para empatar.
        if cur_price > self.max_entry:
            log.info("no copio '%s': entrada %.3f por encima del techo %.2f",
                     cand.title[:40], cur_price, self.max_entry)
            return None
        # Deportes en vivo: la wallet entra con el partido corriendo y para
        # cuando copiamos el precio ya se movió. El backtest lo dijo y el
        # dinero real lo confirmó.
        if (self.prematch_only_sports
                and (market["category"] or "") in SPORT_CATEGORIES
                and not market_not_started(market)):
            log.info("no copio '%s': deporte ya empezado", cand.title[:40])
            return None
        if (self.esports_prematch_only
                and (market["category"] or "") == "esports"
                and not market_not_started(market)):
            log.info("no copio '%s': esports en vivo (scalpers)",
                     cand.title[:40])
            return None
        justo = self._precio_justo_sharp(cand.condition_id,
                                         cand.outcome_index)
        if justo is not None and cur_price > justo + self.sharp_tolerance:
            log.info("no copio '%s': pagaríamos %.3f y la línea sharp dice "
                     "%.3f", cand.title[:40], cur_price, justo)
            return None
        if not slippage_ok(leader["price"], cur_price, self.max_slippage):
            log.info("no copio '%s': precio ya movió %.3f→%.3f (>%.0f%%)",
                     cand.title[:40], leader["price"], cur_price,
                     self.max_slippage * 100)
            return None

        import json as _json
        tokens = _json.loads(market["clob_token_ids"] or "[]")
        if cand.outcome_index >= len(tokens):
            return None
        usdc_target = self._size_usdc(
            [w["wallet"] for w in cand.wallets], leader["price"], cur_price,
            cur_price)
        if usdc_target <= 0:
            log.info("no copio '%s': sin ventaja estimada", cand.title[:40])
            return None
        size = usdc_target / max(cur_price, 0.01)
        names = ", ".join(w["wallet"][:8] for w in cand.wallets)
        reason = (f"copy: {len(cand.wallets)} wallet(s) top [{names}] "
                  f"compraron {cand.outcome} @ {leader['price']:.3f} "
                  f"(score líder {leader['score']:.2f}; "
                  f"apuesta ${usdc_target:.2f} por la ventaja estimada)")
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
                days_to_resolution=dias_hasta(market["end_date"]),
                meta={"question": market["question"],
                      "copied_wallet": leader["wallet"],
                      "copied_entry_price": leader["price"]}))
        if fill.status == "FILLED":
            return f"{cand.title[:60]} — {reason}"
        return None

    async def check_holdings_consensus(self) -> list[str]:
        """Entradas por consenso de posiciones sostenidas (rutina diaria)."""
        hc_cfg = self.cfg.get("holdings_consensus") or {}
        if not (self.enabled and hc_cfg.get("enabled")):
            return []
        allowed_cats = set(hc_cfg.get("categories") or [])
        max_entry = float(hc_cfg.get("max_entry_price", 0.90))
        scores = self._wallet_scores()

        holdings = [
            {"wallet": r["wallet"], "condition_id": r["condition_id"],
             "outcome": r["outcome"] or "", "value": r["value_usdc"] or 0.0,
             "avg_price": r["avg_price"] or 0.0}
            for r in self.conn.execute("SELECT * FROM wallet_positions")
            if r["wallet"] in scores  # solo wallets rankeadas y no blacklisteadas
        ]
        executed = []
        for cand in pick_holdings_consensus(holdings, hc_cfg):
            market = self.conn.execute(
                "SELECT * FROM markets WHERE condition_id = ? AND active = 1",
                (cand["condition_id"],)).fetchone()
            if not market and self.gamma is not None:
                # Mercado fuera del cache de volumen: traerlo bajo demanda.
                fetched = await self.gamma.fetch_market(cand["condition_id"])
                if fetched:
                    self.market_store.upsert_one(fetched)
                    market = self.conn.execute(
                        "SELECT * FROM markets WHERE condition_id = ?",
                        (cand["condition_id"],)).fetchone()
            if not market:
                continue
            # El filtro real no es la categoría sino si el evento ya empezó:
            # en vivo el precio se mueve con el juego y copiar tarde es perder.
            if allowed_cats and market["category"] not in allowed_cats:
                continue
            if (hc_cfg.get("sports_only_prematch", True)
                    and not market_not_started(market)):
                continue
            idx = _outcome_index(market, cand["outcome"])
            if idx is None:
                continue
            cur_price = self.broker.mark_price(
                cand["condition_id"], idx, cand["avg_entry"])
            if cur_price > max_entry or not slippage_ok(
                    cand["avg_entry"], cur_price, self.max_slippage):
                continue
            import json as _json
            tokens = _json.loads(market["clob_token_ids"] or "[]")
            if idx >= len(tokens):
                continue
            leader = max(cand["wallets"], key=lambda w: scores.get(w, 0))
            usdc_target = self._size_usdc(
                list(cand["wallets"]), cand["avg_entry"], cur_price, cur_price)
            if usdc_target <= 0:
                continue
            size = usdc_target / max(cur_price, 0.01)
            names = ", ".join(w[:8] for w in cand["wallets"])
            reason = (f"consenso de posiciones: {len(cand['wallets'])} wallets "
                      f"top [{names}] sostienen {cand['outcome']} "
                      f"(${cand['total_value']:,.0f}) desde ~{cand['avg_entry']:.3f}")
            fill = await self.broker.execute(
                f"consensus:{cand['condition_id']}:{idx}",
                OrderRequest(
                    strategy=self.name, condition_id=cand["condition_id"],
                    category=market["category"], token_id=tokens[idx],
                    outcome=cand["outcome"], outcome_index=idx, side="BUY",
                    size=size,
                    price=min(cur_price * (1 + self.max_slippage), 0.99),
                        reason=reason, strategy_budget_pct=self.budget_pct,
                    copied_wallet=leader,
                    days_to_resolution=dias_hasta(market["end_date"]),
                    meta={"question": market["question"],
                          "copied_wallet": leader,
                          "copied_entry_price": cand["avg_entry"],
                          "consensus_wallets": cand["wallets"]}))
            if fill.status == "FILLED":
                executed.append(f"{market['question'][:60]} — {reason}")
        return executed

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

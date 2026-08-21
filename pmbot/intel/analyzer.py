"""Mapeo noticia -> mercados de Polymarket afectados.

Dos etapas:
1. Candidatos por keywords (siempre, puro y testeable): solapamiento de
   tokens entre el titular y las preguntas de los mercados activos.
2. Análisis con Claude (si hay ANTHROPIC_API_KEY y llm.enabled): resumen,
   relevancia y dirección/impacto estimado sobre los candidatos. Sin clave,
   se degrada al resultado de keywords con impacto "unknown".

El análisis se guarda como JSON en news_items.analysis:
{
  "method": "llm" | "keywords",
  "relevant": bool,
  "summary": str,
  "markets": [{"condition_id", "question", "direction": "up|down|unclear",
               "impact": "low|medium|high|unknown", "rationale"}]
}
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("pmbot.intel.analyzer")

# Stopwords mínimas (EN + ES) para el matching por keywords.
STOPWORDS = frozenset("""
a an and are as at be but by for from has have in is it its of on or that the
this to was were will with what when who how why not no yes after before over
under new says said say more most than then their there они
el la los las un una unos unas de del al y o que en es son fue por para con
su sus se lo le les como más pero este esta estos estas
""".split())

WORD_RE = re.compile(r"[a-záéíóúüñ0-9']+", re.IGNORECASE)


def tokenize(text: str) -> set[str]:
    return {t for t in WORD_RE.findall(text.lower())
            if len(t) > 2 and t not in STOPWORDS}


@dataclass
class MarketRef:
    condition_id: str
    question: str
    category: str
    yes_price: float | None


def keyword_candidates(title: str, summary: str, markets: list[MarketRef],
                       top_k: int = 8, min_overlap: int = 2) -> list[tuple[MarketRef, float]]:
    """Mercados candidatos por solapamiento de tokens. Puro y testeable.

    Score = |tokens comunes| ponderado: los tokens del titular valen doble.
    min_overlap=2 evita falsos positivos por una sola palabra común.
    """
    title_tokens = tokenize(title)
    summary_tokens = tokenize(summary) - title_tokens
    scored: list[tuple[MarketRef, float]] = []
    for market in markets:
        m_tokens = tokenize(market.question)
        overlap_title = len(m_tokens & title_tokens)
        overlap_summary = len(m_tokens & summary_tokens)
        if overlap_title + overlap_summary < min_overlap:
            continue
        score = 2.0 * overlap_title + 1.0 * overlap_summary
        scored.append((market, score))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def extract_json(text: str) -> dict[str, Any] | None:
    """Extrae el primer objeto JSON de una respuesta de texto del LLM."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


ANALYSIS_PROMPT = """Eres analista de mercados de predicción (Polymarket).

Noticia:
Titular: {title}
Resumen: {summary}
Categoría: {category}

Mercados de Polymarket posiblemente afectados (precio actual = probabilidad implícita del YES):
{markets_block}

Responde SOLO un objeto JSON:
{{
  "relevant": true/false,          // ¿la noticia mueve alguno de estos mercados?
  "summary": "resumen en 1-2 frases en español",
  "markets": [                      // solo mercados realmente afectados; puede ser []
    {{"condition_id": "...", "direction": "up"|"down"|"unclear",
      "impact": "low"|"medium"|"high",
      "rationale": "1 frase: por qué y en qué dirección mueve el YES"}}
  ]
}}
Sé conservador: si la noticia ya está claramente reflejada en el precio o la
relación es tenue, marca impact "low" o excluye el mercado."""


class NewsAnalyzer:
    def __init__(self, conn: sqlite3.Connection, cfg: dict[str, Any],
                 api_key: str | None) -> None:
        self.conn = conn
        self.cfg = cfg
        llm_cfg = cfg.get("llm") or {}
        self.llm_enabled = bool(llm_cfg.get("enabled", True)) and bool(api_key)
        self.model = llm_cfg.get("model", "claude-opus-5")
        self.effort = llm_cfg.get("effort", "low")
        self.max_news_per_run = int(llm_cfg.get("max_news_per_run", 20))
        self.candidate_markets = int(llm_cfg.get("candidate_markets", 8))
        self._client = None
        if self.llm_enabled:
            import anthropic  # import diferido: opcional sin clave
            self._client = anthropic.AsyncAnthropic(api_key=api_key)

    def _load_market_refs(self) -> list[MarketRef]:
        rows = self.conn.execute(
            """SELECT condition_id, question, category, yes_price
               FROM markets WHERE active = 1""").fetchall()
        return [MarketRef(r["condition_id"], r["question"], r["category"],
                          r["yes_price"]) for r in rows]

    async def analyze_pending(self, pending: list[sqlite3.Row]) -> int:
        """Analiza noticias sin procesar. Devuelve cuántas quedaron analizadas."""
        markets = self._load_market_refs()
        done = 0
        budget = self.max_news_per_run
        for row in pending:
            candidates = keyword_candidates(
                row["title"], row["summary"] or "", markets,
                top_k=self.candidate_markets)
            analysis: dict[str, Any]
            if not candidates:
                analysis = {"method": "keywords", "relevant": False,
                            "summary": "", "markets": []}
            elif self.llm_enabled and budget > 0:
                budget -= 1
                analysis = await self._analyze_llm(row, candidates)
            else:
                analysis = self._analyze_keywords(candidates)
            self._save(row["id"], analysis)
            done += 1
        return done

    @staticmethod
    def _analyze_keywords(candidates: list[tuple[MarketRef, float]]) -> dict[str, Any]:
        return {
            "method": "keywords", "relevant": True, "summary": "",
            "markets": [
                {"condition_id": m.condition_id, "question": m.question,
                 "direction": "unclear", "impact": "unknown",
                 "rationale": f"match por keywords (score {score:.0f})"}
                for m, score in candidates[:3]
            ],
        }

    async def _analyze_llm(self, row: sqlite3.Row,
                           candidates: list[tuple[MarketRef, float]]) -> dict[str, Any]:
        markets_block = "\n".join(
            f"- condition_id={m.condition_id} | precio YES={m.yes_price} | {m.question}"
            for m, _ in candidates)
        prompt = ANALYSIS_PROMPT.format(
            title=row["title"], summary=(row["summary"] or "")[:600],
            category=row["category"], markets_block=markets_block)
        try:
            assert self._client is not None
            response = await self._client.messages.create(
                model=self.model, max_tokens=1024,
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}])
            if response.stop_reason == "refusal":
                return self._analyze_keywords(candidates)
            text = "".join(b.text for b in response.content if b.type == "text")
        except Exception as exc:
            log.warning("LLM falló para '%s': %s — fallback keywords",
                        row["title"][:60], exc)
            return self._analyze_keywords(candidates)

        parsed = extract_json(text)
        if not parsed:
            return self._analyze_keywords(candidates)
        # Enriquecer con las preguntas (el LLM devuelve solo condition_id).
        by_id = {m.condition_id: m.question for m, _ in candidates}
        for market in parsed.get("markets", []):
            market.setdefault("question", by_id.get(market.get("condition_id"), ""))
        parsed["method"] = "llm"
        parsed.setdefault("relevant", False)
        parsed.setdefault("markets", [])
        return parsed

    def _save(self, news_id: str, analysis: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "UPDATE news_items SET analyzed = 1, analysis = ? WHERE id = ?",
                (json.dumps(analysis, ensure_ascii=False), news_id))
            # Noticias con impacto claro quedan como señal para research/ (fase 2).
            if analysis.get("relevant") and any(
                    m.get("impact") in ("medium", "high")
                    for m in analysis.get("markets", [])):
                self.conn.execute(
                    """INSERT INTO signals (source, kind, condition_id, payload,
                       created_at) VALUES (?,?,?,?,?)""",
                    ("intel", "news_impact",
                     analysis["markets"][0].get("condition_id"),
                     json.dumps({"news_id": news_id, **analysis},
                                ensure_ascii=False), now))

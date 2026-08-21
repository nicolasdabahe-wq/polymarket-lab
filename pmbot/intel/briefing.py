"""Briefing diario: agrega las noticias analizadas por categoría."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from ..db import from_json

IMPACT_ORDER = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
ARROW = {"up": "↑", "down": "↓", "unclear": "≈"}


class BriefingBuilder:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def build_daily(self, date: str | None = None) -> dict[str, str]:
        """Genera y persiste el briefing del día. Devuelve {categoría: texto}."""
        date = date or datetime.now(timezone.utc).date().isoformat()
        rows = self.conn.execute(
            """SELECT * FROM news_items
               WHERE analyzed = 1 AND date(fetched_at) = ?
               ORDER BY category, fetched_at DESC""", (date,)).fetchall()

        by_category: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_category.setdefault(row["category"], []).append(row)

        briefings: dict[str, str] = {}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn:
            for category, items in by_category.items():
                text = self._category_text(category, items)
                briefings[category] = text
                self.conn.execute(
                    """INSERT OR REPLACE INTO briefings
                       (briefing_date, category, content, created_at)
                       VALUES (?,?,?,?)""", (date, category, text, now))
        return briefings

    @staticmethod
    def _category_text(category: str, items: list[sqlite3.Row]) -> str:
        relevant: list[tuple[int, str]] = []
        others = 0
        for row in items:
            analysis = from_json(row["analysis"]) or {}
            markets = analysis.get("markets") or []
            if analysis.get("relevant") and markets:
                top = min(markets,
                          key=lambda m: IMPACT_ORDER.get(m.get("impact"), 9))
                impact = top.get("impact", "unknown")
                line = f"• {row['title']} [{row['feed']}]"
                summary = analysis.get("summary")
                if summary:
                    line += f"\n  {summary}"
                for m in markets[:2]:
                    arrow = ARROW.get(m.get("direction"), "?")
                    line += (f"\n  → {arrow} {m.get('question', m.get('condition_id'))}"
                             f" (impacto {m.get('impact', '?')})")
                    if m.get("rationale"):
                        line += f" — {m['rationale']}"
                relevant.append((IMPACT_ORDER.get(impact, 9), line))
            else:
                others += 1
        relevant.sort(key=lambda x: x[0])
        lines = [f"=== {category.upper()} ==="]
        if relevant:
            lines.extend(line for _, line in relevant)
        else:
            lines.append("(sin noticias con impacto en mercados)")
        if others:
            lines.append(f"({others} noticias más sin impacto directo)")
        return "\n".join(lines)

    def latest(self, date: str | None = None) -> dict[str, str]:
        date = date or datetime.now(timezone.utc).date().isoformat()
        rows = self.conn.execute(
            "SELECT category, content FROM briefings WHERE briefing_date = ?",
            (date,)).fetchall()
        return {r["category"]: r["content"] for r in rows}

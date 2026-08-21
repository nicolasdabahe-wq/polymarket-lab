"""Fetch de noticias desde feeds RSS configurables (config.yaml -> intel.feeds)."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import feedparser

from ..http import HttpClient

log = logging.getLogger("pmbot.intel.sources")


def _news_id(link: str, title: str) -> str:
    return hashlib.sha256(f"{link}|{title}".encode()).hexdigest()[:24]


def _parse_published(entry: Any) -> datetime | None:
    for key in ("published", "updated"):
        raw = entry.get(key)
        if not raw:
            continue
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


class NewsFetcher:
    def __init__(self, http: HttpClient, conn: sqlite3.Connection,
                 cfg: dict[str, Any]) -> None:
        self.http = http
        self.conn = conn
        self.cfg = cfg

    async def fetch_all(self) -> int:
        """Baja todos los feeds y guarda los ítems nuevos. Devuelve # nuevos."""
        feeds = self.cfg.get("feeds") or []
        results = await asyncio.gather(
            *(self._fetch_feed(f) for f in feeds), return_exceptions=True)
        new_total = 0
        for feed, result in zip(feeds, results):
            if isinstance(result, Exception):
                log.warning("feed %s falló: %s", feed.get("name"), result)
            else:
                new_total += result
        log.info("intel: %d noticias nuevas", new_total)
        return new_total

    async def _fetch_feed(self, feed: dict[str, Any]) -> int:
        text = await self.http.get_text(feed["url"])
        parsed = feedparser.parse(text)
        max_items = int(self.cfg.get("max_items_per_feed", 20))
        max_age = timedelta(hours=float(self.cfg.get("max_age_hours", 36)))
        now = datetime.now(timezone.utc)

        new_count = 0
        with self.conn:
            for entry in parsed.entries[:max_items]:
                title = (entry.get("title") or "").strip()
                link = entry.get("link") or ""
                if not title:
                    continue
                published = _parse_published(entry)
                if published and now - published > max_age:
                    continue
                summary = (entry.get("summary") or "")[:1000]
                cur = self.conn.execute(
                    """INSERT OR IGNORE INTO news_items
                       (id, feed, category, title, link, published_at,
                        summary, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (_news_id(link, title), feed.get("name", "?"),
                     feed.get("category", "other"), title, link,
                     published.isoformat(timespec="seconds") if published else None,
                     summary, now.isoformat(timespec="seconds")))
                new_count += cur.rowcount
        return new_count

    def pending_analysis(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM news_items WHERE analyzed = 0
               ORDER BY fetched_at DESC LIMIT ?""", (limit,)).fetchall()

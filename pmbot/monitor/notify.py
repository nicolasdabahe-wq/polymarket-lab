"""Notificaciones por Telegram. Si no está configurado, degrada a log."""
from __future__ import annotations

import logging

from ..http import HttpClient

log = logging.getLogger("pmbot.notify")

TELEGRAM_MAX_LEN = 4000  # límite real 4096; margen para no partir entidades


class Notifier:
    def __init__(self, bot_token: str | None, chat_id: str | None, enabled: bool) -> None:
        self.enabled = bool(enabled and bot_token and chat_id)
        self._token = bot_token
        self._chat_id = chat_id

    async def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram deshabilitado] %s", text[:300])
            return
        async with HttpClient(timeout=15) as client:
            for chunk in _chunks(text, TELEGRAM_MAX_LEN):
                await client._request(  # POST simple; reutiliza los reintentos
                    "POST",
                    f"https://api.telegram.org/bot{self._token}/sendMessage",
                    json={"chat_id": self._chat_id, "text": chunk},
                )


def _chunks(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]

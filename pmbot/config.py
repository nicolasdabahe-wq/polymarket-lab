"""Carga de configuración: .env (secretos) + config.yaml (parámetros).

Regla de seguridad central: el bot solo opera con dinero real si la variable
de entorno LIVE_TRADING vale exactamente "I_UNDERSTAND_THE_RISKS". Cualquier
otro valor deja el bot en paper trading. Esta regla tiene test unitario y no
debe relajarse.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

LIVE_TRADING_MAGIC = "I_UNDERSTAND_THE_RISKS"


@dataclass(frozen=True)
class Config:
    raw: dict[str, Any]
    var_dir: Path
    live_trading: bool
    anthropic_api_key: str | None
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    polymarket_private_key: str | None
    polymarket_proxy_address: str | None
    polymarket_signature_type: int

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name) or {}
        if not isinstance(value, dict):
            raise TypeError(f"config.yaml: la sección '{name}' debe ser un mapa")
        return value

    @property
    def mode(self) -> str:
        return "LIVE" if self.live_trading else "PAPER"

    @property
    def db_path(self) -> Path:
        return self.var_dir / "pmbot.db"


def is_live_trading(env: dict[str, str] | os._Environ = os.environ) -> bool:
    """True solo con el valor mágico exacto. Fail-safe: default paper."""
    return env.get("LIVE_TRADING", "") == LIVE_TRADING_MAGIC


def load_config(config_path: str | Path | None = None) -> Config:
    load_dotenv()
    path = Path(config_path or os.environ.get("PMBOT_CONFIG") or "config.yaml")
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    var_dir = Path(os.environ.get("PMBOT_VAR_DIR") or "var")
    var_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        raw=raw,
        var_dir=var_dir,
        live_trading=is_live_trading(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY") or None,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        polymarket_private_key=os.environ.get("POLYMARKET_PRIVATE_KEY") or None,
        polymarket_proxy_address=os.environ.get("POLYMARKET_PROXY_ADDRESS") or None,
        # 1 = cuenta con login por email/Magic (lo usual); 2 = browser wallet
        # con proxy de Polymarket; 0 = EOA directa.
        polymarket_signature_type=int(
            os.environ.get("POLYMARKET_SIGNATURE_TYPE") or 1),
    )

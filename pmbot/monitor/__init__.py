"""monitor/: logging estructurado y notificaciones. Métricas/dashboard: fase 3."""
from .logs import setup_logging
from .notify import Notifier

__all__ = ["setup_logging", "Notifier"]

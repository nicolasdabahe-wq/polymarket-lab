"""scheduler/: rutina diaria y loop 24/7 (fase 1: solo lectura + paper)."""
from .daily import DailyRoutine, run_forever

__all__ = ["DailyRoutine", "run_forever"]

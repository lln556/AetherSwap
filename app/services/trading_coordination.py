"""Serialize legacy market mutations with reconciliation and repricing."""
from functools import wraps


def market_mutation(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        from app.sell_pipeline import _sell_phase_lock
        if not _sell_phase_lock.acquire(blocking=False):
            return {"ok": False, "error": "市场任务正在执行，请稍后重试"}
        try:
            return fn(*args, **kwargs)
        finally:
            _sell_phase_lock.release()
    return wrapped

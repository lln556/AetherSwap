"""Identify the deployed rule code so old evidence never uses changed rules.

A process runs one deployed ruleset. A new deployment with different source
keeps saved explanations readable, but requires fresh checks for comparisons.
User module definitions are separately frozen inside each decision record.
"""
from functools import lru_cache
import hashlib
from pathlib import Path


@lru_cache(maxsize=3)
def rules_version(kind: str) -> str:
    root = Path(__file__).resolve().parent.parent
    common = ('app/strategy_engine.py', 'utils/money.py', 'utils/time.py', 'utils/trend.py', 'steam/market_orders.py')
    specific = {
        'buy': ('app/decision_buy.py', 'app/decision_capture_buy.py', 'app/pipeline_steps.py', 'analysis/stability.py', 'app/services/analysis_client.py'),
        'sell': ('app/decision_sell.py', 'app/sell_pipeline.py', 'analysis/stability.py'),
        'reprice': ('app/services/repricing.py', 'app/decision_sell.py', 'app/sell_pipeline.py'),
    }
    digest = hashlib.sha256()
    for relative in common + specific[kind]:
        digest.update(relative.encode())
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()

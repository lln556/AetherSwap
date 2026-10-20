"""Observe saved buy strategies at scan and purchase boundaries."""
import copy
import json
import threading
import time

from app import strategy_engine as strategies
from app.config_loader import load_app_config_validated, _config_update_lock

POLICY_CHANGED = object()


def _settings(config):
    runtime = (config.get('_strategy_runtime') or {}).get('buy') or {}
    keys = {
        key for module in strategies.BUILTIN_MODULES.values()
        if 'buy' in module['strategy_types']
        for key in module.get('params_schema', {})
    }
    return {
        'pipeline': {k: v for k, v in config.get('pipeline', {}).items() if k in keys},
        'stability': {k: v for k, v in config.get('stability', {}).items() if k in keys},
        'runtime': runtime,
    }


def _signature(settings):
    return json.dumps(settings, sort_keys=True, ensure_ascii=False, allow_nan=False)


class BuyPolicyMonitor:
    def __init__(self, config):
        self._lock = threading.RLock()
        self._saved = self._read()
        self._applied = _settings(config)
        self.applied_at = time.time()

    def _read(self):
        # Strategy writes and configuration updates share this lock. A reader
        # cannot observe half of a save or activation.
        with _config_update_lock:
            return _settings(strategies.apply_strategy_to_config(load_app_config_validated(), 'buy'))

    def is_current(self):
        with self._lock:
            return _signature(self._read()) == _signature(self._saved)

    def refresh(self, config):
        with self._lock:
            saved = self._read()
            if _signature(saved) == _signature(self._saved):
                return config, False
            updated = copy.deepcopy(config)
            for section in ('pipeline', 'stability'):
                updated.setdefault(section, {}).update(saved[section])
            updated.setdefault('_strategy_runtime', {})['buy'] = copy.deepcopy(saved['runtime'])
            self._saved = saved
            self._applied = _settings(updated)
            self.applied_at = time.time()
            return updated, True

    def status(self):
        with self._lock:
            return {
                'strategy_id': self._applied['runtime'].get('active_strategy_id'),
                'max_discount': self._applied['pipeline'].get('max_discount'),
                'applied_at': self.applied_at,
                'pending': not self.is_current(),
            }

"""Collect only business evidence already read by the live buy workflow."""
import copy
import time

from app import decision_records
from app.decision_buy import evaluate


def safe_orders(rows, *, individual=False):
    result = []
    for row in rows or []:
        try:
            price = float(row.get('price') if isinstance(row, dict) else row[0])
            quantity = 1 if individual else int(row.get('quantity', 0) if isinstance(row, dict) else row[1])
        except (TypeError, ValueError, IndexError):
            continue
        if price > 0 and quantity > 0:
            result.append({'price': price, 'quantity': quantity})
    return result


class BuyEvidence:
    def __init__(self, config, item, purchases):
        from app.accounts import get_current_account
        from utils.money import _load_exchange_rates
        self.evaluated_at = time.time()
        self.config = copy.deepcopy(config)
        self.config['_decision_account_id'] = str((get_current_account() or {}).get('id') or '')
        self.strategy, self.modules = decision_records.snapshot_strategy(config, 'buy')
        self.item = item
        item.pop('_decision_buff_orders', None)
        purchases = purchases() if callable(purchases) else purchases
        pipe = config.get('pipeline') or {}
        rates = dict(_load_exchange_rates())
        rates.setdefault('USD', float(pipe.get('usd_to_cny', 7.2)))
        self.rates = rates
        market_name = item.get('steam_market_name') or item.get('market_hash_name') or item.get('name')
        safe_item = {key: item[key] for key in ('name', 'goods_id', 'assetid', 'min_price', 'daily_volume', 'ratio') if key in item}
        safe_item['market_hash_name'] = market_name
        held = sum(1 for p in purchases if (p.get('name') or '').strip() == market_name
                   and not (p.get('sale_price') and float(p.get('sale_price') or 0) > 0))
        self.inputs = {
            'item': safe_item,
            'candidate': {'rank': item.get('_candidate_rank'), 'source_total': item.get('_candidate_source_total'),
                          'name_cn': item.get('_candidate_name_cn'), 'has_buff_link': item.get('_candidate_has_buff_link')},
            'market': {
                'steam': {'available': False, 'reason': '尚未取得 Steam 卖单', 'source': 'steam_market', 'currency': 'CNY'},
                'history': {'available': False, 'reason': '尚未获取历史行情', 'source': 'steam_history', 'rows': []},
                'buff': {'available': False, 'reason': '尚未获取 BUFF 卖单', 'source': 'buff_market', 'currency': 'CNY'},
            },
            'account': {'target_balance': pipe.get('target_balance'),
                        'accumulated_spend': config.get('_decision_accumulated_spend'), 'held_same_name': held},
            'valuation': {'usd_to_cny': rates.get('USD'), 'rates': rates},
            'policy': {'huge_profit_offset': pipe.get('huge_profit_offset', .05),
                       'price_tolerance': (config.get('buff') or {}).get('price_tolerance', .5)},
        }

    def history(self, raw, *, adjustment=False):
        rows = raw.get('history') if isinstance(raw, dict) else raw
        currency = raw.get('currency') if isinstance(raw, dict) else None
        # Steam rows are timestamp/price/volume only; never persist raw responses.
        rows = [list(row[:3]) for row in rows or [] if isinstance(row, (list, tuple)) and len(row) >= 2]
        snapshot = self.inputs['market']['history']
        if adjustment:
            snapshot['adjustment_rows'] = rows
            snapshot['adjustment_currency'] = currency
        else:
            snapshot.update(rows=rows, available=bool(rows), currency=currency,
                            captured_at=time.time(), window_days=(self.config.get('stability') or {}).get('days', 30),
                            reason=None if rows else '历史行情获取失败或为空')
        if adjustment and 'currency' not in snapshot:
            snapshot['currency'] = currency

    def finish(self, steam_data=None):
        """Persist an evaluation without changing the live pipeline's control flow."""
        try:
            if steam_data:
                pipe = self.config.get('pipeline') or {}
                self.inputs['market']['steam'].update(
                    available=True, reason=None, captured_at=time.time(), smart_price=steam_data.get('smart_price'),
                    sell_orders=safe_orders(steam_data.get('sell_orders')),
                    pricing_params={'sell_price_wall_volume': pipe.get('sell_price_wall_volume', 20),
                                    'sell_price_max_ignore_volume': pipe.get('sell_price_max_ignore_volume', 4)},
                )
            orders = safe_orders(self.item.get('_decision_buff_orders'), individual=True)
            if orders:
                lowest = min(row['price'] for row in orders)
                self.inputs['market']['buff'].update(available=True, reason=None, orders=orders,
                    lowest_price=lowest, count_at_lowest=sum(row['price'] == lowest for row in orders), captured_at=time.time())
            result = evaluate(self.inputs, self.strategy, self.evaluated_at, modules=self.modules)
            return decision_records.capture_decision(self.config, 'buy', self.inputs['item'], self.inputs, result,
                                                     evaluated_at=self.evaluated_at)
        except Exception:
            decision_records.report_recording_failure()
            return None


def begin(config, item, purchases):
    try:
        return BuyEvidence(config, item, purchases)
    except Exception:
        decision_records.report_recording_failure()
        return None

"""Parameter-only comparisons over immutable recorded evidence."""
import copy
import uuid

from app import strategy_engine as se
from app.decision_records import DecisionError, SCHEMA_VERSION, get_record, list_versions


def _supported(record):
    if record['schema_version'] != SCHEMA_VERSION:
        raise DecisionError('此检查记录版本不支持预演，仍可查看原始解释')
    from app.decision_versions import rules_version
    if record.get('rules_version') != rules_version(record['kind']):
        raise DecisionError('规则实现版本已变化，此记录仅支持查看原始解释，请选择新的检查记录')
    if record['kind'] == 'reprice':
        raise DecisionError('自动调价仅提供决策解释，不支持参数预演或另存副本')


def parameter_strategy(record, params):
    _supported(record)
    strategy = copy.deepcopy(record['strategy'])
    if not isinstance(params, dict):
        raise DecisionError('临时参数必须是对象')
    for index, changes in params.items():
        try:
            position = int(index)
            if str(position) != str(index) or position < 0:
                raise ValueError
            step = strategy['steps'][position]
        except (ValueError, TypeError, IndexError):
            raise DecisionError('临时参数指定了不存在的步骤') from None
        if not isinstance(changes, dict):
            raise DecisionError('步骤参数必须是对象')
        module = record['modules'].get(step['module_id']) or {}
        schema = module.get('params_schema') or {}
        if step.get('enabled') is False or not set(changes).issubset(schema):
            raise DecisionError('只能修改已启用模块的既有参数，不能改变策略结构')
        candidate = {**step.get('params', {}), **changes}
        for key, value in changes.items():
            expected = schema[key].get('type')
            if value is None or (isinstance(value, str) and not value.strip() and expected in {'number', 'integer'}):
                raise DecisionError('参数值不能为空')
        errors = se._validate_step_params(step['module_id'], module, candidate)
        if errors:
            raise DecisionError('；'.join(errors))
        step['params'] = candidate
    return strategy


def compare(raw):
    record_id = raw.get('record_id')
    if not record_id:
        raise DecisionError('请选择检查记录，再进行策略预演')
    record = get_record(record_id)
    preview_strategy = parameter_strategy(record, raw.get('params') or {})
    version_id = raw.get('version_id') or 'original'
    inputs, evaluated_at = record['inputs'], record['evaluated_at']
    if version_id != 'original':
        version = next((v for v in list_versions(record_id) if v['id'] == version_id), None)
        if version is None:
            raise DecisionError('行情数据版本不存在或已清理，请重新选择')
        inputs, evaluated_at = version['inputs'], version['evaluated_at']
    if record['kind'] == 'buy':
        from app.decision_buy import evaluate
    else:
        from app.decision_sell import evaluate
    original = evaluate(copy.deepcopy(inputs), copy.deepcopy(record['strategy']), evaluated_at, modules=copy.deepcopy(record['modules']))
    preview = evaluate(copy.deepcopy(inputs), preview_strategy, evaluated_at, modules=copy.deepcopy(record['modules']))
    before, after = original.get('steps', []), preview.get('steps', [])
    changed = [i for i in range(max(len(before), len(after))) if (before[i] if i < len(before) else None) != (after[i] if i < len(after) else None)]
    return {'ok': True, 'record_id': record_id, 'version_id': version_id, 'evaluated_at': evaluated_at,
            'original': original, 'preview': preview, 'strategy': record['strategy'],
            'preview_strategy': preview_strategy, 'inputs': inputs, 'changed_steps': changed}


def save_copy(record_id, name, params):
    record = get_record(record_id)
    strategy = parameter_strategy(record, params)
    if not isinstance(name, str) or not name.strip():
        raise DecisionError('请输入策略副本名称')
    # Saving uses today's validator: never silently substitute a changed module
    # implementation for the module recorded in the decision.
    current_modules = se._module_by_id()
    for step in strategy['steps']:
        if step.get('enabled') is False:
            continue
        mid = step['module_id']
        if record['modules'].get(mid) != current_modules.get(mid):
            raise DecisionError('策略模块版本已变化，无法安全另存此记录的参数')
    strategy.update(id=f"custom.{strategy['strategy_type']}.{uuid.uuid4().hex}", name=name.strip(), origin='custom', readonly=False)
    strategy.pop('created_at', None)
    strategy.pop('updated_at', None)
    saved = se.save_strategy(strategy)
    return {'ok': True, 'strategy': saved, 'enabled': False}

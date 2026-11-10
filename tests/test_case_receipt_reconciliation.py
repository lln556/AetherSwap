"""Cases require an authenticated Steam old-to-new asset mapping."""
import copy
import pytest
from app.services import receipt_reconciliation as receipt
from tests.test_receipt_reconciliation import sample


def case():
    p, o, i = sample()
    p['name'] = o['_market_hash_name'] = i['market_hash_name'] = 'Fracture Case'
    o['asset_info'].update(assetid='100', appid=730, contextid='2', paintwear='', info={})
    i.update(assetid='200', contextid='2')
    i.pop('paintseed'); i.pop('paintwear')
    evidence = {'steam_id': 'buyer', 'trade_id': '300', 'time_init': 110,
                'old_assetid': '100', 'assetid': '200', 'contextid': '2',
                'classid': 'class', 'appid': 730, 'amount': 1}
    return p, o, i, evidence


def plan(p, o, inventory, mappings, **kwargs):
    return receipt.build_reconciliation_plan([p], [o], inventory, 'buyer',
                                             trade_evidence={'mappings': mappings}, **kwargs)


def test_case_uses_exact_trade_mapping_despite_personal_same_name_items():
    p, o, i, e = case()
    rows, closed = plan(p, o, [dict(i, assetid='personal'), i], [e])
    assert closed == []
    assert len(rows) == 1 and rows[0]['assetid'] == '200'
    assert rows[0]['receipt_evidence'] == e


@pytest.mark.parametrize('field,value', [
    ('steam_id', 'someone-else'), ('old_assetid', '101'), ('assetid', '201'),
    ('contextid', '16'), ('classid', 'other'), ('appid', 570), ('amount', 2),
    ('amount', True), ('time_init', 99), ('time_init', float('nan')),
    ('trade_id', ''), ('assetid', ''),
])
def test_bad_trade_evidence_does_not_claim_personal_inventory(field, value):
    p, o, i, e = case(); e[field] = value
    assert plan(p, o, [i], [e]) == ([], [])


@pytest.mark.parametrize('field,value', [('price', '9'), ('buyer_steamid', 'other'), ('state', 'REFUNDED'),
                                         ('goods_id', 99), ('game', 'dota2'), ('buyer_pay_time', 0)])
def test_trade_evidence_never_overrides_buff_order_identity(field, value):
    p, o, i, e = case(); o[field] = value
    assert plan(p, o, [i], [e])[0] == []


def test_ambiguous_evidence_or_inventory_fails_closed():
    p, o, i, e = case()
    for mappings, inventory in [([e, dict(e)], [i]),
                                ([e, dict(e, old_assetid='101')], [i]),
                                ([e, dict(e, assetid='201')], [i]),
                                ([e], [i, dict(i)])]:
        assert plan(p, o, inventory, mappings) == ([], [])


def test_claimed_assets_and_duplicate_order_claims_stay_unassigned():
    p, o, i, e = case()
    evidence = {'mappings': [e]}
    owner = {'_db_id': 2, 'assetid': '200'}
    assert receipt.build_reconciliation_plan([p, owner], [o], [i], 'buyer', trade_evidence=evidence) == ([], [])
    p2 = dict(p, _db_id=2, buff_order_id='second')
    o2 = dict(o, id='second')
    assert receipt.build_reconciliation_plan([p, p2], [o, o2], [i], 'buyer', trade_evidence=evidence) == ([], [])


def test_protected_case_waits_for_mapping_and_reports_specific_reason():
    p, o, i, e = case(); reasons = {}
    rows, _ = receipt.build_reconciliation_plan([p], [o], [i], 'buyer', diagnostics=reasons,
                 trade_evidence={'mappings': [], 'pending_protection': ['100'], 'error': ''})
    assert rows == []
    assert reasons == {'steam_trade_protected': 1}


def test_missing_mapping_does_not_fall_back_to_name_or_count():
    p, o, i, e = case(); reasons = {}
    assert plan(p, o, [i], [], diagnostics=reasons) == ([], [])
    assert reasons == {'steam_asset_mapping_unavailable': 1}


def test_receipt_mapping_audit_and_purchase_update_commit_together(monkeypatch):
    from app import database as db
    from app.trading_models import TradeEvent
    from sqlmodel import SQLModel, create_engine, select
    from sqlalchemy.pool import StaticPool
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    monkeypatch.setattr(db, '_engine', engine); SQLModel.metadata.create_all(engine)
    # The purchase is bound to the account that bought it, recorded at buy
    # time.  State it explicitly so the test needs no live Steam session.
    monkeypatch.setattr(db, '_purchase_account', lambda record: 'buyer')
    p, o, i, e = case(); db.db_append_purchase(p)
    p = db.db_get_purchases()[0]
    assert db.db_apply_receipt_reconciliation(p, assetid='200', receipt_evidence=e)
    assert not db.db_apply_receipt_reconciliation(p, assetid='200', receipt_evidence=e)
    with db.get_session() as session:
        events = session.exec(select(TradeEvent).where(TradeEvent.kind == 'receipt_verified')).all()
        assert len(events) == 1
        import json
        assert json.loads(events[0].details) == e
    assert db.db_get_purchases()[0]['pending_receipt'] is False


def integration(monkeypatch):
    p, o, i, e = case()
    monkeypatch.setattr(receipt, 'get_purchases', lambda: [p])
    monkeypatch.setattr(receipt, 'get_steam_credentials', lambda: {'steam_id': 'buyer', 'cookies': 'session'})
    monkeypatch.setattr(receipt, 'load_app_config_validated', lambda: {})
    from app.services import workers
    monkeypatch.setattr(workers, '_buff_background_request_is_safe', lambda: True)
    from unittest.mock import Mock
    client = Mock(); client.get_purchase_history_orders.return_value = [o]
    apply = Mock(return_value=True)
    monkeypatch.setattr(receipt, 'db_apply_receipt_reconciliation', apply)
    monkeypatch.setattr(receipt, 'log', lambda *_a, **_k: None)
    return p, o, i, e, client, apply


def test_worker_loads_case_evidence_and_applies_only_verified_mapping(monkeypatch):
    p, o, i, e, client, apply = integration(monkeypatch)
    monkeypatch.setattr(receipt, '_load_case_evidence', lambda *_: {'mappings': [e]})
    result = receipt.reconcile_receipts_once(client, scan_inventory=lambda: (True, [i], ''))
    assert result['received'] == 1
    apply.assert_called_once_with(p, assetid='200', receipt_evidence=e)
    client.get_purchase_history_orders.assert_called_once_with('csgo', ['bill'])


def test_steam_session_switch_during_scan_prevents_all_application(monkeypatch):
    p, o, i, e, client, apply = integration(monkeypatch)
    monkeypatch.setattr(receipt, '_load_case_evidence', lambda *_: {'mappings': [e]})
    def scan():
        monkeypatch.setattr(receipt, 'get_steam_credentials', lambda: {'steam_id': 'other', 'cookies': 'other'})
        return True, [i], ''
    result = receipt.reconcile_receipts_once(client, scan_inventory=scan)
    assert result['ok'] is False
    apply.assert_not_called()


def test_history_failure_keeps_case_pending_without_writes(monkeypatch):
    p, o, i, e, client, apply = integration(monkeypatch)
    monkeypatch.setattr(receipt, '_load_case_evidence', lambda *_: {'mappings': [], 'error': 'unavailable'})
    result = receipt.reconcile_receipts_once(client, scan_inventory=lambda: (True, [i], ''))
    assert result['received'] == 0
    assert result['unmatched_reasons'] == {'steam_trade_history_unavailable': 1}
    apply.assert_not_called()


def test_evidence_requests_are_cached_but_invalidated_by_session_or_targets(monkeypatch):
    from app.services import steam_receipt_evidence as reader
    from unittest.mock import Mock
    p, o, i, e = case()
    read = Mock(return_value={'mappings': [e]})
    monkeypatch.setattr(reader, 'read_receipt_evidence', read)
    monkeypatch.setattr(receipt, '_evidence_cache', None)
    clock = [100]
    monkeypatch.setattr(receipt.time, 'monotonic', lambda: clock[0])
    credentials = {'steam_id': 'buyer', 'cookies': 'session-a'}
    receipt._load_case_evidence(credentials, [o]); receipt._load_case_evidence(credentials, [o])
    assert read.call_count == 1
    clock[0] += 301
    receipt._load_case_evidence(credentials, [o]); assert read.call_count == 2
    receipt._load_case_evidence(dict(credentials, cookies='session-b'), [o]); assert read.call_count == 3
    o2 = copy.deepcopy(o); o2['asset_info']['assetid'] = '101'
    receipt._load_case_evidence(dict(credentials, cookies='session-b'), [o2]); assert read.call_count == 4


def test_database_rejects_plan_when_purchase_account_changed(monkeypatch):
    from app import database as db
    from sqlmodel import SQLModel, create_engine
    from sqlalchemy.pool import StaticPool
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    monkeypatch.setattr(db, '_engine', engine); SQLModel.metadata.create_all(engine)
    p, o, i, e = case(); p['account_id'] = 'buyer'
    db.db_append_purchase(p); p = db.db_get_purchases()[0]
    with db.get_session() as session:
        row = session.get(db.Purchase, p['_db_id']); row.account_id = 'buyer'
        session.add(row); session.commit()
    p = db.db_get_purchases()[0]
    with db.get_session() as session:
        row = session.get(db.Purchase, p['_db_id']); row.account_id = ''
        session.add(row); session.commit()
    assert not db.db_apply_receipt_reconciliation(p, assetid='200', receipt_evidence=e)
    assert db.db_get_purchases()[0]['pending_receipt'] is True


def test_purchase_account_is_resolved_once_at_record_time(monkeypatch):
    """An explicit account wins; otherwise it is read once, when recorded."""
    from app import database as db
    from sqlmodel import SQLModel, create_engine
    from sqlalchemy.pool import StaticPool
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    monkeypatch.setattr(db, '_engine', engine); SQLModel.metadata.create_all(engine)
    monkeypatch.setattr('app.config_loader.get_steam_credentials', lambda: {'steam_id': 'ambient'})
    db.db_append_purchase({'name': 'A', 'price': 1, 'at': 100})
    db.db_append_purchase({'name': 'B', 'price': 1, 'at': 100, 'account_id': 'explicit'})
    rows = db.db_get_purchases()
    assert [r['account_id'] for r in rows] == ['ambient', 'explicit']


def test_audit_purchase_does_not_bind_account_from_credentials(monkeypatch):
    from app.trading_ledger import audit_purchase
    from app import database as db
    from sqlmodel import SQLModel, create_engine, select
    from sqlalchemy.pool import StaticPool
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    monkeypatch.setattr(db, '_engine', engine); SQLModel.metadata.create_all(engine)
    monkeypatch.setattr('app.config_loader.get_steam_credentials', lambda: {'steam_id': 'someone-else'})
    with db.get_session() as s:
        row = db._purchase_from_dict({'name': 'Item', 'price': 1, 'at': 100})
        s.add(row); s.flush()
        audit_purchase(s, row, None)          # no account_id supplied
        assert row.account_id == ''           # stays unbound; nothing invented
        s.rollback()

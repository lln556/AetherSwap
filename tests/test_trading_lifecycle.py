from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import Mock
import pytest
from sqlmodel import SQLModel, create_engine, Session, select


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from app import database
    from app import trading_models
    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    monkeypatch.setattr(database, "_engine", engine)
    database.init_db()
    monkeypatch.setattr("app.config_loader.get_steam_credentials", lambda: {"steam_id": "account", "cookies": "cookie"})
    from steam.request_policy import MarketCooldown
    monkeypatch.setattr("app.sell_pipeline._listing_cooldown", MarketCooldown())
    yield database
    engine.dispose()


def test_reprice_is_total_loss_from_original_cost_and_never_compounds():
    from app.services.repricing import evaluate_price, buyer_total
    first = evaluate_price(cost_minor=1000, old_buyer_minor=1500, market_minor=500)
    assert first["adjust"] and first["target_seller_minor"] == 700
    assert first["target_buyer_minor"] == buyer_total(700)
    second = evaluate_price(cost_minor=1000, old_buyer_minor=first["target_buyer_minor"], market_minor=100)
    assert not second["adjust"] and second["floor_seller_minor"] == 700


@pytest.mark.parametrize("old,market", [(1000, 1100), (1000, 1000), (1000, 995), (2000, 1985)])
def test_no_upward_or_tiny_price_changes(old, market):
    from app.services.repricing import evaluate_price
    assert not evaluate_price(cost_minor=500, old_buyer_minor=old, market_minor=market)["adjust"]


def test_loss_floor_rounds_up_and_uses_actual_fee_minimum():
    from app.services.repricing import evaluate_price, buyer_total
    d = evaluate_price(cost_minor=101, old_buyer_minor=150, market_minor=1)
    assert d["target_seller_minor"] == 71
    assert d["target_buyer_minor"] == 85
    assert buyer_total(71) == 85


def test_history_year_inference_requires_unambiguous_ownership_interval():
    from app.steam_sale_history import history_day
    tz = ZoneInfo("Asia/Shanghai")
    now = datetime(2026, 10, 19, tzinfo=tz)
    assert history_day("Sold: 18 Oct", now=now, purchased_at=datetime(2026,10,11,tzinfo=tz).timestamp()) == "2026-10-18"
    assert history_day("已售出: 10 月 18 日", now=now, purchased_at=datetime(2026,10,11,tzinfo=tz).timestamp()) == "2026-10-18"
    assert history_day("Sold: 18 Oct", now=now, purchased_at=datetime(2025,1,1,tzinfo=tz).timestamp()) is None
    assert history_day("Sold: 20 Oct", now=now, purchased_at=datetime(2026,10,11,tzinfo=tz).timestamp()) is None


def test_history_reads_net_proceeds_and_only_explicit_sales():
    from app.steam_sale_history import parse_history
    data = {"hovers": "CreateItemHoverFromContainer( g_rgAssets, 'history_row_1_2_name', 730, '2', '42');", "results_html": '<div class="market_listing_row" id="history_row_1_2"><div class="market_listing_listed_date_combined">Sold: 18 Oct</div><span class="market_listing_price">¥ 8.21</span></div>'}
    rows, _ = parse_history(data, {"42": 1791702188})
    assert rows[0]["proceeds_minor"] == 821
    data["results_html"] = data["results_html"].replace("Sold:", "Purchased:")
    assert parse_history(data, {"42":1791702188})[0] == []


def test_bootstrap_imports_listing_actions_without_inventing_sales(ledger):
    from app.trading_ledger import bootstrap_ledger
    from app.trading_models import MarketListing, TradeSettlement, TradeEvent
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a","listing":True})
    ledger.db_append_sale({"name":"Item","price":15,"at":200,"assetid":"a"})
    bootstrap_ledger(); bootstrap_ledger()
    with ledger.get_session() as s:
        assert len(s.exec(select(MarketListing)).all()) == 1
        assert s.exec(select(TradeSettlement)).all() == []
        assert len(s.exec(select(TradeEvent).where(TradeEvent.kind == "purchased")).all()) == 1


def test_legacy_sold_at_is_discovery_time_not_execution_time(ledger):
    from app.trading_models import TradeSettlement
    ledger.db_append_purchase({"name":"Item","price":5.95,"at":100,"assetid":"a","sale_price":9.44,"sold_at":200})
    with ledger.get_session() as s:
        r=s.get(TradeSettlement,1)
        assert r.detected_at == 200 and r.sold_at is None and r.sold_day is None
        assert r.amount_source == "estimated_legacy"


def test_positive_snapshot_starts_24_hour_clock_and_missing_is_not_sale(ledger):
    from app.trading_ledger import record_snapshot
    from app.trading_models import MarketListing, TradeSettlement
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a"})
    snapshot={"a":{"listing_id":"lid","status":"active","buyer_minor":1500,"seller_minor":1305,"currency":"CNY"}}
    record_snapshot(snapshot,"account",now=1000)
    record_snapshot(snapshot,"account",now=1100)
    with ledger.get_session() as s:
        row=s.exec(select(MarketListing)).one()
        assert row.activated_at == 1000 and row.next_evaluation_at == 1000+86400
    record_snapshot({},"account",now=1200)
    with ledger.get_session() as s:
        assert s.exec(select(MarketListing)).one().status == "unknown"
        assert s.exec(select(TradeSettlement)).all() == []
        assert s.get(ledger.Purchase,1).sale_price is None


def test_settlement_is_idempotent_and_preserves_actual_proceeds(ledger):
    from app.trading_ledger import record_settlements
    from app.trading_models import TradeSettlement, TradeEvent
    ledger.db_append_purchase({"name":"Item","price":5.95,"at":100,"assetid":"a","listing":True})
    rows=[{"external_id":"history_1","assetid":"a","proceeds_minor":821,"currency":"CNY","sold_day":"2026-10-18","date_label":"Sold: 18 Oct"}]
    assert record_settlements(rows,"account") == 1
    assert record_settlements(rows,"account") == 0
    with ledger.get_session() as s:
        sale=s.get(TradeSettlement,1)
        assert sale.proceeds_minor == 821 and sale.sold_at is None and sale.time_precision == "day"
        assert not s.get(ledger.Purchase,1).listing
        assert len(s.exec(select(TradeEvent).where(TradeEvent.kind == "sold")).all()) == 1


def test_cross_account_snapshot_does_not_change_owned_item(ledger):
    from app.trading_ledger import record_snapshot
    from app.trading_models import MarketListing
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a","account_id":"other"})
    record_snapshot({"a":{"listing_id":"lid","status":"active"}},"account",now=1000)
    with ledger.get_session() as s:
        assert s.exec(select(MarketListing)).all() == []


def test_business_changes_persist_without_memory_logs(ledger):
    from app.trading_models import TradeEvent
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"pending_receipt":True})
    ledger.db_update_purchase_by_id(1,{"pending_receipt":False,"assetid":"a"})
    with ledger.get_session() as s:
        assert {e.kind for e in s.exec(select(TradeEvent)).all()} >= {"purchased","received"}


def test_unconfirmed_listing_response_is_not_resubmitted(ledger,monkeypatch):
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob
    import time
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a","account_id":"account"})
    with ledger.get_session() as s:
        s.add(RepriceJob(listing_id="l",purchase_id=1,account_id="account",old_assetid="a",assetid="a",old_steam_listing_id="old",
                        status="submitting",old_buyer_minor=1500,target_buyer_minor=1000,target_seller_minor=870,floor_seller_minor=700))
        s.commit()
    post=Mock(side_effect=AssertionError("must not resubmit unknown result"))
    monkeypatch.setattr("steam.market.list_item",post)
    resume_jobs({}, {"steam_id":"account","cookies":"cookie"}, {})
    post.assert_not_called()


def test_reconciliation_runs_even_during_purchase_scan(ledger,monkeypatch):
    from app.services.trading_worker import run_trading_cycle
    monkeypatch.setattr("app.config_loader.load_app_config_validated",lambda:{})
    monkeypatch.setattr("app.state.is_steam_background_allowed",lambda:False)
    snapshot=Mock(return_value=(True,{},""))
    monkeypatch.setattr("app.steam_listings.fetch_listing_snapshot",snapshot)
    monkeypatch.setattr("app.steam_sale_history.fetch_sale_history",lambda *a:(True,[],""))
    monkeypatch.setattr("app.services.listing_confirmation.sync_listing_confirmations",lambda *a,**kw:(True,{},""))
    assert run_trading_cycle()["ok"]
    snapshot.assert_called_once()


def test_steam_listing_time_is_used_but_confirmation_wait_does_not_age(ledger):
    from app.trading_ledger import record_snapshot
    from app.trading_models import MarketListing
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a"})
    snap={"a":{"listing_id":"l","status":"pending_confirmation","created_at":200}}
    record_snapshot(snap,"account",now=500)
    snap["a"]["status"]="active"
    record_snapshot(snap,"account",now=1000)
    with ledger.get_session() as s:
        row=s.exec(select(MarketListing)).one()
        assert row.activated_at == 1000 and row.next_evaluation_at == 87400


def test_reprice_resumes_each_phase_and_records_exactly_one_new_listing(ledger,monkeypatch):
    from app.services.repricing import resume_jobs,buyer_total
    from app.trading_ledger import record_snapshot
    from app.trading_models import RepriceJob,MarketListing,TradingPolicy
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a"})
    with ledger.get_session() as s:
        s.add(TradingPolicy(enabled=True))
        s.add(MarketListing(id="old",purchase_id=1,account_id="account",assetid="a",steam_listing_id="oldremote",status="repricing"))
        s.add(RepriceJob(id="job",listing_id="old",purchase_id=1,account_id="account",old_assetid="a",assetid="a",old_steam_listing_id="oldremote",old_buyer_minor=1500,target_buyer_minor=buyer_total(900),target_seller_minor=900,floor_seller_minor=700))
        s.commit()
    remove=Mock(return_value=(True,"b",None));post=Mock(return_value={"status_code":200,"text":'{"success":true}'})
    monkeypatch.setattr("app.steam_delist.delist_item",remove)
    monkeypatch.setattr("steam.market.list_item",post)
    monkeypatch.setattr("app.steam_listings.fetch_listing_snapshot",lambda *a:(True,{},""))
    monkeypatch.setattr("app.inventory_cs2.scan_cs2_inventory",lambda:(True,[{"assetid":"b","can_sell":True}],""))
    monkeypatch.setattr("app.services.account_region.refresh_account_region_currency",lambda **kw:{"ok":True,"currency_code":"CNY"})
    monkeypatch.setattr("app.config_loader.get_steam_credentials",lambda:{"steam_id":"account","cookies":"sessionid=x"})
    cred={"steam_id":"account","cookies":"sessionid=x"};cfg={"pipeline":{"sell_strategy":1}}
    resume_jobs({"a":{"listing_id":"oldremote","status":"active"}},cred,cfg)
    with ledger.get_session() as s:
        assert s.get(RepriceJob,"job").status == "ready"
        assert s.get(ledger.Purchase,1).assetid == "b"
    resume_jobs({},cred,cfg)
    with ledger.get_session() as s:
        assert s.get(RepriceJob,"job").status == "confirming"
    snapshot={"b":{"listing_id":"newremote","status":"active","seller_minor":900,"buyer_minor":buyer_total(900),"currency":"CNY"}}
    record_snapshot(snapshot,"account")
    resume_jobs(snapshot,cred,cfg);resume_jobs(snapshot,cred,cfg)
    assert post.call_count == 1 and remove.call_count == 1
    with ledger.get_session() as s:
        assert s.get(RepriceJob,"job").status == "completed"
        assert len(s.exec(select(MarketListing)).all()) == 2
        assert s.get(MarketListing,"old").status == "cancelled"


def test_post_intent_survives_ambiguous_response_and_prevents_another_attempt(ledger):
    from app.trading_ledger import begin_listing_attempt,finish_listing_attempt
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a"})
    lid=begin_listing_attempt(1,"a",1500,1305,"CNY")
    finish_listing_attempt(lid,message="timeout")
    with pytest.raises(ValueError):
        begin_listing_attempt(1,"a",1500,1305,"CNY")
    assert ledger.db_get_purchases()[0]["listing_status"] == "submitted"


def test_manual_net_proceeds_are_not_roundtripped_through_fee_estimate(ledger):
    from app.trading_models import TradeSettlement
    ledger.db_append_purchase({"name":"Item","price":10,"at":100,"assetid":"a"})
    ledger.db_update_purchase_by_id(1,{"sale_price":115.01,"sold_at":200,"_sale_proceeds_minor":10001})
    with ledger.get_session() as s:
        assert s.get(TradeSettlement,1).proceeds_minor == 10001
        assert s.get(TradeSettlement,1).amount_source == "user_reported"


def test_reprice_quote_obeys_trend_and_custom_strategy_guards(monkeypatch):
    from app.services.repricing import _strategy_quote
    monkeypatch.setattr('app.sell_pipeline.get_sell_orders_cny', lambda *a, **k: {'sell_orders': [[10, 20]]})
    monkeypatch.setattr('app.sell_pipeline.compute_smart_list_price', lambda *a, **k: (10, 'market'))
    monkeypatch.setattr('app.sell_pipeline._steam_latest_price_and_trend', lambda *a, **k: (10, 1, []))
    monkeypatch.setattr('app.sell_pipeline.evaluate_strategy_runtime_modules', lambda *a, **k: ([], False))
    p = {'name': 'Item', 'assetid': 'a', 'price': 5, 'listing': True}
    snapshot = {'a': {'name': 'Item'}, 'b': {'name': 'Item'}}
    assert _strategy_quote(None, p, snapshot, {}, {'sell_strategy': 2})[0] is None
    assert _strategy_quote(None, p, snapshot, {}, {'sell_strategy': 1, 'max_listings_per_item': 2})[0] == 1000
    monkeypatch.setattr('app.sell_pipeline.evaluate_strategy_runtime_modules', lambda *a, **k: ([], True))
    assert _strategy_quote(None, p, snapshot, {}, {'sell_strategy': 1})[0] is None


def test_database_unlock_failure_still_releases_local_market_locks(monkeypatch):
    from app.services import trading_worker
    from app.sell_pipeline import _sell_phase_lock
    connection = Mock()
    connection.execute.side_effect = [Mock(scalar=lambda: False), RuntimeError('connection lost')]
    engine = Mock(dialect=Mock(name='postgresql'))
    engine.dialect.name = 'postgresql'
    engine.connect.return_value = connection
    monkeypatch.setattr('app.database.get_engine', lambda: engine)
    with pytest.raises(RuntimeError):
        trading_worker.run_trading_cycle()
    assert not trading_worker._cycle_lock.locked()
    assert _sell_phase_lock.acquire(blocking=False)
    _sell_phase_lock.release()
    connection.close.assert_called_once()


def test_archived_purchase_preserves_timeline_and_is_not_reused(ledger):
    from app.trading_models import TradeEvent
    ledger.db_append_purchase({'name': 'First', 'price': 10, 'at': 100, 'assetid': 'a'})
    ledger.db_replace_transactions([], [])
    ledger.db_append_purchase({'name': 'Second', 'price': 20, 'at': 200, 'assetid': 'b'})
    with ledger.get_session() as s:
        assert s.get(ledger.Purchase, 1).archived_at
        assert s.get(ledger.Purchase, 2).name == 'Second'
        assert s.exec(select(TradeEvent).where(TradeEvent.purchase_id == 1)).first()
    assert [p['_db_id'] for p in ledger.db_get_purchases()] == [2]


def test_policy_response_contains_persisted_settings(ledger):
    from app.routes.trading import set_policy, PolicyBody
    result = set_policy(PolicyBody(enabled=True, age_hours=24, max_loss_percent=30))
    assert result['policy']['enabled'] is True
    assert result['policy']['age_hours'] == 24
    assert result['policy']['max_loss_percent'] == 30


def test_queued_reprice_does_not_hide_live_listing_count(ledger):
    from app.trading_models import MarketListing, RepriceJob
    from app.trading_ledger import trading_overview
    ledger.db_append_purchase({'name': 'Item', 'price': 10, 'at': 100, 'assetid': 'a'})
    with ledger.get_session() as s:
        s.add(MarketListing(id='l', purchase_id=1, account_id='account', assetid='a', status='active'))
        s.add(RepriceJob(listing_id='l', purchase_id=1, account_id='account', old_assetid='a', assetid='a', old_steam_listing_id='remote', old_buyer_minor=1500, target_buyer_minor=1300, target_seller_minor=1132, floor_seller_minor=700, error='rate limited'))
        s.commit()
    summary = trading_overview()['summary']
    assert summary['active'] == 1 and summary['repricing'] == 1 and summary['attention'] == 1


def _queued_job(ledger, *, count=1):
    from app.trading_models import RepriceJob, TradingPolicy, MarketListing
    with ledger.get_session() as s:
        s.add(TradingPolicy(enabled=True));s.commit()
    snapshot = {}
    for i in range(1,count+1):
        aid = str(i)
        ledger.db_append_purchase({'name':'Item','price':10,'at':100,'assetid':aid,'listing':True})
        with ledger.get_session() as s:
            s.add(MarketListing(id=aid,purchase_id=i,account_id='account',assetid=aid,steam_listing_id='remote'+aid,status='active'))
            s.add(RepriceJob(id=aid,listing_id=aid,purchase_id=i,account_id='account',old_assetid=aid,assetid=aid,old_steam_listing_id='remote'+aid,old_buyer_minor=1500,target_buyer_minor=1035,target_seller_minor=900,floor_seller_minor=700))
            s.commit()
        snapshot[aid]={'listing_id':'remote'+aid,'status':'active','buyer_minor':1500,'seller_minor':1305}
    return snapshot


def test_inventory_preflight_failure_is_not_return_to_inventory_and_stops_batch(ledger,monkeypatch):
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob,TradingPolicy,TradeEvent
    import json,time
    snapshot=_queued_job(ledger,count=2)
    def reject(*args,outcome):
        outcome.update(request_sent=False,http_status=429,retry_after=3600,phase='preflight')
        return False,None,'库存接口 HTTP 429，尚未提交下架请求'
    remove=Mock(side_effect=reject);monkeypatch.setattr('app.steam_delist.delist_item',remove)
    resume_jobs(snapshot,{'steam_id':'account','cookies':'cookie'},{'pipeline':{'sell_strategy':1}})
    resume_jobs(snapshot,{'steam_id':'account','cookies':'cookie'},{'pipeline':{'sell_strategy':1}})
    assert remove.call_count == 1
    with ledger.get_session() as s:
        job=s.get(RepriceJob,'1');assert job.status=='planned' and job.consecutive_failures==1
        assert job.retry_at >= time.time()+3590
        assert s.get(TradingPolicy,1).mutation_retry_at == job.retry_at
        assert s.get(RepriceJob,'2').attempts == 0
        assert not s.exec(select(TradeEvent).where(TradeEvent.kind=='reprice_awaiting_inventory')).all()
        event=s.exec(select(TradeEvent).where(TradeEvent.kind=='reprice_planned')).one()
        assert json.loads(event.details)['request_sent'] is False


def test_repeated_failures_back_off_then_stop_automatically(ledger):
    from app.services.repricing import _defer_withdrawal
    from app.trading_models import RepriceJob
    import time
    _queued_job(ledger)
    delays=[]
    for i in range(5):
        with ledger.get_session() as s: job=s.get(RepriceJob,'1').model_dump()
        _defer_withdrawal(job,'库存接口 HTTP 429',{'request_sent':False,'http_status':429})
        with ledger.get_session() as s:
            job=s.get(RepriceJob,'1');delays.append(round(job.retry_at-time.time()))
    assert delays == [1800,3600,7200,7200,7200]
    assert job.status=='needs_review' and job.review_from=='planned'


def test_ambiguous_withdrawal_preserves_unknown_state(ledger,monkeypatch):
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob
    snapshot=_queued_job(ledger)
    def timeout(*args,outcome):
        outcome.update(request_sent=True,phase='withdrawal')
        return False,None,'撤单流程请求异常（Timeout）'
    monkeypatch.setattr('app.steam_delist.delist_item',timeout)
    resume_jobs(snapshot,{'steam_id':'account','cookies':'cookie'},{'pipeline':{'sell_strategy':1}})
    with ledger.get_session() as s: assert s.get(RepriceJob,'1').status=='awaiting_inventory'


def test_waiting_retries_do_not_trigger_minute_polling():
    from app.services.trading_worker import next_cycle_delay
    from app.trading_models import TradingPolicy
    from types import SimpleNamespace
    p=TradingPolicy(enabled=True)
    job=SimpleNamespace(status='planned',retry_at=5000)
    assert next_cycle_delay(p,[job],{'ok':True},now=1000)==600
    job.retry_at=1100;assert next_cycle_delay(p,[job],{'ok':True},now=1000)==100
    p.enabled=False;assert next_cycle_delay(p,[job],{'ok':True},now=1000)==600
    assert next_cycle_delay(p,[],{'ok':False,'error':'HTTP 429'},now=1000)==600


def test_retry_refreshes_stale_quote_before_any_withdrawal(ledger,monkeypatch):
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob
    snapshot=_queued_job(ledger)
    with ledger.get_session() as s:
        job=s.get(RepriceJob,'1');job.last_quoted_at=1;s.add(job);s.commit()
    monkeypatch.setattr('app.services.repricing._strategy_quote',lambda *args:(1600,'price rose'))
    remove=Mock();monkeypatch.setattr('app.steam_delist.delist_item',remove)
    resume_jobs(snapshot,{'steam_id':'account','cookies':'cookie'},{'pipeline':{'sell_strategy':1}})
    remove.assert_not_called()
    with ledger.get_session() as s: assert s.get(RepriceJob,'1').status=='cancelled'


def _ready_listing_jobs(ledger, monkeypatch, count=1):
    from app.trading_models import RepriceJob, MarketListing
    _queued_job(ledger, count=count)
    with ledger.get_session() as s:
        for i in range(1, count + 1):
            job = s.get(RepriceJob, str(i)); job.status = 'ready'; s.add(job)
            p = s.get(ledger.Purchase, i); p.listing = False; s.add(p)
            old = s.get(MarketListing, str(i)); old.status = 'cancelled'; s.add(old)
        s.commit()
    cred = {'steam_id': 'account', 'cookies': 'sessionid=x'}
    monkeypatch.setattr('app.config_loader.get_steam_credentials', lambda: cred)
    monkeypatch.setattr('app.steam_listings.fetch_listing_snapshot', lambda *a: (True, {}, ''))
    monkeypatch.setattr('app.services.account_region.refresh_account_region_currency', lambda **kw: {'ok': True, 'currency_code': 'CNY'})
    monkeypatch.setattr('app.inventory_cs2.scan_cs2_inventory', lambda: (True, [{'assetid': str(i), 'can_sell': True} for i in range(1, count + 1)], ''))
    return cred


def test_listing_429_is_durable_retry_not_ambiguous_and_stops_batch(ledger, monkeypatch):
    import json, time
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob, TradingPolicy, TradeEvent
    cred = _ready_listing_jobs(ledger, monkeypatch, count=2)
    post = Mock(return_value={'status_code': 429, 'text': 'null', 'retry_after': '3600', 'request_sent': True, 'transport': 'httpx'})
    monkeypatch.setattr('steam.market.list_item', post)
    resume_jobs({}, cred, {})
    resume_jobs({}, cred, {})
    assert post.call_count == 1
    with ledger.get_session() as s:
        job = s.get(RepriceJob, '1')
        assert job.status == 'ready' and job.consecutive_failures == 1
        assert job.retry_at >= time.time() + 3590
        assert s.get(TradingPolicy, 1).mutation_retry_at == job.retry_at
        assert s.get(RepriceJob, '2').attempts == 0
        event = s.exec(select(TradeEvent).where(TradeEvent.kind == 'reprice_ready')).one()
        details = json.loads(event.details)
        assert details['http_status'] == 429 and details['response_body'] == 'null'
        assert details['transport'] == 'httpx'
        assert '等待重新上架' in event.message
    # After a restart, persisted cooldown must still block a fresh process.
    from steam.request_policy import MarketCooldown
    monkeypatch.setattr('app.sell_pipeline._listing_cooldown', MarketCooldown())
    resume_jobs({}, cred, {})
    assert post.call_count == 1


def test_repeated_listing_limits_stop_with_correct_resume_phase(ledger, monkeypatch):
    from app.services.repricing import _defer_listing
    from app.trading_models import RepriceJob
    _ready_listing_jobs(ledger, monkeypatch)
    for _ in range(5):
        with ledger.get_session() as s: job = s.get(RepriceJob, '1').model_dump()
        _defer_listing(job, {'status_code': 429, 'text': 'null', 'request_sent': True})
    with ledger.get_session() as s:
        job = s.get(RepriceJob, '1')
        assert job.status == 'needs_review' and job.review_from == 'ready'
        assert job.consecutive_failures == 5


@pytest.mark.parametrize('result, expected', [
    ({'status_code': None, 'text': '', 'error': 'httpx 上架超时', 'request_sent': True}, 'submitting'),
    ({'status_code': 200, 'text': 'null', 'request_sent': True}, 'submitting'),
    ({'status_code': None, 'text': '', 'error': 'httpx 连接失败', 'request_sent': False}, 'ready'),
    ({'status_code': 403, 'text': 'denied', 'request_sent': True}, 'needs_review'),
    ({'status_code': 200, 'text': '{"success":false,"message":"sessionid=private-token"}', 'request_sent': True}, 'needs_review'),
])
def test_listing_failures_keep_evidence_and_do_not_resubmit(ledger, monkeypatch, result, expected):
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob, TradeEvent
    cred = _ready_listing_jobs(ledger, monkeypatch)
    post = Mock(return_value=result)
    monkeypatch.setattr('steam.market.list_item', post)
    resume_jobs({}, cred, {})
    resume_jobs({}, cred, {})
    assert post.call_count == 1
    with ledger.get_session() as s:
        job = s.get(RepriceJob, '1')
        assert job.status == expected
        assert 'private-token' not in job.error
        for e in s.exec(select(TradeEvent)).all():
            assert 'private-token' not in e.details


@pytest.mark.parametrize('final_status', ['active', 'pending_confirmation'])
def test_reprice_confirms_exact_new_listing_and_verifies_active(ledger, monkeypatch, final_status):
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob
    from app.services import listing_confirmation
    cred = _ready_listing_jobs(ledger, monkeypatch)
    post = Mock(return_value={'status_code': 200, 'text': '{"success":true,"needs_mobile_confirmation":true}', 'request_sent': True, 'transport': 'httpx'})
    monkeypatch.setattr('steam.market.list_item', post)
    pending = {'1': {'listing_id': 'newremote', 'status': 'pending_confirmation', 'seller_minor': 900, 'buyer_minor': 1035, 'currency': 'CNY'}}
    verified = {'1': {**pending['1'], 'status': final_status}}
    monkeypatch.setattr(listing_confirmation, 'get_steam_credentials', lambda: cred)
    fetch = Mock(side_effect=[(True, pending, ''), (True, verified, '')])
    confirm = Mock(return_value=(True, 1, ''))
    monkeypatch.setattr(listing_confirmation, 'fetch_listing_snapshot', fetch)
    monkeypatch.setattr(listing_confirmation, 'auto_confirm_once', confirm)
    cfg = {'steam_confirm': {'enabled': True, 'identity_secret': 'test-secret'}}
    resume_jobs({}, cred, cfg)
    confirm.assert_called_once()
    assert confirm.call_args.kwargs['listing_ids'] == {'newremote'}
    with ledger.get_session() as s:
        assert s.get(RepriceJob, '1').status == ('completed' if final_status == 'active' else 'confirming')
        assert s.get(ledger.Purchase, 1).listing_status == final_status
    resume_jobs(verified, cred, cfg)
    assert post.call_count == 1


def test_relisting_requires_the_exact_sellable_asset(ledger, monkeypatch):
    from app.services.repricing import resume_jobs
    from app.trading_models import RepriceJob
    cred = _ready_listing_jobs(ledger, monkeypatch)
    monkeypatch.setattr('app.inventory_cs2.scan_cs2_inventory', lambda: (True, [{'assetid': 'other', 'can_sell': True}], ''))
    post = Mock()
    monkeypatch.setattr('steam.market.list_item', post)
    resume_jobs({}, cred, {})
    post.assert_not_called()
    with ledger.get_session() as s: assert s.get(RepriceJob, '1').status == 'needs_review'

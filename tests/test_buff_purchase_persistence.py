def test_purchase_round_trip_keeps_buff_reconciliation_ids(monkeypatch, tmp_path):
    from app import database

    old_engine = database._engine
    if old_engine is not None:
        old_engine.dispose()
    monkeypatch.setattr(database, "_DB_PATH", tmp_path / "app.db")
    monkeypatch.setattr(database, "_engine", None)

    database.init_db()
    database.db_append_purchase(
        {
            "name": "Test Item",
            "goods_id": 42,
            "price": 10.0,
            "at": 123.0,
            "pending_receipt": True,
            "buff_order_id": "bill-1",
            "buff_sell_order_id": "sell-1",
            "batch_id": "batch-1",
            "bill_order_id": "bill-1",
        }
    )

    [saved] = database.db_get_purchases()
    assert saved["buff_order_id"] == "bill-1"
    assert saved["buff_sell_order_id"] == "sell-1"
    assert saved["batch_id"] == "batch-1"
    assert saved["bill_order_id"] == "bill-1"

    database.get_engine().dispose()

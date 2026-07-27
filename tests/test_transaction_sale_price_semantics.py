from pathlib import Path


def test_manual_sale_proceeds_are_normalized_to_legacy_gross_price(monkeypatch):
    """The holdings dialog accepts net proceeds, while sale_price stays gross."""
    from app.routes import transactions

    updates = []
    monkeypatch.setattr(
        transactions,
        "update_purchase_by_id",
        lambda db_id, data: updates.append((db_id, data)) or True,
    )

    body = transactions.TransactionUpdateBody(
        type="purchase",
        idx=99,
        db_id=42,
        sale_proceeds=100.01,
    )
    result = transactions.api_update_transaction(body)

    assert result["ok"] is True
    assert updates[0][0] == 42
    assert updates[0][1]["sale_price"] == 115.01
    assert updates[0][1]["sold_at"] > 0


def test_legacy_sale_price_clients_remain_gross_and_are_not_converted(monkeypatch):
    """Existing API clients and automatically synchronized data keep semantics."""
    from app.routes import transactions

    updates = []
    monkeypatch.setattr(
        transactions,
        "update_purchase",
        lambda idx, data: updates.append((idx, data)) or True,
    )

    body = transactions.TransactionUpdateBody(
        type="purchase",
        idx=3,
        sale_price=115.01,
    )
    result = transactions.api_update_transaction(body)

    assert result["ok"] is True
    assert updates[0][0] == 3
    assert updates[0][1]["sale_price"] == 115.01


def test_explicit_sale_proceeds_take_precedence_for_mixed_version_clients(
    monkeypatch,
):
    from app.routes import transactions

    updates = []
    monkeypatch.setattr(
        transactions,
        "update_purchase",
        lambda idx, data: updates.append(data) or True,
    )

    body = transactions.TransactionUpdateBody(
        type="purchase",
        idx=0,
        sale_proceeds=20,
        sale_price=20,
    )
    result = transactions.api_update_transaction(body)

    assert result["ok"] is True
    assert updates[0]["sale_price"] == 23.0


def test_holdings_dialog_submits_net_proceeds_with_explicit_semantics():
    root = Path(__file__).resolve().parent.parent
    main_js = (root / "web" / "js" / "main.js").read_text(encoding="utf-8")
    index_html = (root / "web" / "index.html").read_text(encoding="utf-8")

    assert "sale_proceeds: priceRounded" in main_js
    assert "sale_price: priceRounded" not in main_js
    assert "实际到手金额（税后）" in index_html
    assert "换算为税前出售价" in index_html

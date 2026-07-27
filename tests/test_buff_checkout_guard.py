import json

import pytest

from app.services import buff_checkout_guard as guard


@pytest.fixture(autouse=True)
def _isolated_guard(monkeypatch, tmp_path):
    path = tmp_path / "buff_checkout_guard.json"
    monkeypatch.setattr(guard, "_GUARD_PATH", path)
    return path


def test_intent_is_durable_secret_free_and_blocks_a_second_checkout():
    state = guard.begin_checkout(
        "single",
        123,
        sell_order_id="sell-1",
        quantity=1,
        credential_generation=7,
        credential_fingerprint="abc123",
        price=10.5,
    )

    persisted = json.loads(guard._GUARD_PATH.read_text(encoding="utf-8"))
    assert persisted["intent_id"] == state["intent_id"]
    assert persisted["unresolved"] is True
    assert persisted["stage"] == "intent_prepared"
    assert persisted["sell_order_id"] == "sell-1"
    assert "cookie" not in guard._GUARD_PATH.read_text(encoding="utf-8").lower()

    with pytest.raises(guard.BuffCheckoutGuardActive) as exc_info:
        guard.begin_checkout("single", 456, sell_order_id="sell-2")
    assert exc_info.value.state["intent_id"] == state["intent_id"]


def test_update_then_resolve_allows_a_new_intent():
    guard.begin_checkout("batch", 123, quantity=2)
    updated = guard.update_checkout(
        stage="batch_created_pending",
        batch_id="batch-1",
        completed_order_ids=["bill-1"],
        reason="waiting",
    )

    assert updated["batch_id"] == "batch-1"
    assert updated["completed_order_ids"] == ["bill-1"]
    assert guard.get_unresolved_checkout()["stage"] == "batch_created_pending"

    resolved = guard.resolve_checkout("recorded")
    assert resolved["unresolved"] is False
    assert guard.get_unresolved_checkout() is None
    replacement = guard.begin_checkout("single", 456, sell_order_id="sell-2")
    assert replacement["intent_id"] != updated["intent_id"]


def test_acknowledgement_is_explicit_and_persists():
    intent = guard.begin_checkout("single", 123)
    acknowledged = guard.acknowledge_checkout(
        intent["intent_id"],
        "user_checked_buff_history",
    )

    assert acknowledged["stage"] == "acknowledged"
    assert acknowledged["unresolved"] is False
    assert acknowledged["acknowledged_at"] > 0
    assert guard.get_unresolved_checkout() is None


def test_corrupt_journal_fails_closed():
    guard._GUARD_PATH.write_text("{broken", encoding="utf-8")

    state = guard.get_unresolved_checkout()

    assert state["unresolved"] is True
    assert state["stage"] == "journal_unreadable"
    with pytest.raises(guard.BuffCheckoutGuardActive):
        guard.begin_checkout("single", 1)


def test_duplicate_journal_keys_fail_closed():
    guard._GUARD_PATH.write_text(
        '{"version": 1, "version": 1, "unresolved": false}',
        encoding="utf-8",
    )

    state = guard.get_unresolved_checkout()

    assert state["unresolved"] is True
    assert state["stage"] == "journal_unreadable"


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_journal_numbers_fail_closed(value):
    guard._GUARD_PATH.write_text(
        (
            '{"version": 1, "unresolved": false, "intent_id": "fake", '
            '"kind": "single", "stage": "resolved", "goods_id": 1, '
            '"quantity": 1, "created_at": 1, "updated_at": 1, '
            f'"resolved_at": {value}}}'
        ),
        encoding="utf-8",
    )

    state = guard.get_unresolved_checkout()

    assert state["unresolved"] is True
    assert state["stage"] == "journal_unreadable"
    with pytest.raises(guard.BuffCheckoutGuardActive):
        guard.begin_checkout("single", 1)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"version": 2, "unresolved": False},
        {"version": 1, "unresolved": 1},
        {
            "version": 1,
            "unresolved": False,
            "intent_id": "fake",
            "kind": "single",
            "stage": "intent_prepared",
            "goods_id": 1,
            "quantity": 1,
            "created_at": 1.0,
            "updated_at": 1.0,
        },
    ],
)
def test_parseable_but_invalid_journal_fails_closed(payload):
    guard._GUARD_PATH.write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    state = guard.get_unresolved_checkout()

    assert state["unresolved"] is True
    assert state["stage"] == "journal_invalid"
    with pytest.raises(guard.BuffCheckoutGuardActive):
        guard.begin_checkout("single", 1)


def test_stale_acknowledgement_cannot_clear_a_new_intent():
    first = guard.begin_checkout("single", 1)
    guard.acknowledge_checkout(first["intent_id"], "first reconciled")
    second = guard.begin_checkout("single", 2)

    with pytest.raises(guard.BuffCheckoutGuardMismatch):
        guard.acknowledge_checkout(first["intent_id"], "stale browser modal")

    assert guard.get_unresolved_checkout()["intent_id"] == second["intent_id"]

"""Payment-confirmation generation isolation (late email waiter safety)."""


def _state():
    from app.state import State
    return State()


def test_stale_confirm_from_previous_order_is_ignored():
    st = _state()
    gen1 = st.begin_payment_wait()
    gen2 = st.begin_payment_wait()
    assert gen2 == gen1 + 1
    # A late email thread from the previous order posts its result...
    st.confirm_payment(True, generation=gen1)
    # ...and must not resolve the current wait.
    assert st.wait_payment_confirm(timeout_seconds=0.05, generation=gen2) is False


def test_current_generation_confirm_resolves_wait():
    st = _state()
    gen = st.begin_payment_wait()
    st.confirm_payment(True, generation=gen)
    assert st.wait_payment_confirm(timeout_seconds=0.5, generation=gen) is True


def test_ui_confirm_without_generation_applies_to_current_wait():
    st = _state()
    gen = st.begin_payment_wait()
    st.confirm_payment(True)
    assert st.wait_payment_confirm(timeout_seconds=0.5, generation=gen) is True


def test_wait_on_superseded_generation_returns_false():
    st = _state()
    gen1 = st.begin_payment_wait()
    st.begin_payment_wait()
    assert st.wait_payment_confirm(timeout_seconds=0.05, generation=gen1) is False


def test_timeout_without_confirm_returns_false():
    st = _state()
    gen = st.begin_payment_wait()
    assert st.wait_payment_confirm(timeout_seconds=0.05, generation=gen) is False

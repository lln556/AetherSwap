import pytest


def test_buff_page_expired_is_verification_required():
    from buff.buyer import _is_verification_required

    assert _is_verification_required({"code": "FAIL", "msg": "页面已过期，请刷新当前页面"})


def test_make_request_raises_verification_required(monkeypatch):
    from buff.buyer import BuffBuyer, BuffVerificationRequired

    class FakeResponse:
        status_code = 200
        text = '{"code":"FAIL","msg":"页面已过期，请刷新当前页面"}'

        def json(self):
            return {"code": "FAIL", "msg": "页面已过期，请刷新当前页面"}

    monkeypatch.setattr("requests.request", lambda *args, **kwargs: FakeResponse())
    buyer = BuffBuyer("csrf_token=abc")

    with pytest.raises(BuffVerificationRequired):
        buyer._make_request("POST", "https://buff.163.com/api/fake")

"""Single-attempt HTTPX market writes, preserving submission certainty."""
import httpx


class MarketRequestError(RuntimeError):
    """Safe for the operation journal: never carries a URL, Cookie or payload."""


def post_market(session, url, *, data, headers, timeout, outcome, operation='withdrawal'):
    label = '上架' if operation == 'listing' else '撤单'
    outcome.update(request_sent=False, phase='preflight', transport='httpx')
    # Validate and encode before recording a possibly sent request.
    prepared = session.build_request('POST', url, data=data, headers=headers)
    # Reads during inventory/pricing may rotate the CSRF cookie after the
    # caller captured its session ID. Match the cookie for this exact URL.
    session_ids = {
        value.strip() for part in prepared.headers.get('Cookie', '').split(';')
        for key, sep, value in [part.strip().partition('=')]
        if sep and key == 'sessionid' and value.strip()
    }
    if len(session_ids) > 1:
        raise MarketRequestError(f'{label}会话凭据冲突，尚未提交请求')
    if session_ids and 'sessionid' in data:
        data = {**data, 'sessionid': session_ids.pop()}
        session.build_request('POST', url, data=data, headers=headers)
    outcome.update(request_sent=True, phase=operation)
    try:
        return session.post(url, data=data, headers=headers, timeout=timeout, follow_redirects=False)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError):
        outcome.update(request_sent=False, phase='preflight')
        raise MarketRequestError(f'httpx {label}连接失败，尚未提交请求') from None
    except httpx.HTTPError as exc:
        raise MarketRequestError(f'httpx {label}请求异常（{type(exc).__name__}），结果待核实，勿立即重复提交') from None

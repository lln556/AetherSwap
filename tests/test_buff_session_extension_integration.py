"""Installed extension -> authenticated local HTTP -> real session import API."""
import json

from tests.test_buff_session_api import session_api
from tests.test_buff_extension import (
    _start_service, _prepare_test_extension, _loaded_extension,
    _authorize, _request_import_and_wait_confirmation, _wait_page_message,
)


def test_installed_extension_validates_and_saves_via_real_api(session_api, tmp_path):
    client, response, calls, account, policy, config = session_api
    service, server, origin = _start_service()
    def submit(body, headers):
        # Keep the real browser Origin/Host relationship at the ASGI seam.
        headers = {key.lower(): value for key, value in headers.items()}
        headers.pop('content-length', None)
        result = client.post('/api/auth/buff/session/import', json=body, headers=headers)
        return result.status_code, result.json()
    service.import_handler = submit
    secret = 'integration-cookie-not-for-page'
    extension = _prepare_test_extension(tmp_path)
    try:
        with _loaded_extension(extension, [origin], tmp_path / 'profile') as (context, worker, extension_id):
            context.add_cookies([
                {'name': 'session', 'value': secret, 'domain': 'buff.163.com', 'path': '/', 'secure': True, 'httpOnly': True},
                {'name': 'csrf_token', 'value': 'integration-csrf', 'domain': 'buff.163.com', 'path': '/', 'secure': True},
            ])
            page = context.new_page(); page.goto(origin)
            assert _authorize(context, extension_id, origin)['ok']
            confirmation = _request_import_and_wait_confirmation(context, extension_id, page, 'real-api-import')
            assert not config.get_buff().get('cookies')
            confirmation.locator('#confirm').click()
            result = _wait_page_message(page, 'RESULT', 'real-api-import')
            assert result['ok'], result
            assert result['session']['status'] == 'valid'
            assert isinstance(result['session']['checked_at'], (float, int))
            assert result['session']['can_replace'] is True
            assert config.get_buff()['source'] == 'extension'
            assert secret in config.get_buff()['cookies']
            assert len(calls) == 1
            assert secret not in json.dumps(page.evaluate('window.__aetherMessages'))
    finally:
        server.shutdown()

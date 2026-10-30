"""Explicit BUFF session checks; cached status is never proof of a new session."""
import copy
import hashlib
from contextlib import contextmanager
import json
import threading
import time
from pathlib import Path

from app import accounts
from app.config_loader import get_buff_credentials
from app.services.buff_auth import buff_credential_replacement_block_reason, get_buff_auth_lock
from utils.json_store import atomic_write_json, load_json

STATUS_PATH = Path(__file__).resolve().parents[2] / 'config' / 'buff_session_status.json'
_operation_lock = threading.Lock()
VALID_STATUS_SECONDS = 30 * 60


def _identity(credentials, account):
    return hashlib.sha256(json.dumps([
        credentials.get('cookies', ''), credentials.get('user_agent', ''), credentials.get('generation', 0),
        account.get('id', ''), account.get('steam_id', ''),
    ], ensure_ascii=False).encode()).hexdigest()


def _account():
    return copy.deepcopy(accounts.get_current_account() or {})


def session_status():
    credentials, account = get_buff_credentials(), _account()
    saved = load_json(STATUS_PATH, default={}) or {}
    if not isinstance(saved, dict):
        saved = {}
    configured = bool(str(credentials.get('cookies') or '').strip())
    result = {'status': 'unchecked' if configured else 'unconfigured', 'checked_at': None,
              'message': '凭据已配置，尚未检测' if configured else '尚未配置 BUFF 登录会话'}
    same_identity = configured and saved.get('identity') == _identity(credentials, account)
    if same_identity:
        result.update({k: saved[k] for k in ('status', 'checked_at', 'message', 'retry_after') if k in saved})
    if configured:
        from app.state import get_status
        from buff import get_global_policy, BuffRateLimited, BuffRequestBlocked
        from buff.request_policy import account_fingerprint
        flags = get_status() or {}
        newer_failure = not same_identity or saved.get('observed_auth_flags') != _auth_flags(flags)
        if flags.get('buff_auth_expired') and newer_failure:
            result.update(status='expired', message='后台检测到 BUFF 登录失效，请重新登录后导入')
        if flags.get('buff_verification_required') and newer_failure:
            result.update(status='verification', message='BUFF 需要安全验证，请在浏览器完成验证后主动导入')
        try:
            get_global_policy().raise_if_blocked(account_fingerprint({}, account_id='default'))
            if result['status'] == 'rate_limited':
                result.update(status='unchecked', message='请求冷却已结束，请主动检测登录')
                result.pop('retry_after', None)
        except BuffRateLimited as exc:
            result.update(status='rate_limited', message='BUFF 请求正在冷却，请稍后重试', retry_after=max(0, exc.retry_after))
        except BuffRequestBlocked:
            result.update(status='verification', message='BUFF 需要安全验证，请在浏览器完成验证后主动导入')
    if result['status'] == 'valid' and time.time() - (result.get('checked_at') or 0) >= VALID_STATUS_SECONDS:
        result.update(status='unchecked', message='上次验证已超过 30 分钟，请主动重新检测登录')
    reason = buff_credential_replacement_block_reason()
    result.update(can_replace=not bool(reason), replace_block_reason=reason)
    return result


def _auth_flags(flags):
    return {key: bool(flags.get(key)) for key in ('buff_auth_expired', 'buff_verification_required')}


def _remember(credentials, account, status, message, retry_after=None):
    from app.state import get_status, set_buff_auth_expired, set_buff_verification_required
    if status == 'expired':
        set_buff_auth_expired(True)
    elif status == 'verification':
        set_buff_verification_required(True, message)
    elif status == 'valid':
        set_buff_auth_expired(False)
        set_buff_verification_required(False)
    saved = {'identity': _identity(credentials, account), 'status': status, 'checked_at': time.time(),
             'message': message, 'observed_auth_flags': _auth_flags(get_status() or {})}
    if retry_after is not None:
        saved['retry_after'] = max(0, retry_after)
    atomic_write_json(STATUS_PATH, saved, indent=2)


class _ProbePolicy:
    """Single-use transport policy, never shared with a trading client."""
    def __init__(self, policy, recovery):
        self.policy, self.recovery, self.used = policy, recovery, False

    @contextmanager
    def request_slot(self, key):
        if self.used:
            raise RuntimeError('Only one recovery request is permitted')
        self.used = True
        slot = self.policy.verification_slot if self.recovery else self.policy.request_slot
        with slot(key):
            yield

    def __getattr__(self, name):
        # Errors must still trip the shared circuit; only the request slot differs.
        return getattr(self.policy, name)


def _probe(credentials, recovery=False):
    from buff import BuffBuyer, get_global_policy
    buyer = BuffBuyer(credentials.get('cookies') or '', user_agent=credentials.get('user_agent') or None,
                      request_policy=_ProbePolicy(get_global_policy(), recovery), account_id='default')
    try:
        verified = buyer.verify_session()
        return verified, str(buyer.steam_id or ''), buyer.export_cookie_string()
    finally:
        buyer.close()


def _outcome(exc):
    from buff import BuffAuthExpired, BuffRateLimited, BuffRequestBlocked
    if isinstance(exc, BuffAuthExpired):
        return 'expired', 'BUFF 登录已失效，请重新登录后导入', None
    if isinstance(exc, BuffRateLimited):
        return 'rate_limited', 'BUFF 请求正在冷却，请稍后重试', exc.retry_after
    if isinstance(exc, BuffRequestBlocked):
        return 'verification', 'BUFF 需要安全验证，请在浏览器完成验证后主动导入', None
    return 'error', 'BUFF 登录检测失败：网络或服务暂不可用，请稍后重试', None


def check_session():
    if not _operation_lock.acquire(blocking=False):
        return {'ok': False, 'code': 'busy', 'error': '正在检测或更新 BUFF 会话，请稍后查看结果', 'session': session_status()}
    auth_lock = get_buff_auth_lock()
    acquired = auth_lock.acquire(blocking=False)
    try:
        if not acquired:
            return {'ok': False, 'code': 'busy', 'error': 'BUFF 正在执行其他操作，请稍后检测', 'session': session_status()}
        credentials, account = get_buff_credentials(), _account()
        if not str(credentials.get('cookies') or '').strip():
            return {'ok': False, 'error': '尚未配置 BUFF 会话', 'session': session_status()}
        try:
            verified, steam_id, _ = _probe(credentials)
            if not verified:
                status, message, retry = 'error', 'BUFF 未返回可确认的登录信息', None
            elif not account.get('steam_id') or steam_id != str(account['steam_id']):
                status, message, retry = 'account_mismatch', 'BUFF 绑定的 Steam 账号与当前账号不一致或无法核实', None
            else:
                status, message, retry = 'valid', '最近检测有效', None
        except Exception as exc:
            status, message, retry = _outcome(exc)
        if _identity(credentials, account) != _identity(get_buff_credentials(), _account()):
            return {'ok': False, 'code': 'context_changed', 'error': '检测期间账号或凭据已变化，请重新检测', 'session': session_status()}
        try:
            _remember(credentials, account, status, message, retry)
        except Exception:
            return {'ok': False, 'code': 'status_save_failed', 'error': '检测结果无法保存，请稍后重试', 'session': session_status()}
        return {'ok': status == 'valid', 'session': session_status(), **({'error': message, 'code': status} if status != 'valid' else {})}
    finally:
        if acquired:
            auth_lock.release()
        _operation_lock.release()


def import_session(cookies, user_agent='', source='manual', recovery=False):
    from app.services.session_input import normalize_cookie_input, cookie_value, normalize_user_agent
    from app.config_loader import update_buff_creds
    from app.services.buff_checkout_guard import buff_activity_guard
    from app.services.buff_auth import clear_buff_request_policy_after_verification

    def failure(code, message):
        return {'ok': False, 'code': code, 'error': message, 'session': session_status()}

    cookies = normalize_cookie_input(cookies)
    if not cookie_value(cookies, 'session') or not cookie_value(cookies, 'csrf_token'):
        return failure('invalid_cookie', 'BUFF Cookie 必须包含 session 和 csrf_token')
    if source not in {'manual', 'extension', 'playwright', 'playwright_ephemeral'}:
        return failure('invalid_source', '不支持的会话导入来源')
    if not _operation_lock.acquire(blocking=False):
        return failure('busy', '正在检测或更新 BUFF 会话，请稍后重试')
    auth_lock = get_buff_auth_lock()
    acquired = auth_lock.acquire(blocking=False)
    try:
        if not acquired:
            return failure('busy', 'BUFF 正在执行其他操作，请稍后导入')
        with buff_activity_guard():
            reason = buff_credential_replacement_block_reason()
            if reason:
                return failure('checkout_pending', reason)
            before, account = get_buff_credentials(), _account()
            previous_status = load_json(STATUS_PATH, default={}) or {}
            if not account.get('steam_id'):
                return failure('account_mismatch', '请先选择并核实当前 Steam 账号，再导入 BUFF 会话')
            from buff.buyer import DEFAULT_USER_AGENT
            candidate = {'cookies': cookies, 'user_agent': normalize_user_agent(user_agent) or before.get('user_agent') or DEFAULT_USER_AGENT}
            try:
                verified, steam_id, rotated = _probe(candidate, recovery=recovery)
            except Exception as exc:
                status, message, retry = _outcome(exc)
                result = failure(status, message)
                if retry is not None:
                    result['retry_after'] = max(0, retry)
                return result
            if not verified:
                return failure('error', 'BUFF 未返回可确认的登录信息，原会话未改变')
            if steam_id != str(account['steam_id']):
                return failure('account_mismatch', '导入的 BUFF 绑定 Steam 账号与当前账号不一致或无法核实，原会话未改变')
            # Account switching uses this same lock. The authentication/activity
            # slots already prevent buy startup or a checkout during replacement.
            with accounts.current_account_guard():
                if _identity(before, account) != _identity(get_buff_credentials(), _account()):
                    return failure('context_changed', '验证期间账号或凭据已变化，原会话未改变')
                reason = buff_credential_replacement_block_reason()
                if reason:
                    return failure('checkout_pending', reason)
                try:
                    update_buff_creds(rotated or cookies, user_agent=candidate['user_agent'] or None, source=source)
                except Exception:
                    return failure('save_failed', '会话保存失败，请重试；未启动买入')
                if not clear_buff_request_policy_after_verification():
                    # Keep the circuit closed and restore the previous credentials.
                    try:
                        update_buff_creds(before.get('cookies') or '', user_agent=before.get('user_agent') or '', source=before.get('source') or 'manual')
                    except Exception:
                        result = failure('rollback_failed', '会话已保存但安全限制未解除，请核实；未启动买入')
                        result['credentials_saved'] = True
                        return result
                    if isinstance(previous_status, dict) and previous_status.get('identity') == _identity(before, account):
                        previous_status['identity'] = _identity(get_buff_credentials(), account)
                        try:
                            atomic_write_json(STATUS_PATH, previous_status, indent=2)
                        except Exception:
                            pass  # Unknown status is safer than retaining the candidate result.
                    return failure('verification', '安全限制无法解除，已恢复原凭据；未启动买入')
                try:
                    _remember(get_buff_credentials(), account, 'valid', '会话已验证并保存；未启动买入')
                except Exception:
                    current = session_status()
                    current['message'] = '会话已验证并保存，但检测记录无法持久化；可重新检测，未启动买入'
                    return {'ok': True, 'credentials_saved': True, 'session': current, 'warning': 'status_save_failed'}
            return {'ok': True, 'session': session_status()}
    finally:
        if acquired:
            auth_lock.release()
        _operation_lock.release()

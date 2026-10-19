import asyncio
import threading
import time
from unittest.mock import Mock

import pytest


@pytest.fixture
def lifecycle(monkeypatch):
    from app.services import lifecycle
    monkeypatch.setattr(lifecycle, '_shutdown', threading.Event())
    return lifecycle


def test_service_shutdown_wakes_long_wait_and_joins_workers_before_exit(monkeypatch, lifecycle):
    from app import api, pipeline
    from app.services.task_queue import TaskQueue
    q = TaskQueue(max_workers=1)
    started, finished = threading.Event(), threading.Event()
    calls = []

    def worker():
        started.set()
        assert lifecycle.wait_for_shutdown(3600)
        finished.set()

    def start():
        q.submit(worker)
        assert started.wait(2)

    monkeypatch.setattr(api, '_start_background_workers', start)
    monkeypatch.setattr(api, 'get_task_queue', lambda: q)
    monkeypatch.setattr(api, 'request_stop', lambda: calls.append('stop-pipeline'))
    monkeypatch.setattr(pipeline, 'mark_shutdown_pending', lambda: calls.append('block-starts'))
    monkeypatch.setattr(pipeline, 'wait_for_pipeline_exit', lambda: calls.append('join-pipeline'))

    async def run():
        async with api._lifespan(api.app):
            assert not lifecycle.is_shutting_down()
        assert finished.is_set()
        assert q.active_count() == 0
        assert all(not t.is_alive() for t in q._executor._threads)

    try:
        asyncio.run(run())
    finally:
        lifecycle.begin_shutdown()
        q.shutdown()
    assert calls == ['block-starts', 'stop-pipeline', 'join-pipeline']


def test_queue_cancels_queued_work_and_rejects_new_submissions(lifecycle):
    from app.services.task_queue import TaskQueue
    q = TaskQueue(max_workers=1)
    started, release = threading.Event(), threading.Event()
    pending = Mock()

    def busy():
        started.set()
        release.wait(3)

    try:
        q.submit(busy)
        assert started.wait(2)
        task_id = q.submit(pending, name='pending')
        q.shutdown(wait=False)
        assert q.get_task(task_id)['status'] == 'cancelled'
        with pytest.raises(RuntimeError, match='shutting down'):
            q.submit(pending, name='rejected')
        pending.assert_not_called()
    finally:
        release.set()
        q.shutdown()


def test_queue_shutdown_interrupts_retry_backoff(lifecycle):
    from app.services.task_queue import TaskQueue
    q = TaskQueue(max_workers=1)
    called = threading.Event()

    def fail():
        called.set()
        raise ValueError('test failure')

    task_id = q.submit(fail, max_retries=3, retry_base_delay=300)
    try:
        assert called.wait(2)
        before = time.monotonic()
        q.shutdown()
        assert time.monotonic() - before < 2
        assert q.get_task(task_id)['attempts'] == 1
        assert q.get_task(task_id)['status'] == 'cancelled'
    finally:
        q.shutdown()


@pytest.mark.parametrize('worker_name', [
    'receive_worker', 'listing_check_worker', 'holdings_report_worker',
    'exchange_rate_worker', 'session_keepalive_worker', 'steam_session_worker',
    'sync_account_region_worker',
])
def test_workers_do_not_start_new_round_after_shutdown(monkeypatch, lifecycle, worker_name):
    from app.services import workers
    lifecycle.begin_shutdown()
    read = Mock(side_effect=AssertionError('no new work during shutdown'))
    monkeypatch.setattr(workers, 'load_app_config_validated', read)
    monkeypatch.setattr(workers, 'get_current_account', read)
    getattr(workers, worker_name)()
    read.assert_not_called()


@pytest.mark.parametrize('failure', [
    RuntimeError('cannot schedule new futures after interpreter shutdown'),
    ValueError('failed read during service shutdown'),
])
def test_receive_worker_exits_and_closes_client_without_shutdown_alert(monkeypatch, lifecycle, failure):
    from app.services import workers, buff_client
    buyer = Mock()
    alert = Mock()
    monkeypatch.setattr(workers, 'load_app_config_validated', lambda: {})
    monkeypatch.setattr(workers, 'get_buff_credentials', lambda: {'cookies': 'test'})
    monkeypatch.setattr(workers, 'is_steam_background_allowed', lambda: True)
    monkeypatch.setattr(workers, '_buff_background_request_is_safe', lambda: True)
    monkeypatch.setattr(workers, 'wait_for_shutdown', lambda _: False)
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *_: buyer)
    monkeypatch.setattr(workers, '_worker_alert', alert)

    def fail(*args, **kwargs):
        if isinstance(failure, ValueError):
            lifecycle.begin_shutdown()
        raise failure

    monkeypatch.setattr(workers, 'run_receive_check_once', fail)
    workers.receive_worker()
    alert.assert_not_called()
    buyer.close.assert_called_once()


def test_real_worker_failure_is_still_reported_and_retried(monkeypatch, lifecycle):
    from app.services import workers, buff_client
    buyer, alert = Mock(), Mock()
    waits = []
    monkeypatch.setattr(workers, 'load_app_config_validated', lambda: {})
    monkeypatch.setattr(workers, 'get_buff_credentials', lambda: {'cookies': 'test'})
    monkeypatch.setattr(workers, 'is_steam_background_allowed', lambda: True)
    monkeypatch.setattr(workers, '_buff_background_request_is_safe', lambda: True)
    monkeypatch.setattr(buff_client, 'create_buff_client_from_config', lambda *_: buyer)
    monkeypatch.setattr(workers, '_worker_alert', alert)
    monkeypatch.setattr(workers, 'run_receive_check_once', Mock(side_effect=ValueError('real failure')))

    def wait(seconds):
        waits.append(seconds)
        return len(waits) > 1

    monkeypatch.setattr(workers, 'wait_for_shutdown', wait)
    workers.receive_worker()
    alert.assert_called_once()
    assert waits == [30, 60]
    buyer.close.assert_called_once()


def test_shutdown_alert_is_not_sent_but_other_runtime_errors_still_are(monkeypatch, lifecycle):
    from app.services import workers
    send = Mock(return_value=True)
    monkeypatch.setattr(workers, 'send_pushplus', send)
    monkeypatch.setattr(workers, 'load_app_config_validated', lambda: {'notify': {'pushplus_token': 'test-token'}})
    monkeypatch.setattr(workers, '_worker_alert_last', {})
    workers._worker_alert('receive_worker', RuntimeError('cannot schedule new futures after interpreter shutdown'))
    send.assert_not_called()
    workers._worker_alert('receive_worker', RuntimeError('unrelated failure'))
    send.assert_called_once()
    assert '60 秒后重试' in send.call_args.args[2]


def test_service_process_exits_on_sigterm_without_executor_alert(tmp_path):
    import os
    from pathlib import Path
    import signal
    import socket
    import subprocess
    import sys
    import httpx

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    script = '''
import uvicorn
from app import api
from app.services import workers, trading_worker
workers.load_app_config_validated = lambda: {}
workers.get_current_account = lambda: None
workers.get_buff_credentials = lambda: {}
workers.get_steam_credentials = lambda: {}
workers.send_pushplus = lambda *a, **kw: print('UNEXPECTED_ALERT', flush=True)
trading_worker.run_trading_cycle = lambda: {'ok': True, 'sold': 0}
stop_workers = api._stop_background_workers
def verified_shutdown():
    stop_workers()
    queue = api.get_task_queue()
    assert queue.active_count() == 0
    assert all(not thread.is_alive() for thread in queue._executor._threads)
    print('CLEAN_SHUTDOWN', flush=True)
api._stop_background_workers = verified_shutdown
uvicorn.run(api.app, host='127.0.0.1', port=PORT, log_level='error')
'''.replace('PORT', str(port))
    env = {**os.environ, 'AETHERSWAP_DATABASE_URL': f'sqlite:///{tmp_path / "shutdown.db"}',
           'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
    process = subprocess.Popen([sys.executable, '-u', '-c', script], cwd=tmp_path,
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        ready = False
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            for _ in range(100):
                if process.poll() is not None:
                    break
                try:
                    ready = client.get(f'http://127.0.0.1:{port}/api/runtime').status_code == 200
                except httpx.RequestError:
                    pass
                if ready:
                    break
                time.sleep(0.05)
        assert ready, 'isolated service did not start'
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=10)
        # Uvicorn re-raises the captured signal after lifespan cleanup.
        assert process.returncode in (0, -signal.SIGTERM), output
        assert 'CLEAN_SHUTDOWN' in output, output
        assert 'UNEXPECTED_ALERT' not in output
        assert 'cannot schedule new futures' not in output
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

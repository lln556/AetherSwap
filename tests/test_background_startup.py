import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_task_queue_has_spare_capacity_for_startup_workers(monkeypatch):
    from app.services import task_queue

    monkeypatch.setattr(task_queue, "_queue", None)
    q = task_queue.get_task_queue()

    assert q._executor._max_workers >= 8


def test_region_sync_is_submitted_before_long_running_workers(monkeypatch):
    from app import api

    submitted = []

    class FakeQueue:
        def submit(self, fn, *args, name="", max_retries=0, retry_base_delay=0, **kwargs):
            submitted.append(name or fn.__name__)
            return name or fn.__name__

    monkeypatch.setattr(api, "_bg_started", False)
    monkeypatch.setattr(api, "get_task_queue", lambda: FakeQueue())

    api._start_background_workers()

    assert submitted[0] == "sync_account_region"
    assert "session_keepalive_worker" in submitted


def test_wait_for_server_ready_probes_localhost_for_wildcard_host(monkeypatch):
    from app import main

    calls = []

    class FakeThread:
        def is_alive(self):
            return True

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    def fake_create_connection(address, timeout=0):
        calls.append((address, timeout))
        return FakeSocket()

    monkeypatch.setattr(main.socket, "create_connection", fake_create_connection)

    assert main._wait_for_server_ready(FakeThread(), "0.0.0.0", 28472, timeout=0.1) is True
    assert calls[0][0] == ("127.0.0.1", 28472)


def test_wait_for_server_ready_stops_when_server_thread_exits(monkeypatch):
    from app import main

    class DeadThread:
        def is_alive(self):
            return False

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not probe a stopped server thread")

    monkeypatch.setattr(main.socket, "create_connection", fail_if_called)

    assert main._wait_for_server_ready(DeadThread(), "127.0.0.1", 28472, timeout=0.1) is False


def test_region_sync_worker_does_not_sleep_before_missing_account_skip(monkeypatch):
    from app.services import workers

    monkeypatch.setattr(workers, "get_current_account", lambda: None)
    monkeypatch.setattr(workers, "log", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        workers.time,
        "sleep",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("startup sync should not pre-sleep")),
    )

    workers.sync_account_region_worker()

"""Process-wide cooperative shutdown, separate from pausing the buy pipeline."""
import threading

_shutdown = threading.Event()


def begin_shutdown():
    _shutdown.set()


def is_shutting_down():
    return _shutdown.is_set()


def wait_for_shutdown(seconds):
    """Interrupt long worker intervals immediately when the service stops."""
    return _shutdown.wait(max(0, seconds))


def is_shutdown_error(error):
    return is_shutting_down() or (
        isinstance(error, RuntimeError)
        and str(error) == 'cannot schedule new futures after interpreter shutdown'
    )

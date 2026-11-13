"""Run blocking endpoint calls off the UI thread, deliver results on it."""

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Qt, Signal

_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ui-io")


class _Bridge(QObject):
    done = Signal(object, object, object)  # callback, result, exception


_bridge = None
_closing = False


def shutdown():
    """At quit: stop delivering results to (soon to be destroyed) widgets."""
    global _closing
    _closing = True
    _pool.shutdown(wait=False, cancel_futures=True)


def _ensure():
    global _bridge
    if _bridge is None:
        _bridge = _Bridge()
        _bridge.done.connect(lambda cb, r, e: cb(r, e), Qt.QueuedConnection)
    return _bridge


def run_async(fn, callback):
    """fn() runs in a worker thread; callback(result, exc) runs on the UI thread."""
    b = _ensure()

    def task():
        try:
            r, ex = fn(), None
        except Exception as e:  # delivered to the callback
            r, ex = None, e
        if not _closing:
            b.done.emit(callback, r, ex)

    if not _closing:
        _pool.submit(task)

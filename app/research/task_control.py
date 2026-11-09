"""Cooperative cancellation for local, single-process research workers."""
from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager


class TaskCancelled(Exception):
    pass


_current = contextvars.ContextVar('research_cancel_event', default=None)
_lock = threading.Lock()
_events: dict[str, threading.Event] = {}


@contextmanager
def task_scope(task_id: str):
    with _lock:
        event = _events.setdefault(task_id, threading.Event())
    token = _current.set(event)
    try:
        yield
    finally:
        _current.reset(token)
        with _lock:
            if _events.get(task_id) is event:
                _events.pop(task_id, None)


def request_cancel(task_id: str) -> bool:
    with _lock:
        event = _events.get(task_id)
        if event is None:
            return False
        event.set()
        return True


def in_task() -> bool:
    return _current.get() is not None


def check_cancelled() -> None:
    event = _current.get()
    if event is not None and event.is_set():
        raise TaskCancelled('用户请求取消')

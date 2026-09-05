"""In-memory registry of long-running background tasks (receive / classify /
reclassify), with pause + cancel controls.

Design:
- One task per (user, kind) at most. Starting a new task of the same kind
  while one is already running returns the existing task instead of a new
  one (so a stray click doesn't spawn a duplicate).
- Task state lives entirely in-process. On backend restart, all in-flight
  tasks are lost — the desktop UI treats "no active task" as the default,
  so a restart is graceful.
- Progress is a small dict the worker updates in place; the HTTP layer
  polls a snapshot.
- Pause is a threading.Event: set = running, clear = paused. Workers call
  `control.check()` at safe points; check() blocks until resumed and
  raises TaskCancelled if the task was cancelled while paused.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


class TaskCancelled(Exception):
    """Raised inside a worker when the task was cancelled. Workers should
    let this propagate; the runner catches it and marks the task done."""


@dataclass
class TaskControl:
    """Handed to workers so they can cooperatively yield to pause / cancel
    without importing the registry."""

    pause_event: threading.Event
    cancel_event: threading.Event
    on_paused_state_change: Optional[Callable[[bool], None]] = None

    def check(self) -> None:
        """Call this at a safe interruption point. Blocks while paused;
        raises TaskCancelled if the task was cancelled."""
        if self.cancel_event.is_set():
            raise TaskCancelled()
        if not self.pause_event.is_set():
            if self.on_paused_state_change:
                try:
                    self.on_paused_state_change(True)
                except Exception:
                    pass
            # Wake up periodically so a cancel-while-paused breaks the wait
            # promptly (Event.wait with no timeout is uninterruptible).
            while not self.pause_event.wait(timeout=0.5):
                if self.cancel_event.is_set():
                    if self.on_paused_state_change:
                        try:
                            self.on_paused_state_change(False)
                        except Exception:
                            pass
                    raise TaskCancelled()
            if self.on_paused_state_change:
                try:
                    self.on_paused_state_change(False)
                except Exception:
                    pass
        if self.cancel_event.is_set():
            raise TaskCancelled()

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set()


@dataclass
class Task:
    id: str
    kind: str  # "receive" | "classify_unsorted" | "reclassify_all"
    owner: str  # user_id
    label: str
    status: str = "running"  # running | paused | done | error | cancelled
    progress: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: Optional[float] = None
    pause_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        # pause_event.set() = running (not paused). Workers block on
        # pause_event.wait() to pause.
        self.pause_event.set()

    def snapshot(self) -> Dict[str, Any]:
        """JSON-serialisable view of the task's current state."""
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "progress": dict(self.progress),
            "result": dict(self.result),
            "error": self.error,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
        }


_lock = threading.RLock()
_tasks: Dict[str, Task] = {}


TERMINAL_STATUSES = {"done", "error", "cancelled"}


def _is_terminal(t: Task) -> bool:
    return t.status in TERMINAL_STATUSES


def get_task(task_id: str) -> Optional[Task]:
    with _lock:
        return _tasks.get(task_id)


def active_for(owner: str, kind: Optional[str] = None) -> Optional[Task]:
    """Return the most recently started non-terminal task for this owner
    (optionally filtered by kind). None if no active task."""
    with _lock:
        candidates = [
            t for t in _tasks.values()
            if t.owner == owner
            and not _is_terminal(t)
            and (kind is None or t.kind == kind)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda t: t.started_at, reverse=True)
        return candidates[0]


def latest_for(owner: str) -> Optional[Task]:
    """Return the most recently updated task for this owner in ANY state
    (running or terminal). Used by the UI to show a finished task's summary
    after the worker exits, before the user acknowledges it."""
    with _lock:
        candidates = [t for t in _tasks.values() if t.owner == owner]
        if not candidates:
            return None
        candidates.sort(key=lambda t: t.updated_at, reverse=True)
        return candidates[0]


def create_task(*, kind: str, owner: str, label: str) -> Task:
    """Create + register a fresh task. Caller then spawns the worker
    thread via `run_task(...)`."""
    task = Task(
        id=uuid.uuid4().hex,
        kind=kind,
        owner=owner,
        label=label,
    )
    with _lock:
        _tasks[task.id] = task
        _prune_locked()
    return task


def set_progress(task_id: str, **fields: Any) -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        for k, v in fields.items():
            t.progress[k] = v
        t.updated_at = time.time()


def set_status(task_id: str, status: str) -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t.status = status
        t.updated_at = time.time()
        if status in TERMINAL_STATUSES and t.finished_at is None:
            t.finished_at = t.updated_at


def set_result(task_id: str, **fields: Any) -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        for k, v in fields.items():
            t.result[k] = v
        t.updated_at = time.time()


def set_error(task_id: str, message: str) -> None:
    with _lock:
        t = _tasks.get(task_id)
        if not t:
            return
        t.error = message
        t.status = "error"
        t.updated_at = time.time()
        if t.finished_at is None:
            t.finished_at = t.updated_at


def pause(task_id: str) -> bool:
    with _lock:
        t = _tasks.get(task_id)
        if not t or _is_terminal(t):
            return False
        t.pause_event.clear()
        t.status = "paused"
        t.updated_at = time.time()
        return True


def resume(task_id: str) -> bool:
    with _lock:
        t = _tasks.get(task_id)
        if not t or _is_terminal(t):
            return False
        t.pause_event.set()
        t.status = "running"
        t.updated_at = time.time()
        return True


def cancel(task_id: str) -> bool:
    with _lock:
        t = _tasks.get(task_id)
        if not t or _is_terminal(t):
            return False
        t.cancel_event.set()
        # Un-pause so a paused worker wakes up to notice the cancel.
        t.pause_event.set()
        # Note: status stays "running" (or "paused") until the worker
        # actually exits. The runner sets it to "cancelled" then.
        t.updated_at = time.time()
        return True


def acknowledge(task_id: str) -> bool:
    """Remove a terminal task from the registry so it stops appearing to
    the UI. Silently no-op for a non-terminal task — you must cancel + let
    it exit first."""
    with _lock:
        t = _tasks.get(task_id)
        if not t or not _is_terminal(t):
            return False
        _tasks.pop(task_id, None)
        return True


def _prune_locked() -> None:
    """Drop terminal tasks older than 1 hour so the registry doesn't grow
    unbounded across a long-lived process."""
    now = time.time()
    to_remove: List[str] = []
    for tid, t in _tasks.items():
        if _is_terminal(t) and t.finished_at and now - t.finished_at > 3600:
            to_remove.append(tid)
    for tid in to_remove:
        _tasks.pop(tid, None)


def make_control(task: Task) -> TaskControl:
    """Build a TaskControl that flips the task's status ↔ paused as the
    worker checks in and out of the pause point."""
    task_id = task.id

    def _flip(is_paused: bool) -> None:
        # Only update if not cancelled (cancelling wins over pause state).
        with _lock:
            t = _tasks.get(task_id)
            if not t or _is_terminal(t):
                return
            if is_paused:
                t.status = "paused"
            else:
                t.status = "running"
            t.updated_at = time.time()

    return TaskControl(
        pause_event=task.pause_event,
        cancel_event=task.cancel_event,
        on_paused_state_change=_flip,
    )


def run_task(task: Task, worker: Callable[[TaskControl], Dict[str, Any]]) -> None:
    """Spawn `worker` on a daemon thread. The worker receives a TaskControl
    and returns a result dict — which becomes `task.result`. Exceptions
    become `task.error`; TaskCancelled becomes status='cancelled'."""

    def _run() -> None:
        control = make_control(task)
        try:
            result = worker(control)
            with _lock:
                if _tasks.get(task.id) is task:
                    if isinstance(result, dict):
                        task.result.update(result)
                    task.status = "done"
                    task.finished_at = time.time()
                    task.updated_at = task.finished_at
        except TaskCancelled:
            with _lock:
                if _tasks.get(task.id) is task:
                    task.status = "cancelled"
                    task.finished_at = time.time()
                    task.updated_at = task.finished_at
        except Exception as exc:
            with _lock:
                if _tasks.get(task.id) is task:
                    task.error = f"{exc.__class__.__name__}: {exc}"
                    task.status = "error"
                    task.finished_at = time.time()
                    task.updated_at = task.finished_at

    threading.Thread(target=_run, daemon=True, name=f"task-{task.kind}-{task.id[:8]}").start()

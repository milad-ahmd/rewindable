"""Exceptions raised by rewindable."""

from __future__ import annotations


class RewindableError(Exception):
    """Base class for every error raised by rewindable."""


class RunNotFound(RewindableError):
    """No run with the given id exists in the journal."""


class NonDeterminismError(RewindableError):
    """The workflow called a different step than the journal recorded at this position.

    Replay works by matching step calls, in order, against the journal. If the code
    now calls ``fetch`` where the journal says ``summarize`` happened, the cached
    results no longer line up with the code, so replay stops instead of guessing.
    Use ``rewind`` to branch a new run from the last step that still matches.
    """


class BudgetExceeded(RewindableError):
    """The run hit one of its budget limits before making another model call."""


class NestedStepError(RewindableError):
    """A step function tried to open another step.

    Step bodies are opaque to the journal: when a completed step is replayed its
    function is not called at all, so any steps it opened would vanish and shift
    every later step out of position. Open steps from the workflow body (or from
    a ``ctx.parallel`` branch) instead.
    """


class WorkflowNotFound(RewindableError):
    """The workflow recorded for a run could not be imported."""


class RunLocked(RewindableError):
    """Another live process is driving this run right now.

    Each driver holds a lease that it renews while it works. Leases of crashed
    processes expire on their own (immediately, for a dead process on the same
    machine); pass ``force=True`` to take over anyway.
    """


class Suspended(BaseException):
    """Internal control-flow signal: the run is waiting on signals and/or a timer.

    It derives from ``BaseException`` so that a broad ``except Exception`` in user
    code does not swallow it.
    """

    def __init__(self, keys: list[str], wake_at: float | None = None):
        super().__init__(", ".join(keys) or f"timer until {wake_at}")
        self.keys = keys
        self.wake_at = wake_at

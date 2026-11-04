"""rewindable: crash-proof, resumable, rewindable AI agent workflows.

Every model call, tool call and side effect is a step in a SQLite journal.
Crash at step 7, resume, and steps 1-6 come back from the journal for free.
"""

from .agent import Agent, AgentResult, Tool, tool
from .diff import RunDiff, StepDiff, diff_runs
from .errors import (
    ApprovalCodeError,
    BudgetExceeded,
    NestedStepError,
    NonDeterminismError,
    RewindableError,
    RunLocked,
    RunNotFound,
    WorkflowNotFound,
)
from .models import AnthropicModel, Model, OpenAICompatModel, ScriptedModel, call, calls, text_of
from .notify import webhook
from .runtime import Approval, Budget, CheckResult, Context, Engine, RunResult, StepInfo, Waiting, current_step
from .store import Store

__version__ = "0.3.0"

__all__ = [
    "Agent",
    "AgentResult",
    "AnthropicModel",
    "Approval",
    "ApprovalCodeError",
    "Budget",
    "BudgetExceeded",
    "CheckResult",
    "Context",
    "Engine",
    "Model",
    "NestedStepError",
    "NonDeterminismError",
    "OpenAICompatModel",
    "RewindableError",
    "RunDiff",
    "RunLocked",
    "RunNotFound",
    "RunResult",
    "ScriptedModel",
    "StepDiff",
    "StepInfo",
    "Store",
    "Tool",
    "Waiting",
    "WorkflowNotFound",
    "call",
    "calls",
    "current_step",
    "diff_runs",
    "text_of",
    "tool",
    "webhook",
]

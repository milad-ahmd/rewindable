"""rewindable: crash-proof, resumable, rewindable AI agent workflows.

Every model call, tool call and side effect is a step in a SQLite journal.
Crash at step 7, resume, and steps 1-6 come back from the journal for free.
"""

from .agent import Agent, AgentResult, Tool, tool
from .errors import (
    BudgetExceeded,
    NestedStepError,
    NonDeterminismError,
    RewindableError,
    RunLocked,
    RunNotFound,
    WorkflowNotFound,
)
from .models import AnthropicModel, Model, OpenAICompatModel, ScriptedModel, call, calls, text_of
from .runtime import Approval, Budget, Context, Engine, RunResult, StepInfo, current_step
from .store import Store

__version__ = "0.2.0"

__all__ = [
    "Agent",
    "AgentResult",
    "AnthropicModel",
    "Approval",
    "Budget",
    "BudgetExceeded",
    "Context",
    "Engine",
    "Model",
    "NestedStepError",
    "NonDeterminismError",
    "OpenAICompatModel",
    "RewindableError",
    "RunLocked",
    "RunNotFound",
    "RunResult",
    "ScriptedModel",
    "StepInfo",
    "Store",
    "Tool",
    "WorkflowNotFound",
    "call",
    "calls",
    "current_step",
    "text_of",
    "tool",
]

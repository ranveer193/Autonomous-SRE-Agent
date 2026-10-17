"""budget: an enforced cap on investigation tool calls per incident.

Prompts say "at most 8 tool calls" but nothing enforced it. With the feature on, every READ tool call (metrics, logs, traces,
cluster reads) counts toward one budget shared by all subagents; past the cap the tool is not run and the model is told to conclude
from what it has and to say what it did not check. Never counted and never blocked: writes, verify_resolved, current_problems,
memory tools, the plan, file reads and subagent hand-offs. A fix, its verification and the final answer can therefore never be
starved by the budget.

`state()` is returned with every run (used / cap / blocked), so an eval shows whether the cap ever bit. If `blocked` is > 0 in runs
that also lost accuracy, the cap is too low. The default (30) is a starting point: set it per run (console Advanced options, `--tuning tool_budget=N`, SRE_TOOL_BUDGET) from the\n`tool_calls` of a baseline arm."""
import threading

from langchain_core.messages import ToolMessage
from langchain.agents.middleware.types import AgentMiddleware

from . import tuning

DEFAULT_CAP = tuning.Tuning().tool_budget
_lock = threading.Lock()
_S = {"used": 0, "blocked": 0}
READ_TOOLS: frozenset = frozenset()          # filled by agent.py from the real tool lists, so a new read tool is counted automatically

MESSAGE = ("BUDGET REACHED: the investigation tool-call budget for this incident ({cap}) is used up and this call was NOT run. "
           "Do not call more read tools. Answer now from the evidence you already have, and list what you did not get to check.")


def cap() -> int:
    """The run's tuning (console / --tuning), else SRE_TOOL_BUDGET, else the default."""
    return tuning.active().tool_budget


def reset() -> None:
    with _lock:
        _S.update(used=0, blocked=0)


def state() -> dict:
    with _lock:
        return {**_S, "cap": cap()}


def take(tool_name: str) -> bool:
    """True = run the call. Counts it when it is a read tool. Pure bookkeeping, thread safe (inspectors run in parallel)."""
    if tool_name not in READ_TOOLS:
        return True
    with _lock:
        if _S["used"] >= cap():
            _S["blocked"] += 1
            return False
        _S["used"] += 1
        return True


class ToolBudget(AgentMiddleware):
    @property
    def name(self) -> str:
        return "ToolBudget"

    def wrap_tool_call(self, request, handler):
        name = request.tool_call.get("name", "")
        if take(name):
            return handler(request)
        return ToolMessage(content=MESSAGE.format(cap=cap()), tool_call_id=request.tool_call.get("id", ""), name=name, status="error")

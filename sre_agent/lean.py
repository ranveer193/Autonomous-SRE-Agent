"""lean_tools: send the model only the tools its phase needs, with the long built-in descriptions shortened.

Every model call re-sends every tool definition. deepagents gives EVERY agent, subagents included, seven file tools
(ls, read_file, write_file, edit_file, delete, glob, grep) and gives the orchestrator a todo tool whose description alone is ~1k tokens.
An inspector that only queries metrics or logs never touches files. This middleware filters the list at request time:

  * the model is never shown the dropped tools (saves their definition on every call);
  * nothing else changes: same prompts, same data tools, same approval gate on writes;
  * a dropped tool is still registered, so a stray call by name still executes harmlessly inside the sandbox folder.

Pure and stateless; the agent builder adds it only when the feature is on."""
from langchain.agents.middleware.types import AgentMiddleware

FILE_TOOLS = frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep"})
# The orchestrator reads runbooks (ls + read_file) and nothing else in the file system.
ORCHESTRATOR_DROP = frozenset({"write_file", "edit_file", "delete", "glob", "grep"})
SUBAGENT_DROP = FILE_TOOLS

TODO_DESCRIPTION = ("Create or update your plan. Send the FULL list each time: todos=[{content, status}], status = pending | in_progress | "
                    "completed. Only for work with 3+ steps. Mark a step completed as soon as it is done; keep one in_progress.")
SHORT_DESCRIPTIONS = {"write_todos": TODO_DESCRIPTION}


def _name(t) -> str | None:
    n = t.get("name") if isinstance(t, dict) else getattr(t, "name", None)
    return n if isinstance(n, str) else None


def trim_tools(tools: list, drop: frozenset, shorten: dict) -> list:
    """Pure. Returns the tools without `drop`, with the descriptions in `shorten` replaced (argument schemas are untouched)."""
    out = []
    for t in tools:
        n = _name(t)
        if n in drop:
            continue
        if n in shorten and hasattr(t, "model_copy"):
            t = t.model_copy(update={"description": shorten[n]})
        out.append(t)
    return out


class LeanTools(AgentMiddleware):
    def __init__(self, drop: frozenset = ORCHESTRATOR_DROP, shorten: dict | None = None):
        super().__init__()
        self._drop, self._shorten = frozenset(drop), dict(SHORT_DESCRIPTIONS if shorten is None else shorten)

    @property
    def name(self) -> str:                       # one instance per agent; names must be unique inside one agent
        return "LeanTools"

    def wrap_model_call(self, request, handler):
        return handler(request.override(tools=trim_tools(list(request.tools), self._drop, self._shorten)))

    async def awrap_model_call(self, request, handler):
        return await handler(request.override(tools=trim_tools(list(request.tools), self._drop, self._shorten)))

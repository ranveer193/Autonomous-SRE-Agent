import os
from functools import wraps

MAX_CHARS = int(os.getenv("SRE_TOOL_MAX_CHARS", "1800"))   # hard cap on any tool result entering the context


def clip(s: str, n: int = MAX_CHARS) -> str:
    s = str(s)
    if len(s) <= n:
        return s
    head, tail = int(n * 0.65), int(n * 0.30)
    return f"{s[:head]}\n...[{len(s) - head - tail} chars cut; narrow the query for more]...\n{s[-tail:]}"


def bounded(fn):
    """Put under @tool: caps the tool's output so context cannot blow up."""
    @wraps(fn)
    def w(*a, **k):
        return clip(fn(*a, **k))
    return w

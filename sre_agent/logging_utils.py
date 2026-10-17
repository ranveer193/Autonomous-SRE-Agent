"""Readable trace of what the agent does: every LLM call, tool call, subagent hand-off, plan update and approval.
Console: concise, indented by subagent depth. File: full JSONL in logs/ (SRE_LOG_DIR) for later analysis.
Env: SRE_LOG_LEVEL=INFO|DEBUG (DEBUG also prints reasoning + full tool output), SRE_LOG_DIR=logs"""
import json, logging, os, re, sys, threading, time
from collections import Counter
from datetime import datetime
from pathlib import Path
from langchain_core.callbacks import BaseCallbackHandler

log = logging.getLogger("sre")


def setup_logging(run_name: str = "run") -> Path:
    level = os.getenv("SRE_LOG_LEVEL", "INFO").upper()
    log.setLevel(level)
    log.handlers.clear()
    for stream in (sys.stdout, sys.stderr):          # Windows consoles default to cp1252 and choke on emoji
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
    log.addHandler(h)
    log.propagate = False
    for noisy in ("httpx", "httpcore", "chromadb", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    d = Path(os.getenv("SRE_LOG_DIR", "logs"))
    d.mkdir(exist_ok=True)
    return d / f"{datetime.now():%Y%m%dT%H%M%S}-{run_name}.jsonl"


_SECRET_KV = re.compile(r"(?i)((?:api[_-]?key|token|secret|passwd|password|authorization|bearer)[\"'\\\s:=]+)([^\s\"',;}\\]{6,})")
_SECRET_TOKEN = re.compile(r"\b(?:gsk_|sk-|hf_|AIza|ghp_|xox[bap]-)[A-Za-z0-9_\-]{16,}")


def redact(s: str) -> str:
    """Strip credentials from anything written to the console or the trace file (tool output can contain env values)."""
    return _SECRET_TOKEN.sub("[redacted]", _SECRET_KV.sub(lambda m: m.group(1) + "[redacted]", s))


def _rid(run_id) -> str:
    return str(run_id)[:8]


def _short(x, n=300) -> str:
    s = x if isinstance(x, str) else json.dumps(x, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def _tool_output(output):
    """Tool result as plain content. Subagent ('task') tools return a langgraph Command, not a message:
    take the last message from its state update."""
    if hasattr(output, "update"):                                  # Command(update={"messages": [...]})
        upd = output.update
        msgs = upd.get("messages") if isinstance(upd, dict) else None
        return getattr(msgs[-1], "content", "") if msgs else str(upd)
    return getattr(output, "content", output)


class AgentLogger(BaseCallbackHandler):
    """Pass in config={'callbacks': [AgentLogger(path)]}; LangGraph propagates it to subagents and tools."""
    raise_error = False

    def __init__(self, jsonl_path: Path | None = None):
        self.path, self.t0 = jsonl_path, time.time()
        self.depth: dict = {}
        self.starts: dict = {}
        self.names: dict = {}
        self.grp: dict = {}        # run_id -> id of the enclosing subagent (lets the UI nest events)
        self.pgrp: dict = {}
        self.agent: dict = {}      # run_id -> name of the agent doing the work ("orchestrator" or a subagent type)
        self.pagent: dict = {}     # task run_id -> the agent that delegated
        self.ns_task: dict = {}    # langgraph checkpoint_ns of a task call ("tools:<id>") -> (group id, agent name, depth). Each parallel
                                   # task call is its own langgraph task, so its namespace is a reliable key even when parent_run_id is not.
        self.bad_tasks: set = set()  # task calls rejected for missing subagent_type (not real subagents)
        self.sub_stats: dict = {}  # grp id -> per-subagent counters (llm calls, tool calls, tokens)
        self.stats = Counter()
        self.by_agent: dict[str, Counter] = {}   # agent name -> llm_calls / tool_calls / delegations / tokens_in / tokens_out
        self.tool_counts: Counter = Counter()
        self._lock = threading.Lock()      # subagents run in parallel threads; keep JSONL lines whole

    # ---- helpers
    @staticmethod
    def _ns(meta) -> str:
        return str((meta or {}).get("langgraph_checkpoint_ns") or "")

    def _d(self, run_id, parent_id, is_task=False, meta=None) -> int:
        d = self.depth.get(parent_id, 0) + (1 if is_task else 0)
        grp, agent = self.grp.get(parent_id, ""), self.agent.get(parent_id, "orchestrator")
        ns = self._ns(meta)
        if ns:                                       # inside a task call's namespace: it belongs to THAT subagent, whatever parallel
            best = ""                                # siblings' run ids say (parent_run_id can be lost across threads)
            for key in list(self.ns_task):
                if ns.startswith(key + "|") and len(key) > len(best):
                    best = key
            if best:
                grp, agent, d = self.ns_task[best]
        self.depth[run_id], self.grp[run_id], self.agent[run_id] = d, grp, agent
        return d

    def _ctx(self, run_id) -> dict:
        return {"rid": _rid(run_id), "grp": self.grp.get(run_id, ""), "agent": self.agent.get(run_id, "orchestrator")}

    def _count(self, run_id, key: str, n: int = 1) -> None:
        g = self.grp.get(run_id, "")
        if g:
            self.sub_stats.setdefault(g, Counter())[key] += n

    def _agent_count(self, agent: str, key: str, n: int = 1) -> None:
        with self._lock:
            self.by_agent.setdefault(agent, Counter())[key] += n

    def _emit(self, depth: int, icon: str, msg: str, **rec) -> None:
        msg = redact(msg)
        try:
            log.info("%s%s %s", "   " * depth, icon, msg)
        except Exception:                                # a console problem must never kill an agent run
            pass
        if self.path:
            line = json.dumps({"t": round(time.time() - self.t0, 2), "depth": depth, "kind": icon, "msg": msg, **rec}, default=str)
            with self._lock, self.path.open("a", encoding="utf-8") as f:
                f.write(redact(line) + "\n")

    # ---- chains (only used to track nesting)
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, metadata=None, **kw):
        self._d(run_id, parent_run_id, meta=metadata)

    # ---- LLM
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, metadata=None, **kw):
        d = self._d(run_id, parent_run_id, meta=metadata)
        self.starts[run_id] = time.time()
        self.stats["llm_calls"] += 1
        self._count(run_id, "llm")
        last = messages[0][-1]
        c = self._ctx(run_id)
        self._agent_count(c["agent"], "llm_calls")
        self._emit(d, "🧠", f"[{c['agent']}] LLM call #{self.stats['llm_calls']} ({len(messages[0])} msgs, last={last.type})",
                   event="llm_start", **c)

    def on_llm_end(self, response, *, run_id, parent_run_id=None, **kw):
        d, c = self.depth.get(run_id, 0), self._ctx(run_id)
        secs = time.time() - self.starts.pop(run_id, time.time())
        try:
            msg = response.generations[0][0].message
        except Exception:
            self._emit(d, "🧠", f"LLM done in {secs:.1f}s", event="llm_end", secs=round(secs, 2), **c); return
        usage = getattr(msg, "usage_metadata", None) or {}
        ti, to = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        tc = int((usage.get("input_token_details") or {}).get("cache_read", 0) or 0)     # input tokens the provider served from its prompt cache
        self.stats["tokens_in"] += ti
        self.stats["tokens_out"] += to
        self.stats["tokens_cached"] += tc
        self._agent_count(c["agent"], "tokens_cached", tc)
        self._count(run_id, "tokens_in", ti)
        self._count(run_id, "tokens_out", to)
        self._agent_count(c["agent"], "tokens_in", ti)
        self._agent_count(c["agent"], "tokens_out", to)
        calls = [f"{c_['name']}" for c_ in (msg.tool_calls or [])]
        text = _text(msg.content).strip()
        tok = f"{usage.get('input_tokens', '?')}→{usage.get('output_tokens', '?')} tok"
        if calls:
            self._emit(d, "🧠", f"decided: call {', '.join(calls)} ({secs:.1f}s, {tok})", event="llm_end",
                       tool_calls=msg.tool_calls, text=text[:1500], secs=round(secs, 2), **c)
        else:
            self._emit(d, "💬", f"says ({secs:.1f}s, {tok}): {_short(text, 400)}", event="llm_end", text=text, secs=round(secs, 2), **c)
        reasoning = (msg.additional_kwargs or {}).get("reasoning_content")
        if reasoning and log.isEnabledFor(logging.DEBUG):
            log.debug("%s   ~ reasoning: %s", "   " * d, _short(reasoning, 800))

    def on_llm_error(self, error, *, run_id, **kw):
        self._count(run_id, "llm_errors")
        self._emit(self.depth.get(run_id, 0), "❌", f"LLM error: {type(error).__name__}: {_short(str(error), 300)}", event="llm_error", **self._ctx(run_id))

    # ---- tools
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, metadata=None, **kw):
        name = (serialized or {}).get("name") or kw.get("name", "?")
        args = inputs if inputs is not None else input_str
        self.starts[run_id] = time.time()
        self.names[run_id] = name
        if name == "task" and not (isinstance(args, dict) and args.get("subagent_type")):
            # The model called `task` without the required subagent_type: the tool rejects it (ValidationError) and the model retries.
            # That is not a subagent, so do not open a card for it.
            self.bad_tasks.add(run_id)
            d = self._d(run_id, parent_run_id, meta=metadata)
            self._emit(d, "⚠️ ", "orchestrator called `task` without subagent_type (model mistake, rejected, it will retry)",
                       event="tool_error", tool="task", **self._ctx(run_id))
            return
        self.tool_counts[name] += 1
        self.stats["tool_calls"] += 1
        if name == "task":                       # deepagents subagent hand-off
            d = self._d(run_id, parent_run_id, is_task=False, meta=metadata)
            sub = args["subagent_type"]
            self.pagent[run_id] = self.agent[run_id]
            self._agent_count(self.agent[run_id], "delegations")
            self.depth[run_id] = d + 1           # everything inside runs one level deeper
            self.pgrp[run_id] = self.grp[run_id]
            desc = (args or {}).get("description", "") if isinstance(args, dict) else ""
            self._emit(d, "🤝", f"delegate → {sub}: {_short(desc, 220)}", event="subagent_start", subagent=sub,
                       task_full=desc[:4000], rid=_rid(run_id), grp=self.pgrp[run_id], agent=self.pagent[run_id])
            self.grp[run_id] = _rid(run_id)      # children carry this id as their group
            self.agent[run_id] = sub             # ...and this agent name
            ns = self._ns(metadata)
            if ns:
                self.ns_task[ns] = (_rid(run_id), sub, d + 1)
        elif name == "write_todos":
            d = self._d(run_id, parent_run_id, meta=metadata)
            todos = args.get("todos", []) if isinstance(args, dict) else []
            self._emit(d, "📋", "plan: " + " | ".join(f"[{t.get('status', '?')[:4]}] {t.get('content', '')}" for t in todos), event="plan", todos=todos, **self._ctx(run_id))
        else:
            d = self._d(run_id, parent_run_id, meta=metadata)
            self._count(run_id, "tools")
            self._agent_count(self.agent[run_id], "tool_calls")      # real tools only: not 'task' hand-offs, not the todo list
            self._emit(d, "🔧", f"{name}({_short(args, 200)})", event="tool_start", tool=name, args=args, **self._ctx(run_id))

    def on_tool_end(self, output, *, run_id, **kw):
        if run_id in self.bad_tasks:
            self.bad_tasks.discard(run_id); self.names.pop(run_id, None); self.starts.pop(run_id, None); return
        d = self.depth.get(run_id, 0)
        secs = time.time() - self.starts.pop(run_id, time.time())
        name = self.names.pop(run_id, "") or getattr(output, "name", "") or ""
        out = _tool_output(output)
        limit = 1500 if log.isEnabledFor(logging.DEBUG) else 220
        if name == "task":
            g = self.grp.get(run_id, "")
            st = dict(self.sub_stats.pop(g, {}))
            self._emit(max(d - 1, 0), "🏁", f"subagent done ({secs:.0f}s): {_short(out, 350)}", event="subagent_end", out=_text(out)[:6000],
                       rid=_rid(run_id), grp=self.pgrp.get(run_id, ""), agent=self.pagent.pop(run_id, "orchestrator"),
                       subagent=self.agent.get(run_id, "?"), secs=round(secs, 1), sub_stats=st)
            self.grp.pop(run_id, None)
        elif name != "write_todos":
            self._emit(d, "   ↳", f"{_short(out, limit)} ({secs:.1f}s)", event="tool_end", tool=name, out=_text(out)[:4000], secs=round(secs, 2), **self._ctx(run_id))

    def on_tool_error(self, error, *, run_id, **kw):
        if run_id in self.bad_tasks:                 # already reported at start; just show the reason
            self.bad_tasks.discard(run_id); self.names.pop(run_id, None); self.starts.pop(run_id, None)
            self._emit(self.depth.get(run_id, 0), "⚠️ ", f"rejected: {type(error).__name__}: {_short(str(error), 160)}",
                       event="tool_error", tool="task", **self._ctx(run_id))
            return
        name = self.names.pop(run_id, "")
        secs = time.time() - self.starts.pop(run_id, time.time())
        msg = f"{type(error).__name__}: {_short(str(error), 300)}"
        if name == "task":                       # subagent crashed: close its group so the UI does not show it as working
            g = self.grp.get(run_id, "")
            self._emit(max(self.depth.get(run_id, 1) - 1, 0), "❌", f"subagent failed: {msg}", event="subagent_end", failed=True, out=msg,
                       rid=_rid(run_id), grp=self.pgrp.get(run_id, ""), agent=self.pagent.pop(run_id, "orchestrator"),
                       subagent=self.agent.get(run_id, "?"), secs=round(secs, 1), sub_stats=dict(self.sub_stats.pop(g, {})))
            self.grp.pop(run_id, None)
            return
        self._count(run_id, "tool_errors")
        self._emit(self.depth.get(run_id, 0), "❌", f"tool error: {msg}", event="tool_error", tool=name, **self._ctx(run_id))

    # ---- manual events from the runner
    def note(self, icon: str, msg: str, **rec) -> None:
        self._emit(0, icon, msg, **rec)

    def summary(self) -> dict:
        return {"llm_calls": self.stats["llm_calls"], "tool_calls": self.stats["tool_calls"],
                "tokens_in": self.stats["tokens_in"], "tokens_out": self.stats["tokens_out"], "tokens_cached": self.stats["tokens_cached"],
                "tools_used": dict(self.tool_counts),
                "by_agent": {a: dict(c) for a, c in self.by_agent.items()}, "log_file": str(self.path) if self.path else None}

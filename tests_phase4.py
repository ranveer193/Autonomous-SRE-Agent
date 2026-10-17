"""Offline tests for the cost techniques (preflight, cluster_logs, lean_tools, compact_reports, budget) and their toggles.
No cluster, no LLM. The agent is built with a fake model that records exactly what a real model would be sent.
  python tests_phase4.py"""
import json, os, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ["SRE_MEMORY_DIR"] = tempfile.mkdtemp(prefix="sre_mem_test_")
sys.path.insert(0, str(ROOT))
for k in ("SRE_PREFLIGHT", "SRE_CLUSTER_LOGS", "SRE_LEAN_TOOLS", "SRE_COMPACT_REPORTS", "SRE_BUDGET", "SRE_TOOL_BUDGET"):
    os.environ.pop(k, None)

res = []
check = lambda name, cond, d="": res.append((name, bool(cond), d))

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel      # noqa: E402
from langchain_core.messages import AIMessage                                          # noqa: E402
from langchain_core.utils.function_calling import convert_to_openai_tool               # noqa: E402
from sre_agent import features as F, logcluster as LC, budget as B, incident as I      # noqa: E402
from sre_agent import agent as A, preflight as P, metrics as M                          # noqa: E402
from sre_agent.lean import trim_tools, LeanTools, ORCHESTRATOR_DROP, SUBAGENT_DROP     # noqa: E402
import sre_agent.tools.loki as L                                                       # noqa: E402
import sre_agent.tools.k8s as K                                                        # noqa: E402

# ---------- toggles ----------
check("every cost technique is OFF by default (an existing setup behaves as before)", not any(getattr(F.from_env(), n) for n in F.COST))
check("memory stays on by default", F.from_env().memory)
os.environ["SRE_LEAN_TOOLS"] = "1"
check("env switches a cost technique on", F.from_env().lean_tools and not F.from_env().budget)
os.environ.pop("SRE_LEAN_TOOLS")
check("comma list turns on exactly what is listed", F.parse("localizer,lean_tools").as_dict() == {**{n: False for n in F.NAMES}, "localizer": True, "lean_tools": True})
try:
    F.parse("nonsense"); bad = False
except ValueError as e:
    bad = "budget" in str(e) and "lean_tools" in str(e)
check("unknown technique rejected, valid names listed", bad)
cost = [F.COST_ARMS[a] for a in F.COST_ARMS]
check("cost arms are cumulative: each adds one technique", all(sum(getattr(b, n) for n in F.COST) == sum(getattr(a, n) for n in F.COST) + 1 for a, b in zip(cost, cost[1:])))
check("cost arms all keep the full behaviour set (only cost differs)", all(a.localizer and a.falsify and a.memory for a in cost))
check("--compare default stays the four behaviour arms", F.DEFAULT_ARMS == ("llm-only", "localizer", "falsify", "memory"))
check("old four arms unchanged", [F.ARMS[a].label() for a in F.DEFAULT_ARMS] == ["llm-only", "localizer", "localizer+falsify", "localizer+falsify+memory"])
F.set_active(F.Features(cluster_logs=True))
check("active features are what tools see", F.active().cluster_logs)
F.set_active(None)
check("no run in progress: tools fall back to env defaults", not F.active().cluster_logs)

# ---------- log clustering ----------
def line(i, msg, etype, svc="svc-a", dep="db"):
    return (1000 + i, svc, f"level=ERROR event=err message={msg} after {i}.5s error_type={etype} dependency={dep} duration_ms={i * 7} request_id=r{i} trace_id=abc{i:04x}")

rows = [line(i, "connection refused to db:5432", "ConnectionError") for i in range(40)] + \
       [line(100 + i, "timeout to db:5432", "TimeoutError") for i in range(3)] + [line(200, "timeout to db:5432", "TimeoutError", svc="svc-b")]
cs = LC.cluster(rows)
check("44 lines with 3 message shapes -> 3 clusters", len(cs) == 3, str(len(cs)))
check("nothing dropped: counts add up to the number of lines", sum(c.count for c in cs) == len(rows))
check("most frequent cluster first, with its true count", cs[0].count == 40 and "ConnectionError" in cs[0].key)
check("different error types never merge", len({("ConnectionError" in c.key, "TimeoutError" in c.key) for c in cs}) == 2)
check("same message from two services stays two clusters", sum("TimeoutError" in c.key for c in cs) == 2)
check("each cluster keeps the NEWEST line's trace_id as an example", cs[0].trace == "abc0027" and cs[0].last == 1039 and cs[0].first == 1000, f"{cs[0].trace} {cs[0].first}-{cs[0].last}")
txt = LC.render(cs, len(rows))
check("rendered text is a small fraction of the raw lines", len(txt) < 0.15 * sum(len(r[2]) for r in rows), f"{len(txt)} vs {sum(len(r[2]) for r in rows)}")
check("rendered text says counts cover only the lines fetched", "not the whole window" in txt)
many = [(i, "s", f"event=e{i} message=m{i}") for i in range(30)]
r = LC.render(LC.cluster(many), 30, max_clusters=5)
check("pattern list is capped and says how many were left out", "+25 more pattern(s)" in r and r.count("\n") == 6, r[-90:])
check("ids, uuids, hex, ips, pod suffixes are masked; ports and status codes are kept",
      LC.template("trace_id=ab12 id 3f2b8c1e-1111-2222-3333-444455556666 host 10.1.2.3 pod web-7d9f8c6b5-x2k4p db:5432 status_code=503 ok 0123456789abcdef")
      == "trace_id=<*> id <uuid> host <ip> pod web-<pod> db:5432 status_code=503 ok <hex>")
check("empty input renders nothing", LC.render(LC.cluster([]), 0) == "")

# search_logs: off = old behaviour, on = clustered
import json as _j
streams = [{"stream": {"service": "svc-a"}, "values": [[str((1700000000 + i) * 10**9), _j.dumps({"level": "ERROR", "event": "redis_error", "message": "connection refused", "error_type": "ConnectionError", "dependency": "cache", "request_id": f"r{i}", "trace_id": f"t{i}"})] for i in range(60)]}]
asked = {}
L.loki_range = lambda q, m, n: (asked.update(n=n), streams)[1]
F.set_active(F.Features())
raw = L.search_logs.invoke({"service": "svc-a", "limit": 25})
check("cluster_logs OFF: 25 raw lines, as before", len(raw.splitlines()) == 25 and asked["n"] == 25 and raw.splitlines()[0].startswith("1700000059 "), raw[:80])
F.set_active(F.Features(cluster_logs=True))
cl = L.search_logs.invoke({"service": "svc-a", "limit": 25})
check("cluster_logs ON: one pattern with count 60, fetched more lines for fewer tokens", asked["n"] == L.CLUSTER_FETCH and "60x" in cl and len(cl.splitlines()) == 2, cl[:200])
check("clustered answer keeps an example trace_id the trace tools can use", "trace_id=t59" in cl)
check("clustered answer is >10x smaller than the raw one", len(cl) * 10 < len(raw), f"{len(cl)} vs {len(raw)}")
L.loki_range = lambda q, m, n: []
check("no logs: same message either way", "No logs for" in L.search_logs.invoke({"service": "svc-a"}))
F.set_active(None)

# ---------- lean_tools ----------
class T:                                                   # minimal stand-in for a langchain tool
    def __init__(self, name, desc="long " * 400):
        self.name, self.description, self.args_schema = name, desc, {"x": 1}
    def model_copy(self, update):
        c = T(self.name, self.description); c.__dict__.update(update); return c

tools = [T(n) for n in ("ls", "read_file", "write_file", "grep", "task", "write_todos", "search_logs")]
out = trim_tools(tools, ORCHESTRATOR_DROP, {"write_todos": "short"})
check("orchestrator keeps ls/read_file/task/todos/data tools and loses the other file tools", [t.name for t in out] == ["ls", "read_file", "task", "write_todos", "search_logs"])
check("description shortened, schema untouched, original not mutated", next(t for t in out if t.name == "write_todos").description == "short" and next(t for t in out if t.name == "write_todos").args_schema == {"x": 1} and tools[5].description.startswith("long"))
check("subagent drops every file tool, keeps its own tools", [t.name for t in trim_tools(tools, SUBAGENT_DROP, {})] == ["task", "write_todos", "search_logs"])
seen = {}
LeanTools(ORCHESTRATOR_DROP).wrap_model_call(SimpleNamespace(tools=tools, override=lambda tools: SimpleNamespace(tools=tools)), lambda r: seen.update(names=[t.name for t in r.tools]))
check("middleware filters the request the model sees", seen["names"] == ["ls", "read_file", "task", "write_todos", "search_logs"])

# real agent, fake model: what is the model actually sent?
def sent(f):
    cap = []
    class Rec(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            self._t = [convert_to_openai_tool(t) for t in tools]; return self
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            cap.append(list(getattr(self, "_t", []))); return super()._generate(messages, stop, run_manager, **kw)
    seq = [AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": "log-inspector", "description": "look"}, "id": "c1"}]), AIMessage(content="sub"), AIMessage(content="final")]
    m = Rec(messages=iter(seq))
    A.get_llm, A.get_subagent_llm = (lambda *a, **k: m), (lambda *a, **k: None)
    A.build_agent(f).invoke({"messages": [{"role": "user", "content": "ALERT: x"}]}, {"configurable": {"thread_id": "t"}})
    return [([t["function"]["name"] for t in c], len(json.dumps(c)) // 4) for c in cap]

base = F.Features(True, True, True)
off, on = sent(base), sent(F.Features(True, True, True, lean_tools=True))
check("OFF: orchestrator and inspector still see the file tools (unchanged)", "write_file" in off[0][0] and "grep" in off[1][0])
check("ON: orchestrator can still read runbooks, plan, delegate, recall, save, check symptoms",
      {"read_file", "ls", "task", "write_todos", "search_incidents", "save_incident", "current_problems"} <= set(on[0][0]) and "write_file" not in on[0][0])
check("ON: inspector sees only its three log tools", set(on[1][0]) == {"log_error_summary", "search_logs", "logs_for_trace"}, str(on[1][0]))
check("ON: orchestrator request shrinks by at least 30%", on[0][1] < 0.7 * off[0][1], f"{off[0][1]} -> {on[0][1]} tokens (~)")
check("ON: inspector request shrinks by at least 70%", on[1][1] < 0.3 * off[1][1], f"{off[1][1]} -> {on[1][1]} tokens (~)")
subs = {s["name"]: s for s in A.make_subagents(F.Features(lean_tools=True))}
check("remediator keeps its approval gate and its write tools with lean_tools", subs["remediator"]["interrupt_on"] and {t.name for t in subs["remediator"]["tools"]} >= {t.name for t in K.WRITE_TOOLS})

# ---------- budget ----------
check("read tools are known to the budget, writes and verify are not", {"search_logs", "system_overview", "rollout_diff"} <= B.READ_TOOLS
      and not ({t.name for t in K.WRITE_TOOLS} | {"verify_resolved", "current_problems", "read_file", "task", "write_todos", "search_incidents"}) & B.READ_TOOLS)
os.environ["SRE_TOOL_BUDGET"] = "3"; B.reset()
check("calls within the cap run", all(B.take("search_logs") for _ in range(3)))
check("the call past the cap is refused and counted", not B.take("search_logs") and B.state() == {"used": 3, "blocked": 1, "cap": 3}, str(B.state()))
check("writes, verify, symptom check, memory, plan never blocked, even past the cap",
      all(B.take(n) for n in [t.name for t in K.WRITE_TOOLS] + ["verify_resolved", "current_problems", "search_incidents", "save_incident", "task", "write_todos"]))
B.reset()
check("reset per incident", B.state()["used"] == 0 and B.state()["blocked"] == 0)
os.environ["SRE_TOOL_BUDGET"] = "junk"
check("bad env value falls back to the default", B.cap() == B.DEFAULT_CAP)
os.environ["SRE_TOOL_BUDGET"] = "1"; B.reset()
mw, ran = B.ToolBudget(), []
mk = lambda n: SimpleNamespace(tool_call={"name": n, "id": "x1"})
r1 = mw.wrap_tool_call(mk("search_logs"), lambda r: (ran.append(1), "real")[1])
r2 = mw.wrap_tool_call(mk("search_logs"), lambda r: (ran.append(2), "real")[1])
check("middleware runs the tool within the cap and answers BUDGET without running it past it", r1 == "real" and ran == [1] and "BUDGET REACHED" in r2.content and r2.tool_call_id == "x1")
os.environ.pop("SRE_TOOL_BUDGET")

# ---------- prompts ----------
p_off = A.system_prompt(base)
NEW = ("PRE-FLIGHT", "INSPECTOR REPORTS are short", "BUDGET:", "do not call current_problems now")
check("OFF prompt contains none of the cost text (the old arm is unchanged)", not any(s in p_off for s in NEW) and "call current_problems to confirm it" in p_off)
p_on = A.system_prompt(F.Features(True, True, True, preflight=True, compact_reports=True, budget=True))
check("ON prompt carries each technique's rule", all(s in p_on for s in NEW) and "call current_problems to confirm it" not in p_on)
check("pre-flight facts are treated as ONE signal each, not as proof", "ONE logs signal" in p_on and "ONE cluster signal" in p_on)
cs_ = {s["name"]: s["system_prompt"] for s in A.make_subagents(F.Features(falsify=True, compact_reports=True, preflight=True))}
check("inspectors get the short report format; the remediator never does", all("REPORT FORMAT" in cs_[n] for n in cs_ if n != "remediator") and "REPORT FORMAT" not in cs_["remediator"])
check("k8s inspector may still quote the rollout diff verbatim", "DIFF:" in cs_["k8s-inspector"] and "DIFF:" not in cs_["log-inspector"])
check("short format keeps falsify's REFUTED / SURVIVES contract", all("REFUTED or SURVIVES" in cs_[n] for n in cs_ if n != "remediator"))
check("report format has no 'suspect' field (a named suspect from every inspector anchors the orchestrator)", "suspect" not in cs_["log-inspector"].lower().split("report format")[1])
check("inspectors are told to use preloaded facts, not collect them again", all("do not collect" in cs_[n] for n in cs_ if n != "remediator"))

# ---------- pre-flight ----------
I.begin([], onset_ts=time.time())
check("healthy system: pre-flight is empty (no bias, no work)", P.preflight() == "")
I.begin(["svc-a: HTTP 5xx rate 90% (threshold 5%)"], onset_ts=time.time() - 60)
L.loki_range = lambda q, m, n: streams
now_s = int(time.time())
ev = "\n".join(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now_s - 30 + i))}||x|{'Warning' if i % 2 else 'Normal'}|Reason{i}|pod-{i}|message {i}" for i in range(30))
K._kraw = lambda args, timeout=30: ev
out = P.preflight()
check("pre-flight has log patterns and Warning events only", "Log patterns" in out and "Warning events" in out and " Normal " not in out, out[:160])
check("pre-flight is bounded", len(out) <= P.MAX_CHARS + 20, str(len(out)))
check("older Warning events are counted, not silently dropped", "older Warning events not shown" in out)
check("pre-flight says nothing about causes or suspects", not any(w in out.lower() for w in ("root cause", "suspect", "culprit", "most likely", "caused by", "victim")))
L.loki_range = lambda q, m, n: (_ for _ in ()).throw(RuntimeError("loki down"))
K._kraw = lambda args, timeout=30: (_ for _ in ()).throw(RuntimeError("kubectl down"))
try:
    out2, boom = P.preflight(), False
except Exception:
    out2, boom = "", True
check("pre-flight never raises when its sources fail", not boom and out2 == "", repr(out2))

# ---------- metrics ----------
m = M.run_metrics({"mttr_s": 100, "budget": {"used": 30, "cap": 30, "blocked": 4}, "verdict": {}, "stats": {"llm_calls": 9, "tokens_in": 1000, "tokens_cached": 400, "by_agent": {"orchestrator": {"tokens_in": 1000, "tokens_out": 50, "tool_calls": 2}}}})
check("run metrics carry llm calls, input tokens, cached tokens, budget blocks", (m["llm_calls"], m["tokens_in"], m["tokens_cached"], m["budget_blocked"]) == (9, 1000, 400, 4), str(m))
crash = M.run_metrics({"mttr_s": None, "stats": {}, "verdict": {}})
check("crashed run: new fields unknown, not zero", crash["llm_calls"] is None and crash["tokens_cached"] is None and crash["budget_blocked"] is None)
cases = [{"metrics": {**m, "mttr_s": 1, "tokens": 1050, "tool_calls": 2, "main": None, "sub": None}}, {"metrics": {**crash, "mttr_s": None}}]
a = M.aggregate(cases)
check("cached share = cached / input over runs that have input", a["cached_pct"] == 40.0 and a["llm_calls"] == 9 and a["budget_blocked"] == 4, str(a))
old_style = M.aggregate([{"metrics": {"mttr_s": 5, "root_cause_ok": True, "distractor_blamed": False, "tokens": 10, "tool_calls": 1, "main": None, "sub": None}}])
check("results saved before this change still aggregate", old_style["cached_pct"] is None and old_style["tokens"] == 10)

fails = [(n, d) for n, ok, d in res if not ok]
for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok or not d else f"  [{d}]"))
print(f"\n{len(res) - len(fails)}/{len(res)} passed")
sys.exit(1 if fails else 0)

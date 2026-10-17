"""Offline tests for the metrics, per-agent splits, compare mode and report table. No cluster, no LLM.
  python tests_phase2.py"""
import sys, uuid, subprocess
from types import SimpleNamespace as NS
from sre_agent import metrics as M
from sre_agent.logging_utils import AgentLogger

res = []
check = lambda name, cond, d="": res.append((name, bool(cond), d))

# ---- logger: calls are credited to the agent that made them; hand-offs and the todo list are not tool calls
lg = AgentLogger(None)
R, L1, T, L2, S, W = (uuid.uuid4() for _ in range(6))
lg.on_chain_start({}, {}, run_id=R, parent_run_id=None)
def llm(rid, parent, ti, to):
    lg.on_chat_model_start({}, [[NS(type="human")]], run_id=rid, parent_run_id=parent)
    msg = NS(usage_metadata={"input_tokens": ti, "output_tokens": to}, tool_calls=[], content="x", additional_kwargs={})
    lg.on_llm_end(NS(generations=[[NS(message=msg)]]), run_id=rid, parent_run_id=parent)
llm(L1, R, 100, 10)
lg.on_tool_start({"name": "task"}, "", run_id=T, parent_run_id=R, inputs={"subagent_type": "log-inspector", "description": "d"})
llm(L2, T, 200, 20)
lg.on_tool_start({"name": "search_logs"}, "", run_id=S, parent_run_id=T, inputs={"q": "e"})
lg.on_tool_start({"name": "write_todos"}, "", run_id=W, parent_run_id=R, inputs={"todos": []})
ba = lg.summary()["by_agent"]
check("main tokens credited to the orchestrator", ba["orchestrator"]["tokens_in"] == 100 and ba["orchestrator"]["tokens_out"] == 10, str(ba))
check("subagent tokens credited to the subagent", ba["log-inspector"]["tokens_in"] == 200 and ba["log-inspector"]["tokens_out"] == 20)
check("real tool call credited to the subagent", ba["log-inspector"]["tool_calls"] == 1)
check("hand-off counted as delegation, not a tool call; todo list ignored", ba["orchestrator"].get("delegations") == 1 and ba["orchestrator"].get("tool_calls", 0) == 0)

# ---- split + per-run metrics
sp = M.split_by_agent({"orchestrator": {"tokens_in": 100, "tokens_out": 10, "tool_calls": 1}, "a": {"tokens_in": 5, "tool_calls": 3}, "b": {"tokens_out": 7, "tool_calls": 2}})
check("main/sub split sums every subagent", sp["main"]["tokens"] == 110 and sp["sub"]["tokens"] == 12 and sp["sub"]["tool_calls"] == 5, str(sp))
run = {"mttr_s": 120, "verdict": {"root_cause_correct": True, "blamed_distractor": False},
       "stats": {"llm_calls": 4, "by_agent": {"orchestrator": {"tokens_in": 100, "tokens_out": 10, "tool_calls": 1}, "a": {"tokens_in": 50, "tokens_out": 5, "tool_calls": 4}}}}
m = M.run_metrics(run)
check("five metrics for a normal run", (m["mttr_s"], m["root_cause_ok"], m["distractor_blamed"], m["tokens"], m["tool_calls"]) == (120, True, False, 165, 5), str(m))
crash = M.run_metrics({"mttr_s": None, "stats": {}, "verdict": {"error": "judge down"}})
check("crashed agent: tokens/tool calls unknown (None), not zero", crash["tokens"] is None and crash["tool_calls"] is None and crash["main"] is None)
check("judge failure: verdicts unknown (None), not wrong", crash["root_cause_ok"] is None and crash["distractor_blamed"] is None)

# ---- aggregation
def case(arm, run, mttr, rc, bd, tok, tools, status="PASS"):
    mt = {"mttr_s": mttr, "root_cause_ok": rc, "distractor_blamed": bd, "tokens": tok, "tool_calls": tools,
          "main": None if tok is None else {"tokens": tok // 2, "tool_calls": 1},
          "sub": None if tok is None else {"tokens": tok - tok // 2, "tool_calls": tools - 1}}
    return {"kind": "scenario", "name": "sc", "arm": arm, "run": run, "status": status, "metrics": mt}
cases = [case("llm-only", 1, 100, True, True, 1000, 10), case("llm-only", 2, 200, False, False, 3000, 20),
         case("memory", 1, 300, True, False, 2000, 10), case("memory", 2, 100, True, False, 500, 4),
         case("localizer", 1, None, None, None, None, None, status="FAIL"),
         {"kind": "scenario", "name": "sc", "arm": "falsify", "run": 1, "status": "SKIP"}]
a = M.aggregate([c for c in cases if c["arm"] == "llm-only"])
check("means and rates", (a["mttr_s"], a["root_cause_acc"], a["distractor_rate"], a["tokens"], a["tool_calls"]) == (150, 0.5, 0.5, 2000, 15), str(a))
check("main/sub means", a["tokens_main"] == 1000 and a["tokens_sub"] == 1000 and a["tools_main"] == 1 and a["tools_sub"] == 14, str(a))
cmp = M.compare(cases, ["llm-only", "localizer", "falsify", "memory"])
check("memory warm row = run 2 only", cmp["memory (warm)"]["tokens"] == 500 and cmp["memory"]["tokens"] == 1250)
loc = cmp["localizer"]
check("run with no data: counted as a run, no invented numbers", loc["runs"] == 1 and loc["recovered"] == 0 and loc["mttr_s"] is None and loc["tokens"] is None and loc["root_cause_acc"] is None)
check("skipped cases are left out", cmp["falsify"]["runs"] == 0)
check("no warm row without a second memory run", "memory (warm)" not in M.compare([cases[2]], ["memory"]))

# ---- suite: report text and argument checks (imports the harness; needs no cluster)
sys.path.insert(0, "eval")
import run_suite as S
md = S.build_md({"ts": "t", "provider": "p", "model": "m", "namespace": "n", "dry_run": False}, cases, cmp)
check("markdown has the comparison table", "## Technique comparison" in md and "| llm-only | 2 | 2/2 | 150 | 50% | 50% |" in md, md[:600])
check("markdown shows the arm in the case list", "[memory]" in md)
for argv, why in ((["--compare", "--negative"], "compare + negative"), (["--compare", "--arms", "bogus"], "unknown arm"), (["--features", "bogus"], "bad features")):
    p = subprocess.run([sys.executable, "eval/run_suite.py", "--auto-approve", *argv], capture_output=True, text=True)
    check(f"suite refuses {why}", p.returncode != 0 and "Traceback" not in p.stderr, p.stderr[-200:])

# ---- api request validation
import api
check("api exposes arms and models", {"llm-only", "localizer", "falsify", "memory"} <= set(api.features_info()["arms"]) and "subagents" in api.features_info()["models"])
check("api: compare pre-selects only the four behaviour arms; cost arms are available but opt-in",
      api.features_info()["default_arms"] == ["llm-only", "localizer", "falsify", "memory"] and "cost-budget" in api.features_info()["arms"])

for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok or not d else f"  [{d}]"))
bad = [r for r in res if not r[1]]
print(f"\n{len(res) - len(bad)}/{len(res)} passed")
sys.exit(1 if bad else 0)

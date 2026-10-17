"""Offline tests for Tuning: the numbers behind the techniques (tool-call cap, inspector calls, report words, log patterns/fetch,
pre-flight size, step limit), settable per run from the console, the API, the CLI and env. No cluster, no LLM.
  python tests_tuning.py"""
import json, os, re, subprocess, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["SRE_MEMORY_DIR"] = tempfile.mkdtemp(prefix="sre_mem_test_")
os.environ["SRE_LOG_DIR"] = tempfile.mkdtemp(prefix="sre_log_test_")
sys.path.insert(0, str(ROOT))
for n in ("SRE_TOOL_BUDGET", "SRE_INSPECTOR_CALLS", "SRE_REPORT_WORDS", "SRE_LOG_PATTERNS", "SRE_LOG_FETCH", "SRE_PREFLIGHT_CHARS", "SRE_RECURSION_LIMIT",
          "SRE_PREFLIGHT", "SRE_CLUSTER_LOGS", "SRE_LEAN_TOOLS", "SRE_COMPACT_REPORTS", "SRE_BUDGET"):
    os.environ.pop(n, None)

res = []
check = lambda name, cond, d="": res.append((name, bool(cond), d))

from sre_agent import tuning as T, features as F, budget as B, agent as A, preflight as P, runner as R   # noqa: E402
from sre_agent.tuning import Tuning                                                                      # noqa: E402
import sre_agent.tools.loki as L                                                                         # noqa: E402


def raises(fn, *words):
    try:
        fn()
    except ValueError as e:
        return all(w in str(e) for w in words)
    return False


# ---------- defaults reproduce the old behaviour ----------
d = Tuning()
check("defaults are the values that used to be baked in", (d.tool_budget, d.inspector_calls, d.report_words, d.log_patterns, d.log_fetch, d.preflight_chars, d.recursion_limit)
      == (30, 8, 150, 12, 100, 3200, 120), str(d))
check("constants elsewhere still equal the defaults", B.DEFAULT_CAP == d.tool_budget and L.CLUSTER_FETCH == d.log_fetch and P.MAX_CHARS == d.preflight_chars)
check("every number has a range, a label, a hint, and the technique it belongs to", all({"min", "max", "label", "hint", "uses"} <= set(s) for s in T.SPEC.values())
      and all(s["uses"] in ("", *F.NAMES) for s in T.SPEC.values()))
check("every default is inside its own range", all(T.SPEC[k]["min"] <= v <= T.SPEC[k]["max"] for k, v in d.as_dict().items()))
check("no env, no run: tuning is the defaults", T.from_env() == d and T.active() == d and d.changed() == {} and d.label() == "defaults")

# ---------- parsing: strict for what a person types ----------
check("empty / None / {} mean 'use the defaults'", T.parse("") == d and T.parse(None) == d and T.parse({}) == d)
t = T.parse("tool_budget=12, report_words=80")
check("text form: only the named values change", t.tool_budget == 12 and t.report_words == 80 and t.log_fetch == 100 and t.label() == "tool_budget=12,report_words=80")
check("dict form takes numbers or numeric text, and skips blank fields", T.parse({"tool_budget": "7", "log_fetch": 250, "report_words": "", "log_patterns": None}) ==
      Tuning(tool_budget=7, log_fetch=250))
check("unknown name is an error that lists the valid names", raises(lambda: T.parse("budget=3"), "unknown", "tool_budget", "recursion_limit"))
check("not a number is an error that names the field", raises(lambda: T.parse({"tool_budget": "lots"}), "tool_budget", "whole number"))
check("a fraction is an error, not rounded", raises(lambda: T.parse({"report_words": 99.5}), "report_words", "whole number"))
check("true/false is not a number", raises(lambda: T.parse({"tool_budget": True}), "tool_budget"))
check("below the range is an error that states the range", raises(lambda: T.parse("tool_budget=0"), "tool_budget", "between 1 and 200"))
check("above the range is an error that states the range", raises(lambda: T.parse("report_words=9000"), "report_words", "between 30 and 500"))
check("missing '=' is explained", raises(lambda: T.parse("tool_budget"), "name=value"))
check("round trip through the CLI text keeps every value (including ones equal to a default)", T.parse(T.to_cli(Tuning(tool_budget=30, report_words=90))) == Tuning(report_words=90)
      and "tool_budget=30" in T.to_cli(Tuning()))
os.environ["SRE_TOOL_BUDGET"] = "50"
check("a value chosen in the console beats env, even when it equals the built-in default",
      T.parse(T.to_cli(T.parse({"tool_budget": 30}))).tool_budget == 30 and T.parse({"report_words": 90}).tool_budget == 50)
os.environ.pop("SRE_TOOL_BUDGET")

# ---------- env: lenient ----------
os.environ.update(SRE_TOOL_BUDGET="3", SRE_REPORT_WORDS="9999", SRE_LOG_FETCH="junk", SRE_LOG_PATTERNS="")
e = T.from_env()
check("env sets a value", e.tool_budget == 3)
check("env out of range moves to the nearest allowed value", e.report_words == 500)
check("env junk or empty falls back to the default", e.log_fetch == 100 and e.log_patterns == 12)
check("the old SRE_TOOL_BUDGET still drives the cap when no run is active", B.cap() == 3)
for n in ("SRE_TOOL_BUDGET", "SRE_REPORT_WORDS", "SRE_LOG_FETCH", "SRE_LOG_PATTERNS"):
    os.environ.pop(n)

# ---------- budget cap ----------
os.environ["SRE_TOOL_BUDGET"] = "9"
T.set_active(Tuning(tool_budget=2)); B.reset()
check("the run's tuning wins over env for the cap", B.cap() == 2)
check("cap of 2: two calls run, the third is refused and counted", B.take("search_logs") and B.take("search_logs") and not B.take("search_logs")
      and B.state() == {"used": 2, "blocked": 1, "cap": 2}, str(B.state()))
check("writes and verify are still never blocked at a low cap", B.take("verify_resolved") and B.take("current_problems"))
T.set_active(None); B.reset(); os.environ.pop("SRE_TOOL_BUDGET")
check("after the run, tools are back to env/defaults", B.cap() == 30)

# ---------- prompts ----------
def prompts(f, t=None):
    return {s["name"]: s["system_prompt"] for s in A.make_subagents(f, t)}


base = prompts(F.Features(), Tuning())
check("default tuning: inspectors keep 'at most 8 tool calls' exactly as before", all("Budget: at most 8 tool calls." in p for n, p in base.items() if n != "remediator"))
check("remediator never gets an inspector budget line", "tool calls" not in base["remediator"])
five = prompts(F.Features(), Tuning(inspector_calls=5))
check("inspector_calls changes every inspector", all("at most 5 tool calls" in p and "at most 8" not in p for n, p in five.items() if n != "remediator"))
check("inspector_calls leaves the remediator untouched", five["remediator"] == base["remediator"])
check("inspector_calls changes nothing else in the prompt", all(five[n].replace("at most 5 tool calls", "at most 8 tool calls") == base[n] for n in base))
cr_def = prompts(F.Features(compact_reports=True), Tuning())
cr_80 = prompts(F.Features(compact_reports=True), Tuning(report_words=80))
check("default report length is still 150 words", all("max 150 words" in p for n, p in cr_def.items() if n != "remediator"))
check("report_words is written into every inspector's format", all("max 80 words" in p and "150" not in p.split("REPORT FORMAT")[1] for n, p in cr_80.items() if n != "remediator"))
check("remediator is never compacted, whatever the number", "REPORT FORMAT" not in cr_80["remediator"])
check("report_words does nothing while compact_reports is off", prompts(F.Features(), Tuning(report_words=80)) == base)
check("make_subagents without a tuning uses the run's (env/defaults)", prompts(F.Features()) == base)
T.set_active(Tuning(inspector_calls=3))
check("make_subagents without an explicit tuning follows the active run", all("at most 3 tool calls" in p for n, p in prompts(F.Features()).items() if n != "remediator"))
T.set_active(None)
check("tuning never mutates the shared prompt templates", "at most 8 tool calls" in A.BUDGET and "@WORDS@" in A.SUB_COMPACT and prompts(F.Features(compact_reports=True)) == cr_def)

# ---------- log clustering: patterns shown, lines fetched ----------
asked = {}


def fake_range(q, minutes, limit):
    asked["n"] = limit
    vals = [[str(1_000_000_000_000 + i * 1_000_000_000), f"level=ERROR event=err_{i % 20} message=boom_{i % 20} error_type=E{i % 20}"] for i in range(60)]
    return [{"stream": {}, "values": vals}]


orig_range = L.loki_range
L.loki_range = fake_range
F.set_active(F.Features(cluster_logs=True))
T.set_active(Tuning())
out_def = L.search_logs.invoke({"service": "svc-a"})
shown = lambda o: [l for l in o.splitlines() if re.match(r"^\d+x \[", l)]
check("default: fetches 100 lines and shows up to 12 patterns", asked["n"] == 100 and len(shown(out_def)) == 12 and "+8 more pattern(s)" in out_def, out_def[:300])
T.set_active(Tuning(log_patterns=3, log_fetch=40))
out_t = L.search_logs.invoke({"service": "svc-a"})
check("log_fetch sets how many lines are read from Loki", asked["n"] == 40 and out_t.startswith("40 log line(s)"), out_t[:80])
check("log_patterns sets how many patterns are shown, the rest are summarised", len(shown(out_t)) == 3 and "+17 more pattern(s)" in out_t, out_t)
check("shown + summarised counts still add up to every fetched line", sum(int(l.split("x ")[0]) for l in shown(out_t)) + int(out_t.split("more pattern(s), ")[1].split(" ")[0]) == 40, out_t)
F.set_active(F.Features(cluster_logs=False))
L.search_logs.invoke({"service": "svc-a", "limit": 25})
check("log_fetch does nothing while cluster_logs is off (raw mode keeps `limit`)", asked["n"] == 25)
L.loki_range = orig_range
F.set_active(None); T.set_active(None)

# ---------- pre-flight size ----------
orig_collect, orig_snap = P.collect, P.incident.snapshot
P.collect, P.incident.snapshot = (lambda: "x" * 5000), (lambda: "symptoms")
check("default: pre-flight block is cut at 3,200 chars", len(P.preflight()) == 3200 + len("\n...[cut]"))
T.set_active(Tuning(preflight_chars=1000))
check("preflight_chars moves the cut", len(P.preflight()) == 1000 + len("\n...[cut]"))
T.set_active(Tuning(preflight_chars=9000))
check("a block under the cap is returned whole", len(P.preflight()) == 5000)
P.collect, P.incident.snapshot = orig_collect, orig_snap
T.set_active(None)

# ---------- run_alert: tuning reaches the graph, the tools and the result ----------
seen = {}


class FakeAgent:
    def invoke(self, inp, cfg):
        seen["cfg"], seen["cap"], seen["features"] = cfg, B.cap(), F.active()
        seen["log_patterns"] = T.active().log_patterns
        return {"messages": [type("M", (), {"content": "done"})()]}


R.build_agent = lambda f, t=None: (seen.__setitem__("built_with", t), FakeAgent())[1]
R.find_problems, R.snapshot_line = (lambda: []), (lambda: "healthy")
R.incident.begin = lambda *a, **k: None
R.incident.describe_window = lambda: "w"
R.incident.signature = lambda: ""
R.incident.recalled = lambda: False
R.incident.saved_ids = lambda: []
R.start_incident_clock, R.reset_attempts = (lambda: None), (lambda: None)
r = R.run_alert("test alert", features=F.Features(budget=True), tuning=Tuning(tool_budget=11, recursion_limit=40, log_patterns=5))
check("run_alert hands the tuning to the agent builder", seen["built_with"] == Tuning(tool_budget=11, recursion_limit=40, log_patterns=5))
check("recursion_limit is the graph's step limit", seen["cfg"]["recursion_limit"] == 40)
check("tools and budget read this run's tuning while it runs", seen["cap"] == 11 and seen["log_patterns"] == 5)
check("the result records the tuning that produced it", r["tuning"]["tool_budget"] == 11 and r["tuning"]["recursion_limit"] == 40 and r["budget"]["cap"] == 11, str(r.get("budget")))
r2 = R.run_alert("test alert", features=F.Features())
check("no tuning given: the run uses env/defaults (recursion 120)", seen["cfg"]["recursion_limit"] == 120 and r2["tuning"] == Tuning().as_dict())
ev = [json.loads(l) for f in Path(os.environ["SRE_LOG_DIR"]).glob("*.jsonl") for l in f.read_text(encoding="utf-8").splitlines() if '"event": "features"' in l]
tuned = [e for e in ev if e["tuning"]["tool_budget"] == 11]
check("the trace names the tuning of the run, so a replay shows what was used", tuned and "tool_budget=11" in tuned[0]["msg"] and len(ev) >= 1, str(ev[:2]))
T.set_active(None); F.set_active(None)

# ---------- API ----------
import api                                                                                                # noqa: E402
from fastapi import HTTPException                                                                         # noqa: E402

info = api.features_info()["tuning"]
check("api sends the ranges and the defaults the console needs", set(info["spec"]) == set(Tuning().as_dict()) and info["defaults"] == Tuning().as_dict()
      and info["spec"]["tool_budget"]["max"] == 200)


def http(fn):
    try:
        fn()
    except HTTPException as ex:
        return ex.status_code, str(ex.detail)
    return None, ""


check("api: out-of-range suite tuning is a 400 that names the field", http(lambda: api.start_suite(api.SuiteReq(scenarios=["x"], tuning={"tool_budget": 999})))
      == (400, "bad tuning: tool_budget must be between 1 and 200, got 999"))
check("api: non-numeric run tuning is a 400", http(lambda: api.start_run(api.RunReq(tuning={"report_words": "many"})))[0] == 400)
check("api: unknown tuning name is a 400", http(lambda: api.start_suite(api.SuiteReq(scenarios=["x"], tuning={"speed": 9})))[0] == 400)

launched = {}


class FakeProc:
    def poll(self):
        return 0


api.REPORTS = Path(tempfile.mkdtemp(prefix="sre_reports_test_"))
real_popen = subprocess.Popen                          # api.subprocess IS the subprocess module: put the real one back after the API checks
api.subprocess.Popen = lambda cmd, **k: (launched.__setitem__("cmd", cmd), FakeProc())[1]
api.start_suite(api.SuiteReq(scenarios=["a"], tuning={"tool_budget": "20", "report_words": "", "log_fetch": 250}))
cmd = launched["cmd"]
tv = T.parse(cmd[cmd.index("--tuning") + 1])
check("api: the suite subprocess gets every value, with the typed ones applied", tv.tool_budget == 20 and tv.log_fetch == 250 and tv.report_words == 150, str(cmd))
launched.clear()
api.start_suite(api.SuiteReq(scenarios=["a"]))
check("api: no tuning typed -> no --tuning flag (env and defaults apply)", "--tuning" not in launched["cmd"])
launched.clear()
api.start_suite(api.SuiteReq(scenarios=["a"], compare=True, arms=["cost-base", "cost-budget"], tuning={"tool_budget": 15}))
check("api: compare mode passes the same tuning to every arm", "--compare" in launched["cmd"] and "--tuning" in launched["cmd"] and T.parse(launched["cmd"][launched["cmd"].index("--tuning") + 1]).tool_budget == 15)

subprocess.Popen = real_popen
api._worker = lambda run: None
api.find_problems = lambda: []
before = set(api.RUNS)
started = api.start_run(api.RunReq(alert="x", tuning={"tool_budget": 12}))
check("api: a single run stores its tuning", started["tuning"]["tool_budget"] == 12 and started["tuning"]["report_words"] == 150)
for k in set(api.RUNS) - before:
    api.RUNS.pop(k)

# ---------- harness CLI and report ----------
def cli(*args):
    p = subprocess.run([sys.executable, str(ROOT / "eval" / "run_suite.py"), "--auto-approve", "--scenarios", "none", *args], capture_output=True, text=True, cwd=ROOT, timeout=120)
    return p.returncode, p.stdout + p.stderr


rc, out = cli("--tuning", "tool_budget=0")
check("suite CLI: a bad --tuning stops before anything runs and says why", rc != 0 and "--tuning" in out and "between 1 and 200" in out, out[-300:])
rc, out = cli("--tuning", "nonsense=1")
check("suite CLI: an unknown --tuning name is refused", rc != 0 and "unknown" in out, out[-300:])
rr = subprocess.run([sys.executable, str(ROOT / "eval" / "run_scenario.py"), "x", "--tuning", "tool_budget=0"], capture_output=True, text=True, cwd=ROOT, timeout=120)
check("scenario CLI: a bad --tuning is refused up front", rr.returncode != 0 and "between 1 and 200" in rr.stderr, rr.stderr[-300:])

sys.path.insert(0, str(ROOT / "eval"))
import run_suite as RS                                                                                    # noqa: E402
meta = {"ts": "2026-01-01T00:00:00", "provider": "p", "model": "m", "namespace": "n", "dry_run": False}
md_plain = RS.build_md({**meta, "tuning_changed": {}}, [{"kind": "guard", "name": "g", "run": 1, "status": "PASS"}])
md_tuned = RS.build_md({**meta, "tuning_changed": {"tool_budget": 12, "report_words": 80}}, [{"kind": "guard", "name": "g", "run": 1, "status": "PASS"}])
check("report header lists the non-default tuning", "Tuning (non-default): tool_budget=12, report_words=80" in md_tuned)
check("report is unchanged when nothing was tuned", "Tuning" not in md_plain and "dry_run=False\n\n## Summary" in md_plain, md_plain[:200])

fails = [(n, d) for n, ok, d in res if not ok]
for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok or not d else f"  [{d}]"))
print(f"\n{len(res) - len(fails)}/{len(res)} passed")
sys.exit(1 if fails else 0)

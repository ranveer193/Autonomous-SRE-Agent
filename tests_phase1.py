"""Offline tests for the optional techniques (localizer, falsification prompts, feature toggles, subagent model). No cluster, no LLM.
  python tests_phase1.py"""
import sys
from sre_agent.features import Features, ARMS, parse
from sre_agent import localizer as L
from sre_agent import agent as A

res = []
check = lambda name, cond, d="": res.append((name, bool(cond), d))

# ---- features
check("arms are cumulative", ARMS["llm-only"] == Features(False, False, False) and ARMS["memory"] == Features(True, True, True))
check("list parse", parse("localizer,memory") == Features(True, False, True))
check("none = all off", parse("none") == Features(False, False, False))
try:
    parse("bogus"); check("unknown feature rejected", False)
except ValueError:
    check("unknown feature rejected", True)

# ---- localizer = evidence board: facts, no ranking, no verdict
base = {"svc": {"gw": {"err": 80, "p95": 2.4, "b_err": 0.5, "b_p95": 0.05}, "orders": {"err": 85, "p95": 2.4}, "pay": {"err": 0, "p95": 0.02}, "users": {"err": 0, "p95": 0.05}},
        "edges": {("gw", "orders"): {"err": 5, "p95": 2.4}, ("orders", "pay"): {"err": 0, "p95": 0.02}, ("orders", "users"): {"err": 0, "p95": 0.05},
                  ("pay", "db"): {"err": 0.0, "p95": 0.02}, ("users", "db"): {"err": 0.4, "p95": 3.0}, ("orders", "db"): {"err": 0.3, "p95": 3.0}},
        "extra": {}, "deployments": {"pay", "orders", "gw", "users", "db"}, "desired": {"db": 0, "pay": 1, "orders": 1, "gw": 1, "users": 1},
        "ready": {"db": 0, "pay": 0, "orders": 1, "gw": 1, "users": 1}, "max_desired": {"db": 1, "pay": 1, "orders": 1, "gw": 1, "users": 1},
        "restarts": {}, "rollouts": {"pay": 1000.0, "db": 1100.0, "gw": 400.0}, "onset": 1200.0, "now": 1260.0}
b = L.build(base)
txt = L.render(b, "test window")
check("facts are alphabetical, nothing is ranked or called a cause",
      list(b["facts"]) == sorted(b["facts"]) and "suspect" not in txt.lower() and "victim" not in txt.lower() and "root cause" not in txt.lower(), txt[:120])
check("a deployment scaled from 1 to 0 is stated as a fact", "desired replicas 0 now, 1 at some point" in " ".join(b["facts"]["db"]), str(b["facts"].get("db")))
check("own symptoms carry the pre-incident value", "own 5xx 80% (before: 0%)" in " ".join(b["facts"]["gw"]) or "own 5xx 80% (before: 1%)" in " ".join(b["facts"]["gw"]), str(b["facts"]["gw"]))
check("rollouts are listed for every deployment in one neutral line, not attached to unhealthy components",
      [n for n, _ in b["rollouts"]] == ["db", "gw", "pay"] and not any("rollout" in f for fs in b["facts"].values() for f in fs) and "Latest rollout of each deployment" in txt, str(b["rollouts"]))
check("deterministic", L.render(L.build(base)) == L.render(L.build(base)))
healthy = {"svc": {"gw": {"err": 0.1, "p95": 0.05}}, "edges": {}, "extra": {}, "deployments": set(), "rollouts": {}, "onset": None}
check("healthy system: localizer stays silent", L.render(L.build(healthy)) == "")
_real_collect = L.collect
L.collect = lambda: (_ for _ in ()).throw(RuntimeError("prom down"))
check("localize() never raises", L.localize() == "")

# ---- prompts follow the toggles
off, on = A.system_prompt(Features(False, False, False)), A.system_prompt(Features(True, True, True))
check("llm-only prompt: memory disabled, no technique text", "disabled for this run" in off and "FALSIFY" not in off and "LOCALIZER" not in off and "<<" not in off)
check("full prompt: memory step, falsify and localizer text", "search_incidents" in on and "FALSIFY BEFORE YOU DIG" in on and "LOCALIZER:" in on and "<<" not in on)
check("falsify off keeps prompt short", len(A.system_prompt(Features(True, False, True))) < len(on))

# ---- subagents
subs_off = {s["name"]: s for s in A.make_subagents(Features(False, False, False))}
subs_on = {s["name"]: s for s in A.make_subagents(Features(False, True, False))}
check("subagent falsify text only on inspectors, only when enabled",
      "REFUTED" in subs_on["metrics-inspector"]["system_prompt"] and "REFUTED" not in subs_on["remediator"]["system_prompt"]
      and "REFUTED" not in subs_off["metrics-inspector"]["system_prompt"])
check("remediator keeps its approval gate", subs_on["remediator"].get("interrupt_on"))
check("no subagent model override when unset", all("model" not in s for s in subs_off.values()))
check("toggling never mutates shared prompts", "REFUTED" not in A._SUBAGENTS[0]["system_prompt"])

for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok or not d else f"  [{d}]"))
bad = [r for r in res if not r[1]]
# ---- regression: collect() must key rows by their real labels (labels were once passed as ONE tuple -> every key '?' -> ValueError in build)
import time as _t
from sre_agent import incident as _I
def _fake(q, timeout=15):
    if "source_service" in q and "histogram_quantile" in q:
        return [{"metric": {"source_service": "a", "target_service": "b"}, "value": [0, "4.8"]}]
    if "histogram_quantile" in q:
        return [{"metric": {"service": "a"}, "value": [0, "4.8"]}, {"metric": {"service": "b"}, "value": [0, "4.8"]}]
    return []
L.prom_query = _fake
L.collect = _real_collect
_I.begin([], now=_t.time(), onset_ts=_t.time() - 35)
_d = L.collect()
check("collect keys services by label value, edges by (source, target)", set(_d["svc"]) == {"a", "b"} and ("a", "b") in _d["edges"], str(_d["edges"]))
_txt, _st = L.localize_ex()
check("latency-only incident produces facts instead of 'nothing anomalous'", _st == "facts" and "own p95 4.80s" in _txt, f"{_st}: {_txt[:80]}")
check("a malformed edge key cannot crash build()", "x" not in L.build({"svc": {}, "edges": {"?": {"err": 9}}, "deployments": set()})["facts"])


print(f"\n{len(res) - len(bad)}/{len(res)} passed")
sys.exit(1 if bad else 0)
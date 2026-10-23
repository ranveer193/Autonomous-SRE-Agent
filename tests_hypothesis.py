"""Offline tests for hypothesis-driven investigation (feature `hypothesis`). No cluster, no LLM, no network.
  python tests_hypothesis.py
Sections: A ledger rules | B observations and gate (middleware) | C feature flag and prompt/tool wiring, ON and OFF
          D real agent graph with a fake model | E run_alert | F API (FastAPI TestClient) | G UI <-> backend contract"""
import itertools, json, os, re, sys, tempfile, time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent
os.environ["SRE_MEMORY_DIR"] = tempfile.mkdtemp(prefix="sre_mem_test_")
os.environ["SRE_LOG_DIR"] = tempfile.mkdtemp(prefix="sre_log_test_")
sys.path.insert(0, str(ROOT))
for k in [k for k in os.environ if k.startswith("SRE_HYPOTHESIS")] + ["SRE_PREFLIGHT", "SRE_CLUSTER_LOGS", "SRE_LEAN_TOOLS", "SRE_COMPACT_REPORTS", "SRE_BUDGET", "SRE_TOOL_BUDGET",
                                                                    "SRE_LOCALIZER", "SRE_FALSIFY", "SRE_MEMORY"]:
    os.environ.pop(k, None)

res = []
check = lambda name, cond, d="": res.append((name, bool(cond), d))

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel      # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage                              # noqa: E402
from langchain_core.utils.function_calling import convert_to_openai_tool               # noqa: E402
from sre_agent import features as F, hypothesis as H, agent as A, budget as B          # noqa: E402
from sre_agent import runner as R, incident as I                                       # noqa: E402

TOOL = {t.name: t for t in H.TOOLS}


def fresh():
    H.begin()


def obs(sig, tool="x"):
    return H.observe(tool, (sig,), "data")


def prop(statement, preds, **kw):
    return TOOL["propose_hypothesis"].invoke({"statement": statement, "predictions": preds, **kw})


def ev(h, p, result, ids, note=""):
    return TOOL["record_evidence"].invoke({"hypothesis_id": h, "prediction_id": p, "result": result, "observation_ids": ids, "note": note})


def view(hid):
    return next(h for h in H.snapshot()["hypotheses"] if h["id"] == hid)


def two_hypotheses():
    fresh()
    prop("a downstream store is refusing connections", ["if true, error logs name the store", "if true, the store pod is not ready"], component="store")
    prop("the front service is overloaded by traffic", ["if true, request rate is far above normal", "if true, cpu is saturated"], component="front")


# ================= A. ledger rules =================
fresh()
check("propose: empty statement refused", prop("  ", ["p"]).startswith("REJECTED"))
check("propose: a hypothesis with no predictions is refused (it cannot be tested)", prop("x", []).startswith("REJECTED") and prop("x", ["  "]).startswith("REJECTED"))
check("propose: unknown source refused", prop("x", ["p"], source="gut-feeling").startswith("REJECTED"))
check("nothing was recorded by the refusals", H.snapshot()["hypotheses"] == [])
r = prop("cause one", ["if true, a"], stated_confidence="very high")
check("propose: recorded as proposed with confidence 0 however sure the model sounds", "H1 recorded" in r and view("H1")["status"] == "proposed" and view("H1")["confidence"] == 0.0)
check("propose: duplicate statement refused", prop("CAUSE ONE", ["if true, b"]).startswith("REJECTED"))
check("propose: says how many more competing hypotheses are needed", "at least 1 competing" in r, r)
for i in range(2, H.MAX_HYPOTHESES + 1):
    prop(f"cause {i}", ["if true, p"])
check("propose: capped", prop("one too many", ["p"]).startswith("REJECTED: at most"))
fresh()
prop("many", [f"pred {i}" for i in range(9)])
check("at most 5 predictions kept, ids P1..P5", [p["id"] for p in view("H1")["predictions"]] == ["P1", "P2", "P3", "P4", "P5"])

two_hypotheses()
o_m, o_l, o_m2 = obs("metrics"), obs("logs"), obs("metrics")
check("evidence: unknown hypothesis refused", ev("H9", "P1", "confirmed", [o_m]).startswith("REJECTED: unknown hypothesis"))
check("evidence: unknown prediction refused", ev("H1", "P7", "confirmed", [o_m]).startswith("REJECTED: H1 has predictions"))
check("evidence: bad result word refused", ev("H1", "P1", "probably", [o_m]).startswith("REJECTED: result must be"))
check("evidence: an id that no tool produced is refused (nothing can be made up)", ev("H1", "P1", "confirmed", ["O99"]).startswith("REJECTED: unknown observation"))
check("evidence: confirmed with no id is refused (reasoning and recalled incidents have no id)", ev("H1", "P1", "confirmed", []).startswith("REJECTED: confirmed needs"))
check("evidence: violated with no id is refused", ev("H1", "P1", "violated", []).startswith("REJECTED: violated needs"))
check("none of the refusals changed the ledger", view("H1")["evidence"] == [] and view("H1")["status"] == "proposed")
check("evidence: '[O1]' and lower case ids are accepted as written on a tool result", "H1/P1 confirmed" in ev("h1", "p1", "confirmed", ["[" + o_m.lower() + "]"]))
a = view("H1")
check("ONE signal type is not enough: status stays testing, confidence capped at 0.4", a["status"] == "testing" and a["confidence"] <= 0.4 and a["signals"] == ["metrics"], str(a))
ev("H1", "P2", "confirmed", [o_m2])
check("a second observation of the SAME signal type does not make it supported", view("H1")["status"] == "testing" and view("H1")["signals"] == ["metrics"])
ev("H1", "P2", "confirmed", [o_l])
a = view("H1")
check("confirmed predictions from two different signal types: supported", a["status"] == "supported" and a["signals"] == ["logs", "metrics"], str(a))
check("signal types come from the observations, never from what the model passes", "signals" not in TOOL["record_evidence"].args)
check("supported confidence is above the 0.4 cap of unsupported ones", a["confidence"] > 0.4, str(a["confidence"]))

# stated confidence never enters the score
two_hypotheses()
two_hypotheses(); prop("third", ["if true, z"], stated_confidence="certain 100%")
x1 = obs("metrics"); ev("H3", "P1", "confirmed", [x1])
c_hi = view("H3")["confidence"]
two_hypotheses(); prop("third", ["if true, z"], stated_confidence="")
x1 = obs("metrics"); ev("H3", "P1", "confirmed", [x1])
check("identical evidence gives identical confidence whatever the model said about itself", c_hi == view("H3")["confidence"] > 0 and view("H3")["stated_confidence"] == "")

# violated -> refuted
two_hypotheses()
m1, l1 = obs("metrics"), obs("logs")
ev("H2", "P1", "violated", [m1])
b = view("H2")
check("one violated prediction backed by an observation refutes the hypothesis, confidence 0", b["status"] == "refuted" and b["confidence"] == 0.0, str(b))
ev("H2", "P2", "confirmed", [m1]); ev("H2", "P2", "confirmed", [l1])
check("a refuted hypothesis cannot be rescued by confirming its other predictions", view("H2")["status"] == "refuted")
os.environ["SRE_HYPOTHESIS_REFUTE_MIN"] = "2"
check("refutation threshold is configurable (2 violating observations)", view("H2")["status"] == "testing" and view("H2")["confidence"] <= 0.3)
ev("H2", "P1", "violated", [l1])
check("... and two distinct observations refute again", view("H2")["status"] == "refuted")
os.environ.pop("SRE_HYPOTHESIS_REFUTE_MIN")

# latest result per prediction wins; unclear never overrides
two_hypotheses()
m1, l1 = obs("metrics"), obs("logs")
ev("H1", "P1", "confirmed", [m1]); ev("H1", "P1", "unclear", [])
check("'unclear' does not undo a confirmed prediction and adds no signal", view("H1")["prediction_results"]["P1"] == "confirmed" and view("H1")["signals"] == ["metrics"])
ev("H1", "P1", "violated", [l1])
check("a later violated result on the same prediction replaces the confirmed one", view("H1")["prediction_results"]["P1"] == "violated" and view("H1")["status"] == "refuted")

# diagnosis states
def support(hid, sigs=("metrics", "logs")):
    ids = [obs(s) for s in sigs]
    for i, o in enumerate(ids):
        ev(hid, f"P{i + 1}", "confirmed", [o])

fresh()
prop("only idea", ["if true, a", "if true, b"]); support("H1")
check("diagnosis: one hypothesis is never enough, however well supported", H.diagnosis()["state"] == "insufficient_hypotheses" and not H.diagnosis()["allowed"])
two_hypotheses()
check("diagnosis: nothing tested -> unsupported", H.diagnosis()["state"] == "unsupported" and not H.diagnosis()["allowed"])
support("H1")
d = H.diagnosis()
check("diagnosis: a supported leader with an UNTESTED rival is not confirmed", d["state"] == "rivals_untested" and not d["allowed"] and d["hypothesis"] == "H1", str(d))
ev("H2", "P1", "violated", [obs("metrics")])
d = H.diagnosis()
check("diagnosis: leader supported and the rival refuted -> confirmed", d["state"] == "confirmed" and d["allowed"] and d["hypothesis"] == "H1", str(d))
two_hypotheses(); support("H1"); ev("H2", "P1", "confirmed", [obs("metrics")])
check("diagnosis: a rival that was tested and is not supported does not block", H.diagnosis()["state"] == "confirmed")
support("H2")
d = H.diagnosis()
check("diagnosis: two supported hypotheses -> contested, nothing confirmed", d["state"] == "contested" and not d["allowed"] and d["hypothesis"] is None, str(d))
os.environ["SRE_HYPOTHESIS_MIN_SIGNALS"] = "3"
check("signal threshold is configurable: with 3 required, two signal types support nothing", H.diagnosis()["state"] == "unsupported")
os.environ["SRE_HYPOTHESIS_MIN_SIGNALS"] = "junk"
check("bad env value falls back to the default", H.min_signals() == 2)
os.environ.pop("SRE_HYPOTHESIS_MIN_SIGNALS")
os.environ["SRE_HYPOTHESIS_MIN_COUNT"] = "3"
two_hypotheses(); support("H1"); ev("H2", "P1", "violated", [obs("metrics")])
check("minimum number of competing hypotheses is configurable", H.diagnosis()["state"] == "insufficient_hypotheses")
os.environ.pop("SRE_HYPOTHESIS_MIN_COUNT")

# next tests: discriminating, untested, live only
fresh()
prop("store refuses connections", ["if true, error logs show connection refused to the store", "if true, request rate is normal"])
prop("front overloaded", ["if true, request rate is normal", "if true, cpu saturated on front pods"])
nt = H.next_tests()
check("next tests: one per live hypothesis, the prediction its rival shares least", {(t["hypothesis"], t["prediction"]) for t in nt} == {("H1", "P1"), ("H2", "P2")}, str(nt))
check("next tests: the shared prediction ('request rate is normal') is never the pick", all("request rate is normal" not in t["text"] for t in nt))
ev("H1", "P1", "confirmed", [obs("logs")]); ev("H2", "P2", "violated", [obs("metrics")])
nt = H.next_tests()
check("next tests: tested predictions and refuted hypotheses are skipped", [(t["hypothesis"], t["prediction"]) for t in nt] == [("H1", "P2")], str(nt))
check("ledger text lists status, prediction results, diagnosis and next tests", all(s in H.render() for s in ("H1 TESTING", "H2 REFUTED", "VIOLATED", "DIAGNOSIS:", "NEXT TESTS")))
H.begin()
check("begin() empties ledger and observations (nothing leaks into the next incident)", H.snapshot()["hypotheses"] == [] and H.snapshot()["observations"] == [] and H.snapshot()["gate_blocked"] == 0)
check("empty ledger renders an instruction, not an error", "propose_hypothesis" in H.render())

# final note
fresh()
check("final note: healthy system, nothing hypothesised -> no note", H.final_note() == "")
I.begin(["svc: HTTP 5xx rate 90% (threshold 5%)"], onset_ts=time.time() - 60)
n = H.final_note()
check("final note: symptoms but no ledger -> says the ledger is empty and NOT CONFIRMED", "empty" in n and "NOT CONFIRMED" in n)
two_hypotheses(); support("H1")
check("final note: unconfirmed diagnosis carries a warning and the computed ledger", "NOT CONFIRMED" in H.final_note() and "computed by code" in H.final_note())
ev("H2", "P1", "violated", [obs("logs")])
n = H.final_note()
check("final note: confirmed diagnosis has the ledger and no warning", "H1 SUPPORTED" in n and "NOT CONFIRMED" not in n)
I.begin([], onset_ts=time.time())

# ================= B. observations and the gate =================
_SAVED = (dict(H.SIGNAL_OF), B.READ_TOOLS, H.WRITE_NAMES)          # section B swaps in tiny maps; restored before section C
H.SIGNAL_OF.clear(); H.SIGNAL_OF.update({"search_logs": "logs", "system_overview": "metrics"})
rec, mk = H.ObservationRecorder(), (lambda n, **a: SimpleNamespace(tool_call={"name": n, "id": "c1", "args": a}))
tm = lambda c, st="success": ToolMessage(content=c, tool_call_id="c1", name="t", status=st)
fresh()
o = rec.wrap_tool_call(mk("search_logs", service="s"), lambda r: tm("3 errors"))
check("recorder: a successful read result is stamped with its id and keeps its text", o.content == "[O1] 3 errors" and o.tool_call_id == "c1")
check("recorder: the id is registered with the signal type of the tool", H.snapshot()["observations"][0] | {} == {"id": "O1", "tool": "search_logs", "signals": ["logs"], "chars": 8})
check("recorder: tool errors are not observations", rec.wrap_tool_call(mk("search_logs"), lambda r: tm("boom", "error")).content == "boom"
      and rec.wrap_tool_call(mk("search_logs"), lambda r: tm("Error: bad arg")).content == "Error: bad arg"
      and rec.wrap_tool_call(mk("search_logs"), lambda r: tm("kubectl error: x")).content == "kubectl error: x" and len(H.snapshot()["observations"]) == 1)
check("recorder: tools that are not read tools are left alone", rec.wrap_tool_call(mk("write_todos"), lambda r: tm("plan")).content == "plan")
check("recorder: a non-text result passes through untouched", rec.wrap_tool_call(mk("search_logs"), lambda r: "raw") == "raw")
os.environ["SRE_TOOL_BUDGET"] = "1"; B.reset(); B.READ_TOOLS = frozenset({"search_logs"})
chain = lambda req: B.ToolBudget().wrap_tool_call(req, lambda r: rec.wrap_tool_call(r, lambda r2: tm("ok")))
fresh(); first, second = chain(mk("search_logs")), chain(mk("search_logs"))
check("recorder inside the budget: a call the budget refused is not an observation", first.content.startswith("[O1]") and "BUDGET REACHED" in second.content and len(H.snapshot()["observations"]) == 1)
os.environ.pop("SRE_TOOL_BUDGET")

H.WRITE_NAMES = frozenset({"restart_deployment"})
gate, ran = H.RemediationGate(), []
run = lambda n, **a: gate.wrap_tool_call(mk(n, **a), lambda r: (ran.append(n), "ran")[1])
two_hypotheses()
r1 = run("task", subagent_type="remediator", description="restart it")
check("gate: delegating to the remediator is refused while nothing is confirmed, and the task does not run", getattr(r1, "status", "") == "error" and "BLOCKED by the hypothesis ledger" in r1.content and "unsupported" in r1.content and not ran, str(getattr(r1, "content", r1)))
check("gate: the refusal tells the model what to do instead", "NEXT TESTS" in r1.content and "escalate" in r1.content)
r2 = run("restart_deployment", name="x")
check("gate: the write tool itself is refused too (second line of defence)", "BLOCKED" in r2.content and not ran)
check("gate: refusals are counted", H.snapshot()["gate_blocked"] == 2)
check("gate: reads, other subagents, verify and the plan are never blocked",
      [run("task", subagent_type="log-inspector"), run("search_logs"), run("verify_resolved"), run("write_todos"), run("save_incident")] == ["ran"] * 5)
support("H1")
check("gate: still refused while the rival is untested", "BLOCKED" in run("task", subagent_type="remediator").content)
ev("H2", "P1", "violated", [obs("metrics")])
ran.clear()
check("gate: opens when the diagnosis is confirmed", run("task", subagent_type="remediator") == "ran" and run("restart_deployment", name="x") == "ran")
two_hypotheses()
os.environ["SRE_HYPOTHESIS_GATE"] = "0"
check("gate: SRE_HYPOTHESIS_GATE=0 makes it advisory (nothing blocked)", run("task", subagent_type="remediator") == "ran")
os.environ.pop("SRE_HYPOTHESIS_GATE")

H.SIGNAL_OF.clear(); H.SIGNAL_OF.update(_SAVED[0]); B.READ_TOOLS, H.WRITE_NAMES = _SAVED[1], _SAVED[2]

# ================= C. flag, prompts, wiring: ON and OFF =================
names = list(F.NAMES)
check("flag: hypothesis is OFF by default (existing setups are unchanged)", F.from_env().hypothesis is False and F.Features().hypothesis is False)
os.environ["SRE_HYPOTHESIS"] = "1"
check("flag: SRE_HYPOTHESIS switches it on from env", F.from_env().hypothesis and not F.from_env().budget)
os.environ.pop("SRE_HYPOTHESIS")
check("flag: it is the LAST field, so every positional Features(...) call and saved result keeps its meaning", names[-1] == "hypothesis" and names[:8] == [
      "localizer", "falsify", "memory", "preflight", "cluster_logs", "lean_tools", "compact_reports", "budget"])
check("flag: parse('hypothesis') turns on only that technique", F.parse("hypothesis").as_dict() == {**{n: False for n in names}, "hypothesis": True})
check("flag: a list keeps the others as typed", F.parse("memory,hypothesis").label() == "memory+hypothesis")
check("flag: the four behaviour arms and the cost arms are unchanged, and none has it", all(not F.ARMS[a].hypothesis for a in list(F.DEFAULT_ARMS) + list(F.COST_ARMS))
      and F.DEFAULT_ARMS == ("llm-only", "localizer", "falsify", "memory"))
check("flag: opt-in arm 'hypothesis-driven' = full behaviour set + ledger, not pre-selected, name does not collide with the feature name",
      F.ARMS["hypothesis-driven"].label() == "localizer+falsify+memory+hypothesis" and "hypothesis-driven" not in F.DEFAULT_ARMS and "hypothesis-driven" not in F.NAMES)

HYP_TEXT = ("HYPOTHESIS LEDGER", "propose_hypothesis", "record_evidence", "hypothesis_ledger")
old_flags = [n for n in names if n != "hypothesis"]
bad_off, bad_on, n_combo = [], [], 0
for bits in itertools.product([False, True], repeat=len(old_flags)):
    base = F.Features(**dict(zip(old_flags, bits)))
    on = F.Features(**dict(zip(old_flags, bits)), hypothesis=True)
    p_off, p_on = A.system_prompt(base), A.system_prompt(on)
    n_combo += 1
    if any(s in p_off for s in HYP_TEXT):
        bad_off.append(base.label())
    if p_on != p_off + A.HYPOTHESIS_PROMPT:
        bad_on.append(base.label())
check(f"OFF: no combination of the other {len(old_flags)} techniques ({n_combo} prompts) mentions the ledger", not bad_off, str(bad_off[:2]))
check("ON: the prompt is the OFF prompt plus the ledger section, byte for byte, for every combination", not bad_on, str(bad_on[:2]))
mw = lambda f: {s["name"]: [type(m).__name__ for m in s["middleware"]] for s in A.make_subagents(f)}
m_off, m_on = mw(F.Features(budget=True, lean_tools=True)), mw(F.Features(budget=True, lean_tools=True, hypothesis=True))
check("OFF: subagents get no recorder and no gate", not any("Observation" in x or "Gate" in x for v in m_off.values() for x in v))
check("ON: every inspector records observations (after the budget); the remediator gets the gate", all(m_on[n][-1] == "ObservationRecorder" and m_on[n][-2] == "ToolBudget" for n in m_on if n != "remediator")
      and m_on["remediator"][-1] == "RemediationGate")
sp = lambda f: {s["name"]: s["system_prompt"] for s in A.make_subagents(f)}
s_off, s_on = sp(F.Features(falsify=True, compact_reports=True)), sp(F.Features(falsify=True, compact_reports=True, hypothesis=True))
check("ON: inspectors are told to cite [O#] ids and to say 'no data'; OFF prompts are untouched, remediator prompt never changes",
      all(s_on[n] == s_off[n] + A.SUB_HYPOTHESIS for n in s_on if n != "remediator") and s_on["remediator"] == s_off["remediator"] and "[O7]" not in "".join(s_off.values()))
check("every read tool has a signal type (a new read tool is classified automatically); writes are known to the gate",
      set(B.READ_TOOLS) <= set(H.SIGNAL_OF) and {"restart_deployment", "rollback_deployment"} <= set(H.WRITE_NAMES) and H.SIGNAL_OF["get_pod_logs"] == "logs" and H.SIGNAL_OF["rollout_diff"] == "cluster"
      and H.SIGNAL_OF["find_traces"] == "traces" and H.SIGNAL_OF["system_overview"] == "metrics" and "current_problems" not in H.SIGNAL_OF)
src = "\n".join([(ROOT / "sre_agent" / "hypothesis.py").read_text(encoding="utf-8"), A.HYPOTHESIS_PROMPT, A.SUB_HYPOTHESIS]).lower()
SYSTEM_NAMES = ["payment", "order-service", "user-service", "api-gateway", "postgres", "redis", "playground", "memory_leak", "faultctl", "ground_truth", "distractor"]
check("generic: no system, service, fault or scenario name anywhere in the engine or its prompts", not [w for w in SYSTEM_NAMES if w in src], str([w for w in SYSTEM_NAMES if w in src]))
check("generic: the engine ships no pre-written hypotheses", not any(w in src for w in ("oomkilled", "crashloop", "connection pool", "bad rollout")))

# ================= D. the real agent graph with a fake model =================
def graph(f, script, ledger=None):
    """Build the real agent, run it with a scripted model. Returns (messages, number of model calls, tool names the orchestrator was shown)."""
    calls, shown = [], []

    class Rec(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            self._t = [convert_to_openai_tool(t)["function"]["name"] for t in tools]; return self
        def _generate(self, messages, stop=None, run_manager=None, **kw):
            calls.append(1); shown.append(list(getattr(self, "_t", []))); return super()._generate(messages, stop, run_manager, **kw)
    m = Rec(messages=iter(script))
    A.get_llm, A.get_subagent_llm = (lambda *a, **k: m), (lambda *a, **k: None)
    fresh()
    if ledger:
        ledger()
    out = A.build_agent(f).invoke({"messages": [{"role": "user", "content": "ALERT: x"}]}, {"configurable": {"thread_id": "t"}})
    return out["messages"], len(calls), shown


def task(sub):
    return AIMessage(content="", tool_calls=[{"name": "task", "args": {"subagent_type": sub, "description": "restart the thing"}, "id": "c1"}])


script = lambda: [task("remediator"), AIMessage(content="sub done"), AIMessage(content="final")]
msgs, n_off, shown_off = graph(F.Features(), script())
check("OFF (regression): the orchestrator is not shown any ledger tool", not {"propose_hypothesis", "record_evidence", "hypothesis_ledger"} & set(shown_off[0]), str(shown_off[0]))
check("OFF (regression): delegating to the remediator is NOT blocked (old behaviour: subagent runs, 3 model calls)", n_off == 3 and not any("BLOCKED" in str(m.content) for m in msgs), str(n_off))
msgs, n_on, shown_on = graph(F.Features(hypothesis=True), [task("remediator"), AIMessage(content="final")])
check("ON: the orchestrator is shown exactly the three ledger tools", {"propose_hypothesis", "record_evidence", "hypothesis_ledger"} <= set(shown_on[0]), str(shown_on[0]))
blocked = [m for m in msgs if isinstance(m, ToolMessage) and "BLOCKED by the hypothesis ledger" in str(m.content)]
check("ON: delegating to the remediator with no supported diagnosis is refused in the real graph; the remediator never runs (2 model calls, no approval prompt)", len(blocked) == 1 and n_on == 2, f"{n_on} {len(blocked)}")
check("ON: the refusal is recorded for the run", H.snapshot()["gate_blocked"] == 1)


def confirmed_ledger():
    two_hypotheses(); support("H1"); ev("H2", "P1", "violated", [obs("metrics")])


msgs, n_ok, _ = graph(F.Features(hypothesis=True), script(), ledger=confirmed_ledger)
check("ON: with a confirmed diagnosis the same delegation goes through (3 model calls)", n_ok == 3 and not any("BLOCKED" in str(m.content) for m in msgs), str(n_ok))
msgs, _, _ = graph(F.Features(hypothesis=True), [task("log-inspector"), AIMessage(content="sub"), AIMessage(content="final")])
check("ON: investigation (inspector delegation) is never blocked", not any("BLOCKED" in str(m.content) for m in msgs))
# the ledger tools through the real tool node: ids are verified against what the middleware recorded
H.SIGNAL_OF["search_logs"] = "logs"
fresh(); prop("a", ["if true, x"]); prop("b", ["if true, y"])
check("ledger tools are plain LangChain tools with a schema the model can call (required arguments declared)",
      all(convert_to_openai_tool(t)["function"]["parameters"].get("required") for t in H.TOOLS if t.name != "hypothesis_ledger") and not convert_to_openai_tool(TOOL["hypothesis_ledger"])["function"]["parameters"].get("required"))

# ================= E. run_alert, both modes =================
class FakeAgent:
    def __init__(self, fill=None):
        self.first, self.fill = None, fill
    def invoke(self, payload, cfg):
        self.first = payload["messages"][0]["content"] if isinstance(payload, dict) else None
        if self.fill:
            self.fill()
        return {"messages": [SimpleNamespace(content="Root cause: the store is down.")]}


def run_with(f, fill=None, seed_stale=False, loc="", pre=""):
    fake = FakeAgent(fill)
    R.build_agent = lambda f_, t_=None: fake
    R.find_problems = lambda: ["svc: HTTP 5xx rate 90% (threshold 5%)"]
    R.snapshot_line = lambda: "svc: 5xx 90%"
    R.localize, R.preflight = (lambda: loc), (lambda: pre)
    if seed_stale:
        fresh(); prop("stale from a previous run", ["if true, p"])
    out = R.run_alert("svc 5xx", auto_approve=True, features=f, onset=time.time() - 60)
    return out, fake


unconfirmed = lambda: (two_hypotheses(), support("H1"))
out, fake = run_with(F.Features(), seed_stale=True)
check("run OFF (regression): result carries no ledger and the final answer is exactly the agent's", out["hypotheses"] is None and out["final"] == "Root cause: the store is down." and out["features"]["hypothesis"] is False)
check("run OFF: first message has no observation ids", "[O" not in fake.first)
out, fake = run_with(F.Features(hypothesis=True), fill=unconfirmed, seed_stale=True)
check("run ON: the flag reaches the run result", out["features"]["hypothesis"] is True)
check("run ON: the ledger is reset at the start (the stale hypothesis from a previous run is gone)", all("stale" not in h["statement"] for h in out["hypotheses"]["hypotheses"]) and len(out["hypotheses"]["hypotheses"]) == 2)
check("run ON: an unconfirmed diagnosis is flagged in the final answer, the agent's own words are kept", out["final"].startswith("Root cause: the store is down.") and "NOT CONFIRMED" in out["final"] and "rivals_untested" in out["final"], out["final"][-200:])
check("run ON: result carries the ledger, diagnosis and config", out["hypotheses"]["diagnosis"]["state"] == "rivals_untested" and out["hypotheses"]["config"]["gate"] is True)
out, _ = run_with(F.Features(hypothesis=True), fill=lambda: (confirmed_ledger()))
check("run ON: a confirmed diagnosis gets the ledger but no warning", "H1 SUPPORTED" in out["final"] and "NOT CONFIRMED" not in out["final"] and out["hypotheses"]["diagnosis"]["allowed"])
json.dumps(out["hypotheses"])
check("run ON: the ledger in the result is JSON-serialisable (API and result files)", True)
out, fake = run_with(F.Features(hypothesis=True, localizer=True, preflight=True), loc="svc: errors 90% (before 0%)", pre="log pattern x5")
check("run ON: localizer and pre-flight blocks carry observation ids the ledger accepts (metrics / logs+cluster)",
      "LOCALIZER [O1]" in fake.first and "PRE-FLIGHT [O2]" in fake.first
      and [(o["id"], o["signals"]) for o in out["hypotheses"]["observations"]] == [("O1", ["metrics"]), ("O2", ["logs", "cluster"])], fake.first)
out, fake = run_with(F.Features(localizer=True, preflight=True), loc="svc: errors 90%", pre="log pattern x5")
check("run OFF: localizer and pre-flight blocks are exactly as before (no ids)", "LOCALIZER (deterministic" in fake.first and "PRE-FLIGHT (collected" in fake.first and "[O" not in fake.first)
out, _ = run_with(F.Features(hypothesis=True))
check("run ON, agent proposed nothing: the final answer says so and that nothing is confirmed", "empty" in out["final"] and "NOT CONFIRMED" in out["final"])

# ================= F. API =================
from fastapi.testclient import TestClient            # noqa: E402
import api as API                                    # noqa: E402

cl = TestClient(API.app, base_url="http://127.0.0.1:8765")
fi = cl.get("/api/features").json()
check("api: features endpoint lists the technique, off by default, plus its opt-in arm", fi["defaults"]["hypothesis"] is False and "hypothesis-driven" in fi["arms"] and fi["arms"]["hypothesis-driven"]["hypothesis"] is True
      and fi["default_arms"] == ["llm-only", "localizer", "falsify", "memory"])
os.environ["SRE_HYPOTHESIS"] = "true"
check("api: the env default is reported", cl.get("/api/features").json()["defaults"]["hypothesis"] is True)
os.environ.pop("SRE_HYPOTHESIS")

seen = {}


def fake_run_alert(alert, auto_approve=False, ask=input, on_start=None, features=None, onset=None, tuning=None):
    seen.update(features=features, tuning=tuning)
    H.begin()
    if features.hypothesis:
        two_hypotheses()
    return {"final": "done", "actions": [], "stats": {}, "rag_hit": False, "seconds": 1, "features": features.as_dict(), "tuning": tuning.as_dict(), "models": {},
            "hypotheses": H.snapshot() if features.hypothesis else None}


API.run_alert = fake_run_alert
API.find_problems = lambda: ["svc: HTTP 5xx rate 90%"]


def start(features):
    body = {"alert": "x", "auto_approve": True}
    if features is not None:
        body["features"] = features
    r = cl.post("/api/runs", json=body)
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    for _ in range(100):
        j = cl.get(f"/api/runs/{rid}").json()
        if j["status"] in ("done", "error"):
            return rid, j
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def uniq():
    time.sleep(1.05)          # run ids are HHMMSS


rid, j = start({"memory": True, "hypothesis": True})
check("api: the toggle reaches the run record and the agent (Features.hypothesis is True in run_alert)", j["features"]["hypothesis"] is True and seen["features"].hypothesis is True and seen["features"].memory is True)
check("api: the run result carries the ledger", j["status"] == "done" and len(j["result"]["hypotheses"]["hypotheses"]) == 2 and j["result"]["features"]["hypothesis"] is True)
g = cl.get(f"/api/runs/{rid}/hypotheses").json()
check("api: /hypotheses returns the finished ledger", g["enabled"] is True and g["ledger"]["diagnosis"]["state"] == "unsupported" and len(g["ledger"]["hypotheses"]) == 2)
uniq()
rid2, j2 = start({"memory": True, "hypothesis": False})
check("api: toggle OFF reaches the agent as False", j2["features"]["hypothesis"] is False and seen["features"].hypothesis is False)
g2 = cl.get(f"/api/runs/{rid2}/hypotheses").json()
check("api: an OFF run has no ledger: enabled false, result has hypotheses null", g2 == {"enabled": False, "ledger": None} and j2["result"]["hypotheses"] is None)
uniq()
rid3, j3 = start({"memory": True})
check("api: an older client that does not send the key gets it OFF (backward compatible)", j3["features"]["hypothesis"] is False)
uniq()
os.environ["SRE_HYPOTHESIS"] = "1"
rid4, j4 = start(None)
check("api: no features sent at all -> env default (here ON)", j4["features"]["hypothesis"] is True)
os.environ.pop("SRE_HYPOTHESIS")
check("api: unknown run -> 404", cl.get("/api/runs/nope/hypotheses").status_code == 404)

# live ledger while running
import threading                                     # noqa: E402
gate_ev = threading.Event()


def slow_run_alert(alert, auto_approve=False, ask=input, on_start=None, features=None, onset=None, tuning=None):
    H.begin(); two_hypotheses(); gate_ev.wait(5)
    return fake_run_alert(alert, auto_approve, ask, on_start, features, onset, tuning)


uniq()
API.run_alert = slow_run_alert
r = cl.post("/api/runs", json={"alert": "x", "features": {"hypothesis": True}}).json()
time.sleep(0.2)
live = cl.get(f"/api/runs/{r['id']}/hypotheses").json()
check("api: while the run is active the live ledger is served", live["enabled"] and live["ledger"] and len(live["ledger"]["hypotheses"]) == 2)
gate_ev.set()
for _ in range(100):
    if cl.get(f"/api/runs/{r['id']}").json()["status"] == "done":
        break
    time.sleep(0.05)
API.run_alert = fake_run_alert

# suite subprocess: the toggle is passed on the command line and parses back to the same features
popen = []
API.subprocess.Popen = lambda cmd, **kw: (popen.append(cmd), SimpleNamespace(poll=lambda: 0))[1]
API.busy = lambda: None


def suite(features):
    popen.clear()
    r = cl.post("/api/suite", json={"scenarios": ["s1"], "runs": 1, "negative": False, "features": features})
    assert r.status_code == 200, r.text
    return popen[0][popen[0].index("--features") + 1]


arg = suite({"memory": True, "hypothesis": True})
check("api suite: the toggle is sent to the harness and parses back to the same features", F.parse(arg).hypothesis and F.parse(arg).memory and not F.parse(arg).budget, arg)
arg = suite({"hypothesis": True})
check("api suite: hypothesis ALONE is sent as the technique, not expanded into an arm (name collision guard)", F.parse(arg).as_dict() == {**{n: False for n in names}, "hypothesis": True}, arg)
arg = suite({"memory": True, "hypothesis": False})
check("api suite: toggle OFF is not sent", "hypothesis" not in arg.split(","))
popen.clear()
cl.post("/api/suite", json={"scenarios": ["s1"], "runs": 2, "compare": True, "arms": ["memory", "hypothesis-driven"]})
check("api suite: compare accepts the opt-in arm", "memory,hypothesis-driven" in popen[0])
r = cl.post("/api/suite", json={"scenarios": ["s1"], "compare": True, "arms": ["nonsense"]})
check("api suite: unknown arm still refused", r.status_code == 400)

# ================= G. UI <-> backend contract (static; behaviour is in tests_ui.js) =================
html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
feats_js = re.search(r"const FEATS=\[(.*?)\];\nlet featInfo", html, re.S).group(1)
ui_keys = re.findall(r'\["(\w+)","', feats_js)
check("ui: every backend technique has a toggle and the toggle names match the backend exactly", ui_keys == names, f"{ui_keys} vs {names}")
check("ui: the toggle is rendered in BOTH the Investigate form and the failure-test form from the same list", 'featBoxes("rf",featInfo.defaults)' in html and 'featBoxes("sf",featInfo.defaults)' in html)
check("ui: the Investigate request sends every toggle (including this one) to /api/runs", 'api("/api/runs",{alert:$("#alert").value,auto_approve:$("#auto").checked,features:featVals("rf")})' in html)
check("ui: the toggle's default comes from the server (env), not hard-coded", 'on[k]?"checked":""' in html)
check("ui: the ledger panel is polled only for runs started with the technique and cleared otherwise", "r.features.hypothesis" in html and 'id="hypBox"' in html and "/hypotheses" in html)

for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok or not d else f"  [{d}]"))
bad = [r for r in res if not r[1]]
print(f"\n{len(res) - len(bad)}/{len(res)} passed")
sys.exit(1 if bad else 0)

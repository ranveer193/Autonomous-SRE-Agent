import time, uuid
from langgraph.types import Command
from .agent import build_agent
from .config import PROVIDER, MODEL_NAME, SUB_PROVIDER, SUB_MODEL
from . import budget, features as feature_state, tuning as tuning_state
from .features import Features, from_env
from .tuning import Tuning, from_env as tuning_from_env
from .localizer import localize_ex
from .preflight import preflight
from .detect import snapshot_line, find_problems
from . import incident
from . import hypothesis as H
from .rag import start_incident_clock
from .tools.k8s import reset_attempts
from .logging_utils import AgentLogger, setup_logging


def run_alert(alert: str, auto_approve: bool = False, ask=input, on_start=None, features: Features | None = None,
              onset: float | None = None, tuning: Tuning | None = None) -> dict:
    """Run one incident. Returns final answer, approved/rejected actions, rag_hit, wall seconds.
    `onset` = epoch seconds when the alert fired (what an alert rule gives you). Pass it when you have it: the window the tools look
    at then starts there instead of being estimated from the metrics.
    `tuning` = the numbers behind the techniques (tool-call cap, report length ...); omitted = env, then the defaults."""
    f = features or from_env()
    t = tuning or tuning_from_env()
    feature_state.set_active(f)                     # module-level tools (log clustering) read the features of THIS run
    tuning_state.set_active(t)                      # ... and its tuning (budget cap, log fetch size, pre-flight size)
    budget.reset()
    agent = build_agent(f, t)
    tracer = AgentLogger(setup_logging("incident"))
    if on_start:
        on_start(tracer.path)                     # lets a UI tail the live JSONL trace
    cfg = {"configurable": {"thread_id": str(uuid.uuid4())}, "recursion_limit": t.recursion_limit, "callbacks": [tracer]}
    tracer.note("🚨", f"ALERT: {alert}")
    models = {"main": f"{PROVIDER}/{MODEL_NAME}", "subagents": f"{SUB_PROVIDER}/{SUB_MODEL}"}
    tracer.note("🧪", f"features: {f.label()} | tuning: {t.label()} | models: {models}", event="features", features=f.as_dict(), tuning=t.as_dict(), models=models)
    start_incident_clock()
    reset_attempts()
    incident.begin([p for p in find_problems() if not p.startswith("detector error")], onset_ts=onset)   # onset, symptom signature, clean write log
    H.begin()                                         # empty ledger and observation registry (nothing from the previous run; unused when the feature is off)
    tracer.note("🕒", f"incident window: {incident.describe_window()} | signature: {incident.signature() or '-'}", event="window")
    t0 = time.time()
    loc_text, loc_s = "", 0.0
    if f.localizer:                                   # deterministic, zero tokens; counted in wall time (and so in MTTR)
        tl = time.time()
        (loc_text, loc_status), loc_s = localize_ex(), round(time.time() - tl, 2)
        _none = {"no_facts": "no metric facts crossed a threshold (not a health verdict)",
                 "incomplete": "some metric queries failed, no facts available (see warnings above)",
                 "error": "evidence board failed (see warning above), no facts available"}
        tracer.note("🧭", f"evidence board ({loc_s}s, {loc_status}): {loc_text or _none.get(loc_status, 'no facts')}", event="localizer", seconds=loc_s, status=loc_status)
    loc_tag = f" [{H.observe('localizer', ('metrics',), loc_text)}]" if f.hypothesis and loc_text else ""     # ledger evidence may cite it: ONE metrics signal
    loc_block = f"\n\nLOCALIZER{loc_tag} (deterministic, from metrics):\n{loc_text}" if loc_text else ""
    pre_text, pre_s = "", 0.0
    if f.preflight:                                   # code only, no tokens; counted in wall time (and so in MTTR)
        tp = time.time()
        pre_text, pre_s = preflight(), round(time.time() - tp, 2)
        tracer.note("🛫", f"pre-flight ({pre_s}s, {len(pre_text)} chars): {pre_text[:300] or 'nothing to report'}", event="preflight", seconds=pre_s, chars=len(pre_text))
    pre_tag = f" [{H.observe('preflight', ('logs', 'cluster'), pre_text)}]" if f.hypothesis and pre_text else ""   # ONE logs + ONE cluster signal
    pre_block = f"\n\nPRE-FLIGHT{pre_tag} (collected by code, no model):\n{pre_text}" if pre_text else ""
    actions: list[dict] = []
    result = agent.invoke({"messages": [{"role": "user", "content":
                          f"ALERT: {alert}\n\nDETECTOR SNAPSHOT (taken just now): {snapshot_line()}{loc_block}{pre_block}"}]}, cfg)
    while result.get("__interrupt__"):
        decisions = []
        for itr in result["__interrupt__"]:
            for req in itr.value["action_requests"]:
                tracer.note("⚠️ ", f"approval requested: {req['name']}({req['args']})", event="approval_request", tool=req["name"], args=req["args"])
                ok = auto_approve or ask(f"\n⚠️  Agent wants: {req['name']}({req['args']})  approve? [y/N] ").strip().lower() == "y"
                actions.append({"tool": req["name"], "args": req["args"], "approved": ok, "t": round(time.time() - t0)})
                tracer.note("✅" if ok else "⛔", f"{'approved' if ok else 'rejected'}: {req['name']}", event="approval")
                decisions.append({"type": "approve"} if ok else {"type": "reject"})
        result = agent.invoke(Command(resume={"decisions": decisions}), cfg)
    msgs = result["messages"]
    rag_hit = incident.recalled()                     # set by search_incidents itself when it returned a past incident
    stats = tracer.summary()
    bud = budget.state() if f.budget else None
    ledger = H.snapshot() if f.hypothesis else None
    if ledger:
        d = ledger["diagnosis"]
        tracer.note("🧩", f"hypothesis ledger: {len(ledger['hypotheses'])} hypotheses, {len(ledger['observations'])} observations, diagnosis={d['state']}, "
                          f"remediation requests refused={ledger['gate_blocked']}", event="hypotheses", ledger=ledger)
    tracer.note("🏁", f"done in {round(time.time() - t0)}s | llm_calls={stats['llm_calls']} tool_calls={stats['tool_calls']} "
                      f"tokens={stats['tokens_in']}→{stats['tokens_out']} rag_hit={rag_hit}"
                      + (f" budget={bud['used']}/{bud['cap']} blocked={bud['blocked']}" if bud else ""), event="done")
    final = msgs[-1].content if isinstance(msgs[-1].content, str) else str(msgs[-1].content)
    if f.hypothesis:
        final += H.final_note()                       # the ledger as code computed it + a warning when no cause is confirmed
    return {"stats": stats, "final": final,
            "actions": actions, "rag_hit": rag_hit, "seconds": round(time.time() - t0),
            "features": f.as_dict(), "tuning": t.as_dict(), "models": models, "localizer_s": loc_s, "preflight_s": pre_s, "budget": bud, "saved_ids": incident.saved_ids(), "hypotheses": ledger}
"""Hypothesis-driven investigation (feature `hypothesis`): competing explanations kept in a ledger that CODE scores.

Without the feature nothing here runs: no tools, no prompt text, no middleware (the agent is built exactly as before).

Why a ledger. The orchestrator used to "combine the reports into ONE root-cause statement". A model can write that statement from one
loud service, a recalled incident or a plausible story, and its own "high confidence" says nothing about whether it is true. Here:

  * Hypotheses are explicit and COMPETING (at least `SRE_HYPOTHESIS_MIN_COUNT`, default 2), each with predictions: "if this is the cause,
    X must be observable". A prediction is a test that can fail.
  * Evidence is TOOL-GROUNDED. A middleware gives every successful read-tool result an id ([O7]) and records which signal type produced
    it (metrics, logs, traces, cluster). `record_evidence` accepts only those ids, so evidence cannot be invented, and a recalled
    incident or the model's reasoning (no id) can never be evidence. What the model still decides is whether an observation confirms or
    violates a prediction; code checks provenance and counts, not meaning.
  * Status and confidence are COMPUTED. refuted = a violated prediction (backed by an observation). supported = confirmed predictions
    from at least `SRE_HYPOTHESIS_MIN_SIGNALS` (default 2) different signal types and nothing violated. The model's stated confidence is
    stored for display and never enters the score.
  * The DIAGNOSIS is `confirmed` only when exactly one hypothesis is supported and no rival is untested. Anything else is
    unsupported / contested / rivals_untested. Remediation is gated on it (`RemediationGate`): delegating to the remediator, and the
    write tools themselves, are refused until the diagnosis is confirmed (`SRE_HYPOTHESIS_GATE=0` makes the gate advisory).
  * `next_tests` picks, per live hypothesis, the untested prediction least shared with its rivals (most discriminating), so one round
    of delegation separates them instead of re-confirming the favourite.

Generic: nothing here names a system, service, fault or scenario. Process-wide state, one incident at a time, reset by `begin()` (same
pattern as incident.py and budget.py)."""
import os
import re
import threading
import time

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langchain.agents.middleware.types import AgentMiddleware

_lock = threading.RLock()
SOURCES = ("alert", "observation", "memory", "localizer", "other")
RESULTS = ("confirmed", "violated", "unclear")
MAX_HYPOTHESES, MAX_PREDICTIONS = 6, 5
SIGNAL_OF: dict[str, str] = {}              # read-tool name -> signal type; filled by agent.py from the real tool lists (a new read tool is counted automatically)
WRITE_NAMES: frozenset = frozenset()        # write-tool names; filled by agent.py
_ERROR_PREFIXES = ("Error", "kubectl error", "BUDGET REACHED")


def _fresh() -> dict:
    return {"obs": {}, "hyps": [], "blocked": 0}


_S: dict = _fresh()


# ---------- configuration (env; lenient like the other env knobs, so a typo is the default) ----------
def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.getenv(name, "")))) if os.getenv(name, "").strip() else default
    except ValueError:
        return default


def min_signals() -> int:
    return _env_int("SRE_HYPOTHESIS_MIN_SIGNALS", 2, 1, 4)


def min_hypotheses() -> int:
    return _env_int("SRE_HYPOTHESIS_MIN_COUNT", 2, 1, MAX_HYPOTHESES)


def refute_min() -> int:
    return _env_int("SRE_HYPOTHESIS_REFUTE_MIN", 1, 1, 5)


def gate_enabled() -> bool:
    return os.getenv("SRE_HYPOTHESIS_GATE", "1").strip().lower() not in ("0", "false", "no", "off")


def begin() -> None:
    """New incident: empty ledger, empty observation registry."""
    with _lock:
        _S.clear()
        _S.update(_fresh())


# ---------- observations: the only thing evidence may point at ----------
def observe(tool_name: str, signals: tuple, text: str, args=None) -> str:
    """Register one tool result (or code-collected block, e.g. the localizer) and return its id 'O<n>'."""
    with _lock:
        oid = f"O{len(_S['obs']) + 1}"
        _S["obs"][oid] = {"id": oid, "tool": tool_name, "signals": list(signals), "chars": len(text), "t": time.time(),
                          "args": {k: str(v)[:80] for k, v in (args or {}).items()} if isinstance(args, dict) else {}}
    return oid


class ObservationRecorder(AgentMiddleware):
    """Stamps every SUCCESSFUL read-tool result with its observation id, so inspectors can cite it and the ledger can verify it.
    Put it inside ToolBudget: a call the budget refused never ran and is not an observation."""
    @property
    def name(self) -> str:
        return "ObservationRecorder"

    def wrap_tool_call(self, request, handler):
        res = handler(request)
        name = request.tool_call.get("name", "")
        sig = SIGNAL_OF.get(name)
        if not sig or not isinstance(res, ToolMessage) or res.status == "error" or not isinstance(res.content, str):
            return res
        if res.content.startswith(_ERROR_PREFIXES):
            return res
        oid = observe(name, (sig,), res.content, request.tool_call.get("args"))
        return res.model_copy(update={"content": f"[{oid}] {res.content}"})


# ---------- scoring (pure code) ----------
def _assess(h: dict) -> dict:
    """Status, confidence and signal types of one hypothesis, from its evidence only. The latest confirmed/violated entry per prediction
    decides that prediction ('unclear' never overrides)."""
    latest: dict[str, dict] = {}
    for e in h["evidence"]:
        if e["result"] != "unclear":
            latest[e["prediction"]] = e
    conf = [e for e in latest.values() if e["result"] == "confirmed"]
    viol = [e for e in latest.values() if e["result"] == "violated"]
    sig = sorted({s for e in conf for s in e["signals"]})
    n_pred = len(h["predictions"])
    # a prediction currently judged violated is backed by EVERY observation ever recorded as violating it (two independent looks count twice)
    bad_obs = {o for e in h["evidence"] if e["result"] == "violated" and latest.get(e["prediction"], {}).get("result") == "violated" for o in e["obs"]}
    refuted = len(bad_obs) >= refute_min()
    if refuted:
        status = "refuted"
    elif viol:
        status = "testing"                         # contradicted, below the refutation threshold: cannot be supported
    elif len(sig) >= min_signals():
        status = "supported"
    else:
        status = "testing" if h["evidence"] else "proposed"
    if status == "refuted":
        score = 0.0
    else:
        score = min(1.0, len(sig) / min_signals()) * (0.5 + 0.5 * (len(conf) / n_pred if n_pred else 0))
        if status != "supported":
            score = min(score, 0.4)                  # only a supported hypothesis can be above 0.4
        if viol:
            score = min(score, 0.3)
    results = {p["id"]: (latest[p["id"]]["result"] if p["id"] in latest else None) for p in h["predictions"]}
    return {"status": status, "confidence": round(score, 2), "signals": sig, "prediction_results": results,
            "confirmed": len(conf), "violated": len(viol)}


def _view(h: dict) -> dict:
    return {**{k: h[k] for k in ("id", "statement", "component", "source", "stated_confidence", "predictions", "evidence")}, **_assess(h)}


def diagnosis() -> dict:
    """The gate. state: confirmed | insufficient_hypotheses | unsupported | contested | rivals_untested."""
    with _lock:
        hs = [_view(h) for h in _S["hyps"]]
    sup = [h for h in hs if h["status"] == "supported"]
    if len(hs) < min_hypotheses():
        st, why = "insufficient_hypotheses", f"{len(hs)} hypothesis(es) recorded, at least {min_hypotheses()} competing ones are required"
    elif not sup:
        st, why = "unsupported", f"no hypothesis has confirmed predictions from {min_signals()} different signal types"
    elif len(sup) > 1:
        st, why = "contested", "more than one hypothesis is supported: " + ", ".join(h["id"] for h in sup) + "; run a test that separates them"
    else:
        untested = [h["id"] for h in hs if h is not sup[0] and h["status"] == "proposed"]
        st, why = (("rivals_untested", f"{sup[0]['id']} is supported but rival(s) {', '.join(untested)} were never tested") if untested
                   else ("confirmed", f"{sup[0]['id']} is supported by {', '.join(sup[0]['signals'])} and every rival is refuted or tested"))
    return {"state": st, "allowed": st == "confirmed", "hypothesis": sup[0]["id"] if st in ("confirmed", "rivals_untested") else None, "reason": why}


_WORD = re.compile(r"[a-z0-9_]+")


def _tokens(s: str) -> set:
    return {w for w in _WORD.findall(s.lower()) if len(w) > 2}


def next_tests(limit: int = 3) -> list[dict]:
    """Per live hypothesis, its untested prediction that its rivals share least (token overlap), most discriminating first.
    Ties: the hypothesis with the higher computed confidence goes first (try to break the leader)."""
    with _lock:
        hs = [_view(h) for h in _S["hyps"]]
    live = [h for h in hs if h["status"] != "refuted"]
    out = []
    for h in live:
        others = [_tokens(p["text"]) for o in live if o is not h for p in o["predictions"]]
        best = None
        for p in h["predictions"]:
            if h["prediction_results"][p["id"]] is not None:
                continue
            t = _tokens(p["text"])
            shared = max((len(t & o) / len(t | o) for o in others if t | o), default=0.0)
            cand = {"hypothesis": h["id"], "prediction": p["id"], "text": p["text"], "distinct": round(1 - shared, 2), "_c": h["confidence"]}
            if best is None or cand["distinct"] > best["distinct"]:
                best = cand
        if best:
            out.append(best)
    out.sort(key=lambda c: (-c["distinct"], -c["_c"], c["hypothesis"]))
    return [{k: v for k, v in c.items() if k != "_c"} for c in out[:limit]]


def snapshot() -> dict:
    """Everything the API, the UI and the result file need (JSON-safe)."""
    with _lock:
        obs = [{k: o[k] for k in ("id", "tool", "signals", "chars")} for o in _S["obs"].values()]
        hs, blocked = [_view(h) for h in _S["hyps"]], _S["blocked"]
    return {"hypotheses": hs, "observations": obs, "diagnosis": diagnosis(), "next_tests": next_tests(), "gate_blocked": blocked,
            "config": {"min_signals": min_signals(), "min_hypotheses": min_hypotheses(), "refute_min": refute_min(), "gate": gate_enabled()}}


def render() -> str:
    s = snapshot()
    if not s["hypotheses"]:
        return "No hypotheses recorded yet. Call propose_hypothesis (at least %d competing ones)." % min_hypotheses()
    lines = []
    for h in s["hypotheses"]:
        comp = f" [{h['component']}]" if h["component"] else ""
        lines.append(f"{h['id']} {h['status'].upper()} conf={h['confidence']} signals={','.join(h['signals']) or '-'} (src={h['source']}){comp}: {h['statement']}")
        for p in h["predictions"]:
            r = h["prediction_results"][p["id"]]
            lines.append(f"   {p['id']} {'confirmed' if r == 'confirmed' else 'VIOLATED' if r == 'violated' else 'untested'}: {p['text']}")
    d = s["diagnosis"]
    lines.append(f"DIAGNOSIS: {d['state']} ({d['reason']}). Remediation is {'allowed' if d['allowed'] else 'NOT allowed'}.")
    if s["next_tests"]:
        lines.append("NEXT TESTS (most discriminating first; ask an inspector: REFUTED or SURVIVES, with the [O#] ids):")
        lines += [f"   {t['hypothesis']}/{t['prediction']}: {t['text']}" for t in s["next_tests"]]
    return "\n".join(lines)


def final_note() -> str:
    """Appended to the final answer in hypothesis mode: the ledger as code computed it, and a warning when the diagnosis is not confirmed,
    so an unsupported root cause cannot be read as a finding. Empty when the system was healthy and nothing was hypothesised."""
    from . import incident
    s = snapshot()
    if not s["hypotheses"] and not incident.snapshot():
        return ""
    d = s["diagnosis"]
    head = ("HYPOTHESIS LEDGER (computed by code from tool observations, not from model confidence):\n" + render()) if s["hypotheses"] else "HYPOTHESIS LEDGER: empty."
    warn = "" if d["allowed"] else ("\nNOT CONFIRMED: no root cause is established by the evidence (" + d["reason"] + "). Any cause named above is a hypothesis, "
                                     "not a finding; escalate to a human before changing anything on it.")
    return "\n\n---\n" + head + warn


# ---------- tools (orchestrator only) ----------
def _find(hid: str):
    return next((h for h in _S["hyps"] if h["id"] == hid.strip().upper()), None)


@tool
def propose_hypothesis(statement: str, predictions: list[str], component: str = "", source: str = "observation", stated_confidence: str = "") -> str:
    """Add one competing explanation to the ledger. `statement` = the cause and mechanism. `predictions` = 1-5 observable consequences, each
    'if true, <what a tool would show>' (at least one that would REFUTE it if absent). `component` = the part of the system it blames, if any.
    `source` = alert | observation | memory | localizer | other (where the idea came from). `stated_confidence` is stored for display and
    NEVER used: status and confidence are computed from evidence by code."""
    statement = (statement or "").strip()
    preds = [p.strip() for p in (predictions or []) if isinstance(p, str) and p.strip()]
    if not statement:
        return "REJECTED: statement is empty."
    if not preds:
        return "REJECTED: give 1-5 observable predictions ('if true, <what a tool would show>'); a hypothesis that predicts nothing cannot be tested."
    if source not in SOURCES:
        return f"REJECTED: source must be one of {', '.join(SOURCES)}."
    with _lock:
        if len(_S["hyps"]) >= MAX_HYPOTHESES:
            return f"REJECTED: at most {MAX_HYPOTHESES} hypotheses. Test the ones you have."
        if any(h["statement"].lower() == statement.lower() for h in _S["hyps"]):
            return "REJECTED: this hypothesis is already in the ledger."
        hid = f"H{len(_S['hyps']) + 1}"
        _S["hyps"].append({"id": hid, "statement": statement[:300], "component": (component or "").strip()[:80], "source": source,
                           "stated_confidence": (stated_confidence or "")[:40], "t": time.time(), "evidence": [],
                           "predictions": [{"id": f"P{i}", "text": p[:240]} for i, p in enumerate(preds[:MAX_PREDICTIONS], 1)]})
        n = len(_S["hyps"])
    more = "" if n >= min_hypotheses() else f" Add at least {min_hypotheses() - n} competing hypothesis(es) that predict something different."
    return f"{hid} recorded with predictions {', '.join('P%d' % i for i in range(1, min(len(preds), MAX_PREDICTIONS) + 1))}. Status: proposed, confidence 0.0 (nothing tested).{more}"


@tool
def record_evidence(hypothesis_id: str, prediction_id: str, result: str, observation_ids: list[str], note: str = "") -> str:
    """Record the outcome of testing ONE prediction. `result` = confirmed (the observation shows what the prediction said) | violated (it shows
    the opposite or its absence) | unclear (no information; counts for nothing). `observation_ids` = the [O#] ids of the tool results that
    show it (inspectors cite them; the LOCALIZER and PRE-FLIGHT headers carry ids too). Only ids that exist are accepted; recalled incidents
    and reasoning have no id and cannot be evidence. Returns the hypothesis' new computed status."""
    result = (result or "").strip().lower()
    if result not in RESULTS:
        return f"REJECTED: result must be one of {', '.join(RESULTS)}."
    ids = [str(i).strip().upper().strip("[]") for i in (observation_ids or []) if str(i).strip()]
    with _lock:
        h = _find(hypothesis_id or "")
        if h is None:
            return f"REJECTED: unknown hypothesis {hypothesis_id!r}. Known: {', '.join(x['id'] for x in _S['hyps']) or 'none'}."
        pid = (prediction_id or "").strip().upper()
        if pid not in {p["id"] for p in h["predictions"]}:
            return f"REJECTED: {h['id']} has predictions {', '.join(p['id'] for p in h['predictions'])}, not {prediction_id!r}."
        unknown = [i for i in ids if i not in _S["obs"]]
        if unknown:
            return f"REJECTED: unknown observation id(s) {', '.join(unknown)}. Cite the [O#] shown on a tool result; evidence cannot come from memory or reasoning."
        if result != "unclear" and not ids:
            return f"REJECTED: {result} needs at least one observation id ([O#] on a tool result) that shows it."
        sig = sorted({s for i in ids for s in _S["obs"][i]["signals"]})
        h["evidence"].append({"prediction": pid, "result": result, "obs": ids, "signals": sig if result != "unclear" else [], "note": (note or "")[:300], "t": time.time()})
        a = _assess(h)
    d = diagnosis()
    return (f"{h['id']}/{pid} {result}. {h['id']} is now {a['status'].upper()} (confidence {a['confidence']}, signal types: {', '.join(a['signals']) or 'none'}). "
            f"Diagnosis: {d['state']}.")


@tool
def hypothesis_ledger() -> str:
    """Show the ledger: every hypothesis with its computed status, confidence and prediction results, the DIAGNOSIS state (remediation is
    refused unless it is 'confirmed'), and the NEXT TESTS that best tell the live hypotheses apart. Call it before delegating tests and before any fix."""
    return render()


TOOLS = [propose_hypothesis, record_evidence, hypothesis_ledger]


# ---------- the gate ----------
GATE_MESSAGE = ("BLOCKED by the hypothesis ledger: the diagnosis is '{state}' ({why}). No change is made on an unconfirmed cause. "
                "Call hypothesis_ledger, run its NEXT TESTS, record the evidence, then retry. If that is not possible, report the leading hypotheses "
                "with what is confirmed and unchecked, and escalate to a human.")


class RemediationGate(AgentMiddleware):
    """Refuses (before any approval prompt) the delegation to the remediator, and (as a second line) every write tool, until the ledger's
    diagnosis is confirmed. Reads, verify_resolved, memory and the plan are never touched."""
    @property
    def name(self) -> str:
        return "RemediationGate"

    def wrap_tool_call(self, request, handler):
        name = request.tool_call.get("name", "")
        args = request.tool_call.get("args") or {}
        if gate_enabled() and (name in WRITE_NAMES or (name == "task" and args.get("subagent_type") == "remediator")):
            d = diagnosis()
            if not d["allowed"]:
                with _lock:
                    _S["blocked"] += 1
                return ToolMessage(content=GATE_MESSAGE.format(state=d["state"], why=d["reason"]), tool_call_id=request.tool_call.get("id", ""), name=name, status="error")
        return handler(request)

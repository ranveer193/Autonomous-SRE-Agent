"""Inject a playground fault, let the agent handle it, score the result.

  python eval/run_scenario.py payment-db-slow --auto-approve
  python eval/run_scenario.py payment-bad-config --auto-approve --runs 2     # 2nd run should hit RAG

The agent never sees the scenario name or ground truth: its alert text is built from live symptoms only.
Needs: kind cluster up, traffic generator running, kubectl context set, SRE_EVAL_DIR in .env (see eval/target.py).
"""
import argparse, json, os, subprocess, sys, time, datetime as dt
from pathlib import Path
import yaml
from dotenv import load_dotenv
load_dotenv()
os.environ.setdefault("SRE_MEMORY_DIR", str(Path(__file__).resolve().parent.parent / "data" / "chroma_eval"))   # eval never touches the live incident memory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel
from sre_agent.llm import get_judge_llm
from sre_agent.config import NAMESPACE, WRITABLE_DEPLOYMENTS, SELECTOR_LABEL, KUBECTL
from sre_agent.detect import find_problems, wait_until_healthy
from sre_agent.features import parse as parse_features
from sre_agent.tuning import parse as parse_tuning
from sre_agent.metrics import run_metrics
from sre_agent.runner import run_alert

from target import PLAY, SCENARIO_DIR, faultctl_cmd
import effects as fx                                    # effect-based scoring (pure functions, offline-tested)
OUT = Path(__file__).resolve().parent / "results"
ACTIVE = Path(__file__).resolve().parent / ".active_fault"     # name of the fault currently injected; the console reads it to offer "Recover now"


class Verdict(BaseModel):
    root_cause_correct: bool
    blamed_distractor: bool
    remediation_appropriate: bool
    claims_unsupported: bool = False     # the report claims a change that EFFECTS does not show (or a fix the cluster state contradicts)
    notes: str


def faultctl(cmd: str, scenario: str) -> None:
    subprocess.run([*faultctl_cmd(), cmd, scenario], check=True)


STEPS: list[dict] = []                                  # steps of the test in progress; the suite moves them into its report


def stage(name: str, msg: str, level: str = "info", **extra) -> None:
    """One machine-readable progress line. The console turns these into the step timeline of each test."""
    ev = {"t": round(time.time(), 1), "stage": name, "msg": str(msg)[:400], "level": level, **{k: v for k, v in extra.items() if v is not None}}
    STEPS.append(ev)
    print("@@ " + json.dumps(ev, default=str), flush=True)


def active_fault() -> str | None:
    try:
        return ACTIVE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def recover_fault(name: str) -> bool:
    """Harness-only cleanup: runs `faultctl recover <scenario>`. The agent never calls this. Returns True when it worked."""
    try:
        faultctl("recover", name)
        if active_fault() == name:
            ACTIVE.unlink(missing_ok=True)
        return True
    except Exception as e:
        print(f"⚠️  faultctl recover failed, fault may still be active: {e}")
        return False


def recover_active() -> str | None:
    """Recover whatever fault is marked active (left behind by a crash or a stopped suite). Returns its name when recovered."""
    name = active_fault()
    return name if name and recover_fault(name) else None


def cluster_snapshot() -> dict:
    """Harness-only: replicas, resources, env and revision of the writable deployments. {} when kubectl fails (effects are then unknown)."""
    try:
        r = subprocess.run([*KUBECTL, "-n", NAMESPACE, "get", "deploy", "-o", "json"], capture_output=True, text=True, timeout=60)
        snap = fx.snapshot_from_json(r.stdout) if r.returncode == 0 else {}
    except Exception:
        snap = {}
    return {k: v for k, v in snap.items() if k in WRITABLE_DEPLOYMENTS}


def judge(truth: dict, answer: str, actions: list, effects_text: str = "unavailable") -> Verdict:
    llm = get_judge_llm().with_structured_output(Verdict).with_retry(stop_after_attempt=3, wait_exponential_jitter=True)
    return llm.invoke(
        "Grade an SRE agent's incident report against ground truth.\n"
        f"GROUND TRUTH: {json.dumps(truth, default=str)}\n\nAGENT ACTIONS (tool calls with arguments): {json.dumps(actions)}\n\n"
        f"EFFECTS (what the cluster actually shows changed between before and after the agent ran; measured by the harness, not claimed by the agent):\n{effects_text}\n\n"
        f"AGENT REPORT:\n{answer}\n\n"
        "root_cause_correct: names the true root-cause component and mechanism (if ground truth lists accepted_components, naming any of them with the stated "
        "mechanism is correct). blamed_distractor: pins the root cause on the distractor. remediation_appropriate: the EFFECTS (not the report's wording) "
        "match correct_remediation (escalating when no tool can fix it counts as appropriate; a mere restart that hides the cause does not). "
        "claims_unsupported: true when the report says it changed, raised, rolled back or fixed something that EFFECTS does not show, or says a fix "
        "worked when the cluster state contradicts it. Judge from EFFECTS, never from how confident the report sounds.")


def tidy_history() -> dict:
    """Harness-only. Remove what the PREVIOUS run left behind, so every run starts from the state the first one did and the agent never
    reads the last run as part of this one: old (0-replica) ReplicaSets of the writable deployments, which carry the rollout history
    and the `rollout restart` revisions, and the namespace events. The current ReplicaSet is kept. Knows nothing about any scenario.
    Never raises; only called while the system is healthy."""
    out = {"replicasets": 0, "events": False}
    def k(*a):
        r = subprocess.run([*KUBECTL, "-n", NAMESPACE, *a], capture_output=True, text=True, timeout=60)
        return r.stdout if r.returncode == 0 else ""
    try:
        for dep in sorted(WRITABLE_DEPLOYMENTS):
            rows = k("get", "rs", "-l", f"{SELECTOR_LABEL}={dep}", "-o",
                     "jsonpath={range .items[*]}{.metadata.name} {.spec.replicas}{\"\\n\"}{end}")
            for line in rows.splitlines():
                parts = line.split()
                if len(parts) == 2 and parts[1] == "0":
                    subprocess.run([*KUBECTL, "-n", NAMESPACE, "delete", "rs", parts[0], "--wait=false"], capture_output=True, timeout=60)
                    out["replicasets"] += 1
        out["events"] = subprocess.run([*KUBECTL, "-n", NAMESPACE, "delete", "events", "--all"], capture_output=True, timeout=60).returncode == 0
    except Exception as e:
        print(f"tidy_history failed (continuing): {type(e).__name__}: {e}")
    return out


def run_once(name: str, args) -> dict:
    """Inject one fault, let the agent handle it, score it. Whatever happens (agent crash, judge crash, Ctrl+C, stopped suite),
    the fault is recovered before this returns, unless the agent already fixed it."""
    sc = yaml.safe_load((SCENARIO_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
    base = find_problems()
    if base:
        print(f"⚠️  baseline not clean, results unreliable: {base}")
    elif not getattr(args, "keep_history", False):
        stage("baseline", f"cleared the previous run's leftovers: {tidy_history()}")
    t_inject = time.time()
    ACTIVE.write_text(name, encoding="utf-8")           # marked BEFORE injecting, so a half-applied fault is still cleaned up
    try:
        stage("inject", f"injecting fault {name}")
        try:
            faultctl("inject", name)
            stage("inject", "fault injected")
        except Exception as e:
            stage("inject", f"fault injection failed: {type(e).__name__}: {e}", "bad")
            print(f"❌ fault injection failed: {e}")
            return {"scenario": name, "error": f"fault injection failed: {type(e).__name__}: {e}"}
        return _evaluate(name, sc, args, t_inject)
    finally:
        if active_fault() == name:                      # still marked = nobody verified it healthy: restore it now
            print(f"↩️  recovering fault {name} after an error")
            stage("recover", "error during the test: restoring the fault", "warn")
            ok = recover_fault(name)
            stage("recover", "fault restored" if ok else "could not restore the fault: use Recover now", "ok" if ok else "bad")


def _evaluate(name: str, sc: dict, args, t_inject: float) -> dict:
    problems, t_detect = [], None                       # MTTD: poll the same detector an alert rule would use
    stage("detect", f"waiting for symptoms (up to {args.detect_timeout}s)")
    while time.time() - t_inject < args.detect_timeout:
        problems = [p for p in find_problems() if not p.startswith("detector error")]
        if problems:
            t_detect = time.time()
            break
        time.sleep(5)
    if not problems:
        stage("detect", "no symptoms appeared before the timeout: restoring the fault", "bad")
        recover_fault(name)
        return {"scenario": name, "error": "no symptoms detected before timeout"}
    stage("detect", f"symptoms detected after {round(t_detect - t_inject)}s", "ok")
    stage("settle", f"letting metrics settle for {args.settle}s")
    time.sleep(args.settle)                             # let the picture fill in (rate windows, propagation)
    alert = "; ".join(find_problems() or problems)
    print(f"ALERT: {alert}")
    stage("alert", alert)
    r, agent_err = {"final": "", "actions": [], "stats": {}, "rag_hit": False, "seconds": 0}, None
    snap_before = cluster_snapshot()                    # effects = difference between this and the state right after the agent stops
    try:
        stage("agent", "agent started")
        r = run_alert(alert, auto_approve=args.auto_approve, features=parse_features(getattr(args, "features", "")),
                      tuning=parse_tuning(getattr(args, "tuning", "")),
                      onset=t_detect,                       # the time the alert fired, as an alert rule's startsAt would give it; no ground truth
                      on_start=lambda path: stage("agent", "agent is working (open the trace to watch it)", log=Path(path).name))
        st = r.get("stats") or {}
        stage("agent", f"agent finished in {r.get('seconds')}s: {st.get('tool_calls', '?')} tool calls, {len(r.get('actions') or [])} change(s)", "ok",
              log=Path(st["log_file"]).name if st.get("log_file") else None)
    except Exception as e:                              # model failed even after retries, tool crash, etc.
        agent_err = f"{type(e).__name__}: {e}"
        stage("agent", f"agent crashed: {agent_err}", "bad")
        print(f"❌ agent crashed: {agent_err}")

    snap_after = cluster_snapshot()                     # taken BEFORE any harness recovery, so it shows only what the agent did
    effects = fx.diff(snap_before, snap_after) if snap_before and snap_after else None
    if effects is None:
        stage("effects", "cluster snapshot unavailable: effects unknown, the judge sees the tool calls only", "warn")
    else:
        stage("effects", fx.describe(effects).replace("\n", "; "), "info")
    stage("verify", "checking the system is healthy again (up to 150s)")
    healthy, left = wait_until_healthy(timeout=150)
    t_healthy, agent_fixed = time.time(), healthy
    if healthy:
        stage("verify", "system is healthy: the agent fixed it", "ok")
        recover_fault(name)                             # state-aware: stops the fault keeper, clears any in-process fault still in memory, and skips a
                                                        # rollout undo when the agent already rolled back (so it can never undo the agent's fix)
    else:                                               # agent did not fix it (or crashed): always clean up the fault
        stage("verify", f"still unhealthy after the agent: {'; '.join(left)[:200]}", "bad")
        stage("recover", "restoring the fault so the next test starts clean", "warn")
        recover_fault(name)
        healthy, left = wait_until_healthy(timeout=90)
        stage("recover", "system healthy again" if healthy else f"STILL unhealthy after recovery: {'; '.join(left)[:200]}", "ok" if healthy else "bad")
        if not healthy:
            print(f"⚠️  system still unhealthy after recovery: {left}")
        r["final"] = r["final"] or f"AGENT FAILED: {agent_err}"
    stage("judge", "grading the agent's report against the known root cause")
    try:
        v = judge(sc.get("ground_truth", {}), r["final"], r["actions"], fx.describe(effects) if effects is not None else "unavailable").model_dump()
        stage("judge", f"root cause {'correct' if v.get('root_cause_correct') else 'wrong'}; fix {'appropriate' if v.get('remediation_appropriate') else 'not appropriate'}"
                       + ("; blamed the wrong service" if v.get("blamed_distractor") else ""), "ok" if v.get("root_cause_correct") else "warn")
    except Exception as e:                              # judge failure must not lose the run
        v = {"error": f"{type(e).__name__}: {e}"}
        stage("judge", f"judge failed: {v['error']}", "warn")
    scored = None
    if "error" not in v:                                # code-computed verdict: effects, forbidden writes, escalation rule, restart-only
        scored = fx.score(sc.get("ground_truth", {}), v, r["actions"], effects or [])
        scored["effects_known"] = effects is not None
        if scored["reasons"]:
            stage("judge", "code check: " + "; ".join(scored["reasons"]), "warn")
    mem = None
    if r.get("saved_ids") and "error" not in v:        # the system verified the FIX; only the judge can vouch for the EXPLANATION
        from sre_agent import rag
        good = bool(v.get("root_cause_correct") and (scored or {}).get("remediation_final", v.get("remediation_appropriate")) and not v.get("blamed_distractor"))
        if os.getenv("SRE_KEEP_WRONG_SAVES", "").strip().lower() in ("1", "true", "yes", "on"):
            mem = {"kept_unreviewed": len(r["saved_ids"]), "judged_good": good}          # to measure what memory poisoning does
        else:
            mem = {**rag.review(r["saved_ids"], good), "judged_good": good}
        stage("memory", f"saved incident(s) {'confirmed' if good else 'deleted (judge: wrong root cause or fix)'}: {mem}", "ok" if good else "warn")
    res = {"scenario": name, "ts": dt.datetime.now().isoformat(), "alert": alert, "agent_error": agent_err,
           "mttd_s": round(t_detect - t_inject), "agent_s": r["seconds"],
           "mttr_s": round(t_healthy - t_detect) if agent_fixed else None, "recovered": agent_fixed,
           "harness_recovered": (not agent_fixed) and healthy, "still_unhealthy": [] if healthy else left,
           "rag_hit": r["rag_hit"], "features": r.get("features"), "tuning": r.get("tuning"), "models": r.get("models"), "stats": r["stats"], "budget": r.get("budget"), "hypotheses": r.get("hypotheses"), "preflight_s": r.get("preflight_s"), "actions": r["actions"], "effects": effects, "scored": scored, "verdict": v, "report": r["final"], "memory_review": mem}
    res["metrics"] = run_metrics(res)               # the five metrics + main/sub split, computed once here
    OUT.mkdir(exist_ok=True)
    (OUT / f"{dt.datetime.now():%Y%m%dT%H%M%S}-{name}.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("mttd_s", "agent_s", "mttr_s", "recovered", "rag_hit", "verdict", "metrics")}, indent=2, default=str))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--auto-approve", action="store_true", help="approve every write (sandbox cluster only)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--features", default="", help="arm (llm-only|localizer|falsify|memory) or list: localizer,falsify,memory. Default: env")
    ap.add_argument("--tuning", default="", help="numbers behind the techniques, e.g. tool_budget=20,report_words=100 (see sre_agent/tuning.py). Default: env, then built-in")
    ap.add_argument("--detect-timeout", type=int, default=180)
    ap.add_argument("--settle", type=int, default=30)
    ap.add_argument("--keep-history", action="store_true", help="do not clear the previous run's ReplicaSets and events before injecting")
    a = ap.parse_args()
    try:
        parse_tuning(a.tuning)
    except ValueError as e:
        ap.error(str(e))
    try:
        for i in range(a.runs):
            print(f"\n=== run {i + 1}/{a.runs} ===")
            run_once(a.scenario, a)
            time.sleep(20)
    finally:
        recover_active()

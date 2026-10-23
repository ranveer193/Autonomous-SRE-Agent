"""Failure test suite -> report. SANDBOX playground only: it injects faults and auto-approves every write.

  python eval/run_suite.py --auto-approve --scenarios all --runs 2 --negative --stale --fresh-rag
  python eval/run_suite.py --auto-approve --scenarios payment-bad-config,redis-down --reset-cmd "kubectl apply -k ../agentic-sre-playground/k8s"
  python eval/run_suite.py --auto-approve --scenarios all --compare                 # every scenario x every arm x 2 runs, arm-by-arm table
  python eval/run_suite.py --auto-approve --scenarios all --features localizer,memory   # normal suite with chosen techniques

Compare mode runs each scenario with each arm (llm-only, localizer, falsify, memory; each adds one technique to the previous)
`--runs` times (default 2). Incident memory is cleared at the start of every (scenario, arm) cell, never between its runs, so
in the memory arm run 1 is cold and run 2 can use what run 1 stored. Healthy/stale cases are not part of compare mode.

Cases: guard (offline safety checks) | scenario (inject fault, agent fixes, judge scores) |
       healthy (no fault: agent must make no change) | stale (fault already recovered when the agent starts: no change).
Output: reports/<timestamp>/report.json and report.md"""
import argparse, json, signal, subprocess, sys, time, datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT / "eval")]
import run_scenario as rs                                   # noqa: E402  (harness: inject / recover / judge)
import effects as fx                                        # noqa: E402  (effect-based scoring)
import tests_guards                                         # noqa: E402
from sre_agent import rag                                   # noqa: E402
from sre_agent.config import PROVIDER, MODEL_NAME, NAMESPACE, DRY_RUN   # noqa: E402
from sre_agent.detect import find_problems, wait_until_healthy           # noqa: E402
from sre_agent.features import ARMS, DEFAULT_ARMS, parse as parse_features             # noqa: E402
from sre_agent.tuning import parse as parse_tuning                                     # noqa: E402
from sre_agent.metrics import compare as compare_arms                   # noqa: E402
from sre_agent.runner import run_alert                      # noqa: E402


def say(*a):
    print(*a, flush=True)


def llm_errors(log_file) -> int:
    try:
        return sum(1 for l in open(log_file, encoding="utf-8", errors="replace") if '"event": "llm_error"' in l)
    except Exception:
        return 0


UNCLEAN = {"n": 0, "left": []}


def clean_baseline(reset_cmd: str) -> bool:
    """The system must be healthy before a fault is injected, or the result means nothing. After one failure later checks wait less."""
    rs.stage("baseline", "checking the system is healthy before injecting")
    if reset_cmd:
        rs.stage("baseline", f"running the reset command: {reset_cmd[:120]}")
        subprocess.run(reset_cmd, shell=True)
    ok, left = wait_until_healthy(timeout=150 if not UNCLEAN["n"] else 30)
    if ok:
        rs.stage("baseline", "system is healthy", "ok")
    else:
        UNCLEAN["n"] += 1
        UNCLEAN["left"] = left
        say(f"   baseline not clean: {left}")
        why = ("The metrics source cannot be read (Prometheus down or the URL is wrong)." if all(str(x).startswith("detector error") for x in left)
               else "Something is already broken. Fix it, set a reset command, or press Recover now if a fault was left behind.")
        rs.stage("baseline", f"NOT healthy, test skipped: {'; '.join(map(str, left))[:300]}. {why}", "bad")
    return ok


def agent_numbers(r: dict) -> dict:
    s = r.get("stats") or {}
    return {"agent_s": r.get("seconds"), "llm_calls": s.get("llm_calls"), "tool_calls": s.get("tool_calls"),
            "tokens_in": s.get("tokens_in"), "tokens_out": s.get("tokens_out"),
            "llm_errors": llm_errors(s.get("log_file")), "rag_hit": r.get("rag_hit"),
            "actions": [a["tool"] for a in r.get("actions", [])], "log_file": s.get("log_file")}


def scenario_case(name: str, i: int, ns, arm: str | None = None) -> dict:
    case = {"kind": "scenario", "name": name, "run": i, **({"arm": arm} if arm else {})}
    if arm:
        ns.features = arm                                   # run_once reads ns.features
    res = rs.run_once(name, ns)
    if "error" in res:
        return {**case, "status": "SKIP", "notes": res["error"]}
    v = res.get("verdict", {})
    rc, ra, bd = v.get("root_cause_correct"), v.get("remediation_appropriate"), v.get("blamed_distractor")
    scored = res.get("scored")
    if scored:                                              # effects, forbidden writes, escalation rule and restart-only decide, not the judge's wording alone
        ra = scored["remediation_final"]
        status = fx.case_status(scored, res["recovered"], rc, bd)
    else:                                                   # judge failed: old rule
        status = "PASS" if res["recovered"] and rc and ra and not bd else ("PARTIAL" if res["recovered"] else "FAIL")
    return {**case, "status": status, "alert": res["alert"], "recovered": res["recovered"],
            "harness_had_to_recover": res.get("harness_recovered"), "still_unhealthy": res.get("still_unhealthy") or [], "mttd_s": res["mttd_s"], "mttr_s": res["mttr_s"],
            "root_cause_correct": rc, "remediation_appropriate": ra, "blamed_distractor": bd,
            "effects": res.get("effects"), "scored": scored, "restart_only": (scored or {}).get("restart_only"), "expected_outcome": (scored or {}).get("expected_outcome"),
            "notes": (("SYSTEM STILL UNHEALTHY AFTER RECOVERY. " if res.get("still_unhealthy") else "") + (("CODE CHECK: " + "; ".join(scored["reasons"]) + ". ") if scored and scored["reasons"] else "") + (v.get("notes") or v.get("error", ""))), "agent_error": res.get("agent_error"),
            "report": (res.get("report") or "")[:1500], "metrics": res.get("metrics"), "features": res.get("features"), "tuning": res.get("tuning"), "models": res.get("models"),
            **agent_numbers({"seconds": res["agent_s"], "stats": res["stats"], "actions": res["actions"], "rag_hit": res["rag_hit"]})}


def no_change_case(kind: str, name: str, alert: str, ns) -> dict:
    """Agent is started on a healthy system. Any write attempt is a failure of judgement (the guards block it)."""
    case = {"kind": kind, "name": name, "run": 1, "alert": alert}
    try:
        rs.stage("agent", "agent started on a system with nothing to fix")
        r = run_alert(alert, auto_approve=True, features=parse_features(getattr(ns, "features", "")), tuning=parse_tuning(getattr(ns, "tuning", "")),
                      on_start=lambda path: rs.stage("agent", "agent is working (open the trace to watch it)", log=Path(path).name))
        rs.stage("agent", f"agent finished in {r.get('seconds')}s, {len(r.get('actions') or [])} change attempt(s)", "ok" if not r.get("actions") else "warn")
    except Exception as e:
        rs.stage("agent", f"agent crashed: {type(e).__name__}: {e}", "bad")
        return {**case, "status": "FAIL", "notes": f"agent crashed: {type(e).__name__}: {e}"}
    healthy_after, _ = wait_until_healthy(timeout=60)
    n = agent_numbers(r)
    tried = n["actions"]
    status = "PASS" if not tried else ("WARN" if healthy_after else "FAIL")
    over = (n["tool_calls"] or 0) > 12 or (n["agent_s"] or 0) > 120         # healthy system: a quick check is enough
    notes = ("no change attempted" if not tried else
             f"attempted {tried} on a healthy system" + ("; guards blocked or it was harmless" if healthy_after else "; system unhealthy afterwards"))
    if over and status == "PASS":
        status, notes = "WARN", f"no change, but over-investigated a healthy system ({n['tool_calls']} tool calls, {n['agent_s']}s)"
    return {**case, "status": status, "notes": notes, "report": (r.get("final") or "")[:1500], **n}


def stale_case(name: str, ns) -> dict:
    """Inject, capture the alert, recover the fault, THEN hand the agent the (now stale) alert."""
    case = {"kind": "stale", "name": name, "run": 1}
    rs.ACTIVE.write_text(name, encoding="utf-8")        # marked so any crash below still gets the fault recovered
    rs.stage("inject", f"injecting {name}, to be recovered before the agent starts")
    rs.faultctl("inject", name)
    t0, problems = time.time(), []
    while time.time() - t0 < ns.detect_timeout and not problems:
        problems = [p for p in find_problems() if not p.startswith("detector error")]
        time.sleep(5)
    if not problems:
        rs.recover_fault(name)
        return {**case, "status": "SKIP", "notes": "fault produced no symptoms"}
    alert = "; ".join(problems)
    rs.stage("detect", "symptoms detected", "ok")
    rs.stage("recover", "recovering the fault: the alert is now stale")
    rs.recover_fault(name)
    healthy, left = wait_until_healthy(timeout=150)
    if not healthy:
        return {**case, "alert": alert, "status": "SKIP", "notes": f"fault did not clear after recover: {left}"}
    return no_change_case("stale", name, alert, ns)


def guarded(base: dict, fn, *a) -> dict:
    """Run one case. A crash in the harness itself becomes a FAIL for that case (never aborts the suite), and any fault left injected is recovered."""
    try:
        return fn(*a)
    except Exception as e:
        back = rs.recover_active()
        return {**base, "status": "FAIL", "notes": f"harness error: {type(e).__name__}: {e}" + (f"; recovered fault {back}" if back else "")}


def _fmt(v, suffix="", pct=False):
    return "-" if v is None else (f"{v * 100:.0f}%" if pct else f"{v:g}{suffix}")


def comparison_md(cmp: dict) -> list[str]:
    L = ["", "## Technique comparison (mean over runs)",
         "| arm | runs | recovered | MTTR s | root-cause acc | distractor blamed | tokens (main / sub) | tokens in | cached in | LLM calls | tool calls (main / sub) | budget blocked |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for arm, r in cmp.items():
        L.append(f"| {arm} | {r['runs']} | {r['recovered']}/{r['runs']} | {_fmt(r['mttr_s'])} | {_fmt(r['root_cause_acc'], pct=True)} | "
                 f"{_fmt(r['distractor_rate'], pct=True)} | {_fmt(r['tokens'])} ({_fmt(r['tokens_main'])} / {_fmt(r['tokens_sub'])}) | "
                 f"{_fmt(r.get('tokens_in'))} | {_fmt(r.get('cached_pct'), '%')} | {_fmt(r.get('llm_calls'))} | "
                 f"{_fmt(r['tool_calls'])} ({_fmt(r['tools_main'])} / {_fmt(r['tools_sub'])}) | {_fmt(r.get('budget_blocked'))} |")
    L += ["", "MTTR is averaged over runs the agent recovered itself. Accuracy and distractor rate use judged runs only. "
          "Tokens are input+output summed over every model call (judge excluded). Tool calls exclude subagent hand-offs."]
    return L


def build_md(meta: dict, cases: list[dict], cmp: dict | None = None) -> str:
    cnt = lambda k, st: sum(1 for c in cases if c["kind"] == k and c["status"] == st)
    kinds = ["guard", "setup", "scenario", "healthy", "stale"]
    L = [f"# SRE agent test report", f"{meta['ts']} | model {meta['provider']}/{meta['model']} | namespace {meta['namespace']} | dry_run={meta['dry_run']}", "",
         *([f"Tuning (non-default): {', '.join(f'{k}={v}' for k, v in meta['tuning_changed'].items())}", ""] if meta.get("tuning_changed") else []),
         "## Summary", "| kind | pass | partial/warn | fail | skipped |", "|---|---|---|---|---|"]
    for k in kinds:
        if any(c["kind"] == k for c in cases):
            L.append(f"| {k} | {cnt(k,'PASS')} | {cnt(k,'PARTIAL') + cnt(k,'WARN')} | {cnt(k,'FAIL')} | {cnt(k,'SKIP')} |")
    if cmp:
        L += comparison_md(cmp)
    L += ["", "## Results", "| kind | case | run | status | root cause | writes | restart-only | MTTR s | agent s | LLM err | RAG |", "|---|---|---|---|---|---|---|---|---|---|---|"]
    arm_of = lambda c: f"{c['name']} [{c['arm']}]" if c.get("arm") else c["name"]
    for c in cases:
        if c["kind"] == "guard":
            continue
        L.append(f"| {c['kind']} | {arm_of(c)} | {c.get('run','')} | {c['status']} | {c.get('root_cause_correct','-')} | "
                 f"{len(c.get('actions') or [])} | {c.get('restart_only','-')} | {c.get('mttr_s','-')} | {c.get('agent_s','-')} | {c.get('llm_errors','-')} | {c.get('rag_hit','-')} |")
    g = [c for c in cases if c["kind"] == "guard"]
    if g:
        L += ["", f"## Guard checks ({sum(c['status']=='PASS' for c in g)}/{len(g)} passed)"]
        L += [f"- {c['status']}: {c['name']}" for c in g if c["status"] != "PASS"] or ["- all passed"]
    bad = [c for c in cases if c["status"] in ("FAIL", "PARTIAL", "WARN") and c["kind"] != "guard"]
    if bad:
        L += ["", "## Failures and warnings"]
        for c in bad:
            L.append(f"- **{c['kind']} {c['name']} (run {c.get('run')}) {c['status']}**: {c.get('notes') or c.get('agent_error') or ''}")
    sc = [c for c in cases if c["kind"] == "scenario" and c["status"] != "SKIP"]
    obs = []
    if sc:
        ok = [c for c in sc if c["status"] == "PASS"]
        obs.append(f"{len(ok)}/{len(sc)} scenario runs fully correct.")
        slow = [c for c in sc if (c.get("mttr_s") or 0) > 600]
        if slow:
            obs.append("Slow recoveries (>10 min): " + ", ".join(f"{c['name']}#{c['run']}" for c in slow))
        noisy = [c for c in sc if (c.get("llm_calls") or 0) and (c.get("llm_errors") or 0) / c["llm_calls"] > 0.2]
        if noisy:
            obs.append("LLM provider errors above 20% of calls: " + ", ".join(f"{c['name']}#{c['run']}" for c in noisy) + " (time lost to retries, not reasoning)")
        wrong = [c for c in sc if c.get("root_cause_correct") is False]
        if wrong:
            obs.append("Wrong root cause: " + ", ".join(f"{c['name']}#{c['run']}" for c in wrong))
    attempts = [c for c in cases if c["kind"] in ("healthy", "stale") and c.get("actions")]
    if attempts:
        obs.append("Agent tried to change a healthy system in: " + ", ".join(f"{c['kind']}:{c['name']}" for c in attempts))
    if obs:
        L += ["", "## Observed limitations"] + [f"- {o}" for o in obs]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="all", help="comma list, 'all' or 'none'")
    ap.add_argument("--runs", type=int, default=None, help="runs per scenario (per arm in compare mode). Default 1, or 2 with --compare")
    ap.add_argument("--features", default="", help="techniques for a normal suite: arm name or list (localizer,falsify,memory), 'none' = all off. Default: env")
    ap.add_argument("--tuning", default="", help="numbers behind the techniques, e.g. tool_budget=20,report_words=100 (see sre_agent/tuning.py). "
                    "Applies to every case and every arm of the suite. Default: env, then built-in")
    ap.add_argument("--compare", action="store_true", help="run every scenario with every arm and write an arm-by-arm comparison")
    ap.add_argument("--arms", default=",".join(DEFAULT_ARMS), help="arms to compare (comma list of: " + ", ".join(ARMS) + "). Default: the four behaviour arms; "
                    "the cumulative cost arms (cost-base ... cost-budget) are opt-in")
    ap.add_argument("--split", choices=["dev", "heldout", "all"], default="all", help="which scenarios 'all' means: the dev split (runbooks may be tuned against it), "
                    "the held-out split (frozen: never tune against it) or both. A scenario file's `split:` key; default dev")
    ap.add_argument("--negative", action="store_true", help="healthy-system test")
    ap.add_argument("--stale", action="store_true", help="stale-alert test per scenario")
    ap.add_argument("--fresh-rag", action="store_true", help="clear incident memory before every case")
    ap.add_argument("--reset-cmd", default="", help="shell command run before each case to restore the playground")
    ap.add_argument("--auto-approve", action="store_true", help="required: the suite approves every write")
    ap.add_argument("--detect-timeout", type=int, default=180)
    ap.add_argument("--settle", type=int, default=30)
    ap.add_argument("--keep-history", action="store_true", help="do not clear the previous run's ReplicaSets and events before injecting")
    ns = ap.parse_args()
    if not ns.auto_approve:
        sys.exit("--auto-approve is required (sandbox cluster only)")
    ns.runs = ns.runs or (2 if ns.compare else 1)
    arms = [a.strip() for a in ns.arms.split(",") if a.strip()]
    if ns.compare:
        if bad := [a for a in arms if a not in ARMS]:
            sys.exit(f"unknown arms {bad}; use {list(ARMS)}")
        if ns.negative or ns.stale or ns.features:
            sys.exit("--compare picks the techniques itself and runs scenarios only: drop --negative, --stale and --features")
    else:
        try:
            parse_features(ns.features)
        except ValueError as e:
            sys.exit(str(e))
    try:
        tuning = parse_tuning(ns.tuning)
    except ValueError as e:
        sys.exit(f"--tuning: {e}")

    import yaml
    split_of = {p.stem: (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("split", "dev") for p in rs.SCENARIO_DIR.glob("*.yaml")}
    avail = sorted(n for n, s in split_of.items() if ns.split in ("all", s))
    names = [] if ns.scenarios == "none" else avail if ns.scenarios == "all" else ns.scenarios.split(",")
    unknown = [n for n in names if n not in split_of]
    if unknown:
        sys.exit(f"unknown scenarios {unknown}; available: {sorted(split_of)}")
    if ns.scenarios not in ("all", "none") and ns.split != "all" and (off := [n for n in names if split_of[n] != ns.split]):
        sys.exit(f"--split {ns.split} but {off} belong to another split")

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))      # a Stop from the console unwinds through run_once's cleanup
    if (left := rs.recover_active()):
        say(f"== recovered fault {left} left over from an earlier run")
    ts = dt.datetime.now()
    out = ROOT / "reports" / f"{ts:%Y%m%dT%H%M%S}"
    n = 1
    while out.exists():                                         # two suites started within the same second must not share a folder
        n += 1
        out = ROOT / "reports" / f"{ts:%Y%m%dT%H%M%S}-{n}"
    out.mkdir(parents=True)
    meta = {"ts": ts.isoformat(timespec="seconds"), "provider": PROVIDER, "model": MODEL_NAME, "namespace": NAMESPACE,
            "dry_run": DRY_RUN, "args": vars(ns), "splits": {n: split_of[n] for n in names}, "tuning": tuning.as_dict(), "tuning_changed": tuning.changed(), "mode": "compare" if ns.compare else "suite",
            "arms": arms if ns.compare else None}
    cases: list[dict] = []

    def save():
        cmp = compare_arms(cases, arms) if ns.compare else None
        (out / "report.json").write_text(json.dumps({"meta": meta, "cases": cases, **({"comparison": cmp} if cmp else {})}, indent=2, default=str), encoding="utf-8")
        (out / "report.md").write_text(build_md(meta, cases, cmp), encoding="utf-8")

    def record(c):
        rs.stage("result", f"{c['status']}: {c.get('notes', '')}"[:300], {"PASS": "ok", "FAIL": "bad", "SKIP": "info"}.get(c["status"], "warn"))
        c["steps"] = rs.STEPS[:]                                # what happened in this test, step by step (shown in the console)
        rs.STEPS.clear()
        cases.append(c)
        say(f"   -> {c['kind']} {c['name']}{' [' + c['arm'] + ']' if c.get('arm') else ''} run {c.get('run','')}: {c['status']} {c.get('notes','')[:120]}")
        save()

    say("== guard checks (offline)")
    for name, ok, detail in tests_guards.run():
        cases.append({"kind": "guard", "name": name, "run": 1, "status": "PASS" if ok else "FAIL", "notes": detail})
    save()
    say(f"   {sum(c['status']=='PASS' for c in cases)}/{len(cases)} passed")

    def fresh():
        if ns.fresh_rag:
            say(f"   cleared {rag.clear_memory()} stored incidents")

    if names or ns.negative:
        down = [p for p in find_problems() if str(p).startswith("detector error")]
        if down:
            say("== preflight")
            rs.stage("baseline", f"cannot read metrics, nothing was run: {down[0][:250]}. Check PROMETHEUS_URL and that Prometheus is up.", "bad")
            record({"kind": "setup", "name": "metrics reachable", "run": 1, "status": "FAIL", "notes": down[0][:300] + " (check PROMETHEUS_URL and that Prometheus is running)"})
            save()
            say(f"REPORT_DIR={out}")
            return

    if ns.negative:
        say("== healthy system: the agent must change nothing")
        if clean_baseline(ns.reset_cmd):
            fresh()
            record(guarded({"kind": "healthy", "name": "no-fault", "run": 1}, no_change_case, "healthy", "no-fault", "check if there is any issue", ns))
        else:
            record({"kind": "healthy", "name": "no-fault", "run": 1, "status": "SKIP", "notes": "baseline not clean: " + "; ".join(map(str, UNCLEAN["left"]))[:300]})

    if ns.compare:
        for name in names:
            for arm in arms:
                for i in range(1, ns.runs + 1):
                    say(f"== scenario {name} [{arm}] run {i}/{ns.runs}")
                    if not clean_baseline(ns.reset_cmd):
                        record({"kind": "scenario", "name": name, "arm": arm, "run": i, "status": "SKIP", "notes": "baseline not clean: " + "; ".join(map(str, UNCLEAN["left"]))[:300]})
                        continue
                    if i == 1:                                  # new cell: start with empty memory; run 2 keeps what run 1 stored
                        say(f"   cleared {rag.clear_memory()} stored incidents")
                    record(guarded({"kind": "scenario", "name": name, "arm": arm, "run": i}, scenario_case, name, i, ns, arm))
                    time.sleep(20)
        save()
        say(f"REPORT_DIR={out}")
        return

    for name in names:
        for i in range(1, ns.runs + 1):
            say(f"== scenario {name} run {i}/{ns.runs}")
            if not clean_baseline(ns.reset_cmd):
                record({"kind": "scenario", "name": name, "run": i, "status": "SKIP", "notes": "baseline not clean: " + "; ".join(map(str, UNCLEAN["left"]))[:300]})
                continue
            fresh()
            record(guarded({"kind": "scenario", "name": name, "run": i}, scenario_case, name, i, ns))
            time.sleep(20)
        if ns.stale:
            say(f"== stale alert {name}")
            if clean_baseline(ns.reset_cmd):
                fresh()
                record(guarded({"kind": "stale", "name": name, "run": 1}, stale_case, name, ns))
            else:
                record({"kind": "stale", "name": name, "run": 1, "status": "SKIP", "notes": "baseline not clean: " + "; ".join(map(str, UNCLEAN["left"]))[:300]})

    save()
    say(f"REPORT_DIR={out}")


if __name__ == "__main__":
    try:
        main()
    finally:
        rs.recover_active()                                    # last safety net: never leave a fault behind

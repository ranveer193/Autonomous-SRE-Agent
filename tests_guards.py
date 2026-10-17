"""Offline tests for the safety guards in the write tools. No cluster, no LLM.
  python tests_guards.py        -> prints PASS/FAIL per check, exit code 1 on any failure
eval/run_suite.py imports run() and puts the results in the report."""
import sys, tempfile
from pathlib import Path
import sre_agent.tools.k8s as k

ROOT = Path(__file__).resolve().parent
# Names of the system under test and of eval scenarios. None may appear in agent-visible code, prompts or skills.
BANNED = ["payment-service", "order-service", "user-service", "api-gateway", "sre-playground", "postgres", "redis",
          "user_not_found", "POSTGRES_HOST", "memory_leak", "payment-bad-config", "redis-down", "payment-db-slow",
          # phrases that were copied from a scenario's symptom or answer into runbooks/prompts (test-set leakage)
          "could not translate", "/ready", "/health", "DBA", "unknown ids", "payment", "playground"]

REV = "deployment.apps/payment-service with revision #{r}\nPod Template:\n  Labels:\tapp=x\n\tpod-template-hash=h{r}\n  Environment:\n    A:\t1\n{extra}"


def _fake_k(calls):
    def fake(args, timeout=30):
        calls.append(args)
        if args[:2] == ["rollout", "history"]:
            if "--revision=7" in args:
                return REV.format(r=7, extra="    POSTGRES_HOST:\tbad\n")
            if "--revision=6" in args:
                return REV.format(r=6, extra="")
            return "REVISION CHANGE-CAUSE\n5 <none>\n6 <none>\n7 <none>"
        if args[:2] == ["rollout", "status"]:
            return "error: timed out waiting for the condition"
        return "ok"
    return fake


def _setup(symptoms):
    calls = []
    k._k = _fake_k(calls)
    k.find_problems = lambda: list(symptoms)
    k.wait_until_healthy = lambda **kw: (False, ["still broken"])
    k.DRY_RUN = False
    k.WRITABLE_DEPLOYMENTS = {"payment-service"}      # independent of system.yaml
    k.reset_attempts()
    return calls


def _writes(calls):
    return [c for c in calls if c[0] in ("rollout", "scale", "set") and c[1] not in ("history", "status")]


def _run() -> list[tuple[str, bool, str]]:
    out = []

    def check(name, cond, detail=""):
        out.append((name, bool(cond), detail))

    inv = lambda t, **a: t.invoke(a)

    c = _setup([])
    r = inv(k.restart_deployment, name="payment-service")
    check("healthy service: restart refused", r.startswith("NO_ACTION") and not _writes(c), r[:60])
    r = inv(k.scale_deployment, name="payment-service", replicas=2)
    check("healthy service: scale refused", r.startswith("NO_ACTION") and not _writes(c))
    r = inv(k.rollback_deployment, name="payment-service", to_revision=6)
    check("healthy service: rollback refused", r.startswith("NO_ACTION") and not _writes(c))

    c = _setup(["x: 5xx"])
    inv(k.restart_deployment, name="payment-service")
    check("failing service: restart runs", len(_writes(c)) == 1)
    r = inv(k.restart_deployment, name="prometheus")
    check("non-writable deployment refused", "not changeable" in r)
    r = inv(k.scale_deployment, name="payment-service", replicas=99)
    check("scale above 20 refused", r.startswith("Error"))

    c = _setup(["x: 5xx"])
    r = inv(k.rollback_deployment, name="payment-service", to_revision=7)
    check("rollback to current revision refused", r.startswith("Error") and not _writes(c))
    r = inv(k.rollback_deployment, name="payment-service", to_revision=99)
    check("rollback to unknown revision refused", r.startswith("Error") and not _writes(c))
    r = inv(k.rollback_deployment, name="payment-service", to_revision=6)
    check("rollback to older revision runs", ["rollout", "undo", "deployment/payment-service", "--to-revision=6"] in c)

    c = _setup(["x: 5xx"])
    r = inv(k.set_memory_limit, name="payment-service", limit_mi=1024)
    check("memory limit above 512 refused", r.startswith("Error") and not _writes(c))
    r = inv(k.set_memory_limit, name="payment-service", limit_mi=512)
    check("memory limit 512 runs", len(_writes(c)) == 1)
    c = _setup([])
    inv(k.set_memory_limit, name="payment-service", limit_mi=512)
    check("memory limit allowed on a healthy service", len(_writes(c)) == 1)

    c = _setup(["x: 5xx"])
    r1 = inv(k.verify_resolved, deployment="payment-service")
    r2 = inv(k.verify_resolved, deployment="payment-service")
    check("failed verify returns NOT_RESOLVED fast", r1.startswith("NOT_RESOLVED") and "1/2" in r1, r1[:50])
    r3 = inv(k.restart_deployment, name="payment-service")
    check("writes blocked after 2 failed fixes", r3.startswith("STOP") and not _writes(c), r3[:50])
    k.reset_attempts()
    c = _setup(["x: 5xx"])
    check("attempt counter resets per incident", inv(k.restart_deployment, name="payment-service") == "ok")

    c = _setup(["x: 5xx"])
    d = inv(k.rollout_diff, service="payment-service")
    check("rollout_diff shows the added setting", "+    POSTGRES_HOST:\tbad" in d and "pod-template-hash" not in d, d[-60:])

    import sre_agent.detect as d
    d.find_problems = lambda: []
    check("snapshot says healthy when no symptoms", d.snapshot_line() == "no active symptoms")
    d.find_problems = lambda: ["payment-service: 5xx 40%", "detector error: x"]
    check("snapshot lists live symptoms only", d.snapshot_line() == "payment-service: 5xx 40%", d.snapshot_line())

    # ---- generality: no system-specific names in anything the model or a reader of the agent code sees
    files = [*ROOT.glob("sre_agent/**/*.py"), *ROOT.glob("skills/**/*.md"), ROOT / "api.py", ROOT / "ui" / "index.html"]
    hits = [f"{f.relative_to(ROOT)}: {t}" for f in files if f.exists() for t in BANNED if t.lower() in f.read_text(encoding="utf-8").lower()]
    check("no system-specific names in agent code, prompts, skills or UI", not hits, "; ".join(hits[:5]))
    ev = [f"{f.relative_to(ROOT)}" for f in [*ROOT.glob('sre_agent/**/*.py'), *ROOT.glob('skills/**/*.md')] if 'ground_truth' in f.read_text(encoding='utf-8') or 'faultctl' in f.read_text(encoding='utf-8')]
    check("agent code and skills never reference ground truth or the fault tool", not ev, "; ".join(ev))

    from sre_agent import profile
    d = profile.load(ROOT / "does-not-exist.yaml")
    check("no system file: nothing writable, topology unknown", d["writable"] == [] and d["topology"] == "" and not d["metrics"]["dependency"])
    check("profile defaults contain no system names", not [t for t in BANNED if t.lower() in repr(profile.DEFAULTS).lower()])
    with tempfile.TemporaryDirectory() as td:
        f = Path(td) / "s.yaml"
        f.write_text("namespace: other\nmetrics:\n  requests: my_requests_total\n")
        m = profile.load(f)
        check("system file overrides merge with defaults", m["namespace"] == "other" and m["metrics"]["requests"] == "my_requests_total"
              and m["metrics"]["service_label"] == "service" and m["thresholds"]["p95_seconds"] == 1.0)

    k.WRITABLE_DEPLOYMENTS = set()
    check("empty writable list blocks every change and says how to enable", "system.yaml" in (k._writable("x") or ""))

    return out


def run() -> list[tuple[str, bool, str]]:
    """Run every check, then put back everything the checks replaced.
    The checks swap in fake kubectl / health functions. eval/run_suite.py runs them in the SAME process right before it starts the
    real agent, so anything left patched would silently break the suite (health check always failing, fake kubectl, writes refused)."""
    import sre_agent.detect as det
    targets = [(k, n) for n in ("_k", "find_problems", "wait_until_healthy", "DRY_RUN", "WRITABLE_DEPLOYMENTS")] + [(det, "find_problems")]
    saved = [(m, n, getattr(m, n)) for m, n in targets]
    try:
        out = _run()
    finally:
        for m, n, v in saved:
            setattr(m, n, v)
        k.reset_attempts()
    left = [f"{m.__name__.split('.')[-1]}.{n}" for m, n, v in saved if getattr(m, n) is not v]
    out.append(("guard checks leave no test patches behind", not left, ", ".join(left)))
    return out


if __name__ == "__main__":
    res = run()
    for name, ok, detail in res:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok or not detail else f"  [{detail}]"))
    bad = [r for r in res if not r[1]]
    print(f"\n{len(res) - len(bad)}/{len(res)} passed")
    sys.exit(1 if bad else 0)

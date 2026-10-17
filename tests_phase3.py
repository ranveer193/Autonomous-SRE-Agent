"""Offline tests for run-to-run isolation, memory correctness and "nothing leaks to the agent". No cluster, no LLM, no model download.
  python tests_phase3.py"""
import os, sys, tempfile, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.environ["SRE_MEMORY_DIR"] = tempfile.mkdtemp(prefix="sre_mem_test_")      # never touch the real store
sys.path.insert(0, str(ROOT))

res = []
check = lambda name, cond, d="": res.append((name, bool(cond), d))

from sre_agent import incident as I                                          # noqa: E402
from sre_agent.detect import contiguous_start                               # noqa: E402

# ---------- incident window: back-to-back incidents must not merge ----------
STEP, thr = 15, (lambda v: v > 0.5)


def series(gap_s, kind="healthy", prev_end=300, new_len=8):
    pts = [(t, 1.0) for t in range(0, prev_end + 1, STEP)]
    t = prev_end + STEP
    while t <= prev_end + gap_s:
        pts.append((t, 0.0 if kind == "healthy" else float("nan")))
        t += STEP
    pts += [(t + i * STEP, 1.0) for i in range(new_len)]
    return pts


check("30s of healthy samples between two incidents ends the first one", contiguous_start(series(30), thr, STEP) == 345, str(contiguous_start(series(30), thr, STEP)))
check("a single healthy sample inside one incident is forgiven", contiguous_start([(t, 1.0) for t in range(0, 150, STEP)] + [(150, 0.0)] + [(165 + i * STEP, 1.0) for i in range(6)], thr, STEP) == 0)
check("short run of MISSING samples (no traffic) does not end an incident", contiguous_start(series(30, "nan"), thr, STEP) == 0)
check("long run of missing samples does", contiguous_start(series(45, "nan"), thr, STEP) == 360)
check("incident already over: no onset", contiguous_start([(0, 1.0), (15, 0.0)], thr, STEP) is None)

now = time.time()
I.begin(["a: HTTP 5xx rate 90% (threshold 5%)"], now=now, onset_ts=now - 40)
check("alert time given: used as the window start, no estimate", abs(I.window_start() - (now - 40)) < 1e-6)
check("tools look back to onset minus the pre-roll", I.lookback_minutes(120, now=now) == 3, str(I.lookback_minutes(120, now=now)))

# ---------- who gets credit for a fix ----------
I.begin(["a: HTTP 5xx rate 90% (threshold 5%)"], onset_ts=now)
I.record_write("restart", ["rollout", "restart", "deployment/db"])
I.record_write("scale", ["scale", "deployment/db", "--replicas=1"], "replicas=1")
I.record_verify(True, "db")
check("two changes before one RESOLVED: only the last is the fix", I.fix_summary(True) == "scale db replicas=1", I.fix_summary(True))
check("the earlier change is neither credited nor blamed", I.fix_summary(False) == "" and not I.has_failed_attempt())
I.begin([], onset_ts=now)
I.record_write("rollback", ["rollout", "undo", "deployment/other"], "to_revision=3")
I.record_verify(False, "other")
I.record_write("scale", ["scale", "deployment/db", "--replicas=1"], "replicas=1")
I.record_verify(True, "db")
check("a change followed by NOT_RESOLVED is remembered as tried-and-failed, the next one as the fix",
      I.fix_summary(False) == "rollback other to_revision=3" and I.fix_summary(True) == "scale db replicas=1" and I.effective_target() == "db")

# ---------- memory (real chromadb, fake embedder) ----------
from sre_agent import rag                                                    # noqa: E402


class _Emb:
    def embed_documents(self, xs):
        return [[1.0, 0.5, 0.25] for _ in xs]

    def embed_query(self, x):
        return [1.0, 0.5, 0.25]


rag._emb_cache[:] = [_Emb()]
rag.clear_memory()
SYM = ["gw: HTTP 5xx rate 90% (threshold 5%)", "orders: HTTP 5xx rate 80% (threshold 5%)", "users: p95 latency 4.00s (threshold 1.0s)"]
OTHER = ["cache: HTTP 5xx rate 90% (threshold 5%)", "web: p95 latency 4.00s (threshold 1.0s)"]


def search(problems):
    I.begin(problems, onset_ts=now)
    return rag.search_incidents.invoke({})


check("empty store: nothing recalled, not counted as a recall", "No past incidents" in search(SYM) and not I.recalled())

I.begin(SYM, onset_ts=now)
msg = rag.save_incident.invoke({"service": "gw", "symptoms": "x", "root_cause": "gateway is broken", "fix": "restart gw", "outcome": "resolved", "ttr_seconds": 1})
check("no change made: nothing is saved", msg.startswith("NOT SAVED"), msg)

I.begin(SYM, onset_ts=now)
I.record_write("restart", ["rollout", "restart", "deployment/gw"])
I.record_write("scale", ["scale", "deployment/db", "--replicas=1"], "replicas=1")
I.record_verify(True, "db")
msg = rag.save_incident.invoke({"service": "gw", "symptoms": "x", "root_cause": "gateway is broken", "fix": "restart gw", "outcome": "resolved", "ttr_seconds": 1})
rows = rag.list_incidents()
check("saved from what the tools recorded, not from the model's account",
      msg.startswith("Saved") and len(rows) == 1 and rows[0]["fix"] == "scale db replicas=1" and rows[0]["component"] == "db" and rows[0]["claimed"] == "gw"
      and not rows[0]["confirmed"] and "unconfirmed" in rows[0]["doc"], str(rows[:1]))
check("the wasted first action is not stored as part of the fix", "restart" not in rows[0]["fix"] and rows[0]["tried_failed"] == "")

I.begin(SYM, onset_ts=now)
I.record_write("scale", ["scale", "deployment/db", "--replicas=1"], "replicas=1")
I.record_verify(True, "db")
msg = rag.save_incident.invoke({"service": "db", "symptoms": "x", "root_cause": "db was down", "fix": "scale", "outcome": "resolved", "ttr_seconds": 1})
check("the same incident again is merged and counted, not stored twice", msg.startswith("Merged") and len(rag.list_incidents()) == 1 and rag.list_incidents()[0]["count"] == 2, msg)

out = search(SYM)
check("same symptoms: recalled as a hypothesis, flagged as a recall", "HYPOTHESES" in out and "scale db replicas=1" in out and I.recalled(), out[:200])
check("repeats are not presented as independent confirmation", "not independent confirmations" in out)
out = search(OTHER)
check("different symptoms: nothing recalled, and not counted as a recall (rag_hit stays False)", out.startswith("No past incident with a similar") and not I.recalled(), out[:100])
check("a 'failed' save needs a change that was followed by NOT_RESOLVED", (I.begin(SYM, onset_ts=now), rag.save_incident.invoke(
    {"service": "db", "symptoms": "x", "root_cause": "r", "fix": "f", "outcome": "failed", "ttr_seconds": 1}))[1].startswith("NOT SAVED"))
check("memory store path is absolute (same store for console, CLI and harness)", rag.STORE.is_absolute(), str(rag.STORE))

# ---------- tools: no-ops are refused and not counted ----------
import sre_agent.tools.k8s as k                                              # noqa: E402

calls = []
k._k = lambda args, timeout=30: (calls.append(list(args)), "1" if args[:2] == ["get", "deployment"] else "ok")[1]
k.find_problems = lambda: ["gw: HTTP 5xx rate 50% (threshold 5%)"]
k.WRITABLE_DEPLOYMENTS = {"db"}
k.DRY_RUN = False
k.reset_attempts()
msg = k.scale_deployment.invoke({"name": "db", "replicas": 1})
check("scale to the current replica count: refused as a no-op, nothing written, not a failed attempt",
      msg.startswith("NO_OP") and not [c for c in calls if c[0] == "scale"] and k._state["failed"] == 0, msg)
msg = k.scale_deployment.invoke({"name": "db", "replicas": 2})
check("scale to a different count still runs", [c for c in calls if c[0] == "scale"], msg)

# ---------- the localizer's baseline cannot be hidden by the previous run ----------
from sre_agent import localizer as L                                         # noqa: E402

seen = []
L.prom_query = lambda q, timeout=15: (seen.append(q), [])[1]
I.begin(SYM, now=now, onset_ts=now - 200)
L.collect(now)
before = [q for q in seen if "offset" in q]
current = [q for q in seen if "offset" not in q]
check("'before' values are the minimum over the 5 minutes before onset", before and all(q.startswith("min_over_time((") and "[5m:15s] offset" in q for q in before), str(before[:1]))
check("current values are queried without an offset", current)

# ---------- nothing leaks to the agent ----------
from sre_agent import agent as A                                             # noqa: E402

with tempfile.TemporaryDirectory() as td:
    A.SANDBOX = Path(td) / "agent_fs"
    (A.SANDBOX).mkdir(parents=True)
    (A.SANDBOX / "notes.md").write_text("left by the previous run")
    (A.SANDBOX / "skills").mkdir()
    (A.SANDBOX / "skills" / "deleted-runbook.md").write_text("old")
    A._sandbox()
    check("the agent's file sandbox is rebuilt for every run (no notes or stale runbooks carried over)",
          not (A.SANDBOX / "notes.md").exists() and not (A.SANDBOX / "skills" / "deleted-runbook.md").exists() and (A.SANDBOX / "skills").is_dir())

# metrics.py is scoring code (judge verdict names): only the harness and the API import it. The check below proves nothing on the agent's side does.
agent_files = [f for f in [*ROOT.glob("sre_agent/**/*.py"), *ROOT.glob("skills/**/*.md")] if f.name != "metrics.py"]
agent_side = [*ROOT.glob("sre_agent/tools/*.py"), *(ROOT / "sre_agent" / n for n in ("agent.py", "rag.py", "incident.py", "localizer.py", "detect.py", "runner.py"))]
check("scoring code (metrics.py) is not imported by anything on the agent's side",
      not [f.name for f in agent_side if "metrics import" in f.read_text(encoding="utf-8") and "from .metrics" in f.read_text(encoding="utf-8")
           or "import metrics" in f.read_text(encoding="utf-8")])
text = "\n".join(f.read_text(encoding="utf-8") for f in agent_files).lower()
LEAKY = ["faultctl", "ground_truth", "correct_remediation", "/admin/faults", "scenarios/", "distractor", "tidy_history", "run_scenario"]
check("nothing the agent can read mentions fault injection, ground truth or the harness", not [w for w in LEAKY if w in text], str([w for w in LEAKY if w in text]))
check("a change made before the symptoms is not hinted at in the localizer prompt", "a change made before the symptoms began" not in A.LOCALIZER_PROMPT)
check("the harness passes only the alert time, nothing about the scenario",
      "onset=t_detect" in (ROOT / "eval" / "run_scenario.py").read_text(encoding="utf-8"))

for n, ok, d in res:
    print(("PASS " if ok else "FAIL ") + n + ("" if ok or not d else f"  [{d}]"))
bad = [r for r in res if not r[1]]
print(f"\n{len(res) - len(bad)}/{len(res)} passed")
sys.exit(1 if bad else 0)

"""Local ops console for the SRE agent.   python api.py   ->   http://127.0.0.1:8765
Shows live cluster state, runs the agent with in-browser approvals, replays past traces, lists incident memory,
and starts the failure suite. No auth: it binds to localhost only. Do not expose it."""
import json, os, re, subprocess, sys, threading, time, math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

import uvicorn, yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sre_agent import rag
from sre_agent.clients import prom_query
from sre_agent.config import NAMESPACE, DRY_RUN, PROVIDER, MODEL_NAME, SUB_PROVIDER, SUB_MODEL, METRICS as M
from sre_agent.detect import find_problems
from sre_agent.llm import PROVIDERS, key_is_set
from sre_agent.model_check import check_model
from sre_agent.features import ARMS, DEFAULT_ARMS, Features, from_env, parse as parse_features
from sre_agent.runner import run_alert
from sre_agent.tuning import SPEC as TUNING_SPEC, Tuning, from_env as tuning_from_env, parse as parse_tuning, to_cli as tuning_cli
from sre_agent.tools.k8s import _k

ROOT = Path(__file__).resolve().parent
LOGS = Path(os.getenv("SRE_LOG_DIR", "logs"))
REPORTS = ROOT / "reports"
REPORTS.mkdir(exist_ok=True)
sys.path.insert(0, str(Path(__file__).resolve().parent / "eval"))
from target import SCENARIO_DIR, faultctl_cmd   # noqa: E402  (eval target: scenarios + fault command)
ACTIVE = ROOT / "eval" / ".active_fault"          # written by the harness while a fault is injected
NAME_OK = re.compile(r"^[\w][\w.\-]*$")

app = FastAPI(title="SRE agent console")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _hostname(h: str) -> str:
    h = h.split("//", 1)[-1].split("/", 1)[0]
    return h.split("]")[0] + "]" if h.startswith("[") else h.split(":")[0]


@app.middleware("http")
async def local_only(request: Request, call_next):
    """The console has no login and can approve cluster changes, so refuse requests that did not come from this machine's own page."""
    origin = request.headers.get("origin")
    if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS or (origin and _hostname(origin) not in LOCAL_HOSTS):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


app.mount("/files", StaticFiles(directory=str(REPORTS)), name="files")
RUNS: dict[str, dict] = {}
SUITE: dict = {"proc": None}
LOCK = threading.Lock()


def num(v):
    v = float(v)
    return None if math.isnan(v) or math.isinf(v) else round(v, 3)


def busy() -> str | None:
    if any(r["status"] in ("running", "waiting_approval") for r in RUNS.values()):
        return "an agent run is active"
    p = SUITE.get("proc")
    if p and p.poll() is None:
        return "the test suite is running"
    return None


def events(path: Path, after: int) -> dict:
    if not path.exists():
        return {"events": [], "next": after}
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")[:-1]          # last element is a partial line or empty
    rows = []
    for l in lines[after:]:
        try:
            rows.append(json.loads(l))
        except Exception:
            break
    return {"events": rows, "next": after + len(rows)}


def public(r: dict) -> dict:
    return {k: v for k, v in r.items() if k not in ("ev", "decision")}


# ---------- live state ----------
@app.get("/api/overview")
def overview():
    out = {"time": time.strftime("%H:%M:%S"), "agent": {"provider": PROVIDER, "model": MODEL_NAME, "dry_run": DRY_RUN, "namespace": NAMESPACE},
           "problems": find_problems(), "pods": _k(["get", "pods", "-o", "wide"]), "services": {}, "memory": [], "errors": []}
    sl = M["service_label"]
    qs = {"rps": f'sum by ({sl}) (rate({M["requests"]}[1m]))',
          "err_pct": f'100 * sum by ({sl}) (rate({M["errors"]}[1m])) / sum by ({sl}) (rate({M["requests"]}[1m]))',
          "p95_s": f'histogram_quantile(0.95, sum by (le, {sl}) (rate({M["duration"]}[1m])))'}
    def run(q):
        try:
            return prom_query(q, timeout=5), None
        except Exception as e:
            return None, type(e).__name__
    with ThreadPoolExecutor(max_workers=3) as ex:
        res = dict(zip(qs, ex.map(run, qs.values())))
    for key, (rows, e) in res.items():
        if e:
            out["errors"].append(f"{key}: {e}")
            continue
        for s in rows:
            out["services"].setdefault(s["metric"].get(sl, "?"), {})[key] = num(s["value"][1])
    try:
        sel = f'namespace="{NAMESPACE}", container!=""'
        mem = {s["metric"]["pod"]: num(s["value"][1]) for s in prom_query(f'sum by (pod) (container_memory_working_set_bytes{{{sel}}}) / 1048576')}
        lim = {s["metric"]["pod"]: num(s["value"][1]) for s in prom_query(f'sum by (pod) (kube_pod_container_resource_limits{{namespace="{NAMESPACE}", resource="memory"}}) / 1048576')}
        out["memory"] = sorted(({"pod": p, "mib": m, "limit_mib": lim.get(p)} for p, m in mem.items()), key=lambda x: x["pod"])
    except Exception as e:
        out["errors"].append(f"memory: {type(e).__name__}")
    return out


# ---------- model check ----------
def _targets() -> list[dict]:
    t = [{"role": "main agent", "provider": PROVIDER, "model": MODEL_NAME}]
    t.append({"role": "subagents", "provider": SUB_PROVIDER, "model": SUB_MODEL, "same_as_main": (SUB_PROVIDER, SUB_MODEL) == (PROVIDER, MODEL_NAME)})
    return t


@app.get("/api/models")
def models_info():
    return {"providers": list(PROVIDERS), "targets": [{**t, "key_set": key_is_set(t["provider"])} for t in _targets()]}


class ModelCheckReq(BaseModel):
    provider: str = ""            # both empty = check the configured main and subagent models
    model: str = ""


@app.post("/api/models/check")
def models_check(b: ModelCheckReq):
    if b.provider or b.model:                                       # a candidate model, before it is put into .env
        if b.provider not in PROVIDERS or not re.match(r"^[\w./:\-]{1,120}$", b.model):
            raise HTTPException(400, "unknown provider or bad model name")
        targets = [{"role": "candidate", "provider": b.provider, "model": b.model}]
    else:
        targets = _targets()
    keys = list(dict.fromkeys((t["provider"], t["model"]) for t in targets))      # same model for main and subagents: checked once
    found: dict = {}

    def work(k):
        found[k] = check_model(*k)
    threads = [threading.Thread(target=work, args=(k,)) for k in keys]
    for th in threads:                                                              # distinct models are checked in parallel
        th.start()
    for th in threads:
        th.join()
    return {"results": [{**found[(t["provider"], t["model"])], "role": t["role"], "key_set": key_is_set(t["provider"]),
                         "same_as_main": bool(t.get("same_as_main"))} for t in targets]}


# ---------- agent runs ----------
class RunReq(BaseModel):
    alert: str = ""
    auto_approve: bool = False
    features: dict | None = None         # one bool per technique (see sre_agent/features.py); omitted = env defaults
    tuning: dict | None = None           # numbers behind the techniques (see sre_agent/tuning.py); omitted or blank = env, then built-in defaults


def checked_tuning(raw: dict | None) -> Tuning:
    """Strict: a bad number is a 400 that names the field and its range, never silently the default."""
    try:
        return parse_tuning(raw)
    except ValueError as e:
        raise HTTPException(400, f"bad tuning: {e}")


@app.get("/api/features")
def features_info():
    return {"defaults": from_env().as_dict(), "default_arms": list(DEFAULT_ARMS), "arms": {k: v.as_dict() for k, v in ARMS.items()},
            "tuning": {"defaults": tuning_from_env().as_dict(), "spec": TUNING_SPEC},
            "models": {"main": f"{PROVIDER}/{MODEL_NAME}", "subagents": f"{SUB_PROVIDER}/{SUB_MODEL}"}}


def _worker(run: dict) -> None:
    def ask(prompt: str) -> str:
        run["pending"], run["status"] = prompt, "waiting_approval"
        run["ev"].clear()
        ok = run["ev"].wait(900) and run.get("decision") is True            # no answer in 15 min = reject
        run["pending"], run["status"] = None, "running"
        return "y" if ok else "n"
    try:
        r = run_alert(run["alert"], auto_approve=run["auto_approve"], ask=ask, on_start=lambda p: run.update(log=Path(p).name), features=Features(**run["features"]),
                      tuning=Tuning(**run["tuning"]))
        run.update(status="done", result={"final": r["final"], "actions": r["actions"], "stats": r["stats"],
                                          "rag_hit": r["rag_hit"], "seconds": r["seconds"], "features": r["features"], "tuning": r.get("tuning"), "models": r["models"]})
    except Exception as e:
        run.update(status="error", error=f"{type(e).__name__}: {e}")


@app.post("/api/runs")
def start_run(body: RunReq):
    with LOCK:
        if (why := busy()):
            raise HTTPException(409, f"Not started: {why}")
        alert = body.alert.strip() or "; ".join(find_problems()) or "no active symptoms detected"
        rid = time.strftime("%H%M%S")
        try:
            feats = Features(**{k: bool(v) for k, v in (body.features or {}).items() if k in Features.__dataclass_fields__}) if body.features else from_env()
        except Exception:
            raise HTTPException(400, "bad features")
        tune = checked_tuning(body.tuning)
        run = {"id": rid, "features": feats.as_dict(), "tuning": tune.as_dict(), "alert": alert, "auto_approve": body.auto_approve, "status": "running", "started": time.time(),
               "log": None, "pending": None, "result": None, "error": None, "ev": threading.Event()}
        RUNS[rid] = run
    threading.Thread(target=_worker, args=(run,), daemon=True).start()
    return public(run)


@app.get("/api/runs")
def list_runs():
    return [public(r) for r in sorted(RUNS.values(), key=lambda r: -r["started"])]


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    if rid not in RUNS:
        raise HTTPException(404)
    return public(RUNS[rid])


class Decision(BaseModel):
    approve: bool


@app.post("/api/runs/{rid}/decision")
def decide(rid: str, d: Decision):
    run = RUNS.get(rid)
    if not run or run["status"] != "waiting_approval":
        raise HTTPException(409, "nothing is waiting for approval")
    run["decision"] = d.approve
    run["ev"].set()
    return {"ok": True}


# ---------- traces ----------
@app.get("/api/logs")
def list_logs():
    rows = []
    for f in sorted(LOGS.glob("*.jsonl"), reverse=True)[:40]:
        r = {"name": f.name, "alert": "", "tool_calls": 0, "llm_errors": 0, "seconds": 0, "subagents": 0, "writes": 0, "done": False, "failed_subs": 0}
        for l in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(l)
            except Exception:
                continue
            ev = d.get("event")
            if d.get("kind") == "🚨" and not r["alert"]:
                r["alert"] = d["msg"].replace("ALERT: ", "")
            r["tool_calls"] += ev == "tool_start"
            r["llm_errors"] += ev == "llm_error"
            r["subagents"] += ev == "subagent_start"
            r["failed_subs"] += bool(ev == "subagent_end" and d.get("failed"))
            r["writes"] += ev == "approval_request"
            r["done"] = r["done"] or ev == "done"
            r["seconds"] = round(d.get("t", r["seconds"]))
        rows.append(r)
    return rows


@app.get("/api/logs/{name}/events")
def log_events(name: str, after: int = 0):
    if not NAME_OK.match(name):
        raise HTTPException(400)
    return events(LOGS / name, after)


# ---------- memory, scenarios ----------
@app.get("/api/memory")
def memory():
    return rag.list_incidents()


@app.post("/api/memory/clear")
def memory_clear():
    return {"deleted": rag.clear_memory()}


@app.get("/api/scenarios")
def scenarios():
    out = []
    for p in sorted(SCENARIO_DIR.glob("*.yaml")):
        try:
            y = yaml.safe_load(p.read_text(encoding="utf-8"))
            out.append({"name": p.stem, "severity": y.get("severity"), "description": y.get("description"),
                        "root_cause": (y.get("ground_truth") or {}).get("root_cause")})
        except Exception:
            out.append({"name": p.stem})
    return out


# ---------- test suite ----------
class SuiteReq(BaseModel):
    scenarios: list[str] = []
    runs: int = 1
    negative: bool = True
    stale: bool = False
    fresh_rag: bool = True
    reset_cmd: str = ""
    compare: bool = False                 # every scenario x every arm x `runs`; the suite's healthy/stale cases are not part of it
    arms: list[str] = []                  # arms to compare (empty = all)
    features: dict | None = None          # normal suite only: one bool per technique (see sre_agent/features.py); omitted = env defaults
    tuning: dict | None = None            # numbers behind the techniques; applies to every case and every arm. Blank fields = env/built-in default


@app.post("/api/suite")
def start_suite(b: SuiteReq):
    with LOCK:
        if (why := busy()):
            raise HTTPException(409, f"Not started: {why}")
        if not all(NAME_OK.match(s) for s in b.scenarios):
            raise HTTPException(400, "bad scenario name")
        runs = max(1, min(b.runs, 5))
        tune = checked_tuning(b.tuning)               # validated here, so a bad number is an error in the form, not a suite that dies at start
        cmd = [sys.executable, "eval/run_suite.py", "--auto-approve", "--runs", str(runs), "--scenarios", ",".join(b.scenarios) or "none"]
        arms = b.arms or list(DEFAULT_ARMS)
        if b.compare:
            if not b.scenarios or any(a not in ARMS for a in arms):
                raise HTTPException(400, "compare needs at least one scenario and known arms")
            cmd += ["--compare", "--arms", ",".join(arms)]
        else:
            cmd += [f for f, on in (("--negative", b.negative), ("--stale", b.stale), ("--fresh-rag", b.fresh_rag)) if on]
            if b.features is not None:
                on = [k for k in Features.__dataclass_fields__ if b.features.get(k)]
                cmd += ["--features", ",".join(on) or "none"]
        if b.tuning:
            cmd += ["--tuning", tuning_cli(tune)]
        if b.reset_cmd.strip():
            cmd += ["--reset-cmd", b.reset_cmd.strip()]
        logf = REPORTS / "_suite.log"
        plan = (len(b.scenarios) * runs * len(arms) if b.compare
                else len(b.scenarios) * runs + (1 if b.negative else 0) + (len(b.scenarios) if b.stale else 0))
        SUITE.update(plan=plan, stopped=False, recovery=None, proc=subprocess.Popen(cmd, cwd=ROOT, stdout=open(logf, "w", encoding="utf-8"), stderr=subprocess.STDOUT,
                                           env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}), started=time.time(), cmd=" ".join(cmd))
    return {"started": True}


def active_fault() -> str | None:
    try:
        n = ACTIVE.read_text(encoding="utf-8").strip()
        return n if n and NAME_OK.match(n) else None
    except OSError:
        return None


def recover_now() -> dict:
    """Run the harness recovery for the fault marked active. Used by Stop and by the Recover button."""
    name = active_fault()
    if not name:
        return {"recovered": None, "fault": None}
    try:
        subprocess.run([*faultctl_cmd(), "recover", name], check=True, timeout=180, capture_output=True)
        ACTIVE.unlink(missing_ok=True)
        return {"recovered": True, "fault": name}
    except Exception as e:
        return {"recovered": False, "fault": name, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/faults/recover")
def faults_recover():
    if (why := busy()):
        raise HTTPException(409, f"Not recovered: {why}")
    return recover_now()


@app.get("/api/suite")
def suite_status():
    p, logf = SUITE.get("proc"), REPORTS / "_suite.log"
    lines = logf.read_text(encoding="utf-8", errors="replace").splitlines() if logf.exists() else []
    marks = [l for l in lines if l.startswith(("== ", "   -> ", "REPORT_DIR=", "   baseline", "   cleared", "@@ ")) or re.match(r"^\s+\d+/\d+ passed", l)]   # whole-run progress, even when the tail is long
    return {"running": bool(p and p.poll() is None), "exit_code": p.poll() if p else None, "cmd": SUITE.get("cmd"), "tail": lines[-200:], "marks": marks,
            "plan": SUITE.get("plan", 0), "started": SUITE.get("started"), "stopped": SUITE.get("stopped", False),
            "active_fault": active_fault(), "recovery": SUITE.get("recovery")}


@app.post("/api/suite/stop")
def stop_suite():
    p = SUITE.get("proc")
    if not p or p.poll() is not None:
        raise HTTPException(409, "the suite is not running")
    SUITE["stopped"] = True
    p.terminate()

    def cleanup():          # the suite may be killed without running its own cleanup (always on Windows), so restore the fault here
        try:
            p.wait(15)
        except Exception:
            p.kill()
        SUITE["recovery"] = recover_now()
    threading.Thread(target=cleanup, daemon=True).start()
    return {"stopped": True}


@app.get("/api/reports")
def list_reports():
    out = []
    for d in sorted((d for d in REPORTS.iterdir() if d.is_dir() and (d / "report.json").exists()), key=lambda d: d.name, reverse=True)[:30]:
        try:
            r = json.loads((d / "report.json").read_text(encoding="utf-8"))
            cs = [c for c in r["cases"] if c.get("kind") != "guard"]
            cnt = lambda *st: sum(1 for c in cs if c.get("status") in st)
            out.append({"name": d.name, "model": f"{r['meta'].get('provider')}/{r['meta'].get('model')}", "cases": len(cs), "mode": r["meta"].get("mode", "suite"),
                        "pass": cnt("PASS"), "partial": cnt("PARTIAL", "WARN"), "fail": cnt("FAIL"), "skip": cnt("SKIP")})
        except Exception:
            out.append({"name": d.name, "model": "?", "cases": 0, "pass": 0, "partial": 0, "fail": 0, "skip": 0})
    return out


@app.get("/api/reports/{name}")
def get_report(name: str):
    if not NAME_OK.match(name) or not (REPORTS / name / "report.json").exists():
        raise HTTPException(404)
    return {"json": json.loads((REPORTS / name / "report.json").read_text(encoding="utf-8")), "md": (REPORTS / name / "report.md").read_text(encoding="utf-8")}


@app.get("/")
def index():
    return FileResponse(ROOT / "ui" / "index.html")


if __name__ == "__main__":
    port = int(os.getenv("API_PORT", "8765"))      # 8080 is often taken on Windows; set API_PORT in .env to change
    print(f"Console: http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port)

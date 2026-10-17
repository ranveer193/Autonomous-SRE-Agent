"""Kubernetes tools for the configured namespace. READ tools go to inspectors; WRITE tools only to the
remediator and only for WRITABLE_DEPLOYMENTS. (Swap for an MCP server later without touching agent wiring.)"""
import datetime as dt, difflib, subprocess, time
from langchain_core.tools import tool
from ..config import NAMESPACE, DRY_RUN, WRITABLE_DEPLOYMENTS, SELECTOR_LABEL, KUBECTL, MAX_REPLICAS, MAX_MEMORY_MI
from ..detect import wait_until_healthy, find_problems
from .. import incident

MAX_FIX_ATTEMPTS = 2                      # after this many failed fixes the agent must escalate, not keep trying
_state = {"failed": 0}


def reset_attempts() -> None:
    _state["failed"] = 0


def _k(args: list[str], timeout: int = 30) -> str:
    try:
        r = subprocess.run([*KUBECTL, "-n", NAMESPACE, *args], capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return f"kubectl error: {e}"
    out = (r.stdout + r.stderr).strip() or "(no output)"
    return out if len(out) < 6000 else out[:3000] + "\n...[truncated]...\n" + out[-2500:]


def _kraw(args: list[str], timeout: int = 30) -> str:
    """Like _k but never cut and never decorated: for output the code parses. '' on any failure."""
    try:
        r = subprocess.run([*KUBECTL, "-n", NAMESPACE, *args], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _ts(s: str) -> float | None:
    try:
        return dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _rel(t: float) -> str:
    return incident.relative(t)


def events_since(raw: str, since: float | None, limit: int = 45) -> list[str]:
    """Pure. raw = kubectl jsonpath rows 'lastTimestamp|eventTime|created|type|reason|object|message'. Keeps events at or after
    `since` (None = all), oldest first, each tagged with its time relative to the incident onset."""
    rows = []
    for line in raw.splitlines():
        f = line.split("|", 6)
        if len(f) < 7:
            continue
        t = _ts(f[0]) or _ts(f[1]) or _ts(f[2])
        if t is None or (since is not None and t < since):
            continue
        rows.append((t, f"[{_rel(t)}] {f[3]} {f[4]} {f[5]}: {f[6][:160]}" if incident.window_start() is not None
                     else f"{f[0] or f[2]} {f[3]} {f[4]} {f[5]}: {f[6][:160]}"))
    return [r for _, r in sorted(rows)][-limit:]


def _writable(name: str) -> str | None:
    if name in WRITABLE_DEPLOYMENTS:
        return None
    hint = "" if WRITABLE_DEPLOYMENTS else " (no deployment is writable: set `writable` in system.yaml to allow changes)"
    return f"Error: '{name}' is not changeable. Allowed: {sorted(WRITABLE_DEPLOYMENTS)}{hint}"


# ---------- READ ----------
@tool
def list_pods() -> str:
    """List all pods with READY, STATUS, RESTARTS and age. Shows NotReady / restarting / missing pods."""
    return _k(["get", "pods", "-o", "wide"])


@tool
def describe_pod(pod: str) -> str:
    """Describe a pod: container state, last termination (OOMKilled, exit code), probe failures, events."""
    return _k(["describe", "pod", pod])


@tool
def get_pod_logs(pod: str, lines: int = 80, previous: bool = False) -> str:
    """Raw logs of a pod straight from kubectl (use Loki tools first). previous=true = logs of the crashed container."""
    args = ["logs", pod, f"--tail={min(lines, 300)}"] + (["--previous"] if previous else [])
    return _k(args)


@tool
def get_events() -> str:
    """Namespace events of the CURRENT incident (scheduling, probe failures, ScalingReplicaSet, OOM kills), oldest first. Each line
    shows its time relative to symptom onset ('-30s' = before the symptoms began). Events from earlier incidents are left out."""
    since = incident.since_epoch()
    raw = _kraw(["get", "events", "-o", "jsonpath={range .items[*]}{.lastTimestamp}|{.eventTime}|{.metadata.creationTimestamp}|{.type}|{.reason}|{.involvedObject.name}|{.message}{\"\\n\"}{end}"])
    rows = events_since(raw, since)
    if rows:
        return "\n".join(rows)
    if raw.strip():
        return "No events in the current incident window."
    out = _k(["get", "events", "--sort-by=.lastTimestamp"])             # kubectl output not parseable: fall back to the raw list
    return "\n".join(out.splitlines()[-45:])


@tool
def get_deployment(name: str) -> str:
    """Deployment spec as YAML: image, env, resources, probes, replicas, strategy."""
    return _k(["get", "deployment", name, "-o", "yaml"])


@tool
def get_replicasets(service: str) -> str:
    """ReplicaSets of a deployment with creation time and replica counts. A new ReplicaSet just before an
    incident means a rollout (deploy or config change) happened; compare with the incident start."""
    return _k(["get", "rs", "-l", f"{SELECTOR_LABEL}={service}", "--sort-by=.metadata.creationTimestamp",
               "-o", "custom-columns=NAME:.metadata.name,CREATED:.metadata.creationTimestamp,"
                     "DESIRED:.spec.replicas,READY:.status.readyReplicas,REVISION:.metadata.annotations.deployment\\.kubernetes\\.io/revision"])


@tool
def rollout_revision(service: str, revision: int = 0) -> str:
    """Pod template (image + env vars) of one deployment revision. revision=0 lists history only.
    Diff the current and previous revision to see exactly what a rollout changed."""
    args = ["rollout", "history", f"deployment/{service}"] + ([f"--revision={revision}"] if revision else [])
    return _k(args)


@tool
def rollout_diff(service: str) -> str:
    """What the latest rollout changed: diff of the pod template (image, env, resources, probes) between the previous
    and the current revision of a deployment. Call this FIRST for any failing or not-Ready deployment.
    Lines starting with '+' exist only in the current revision. If the suspect setting is on a '+' line, the previous
    revision is the last good one and rolling back to it is the fix. If the diff REMOVES the suspect setting, the previous
    revision is the broken one: do not roll back to it. ReplicaSet creation times are NOT rollout times; trust this diff."""
    hist = _k(["rollout", "history", f"deployment/{service}"])
    revs = sorted({int(l.split()[0]) for l in hist.splitlines() if l.split() and l.split()[0].isdigit()})
    if len(revs) < 2:
        return "Only one revision exists: no rollout has happened, so a rollout did not cause this."
    prev, cur = revs[-2], revs[-1]
    def tpl(r):
        out = _k(["rollout", "history", f"deployment/{service}", f"--revision={r}"], timeout=30)
        return [l.rstrip() for l in out.splitlines() if "pod-template-hash" not in l and "revision #" not in l]
    diff = [l for l in difflib.unified_diff(tpl(prev), tpl(cur), n=0, lineterm="") if not l.startswith(("---", "+++", "@@"))]
    body = "\n".join(diff) if diff else "no differences in the pod template (a plain restart or a scale change)"
    return (f"current revision={cur}, previous revision={prev} (candidate rollback target)\n{_rev_age(service, cur)}"
            f"changes previous -> current:\n{body}")


def _rev_age(service: str, rev: int) -> str:
    """One line: when the revision was created relative to the symptom onset. A change made AFTER the symptoms began cannot have
    started them; one made days before is not a trigger by itself. '' when unknown."""
    rows = _k(["get", "rs", "-l", f"{SELECTOR_LABEL}={service}", "-o",
               "jsonpath={range .items[*]}{.metadata.annotations.deployment\\.kubernetes\\.io/revision} {.metadata.creationTimestamp}{\"\\n\"}{end}"])
    for line in rows.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == str(rev) and (t := _ts(parts[1])) is not None and incident.window_start() is not None:
            return f"current revision was created {_rel(t)} relative to symptom onset (negative = before the symptoms began)\n"
    return ""


# ---------- VERIFY ----------
@tool
def verify_resolved(deployment: str, wait_seconds: int = 120) -> str:
    """Verify a fix. Waits for the rollout to finish, checks all pods are Ready, then requires the SYMPTOM
    DETECTOR (error rate, latency, unavailable replicas, restarts) to be clean on consecutive checks.
    Output starts with RESOLVED or NOT_RESOLVED; only RESOLVED counts as fixed."""
    rollout = _k(["rollout", "status", f"deployment/{deployment}", "--timeout=90s"], timeout=110)
    pods = _k(["get", "pods", "-l", f"{SELECTOR_LABEL}={deployment}", "--no-headers"])
    if "successfully rolled out" not in rollout:       # new pod never became Ready: fail fast and show why
        _state["failed"] += 1
        incident.record_verify(False, deployment)
        logs = _k(["logs", f"deployment/{deployment}", "--tail=8"])
        return (f"NOT_RESOLVED (failed fixes: {_state['failed']}/{MAX_FIX_ATTEMPTS})\nrollout: {rollout}\npods:\n{pods}\n"
                f"last log lines of the pod:\n{logs}")
    ok, problems = wait_until_healthy(timeout=min(max(wait_seconds, 30), 240))
    incident.record_verify(ok, deployment)
    if ok:
        _state["failed"] = 0
        return f"RESOLVED\nrollout: {rollout}\npods:\n{pods}\nremaining symptoms: none"
    _state["failed"] += 1
    return (f"NOT_RESOLVED (failed fixes: {_state['failed']}/{MAX_FIX_ATTEMPTS})\nrollout: {rollout}\npods:\n{pods}\n"
            f"remaining symptoms: {problems}")


# ---------- WRITE (always behind human approval, see agent.py interrupt_on) ----------
def _write(desc: str, args: list[str], detail: str = "") -> str:
    if DRY_RUN:
        return f"[DRY RUN] would run: kubectl -n {NAMESPACE} {' '.join(args)}  # {desc}"
    out = _k(args, timeout=60)
    incident.record_write(desc, args, detail)          # the tool records it, so memory never depends on the model's account
    return out


def _desired_replicas(name: str) -> int | None:
    out = _k(["get", "deployment", name, "-o", "jsonpath={.spec.replicas}"]).strip()
    return int(out) if out.isdigit() else None


def _refuse(name: str, need_symptoms: bool = True) -> str | None:
    """Guards shared by every write: allowed target, attempt cap, and (for disruptive fixes) a live symptom check.
    In-process faults clear when the pod restarts, so by the time we act the service may already be healthy;
    changing a healthy service is how a recovered incident becomes an outage."""
    if err := _writable(name):
        return err
    if _state["failed"] >= MAX_FIX_ATTEMPTS:
        return (f"STOP: {_state['failed']} fixes already failed. Make no more changes; report what you tried and "
                f"escalate to a human.")
    if need_symptoms and not [p for p in find_problems() if not p.startswith("detector error")]:
        return ("NO_ACTION: the detector shows no active symptoms right now, so the service has recovered. Do not "
                "change anything. Report the root cause, say what is still at risk (e.g. memory near its limit, "
                "a leak not fixed) and recommend the durable fix to a human.")
    return None


@tool
def restart_deployment(name: str) -> str:
    """Rolling-restart a deployment. Low risk, but it only hides the cause: restarts clear in-memory state.
    A deployment scaled to 0 has no pods to restart; the call is refused and does not count as a fix attempt."""
    if err := _refuse(name):
        return err
    if _desired_replicas(name) == 0:
        return (f"NO_OP: deployment '{name}' is scaled to 0 replicas, so a restart would create no pod and change nothing. "
                f"If it should be running, bring it back with scale_deployment('{name}', 1).")
    return _write("restart", ["rollout", "restart", f"deployment/{name}"])


@tool
def scale_deployment(name: str, replicas: int) -> str:
    """Scale a deployment to 0..MAX replicas (MAX = limits.max_replicas in system.yaml). Use it to bring back a deployment that was scaled to 0, or to add capacity.
    Do not use 0 then 1 as a restart; use restart_deployment."""
    if err := _refuse(name):
        return err
    if not 0 <= replicas <= MAX_REPLICAS:
        return f"Error: replicas must be between 0 and {MAX_REPLICAS}"
    if _desired_replicas(name) == replicas:
        return (f"NO_OP: deployment '{name}' already has {replicas} desired replica(s), so this changes nothing and is not counted as a fix "
                f"attempt. If its pods are not Ready, look at why (describe_pod, get_events) instead.")
    return _write("scale", ["scale", f"deployment/{name}", f"--replicas={replicas}"], f"replicas={replicas}")


@tool
def set_deployment_env(name: str, key: str, value: str) -> str:
    """Set one env var on a deployment (triggers a rollout). Only with a value you SAW in a previous revision or a ConfigMap.
    Never infer a value from the names of other deployments. If the previous revision is good, rollback instead."""
    return _refuse(name) or _write("set env", ["set", "env", f"deployment/{name}", f"{key}={value}"], f"{key}={value}")


@tool
def set_memory_limit(name: str, limit_mi: int) -> str:
    """Raise a deployment's memory limit to limit_mi MiB (64 up to the configured maximum; request is set to half). Triggers a rollout.
    Fixes an undersized limit. Against a leak it only delays the next OOMKill, so still escalate the leak."""
    if err := _refuse(name, need_symptoms=False):
        return err
    if not 64 <= limit_mi <= MAX_MEMORY_MI:
        return f"Error: limit_mi must be between 64 and {MAX_MEMORY_MI}"
    return _write("memory limit", ["set", "resources", f"deployment/{name}",
                                   f"--limits=memory={limit_mi}Mi", f"--requests=memory={limit_mi // 2}Mi"], f"memory={limit_mi}Mi")


@tool
def rollback_deployment(name: str, to_revision: int) -> str:
    """Roll back to ONE SPECIFIC revision number. Before calling, run rollout_revision on the current revision (highest
    number) and on to_revision and confirm the target is the last GOOD config. Revision numbers are renumbered after
    every undo, and 'previous' may be an old broken config, so never assume it is safe."""
    if err := _refuse(name):
        return err
    hist = _k(["rollout", "history", f"deployment/{name}"])
    revs = {int(l.split()[0]) for l in hist.splitlines() if l.split() and l.split()[0].isdigit()}
    if not revs or to_revision not in revs or to_revision == max(revs):
        return f"Error: to_revision must be an older existing revision (current={max(revs, default='?')}, available={sorted(revs)})"
    return _write("rollback", ["rollout", "undo", f"deployment/{name}", f"--to-revision={to_revision}"], f"to_revision={to_revision}")


K8S_READ_TOOLS = [list_pods, describe_pod, get_events, get_deployment, get_replicasets, rollout_diff, rollout_revision, get_pod_logs]
WRITE_TOOLS = [restart_deployment, scale_deployment, set_deployment_env, set_memory_limit, rollback_deployment]
VERIFY_TOOLS = [verify_resolved]

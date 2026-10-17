"""Localizer = EVIDENCE BOARD. A deterministic function (no LLM, runs once at the start of an incident) that lays out the facts the
metrics already contain, per component, with the pre-incident value next to the current one.

It deliberately does NOT rank suspects and does not say 'deepest failing component', 'victim' or 'cause'. An earlier version did,
and the model adopted the ranking as its first claim; whenever the ranking pointed the wrong way (a quiet component that merely had
an unready replica and a config change in its history) the model spent its budget, and sometimes its fix attempts, on it.
Now it lists observations (alphabetical, so the order carries no weight) and the model has to explain them:

  * own symptoms        5xx and p95 now, and the LOWEST value seen in the 5 minutes before the symptoms began (the healthy level; an earlier
                        incident inside that window, e.g. the previous test run, cannot hide it the way an average would)
  * replicas            desired / not ready, and the highest desired count of the last 30 min (a deployment that was 1 and is now 0)
  * restarts            container restarts inside the incident window only
  * inbound / outbound  anomalous calls into the component and out of it, now vs before
  * latest rollout      listed for EVERY deployment in one neutral line, relative to symptom onset, so a change is not attached to the
                        components that happen to look unhealthy

The call graph is derived from the dependency metrics in system.yaml, never hard-coded. `build()` and `render()` are pure."""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from .clients import prom_query
from .config import NAMESPACE, ERROR_RATE_PCT, P95_SECONDS, METRICS as M
from . import incident

logger = logging.getLogger("sre")
EDGE_ERR_PER_S = 0.05            # a dependency call error rate above this (per second) counts as an anomalous edge
BASE_BEFORE_S = 60               # 'before' = the 5 minutes ending this long before symptom onset (lowest value in that span)
_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="evidence")


def _rows(q: str, *labels: str) -> dict:
    out = {}
    try:
        for s in prom_query(q):
            v = float(s["value"][1])
            if v == v:                                    # drop NaN
                out[tuple(s["metric"].get(l, "?") for l in labels)] = v
    except Exception as e:
        logger.debug("evidence query failed: %s", e)
    return out


def _dep_of(pod: str, deployments: set) -> str:
    parts = pod.rsplit("-", 2)
    return parts[0] if len(parts) == 3 and parts[0] in deployments else pod


def collect(now: float | None = None) -> dict:
    now = now or time.time()
    sl, ns = M["service_label"], f'namespace="{NAMESPACE}"'
    age = incident.age_s(now)
    w = f"{incident.rate_window_s(5, now)}s"                         # the incident so far
    off = None if age is None else f" offset {int(age + BASE_BEFORE_S)}s"   # same query, evaluated before onset
    q: dict[str, tuple] = {}                                          # key -> (promql, labels)

    def pair(key, tmpl, labels):
        fill = lambda win: tmpl.replace("@W@", win).replace("@OFF@", "")        # not str.format: PromQL selectors contain braces
        q[key] = (fill(w), labels)
        if off:                                                                 # healthy level = the minimum 1m-rate over the 5 minutes before onset
            q["b_" + key] = (f"min_over_time(({fill('1m')})[5m:15s]{off})", labels)

    pair("err", f'100 * sum by ({sl}) (rate({M["errors"]}[@W@]@OFF@)) / sum by ({sl}) (rate({M["requests"]}[@W@]@OFF@))', (sl,))
    pair("p95", f'histogram_quantile(0.95, sum by (le, {sl}) (rate({M["duration"]}[@W@]@OFF@)))', (sl,))
    D = M.get("dependency")
    if D:
        a, b = D["source_label"], D["target_label"]
        pair("e_err", f'sum by ({a}, {b}) (rate({D["errors"]}[@W@]@OFF@))', (a, b))
        pair("e_p95", f'histogram_quantile(0.95, sum by (le, {a}, {b}) (rate({D["duration"]}[@W@]@OFF@)))', (a, b))
    for name, metric in (M.get("extra_errors") or {}).items():
        q["x:" + name] = (f'sum by ({sl}) (rate({metric}[{w}]))', (sl,))
    q["desired"] = (f'kube_deployment_spec_replicas{{{ns}}}', ("deployment",))
    q["ready"] = (f'kube_deployment_status_replicas_available{{{ns}}}', ("deployment",))
    q["max_desired"] = (f'max_over_time(kube_deployment_spec_replicas{{{ns}}}[30m])', ("deployment",))
    rw = f"{incident.rate_window_s(10, now) + incident.PREROLL_S}s"
    q["restarts"] = (f'increase(kube_pod_container_status_restarts_total{{{ns}}}[{rw}]) > 0', ("pod",))
    q["rollout"] = (f'max by (owner_name) (kube_replicaset_created{{{ns}}} * on (namespace, replicaset) group_left(owner_name) '
                    f'kube_replicaset_owner{{{ns}, owner_kind="Deployment"}})', ("owner_name",))
    res = dict(zip(q, _pool.map(lambda k: _rows(*q[k]), q)))

    def merge(*keys_fields):
        out: dict = {}
        for key, field in keys_fields:
            for k, v in res.get(key, {}).items():
                out.setdefault(k if len(k) > 1 else k[0], {})[field] = v
        return out

    svc = merge(("err", "err"), ("p95", "p95"), ("b_err", "b_err"), ("b_p95", "b_p95"))
    edges = merge(("e_err", "err"), ("e_p95", "p95"), ("b_e_err", "b_err"), ("b_e_p95", "b_p95"))
    deployments = {k[0] for k in res["desired"]}
    restarts: dict = {}
    for (pod,), v in res["restarts"].items():
        dep = _dep_of(pod, deployments)
        restarts[dep] = restarts.get(dep, 0) + int(v)
    extra: dict = {}
    for k in q:
        if k.startswith("x:"):
            for (s,), v in res[k].items():
                if v > EDGE_ERR_PER_S:
                    extra.setdefault(s, []).append(f"{k[2:]} {v:.2f}/s")
    return {"svc": svc, "edges": edges, "extra": extra, "deployments": deployments,
            "desired": {k[0]: int(v) for k, v in res["desired"].items()}, "ready": {k[0]: int(v) for k, v in res["ready"].items()},
            "max_desired": {k[0]: int(v) for k, v in res["max_desired"].items()}, "restarts": restarts,
            "rollouts": {k[0]: v for k, v in res["rollout"].items()}, "onset": incident.window_start(), "now": now}


def _edge_bad(e: dict) -> bool:
    return e.get("err", 0) > EDGE_ERR_PER_S or e.get("p95", 0) > P95_SECONDS


def _was(cur: str, base) -> str:
    return f"{cur} (before: {base})" if base is not None else cur


def build(d: dict) -> dict:
    """Pure. Returns {'facts': {component: [fact, ...]}, 'quiet': [component, ...]}. Alphabetical, no scoring."""
    svc, edges, desired = d["svc"], d["edges"], d.get("desired", {})
    nodes = set(svc) | set(d.get("deployments", ())) | {n for e in edges for n in e}
    facts: dict[str, list[str]] = {}

    def add(n, text):
        facts.setdefault(n, []).append(text)

    for n, v in svc.items():
        if v.get("err", 0) > ERROR_RATE_PCT:
            add(n, "own 5xx " + _was(f"{v['err']:.0f}%", f"{v['b_err']:.0f}%" if "b_err" in v else None))
        if v.get("p95", 0) > P95_SECONDS:
            add(n, "own p95 " + _was(f"{v['p95']:.2f}s", f"{v['b_p95']:.2f}s" if "b_p95" in v else None))
    for n in sorted(d.get("deployments", ())):
        want, ready, top = desired.get(n), d.get("ready", {}).get(n, 0), d.get("max_desired", {}).get(n)
        if want == 0 and top and top > 0:
            add(n, f"desired replicas 0 now, {top} at some point in the last 30m; {ready} ready")
        elif want and ready < want:
            add(n, f"{want - ready} of {want} replica(s) not ready")
    for n, k in d.get("restarts", {}).items():
        add(n, f"{k} container restart(s) inside the incident window")
    for (a, b), v in sorted(edges.items()):
        if _edge_bad(v):
            parts = [t for t in (f"errors {v['err']:.2f}/s" if v.get("err", 0) > EDGE_ERR_PER_S else "",
                                 f"p95 {v['p95']:.2f}s" if v.get("p95", 0) > P95_SECONDS else "") if t]
            was = f" (before: errors {v.get('b_err', 0):.2f}/s, p95 {v.get('b_p95', 0):.2f}s)" if "b_err" in v or "b_p95" in v else ""
            add(b, f"calls into it from {a}: {', '.join(parts)}{was}")
            add(a, f"calls out to {b}: {', '.join(parts)}")
    for n, items in d.get("extra", {}).items():
        add(n, "error counters: " + ", ".join(items))
    out_edges = {n: [b for (a, b) in edges if a == n] for n in nodes}
    for n in list(facts):
        if out_edges.get(n) and not any(_edge_bad(edges[(n, b)]) for b in out_edges[n]):
            add(n, "calls out: all within thresholds")
    rollouts = []
    if d.get("onset") is not None:                           # every deployment, alphabetical, none singled out
        rollouts = [(n, incident.relative(t)) for n, t in sorted(d.get("rollouts", {}).items()) if t]
    return {"facts": dict(sorted(facts.items())), "quiet": sorted(nodes - set(facts)), "rollouts": rollouts}


def render(b: dict, window: str = "") -> str:
    if not b["facts"]:
        return ""
    L = ["Facts computed from metrics, with no model involved. They are observations, not causes: nothing here is ranked, the order is "
         "alphabetical, and a component can be listed only because something it depends on is wrong."
         + (f" Incident window: {window}." if window else "") + " 'before' = the lowest value in the 5 minutes before the symptoms began."]
    for n, fs in b["facts"].items():
        L.append(f"- {n}: " + "; ".join(fs))
    if b["quiet"]:
        L.append("No anomaly in the metrics: " + ", ".join(b["quiet"]))
    if b.get("rollouts"):
        L.append("Latest rollout of each deployment, relative to symptom onset (negative = before the symptoms began; a restart counts as a rollout): "
                 + ", ".join(f"{n} {t}" for n, t in b["rollouts"]))
    return "\n".join(L)


def localize() -> str:
    """Never raises. Returns '' when nothing is anomalous (healthy system: do not bias the agent)."""
    try:
        return render(build(collect()), incident.describe_window())
    except Exception as e:
        logger.warning("evidence board failed: %s: %s", type(e).__name__, e)
        return ""

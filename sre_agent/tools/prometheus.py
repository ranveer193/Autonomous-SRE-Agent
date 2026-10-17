from langchain_core.tools import tool
from ..clients import prom_query
from ..config import NAMESPACE, METRICS as M
from ..detect import find_problems
from .. import incident

SL = M["service_label"]


def _fmt(res: list[dict], keys: list[str], unit: str = "", digits: int = 3) -> list[str]:
    rows = []
    for s in res:
        v = float(s["value"][1])
        if v != v:
            continue
        rows.append(f"  {' '.join(str(s['metric'].get(k, '?')) for k in keys)}: {v:.{digits}f}{unit}")
    return rows or ["  (no data)"]


@tool
def current_problems() -> str:
    """Run the symptom detector: services over error/latency thresholds, unavailable deployments, recent restarts.
    Symptoms only, never causes. Cheap; call at the start and after any fix."""
    p = find_problems()
    return "\n".join(p) if p else "No active symptoms."


@tool
def system_overview() -> str:
    """Per-service request rate, 5xx %, p95 latency over the incident so far (at most 5 minutes, at least 1). Use to see blast
    radius and which service is the most upstream / deepest failing. The window is shown in the first line."""
    w = f"{incident.rate_window_s(5)}s"
    rps = prom_query(f'sum by ({SL}) (rate({M["requests"]}[{w}]))')
    err = prom_query(f'100 * sum by ({SL}) (rate({M["errors"]}[{w}])) / sum by ({SL}) (rate({M["requests"]}[{w}]))')
    p95 = prom_query(f'histogram_quantile(0.95, sum by (le, {SL}) (rate({M["duration"]}[{w}])))')
    return "\n".join([f"window: last {w}", "req/s:", *_fmt(rps, [SL], digits=2),
                      "5xx %:", *_fmt(err, [SL], "%", 1),
                      "p95 seconds:", *_fmt(p95, [SL], "s")])


@tool
def dependency_health(window_minutes: int = 5) -> str:
    """Calls between components and to datastores: error rate and p95 latency grouped by source->target, plus any
    configured per-service error counters. Key tool to tell 'service is slow itself' from 'its dependency is slow/down'.
    window_minutes is a maximum: the window is the incident so far (at least 1 minute), shown in the first line."""
    D, extra = M.get("dependency"), M.get("extra_errors") or {}
    if not D and not extra:
        return ("Dependency metrics are not configured for this system (metrics.dependency in system.yaml). "
                "Use query_metrics, trace tools or logs to find slow or failing dependencies.")
    w = f"{incident.rate_window_s(max(1, min(window_minutes, 60)))}s"
    out: list[str] = [f"window: last {w}"]
    if D:
        s_, t_ = D["source_label"], D["target_label"]
        err = prom_query(f'sum by ({s_}, {t_}) (rate({D["errors"]}[{w}]))')
        p95 = prom_query(f'histogram_quantile(0.95, sum by (le, {s_}, {t_}) (rate({D["duration"]}[{w}])))')
        out += [f"dependency errors/s ({s_} {t_}):", *_fmt(err, [s_, t_]),
                f"dependency p95 seconds ({s_} {t_}):", *_fmt(p95, [s_, t_], "s")]
    for name, metric in extra.items():
        out += [f"{name}/s by {SL}:", *_fmt(prom_query(f'sum by ({SL}) (rate({metric}[{w}]))'), [SL])]
    return "\n".join(out)


@tool
def pod_resources(service: str) -> str:
    """CPU use vs limit, CPU throttling ratio, memory vs limit, restarts, last termination reason (OOMKilled?)
    and readiness for the pods of one deployment. Use for saturation / OOM / restart questions."""
    pod = f'namespace="{NAMESPACE}", pod=~"{service}-.*"'
    cpu = prom_query(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{pod}, container!=""}}[1m]))')
    lim = prom_query(f'sum by (pod) (kube_pod_container_resource_limits{{{pod}, resource="cpu"}})')
    thr = prom_query(f'sum by (pod) (rate(container_cpu_cfs_throttled_periods_total{{{pod}}}[1m])) '
                     f'/ sum by (pod) (rate(container_cpu_cfs_periods_total{{{pod}}}[1m]))')
    mem = prom_query(f'sum by (pod) (container_memory_working_set_bytes{{{pod}, container!=""}}) / 1024 / 1024')
    mlim = prom_query(f'sum by (pod) (kube_pod_container_resource_limits{{{pod}, resource="memory"}}) / 1024 / 1024')
    rst = prom_query(f'sum by (pod) (kube_pod_container_status_restarts_total{{{pod}}})')
    rst_w = prom_query(f'sum by (pod) (increase(kube_pod_container_status_restarts_total{{{pod}}}[{incident.rate_window_s(10) + incident.PREROLL_S}s]))')
    why = prom_query(f'kube_pod_container_status_last_terminated_reason{{{pod}}} == 1')
    rdy = prom_query(f'kube_pod_status_ready{{{pod}, condition="true"}}')
    return "\n".join(["cpu cores used:", *_fmt(cpu, ["pod"]), "cpu limit cores:", *_fmt(lim, ["pod"]),
                      "cpu throttled fraction (1.0 = always throttled):", *_fmt(thr, ["pod"]),
                      "memory MiB used:", *_fmt(mem, ["pod"], digits=0), "memory limit MiB:", *_fmt(mlim, ["pod"], digits=0),
                      "restarts total (since the pod was created, may predate this incident):", *_fmt(rst, ["pod"], digits=0),
                      "restarts during this incident:", *_fmt(rst_w, ["pod"], digits=0),
                      "last terminated reason:", *([f"  {s['metric'].get('pod')}: {s['metric'].get('reason')}" for s in why] or ["  (none)"]),
                      "ready (1=yes):", *_fmt(rdy, ["pod"], digits=0)])


@tool
def query_metrics(promql: str) -> str:
    """Run any instant PromQL query: application metrics (names are in the system prompt) and kube_* / container_* metrics."""
    try:
        res = prom_query(promql)
    except Exception as e:
        return f"Prometheus error: {e}"
    if not res:
        return f"No results for: {promql}"
    return "\n".join(f"{s['metric']}: {s['value'][1]}" for s in res[:40])


PROM_TOOLS = [current_problems, system_overview, dependency_health, pod_resources, query_metrics]

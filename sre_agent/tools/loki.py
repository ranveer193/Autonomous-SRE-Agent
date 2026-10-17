import json
from langchain_core.tools import tool
from ..clients import loki_range, loki_instant
from ..config import NAMESPACE, LOKI
from .. import incident, features, logcluster, tuning

NS, SVC, LVL = LOKI["namespace_label"], LOKI["service_label"], LOKI["level_label"]
_KEEP = tuple(LOKI["show_fields"])
_GROUP = list(LOKI["group_fields"])


def _body(raw: str) -> str:
    try:
        d = json.loads(raw)
        return " ".join(f"{k}={str(d[k])[:160]}" for k in _KEEP if k in d)
    except Exception:
        return raw[:300]


def _line(ts_ns: str, raw: str) -> str:
    return f"{int(ts_ns) // 1_000_000_000} {_body(raw)}"


CLUSTER_FETCH = 100             # default of `log_fetch` (lines fetched when clustering: more coverage than 25 raw lines, for fewer tokens)


def clustered(rows: list[tuple[int, str, str]], max_clusters: int | None = None) -> str:
    """rows = [(ts_ns, service, raw)] -> pattern lines with counts (see logcluster). '' when empty. max_clusters: this run's `log_patterns`."""
    max_clusters = tuning.active().log_patterns if max_clusters is None else max_clusters
    items = [(t // 1_000_000_000, svc, _body(raw)) for t, svc, raw in rows]
    return logcluster.render(logcluster.cluster(items), len(items), max_clusters=max_clusters, rel=lambda t: incident.relative(t) or str(t))


@tool
def search_logs(service: str, level: str = "", contains: str = "", minutes: int = 10, limit: int = 25) -> str:
    """Search logs in Loki for one service, newest first.
    level: ERROR | WARNING | INFO (optional). contains: plain-text filter, e.g. 'timeout' or 'connection refused'.
    Lines show the configured fields (event, error_type, trace_id ...); use a trace_id with the trace tools.
    `minutes` is a maximum: it is cut to the current incident window, so lines from earlier incidents never appear."""
    sel = f'{{{NS}="{NAMESPACE}", {SVC}="{service}"' + (f', {LVL}="{level.upper()}"' if level else "") + "}"
    q = sel + (f' |= "{contains}"' if contains else "")
    group = features.active().cluster_logs
    n = tuning.active().log_fetch if group else max(1, min(limit, 100))
    try:
        streams = loki_range(q, incident.lookback_minutes(max(1, min(minutes, 120))), n)
    except Exception as e:
        return f"Loki error: {e}"
    if group:
        rows = sorted(((int(t), service, raw) for s in streams for t, raw in s["values"]), reverse=True)[:n]
        return clustered(rows) or f"No logs for {q} in last {minutes}m"
    rows = sorted(((int(t), raw) for s in streams for t, raw in s["values"]), reverse=True)[:limit]
    return "\n".join(_line(str(t), raw) for t, raw in rows) or f"No logs for {q} in last {minutes}m"


def error_summary(minutes: int = 10, top: int = 25) -> str:
    """ERROR/WARNING counts of the incident window grouped by service and the configured fields. Plain function: the tool and the
    pre-flight snapshot share it. Returns 'Loki error: ...' text on failure, never raises."""
    m = incident.lookback_minutes(max(1, min(minutes, 120)))
    by = ", ".join([SVC, *_GROUP])
    parse = ' | json | __error__=""' if LOKI["json_logs"] else ""
    q = f'sum by ({by}) (count_over_time({{{NS}="{NAMESPACE}", {LVL}=~"ERROR|WARNING"}}{parse} [{m}m]))'
    try:
        res = loki_instant(q)
    except Exception as e:
        return f"Loki error: {e}"
    rows = sorted(((float(s["value"][1]), s["metric"]) for s in res), key=lambda x: -x[0])[:top]
    return "\n".join(f"{int(n):>5}  {SVC}={m_.get(SVC)} " + " ".join(f"{k}={m_.get(k, '-')}" for k in _GROUP)
                     for n, m_ in rows) or "No ERROR/WARNING logs in window."


def recent_errors(minutes: int = 10, n: int = CLUSTER_FETCH) -> list[tuple[int, str, str]]:
    """Newest ERROR/WARNING lines of the whole namespace as (ts_ns, service, raw). [] on any failure."""
    q = f'{{{NS}="{NAMESPACE}", {LVL}=~"ERROR|WARNING"}}'
    try:
        streams = loki_range(q, incident.lookback_minutes(max(1, min(minutes, 120))), n)
    except Exception:
        return []
    return sorted(((int(t), s["stream"].get(SVC, "?"), raw) for s in streams for t, raw in s["values"]), reverse=True)[:n]


@tool
def log_error_summary(minutes: int = 10) -> str:
    """Count ERROR/WARNING logs of the last N minutes grouped by service and the configured log fields (e.g. event, error_type).
    Best first log tool: shows which services emit which kinds of errors without reading raw lines.
    The window is cut to the current incident (symptom onset minus 2 min), so earlier incidents are not counted."""
    return error_summary(minutes)


@tool
def logs_for_trace(trace_id: str, minutes: int = 30) -> str:
    """All log lines (every service) that carry one trace_id, oldest first. Follows a failing request end to end."""
    q = f'{{{NS}="{NAMESPACE}"}} |= "{trace_id}"'
    try:
        streams = loki_range(q, incident.lookback_minutes(max(1, min(minutes, 120))), 60)
    except Exception as e:
        return f"Loki error: {e}"
    rows = sorted((int(t), s["stream"].get(SVC, "?"), raw) for s in streams for t, raw in s["values"])
    return "\n".join(f"[{svc}] {_line(str(t), raw)}" for t, svc, raw in rows) or "No logs for that trace_id."


LOKI_TOOLS = [log_error_summary, search_logs, logs_for_trace]

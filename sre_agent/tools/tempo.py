from collections import defaultdict
from langchain_core.tools import tool
from ..clients import tempo_search, tempo_trace
from .. import incident


def _attr(attrs: list[dict], key: str):
    for a in attrs or []:
        if a.get("key") == key:
            v = a.get("value", {})
            return next(iter(v.values()), None)
    return None


def parse_trace(doc: dict) -> list[dict]:
    """Flatten Tempo's OTLP-JSON (v1 'batches' or v2 'trace.resourceSpans') into span dicts."""
    batches = doc.get("batches") or doc.get("trace", {}).get("resourceSpans") or []
    spans = []
    for b in batches:
        svc = _attr(b.get("resource", {}).get("attributes", []), "service.name") or "?"
        for ss in b.get("scopeSpans") or b.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans", []):
                s, e = int(sp["startTimeUnixNano"]), int(sp["endTimeUnixNano"])
                spans.append({"id": sp["spanId"], "parent": sp.get("parentSpanId", ""), "service": svc,
                              "name": sp["name"], "ms": (e - s) / 1e6,
                              "error": (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR"),
                              "db": _attr(sp.get("attributes"), "db.system"),
                              "target": _attr(sp.get("attributes"), "http.url") or _attr(sp.get("attributes"), "net.peer.name")})
    return spans


def breakdown(spans: list[dict], top: int = 10) -> str:
    child_ms = defaultdict(float)
    ids = {s["id"] for s in spans}
    for s in spans:
        if s["parent"] in ids:
            child_ms[s["parent"]] += s["ms"]
    for s in spans:
        s["self_ms"] = max(0.0, s["ms"] - child_ms[s["id"]])
    total = max((s["ms"] for s in spans), default=0)
    lines = [f"spans={len(spans)} total_ms={total:.0f}  (self_ms = time spent in the span itself, excluding children)"]
    for s in sorted(spans, key=lambda x: -x["self_ms"])[:top]:
        extra = f" db={s['db']}" if s["db"] else ""
        extra += " ERROR" if s["error"] else ""
        lines.append(f"  self={s['self_ms']:.0f}ms total={s['ms']:.0f}ms [{s['service']}] {s['name']}{extra}")
    return "\n".join(lines)


@tool
def find_traces(service: str = "", min_duration_ms: int = 0, errors_only: bool = False,
                minutes: int = 10, limit: int = 8) -> str:
    """Find recent traces in Tempo, optionally by service, minimum duration or error status.
    Returns trace IDs to pass to trace_breakdown. `minutes` is a maximum, cut to the current incident window."""
    conds = []
    if service:
        conds.append(f'resource.service.name = "{service}"')
    if min_duration_ms:
        conds.append(f"duration > {int(min_duration_ms)}ms")
    if errors_only:
        conds.append("status = error")
    q = "{ " + " && ".join(conds) + " }" if conds else "{}"
    try:
        traces = tempo_search(q, incident.lookback_minutes(max(1, min(minutes, 60))), max(1, min(limit, 20)))
    except Exception as e:
        return f"Tempo error: {e}"
    return "\n".join(f"{t['traceID']}  root={t.get('rootServiceName')} {t.get('rootTraceName')} "
                     f"{t.get('durationMs', '?')}ms" for t in traces) or f"No traces for {q}"


@tool
def trace_breakdown(trace_id: str) -> str:
    """Where did the time go in one trace? Lists spans by self-time (time inside the span excluding children),
    flagging db spans and errors. If datastore or downstream spans dominate, the dependency is slow, not the service."""
    try:
        spans = parse_trace(tempo_trace(trace_id))
    except Exception as e:
        return f"Tempo error: {e}"
    return breakdown(spans) if spans else "Trace not found or empty."


TEMPO_TOOLS = [find_traces, trace_breakdown]

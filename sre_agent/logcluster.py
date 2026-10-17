"""Log clustering: many near-identical log lines -> one template with a count. Pure functions, no I/O, no model.

Why: 25 raw lines of one repeating error cost ~25x the tokens of one line plus "x412", and say the same thing. What must survive:
  * every DISTINCT message shape stays a separate cluster (an error_type or dependency value is never masked, so 'refused' and
    'timeout' never merge);
  * the count of each cluster, and the sum of counts equals the number of lines given (nothing is dropped silently);
  * when each cluster started and last seen, and ONE real example with its trace_id, so the trace tools still work.
What is masked: ids (anything=...id), uuids, long hex, long numbers, IPs, durations/floats, pod-name suffixes. Short integers
(ports, status codes) stay, so 'host:5432' and 'status_code=503' are kept as written.

Counts are over the lines given, not over the whole window. The caller says so in the output."""
import re
from dataclasses import dataclass, field

_ID_FIELD = re.compile(r"\b(\w*id)=\S+", re.I)                 # trace_id=..., request_id=..., order_id=...
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b[0-9a-f]{12,}\b", re.I)
_IP = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_POD = re.compile(r"\b([a-z][a-z0-9-]*?)-[a-z0-9]{5,10}-[a-z0-9]{5}\b")
_FLOAT = re.compile(r"(?<![\w.])\d+\.\d+")                      # 2.5, 2.5s, 0.31ms (no \b: the unit follows the digit)
_UNIT = re.compile(r"(?<![\w.])\d+(?=(?:ms|us|s|m|h|kb|mb|b)\b)", re.I)   # 250ms, 30s
_MEASURE = re.compile(r"\b(\w*(?:_ms|duration|latency|elapsed|seconds|bytes|size)\w*)=\S+", re.I)   # duration_ms=250
_LONGNUM = re.compile(r"\b\d{6,}\b")
_TRACE = re.compile(r"\btrace_id=(\S+)")
_WS = re.compile(r"\s+")


def template(body: str) -> str:
    """'event=x message=timeout after 2.5s request_id=ab12 ...' -> 'event=x message=timeout after <n>s request_id=<*> ...'"""
    t = _ID_FIELD.sub(lambda m: f"{m.group(1)}=<*>", body)
    t = _MEASURE.sub(lambda m: f"{m.group(1)}=<n>", t)
    t = _UUID.sub("<uuid>", t)
    t = _IP.sub("<ip>", t)
    t = _POD.sub(lambda m: f"{m.group(1)}-<pod>", t)
    t = _HEX.sub("<hex>", t)
    t = _FLOAT.sub("<n>", t)
    t = _UNIT.sub("<n>", t)
    t = _LONGNUM.sub("<n>", t)
    return _WS.sub(" ", t).strip()


@dataclass
class Cluster:
    key: str
    count: int = 0
    first: int = 0              # oldest timestamp seen (epoch seconds)
    last: int = 0               # newest timestamp seen
    example: str = ""           # the newest line, as given
    trace: str = ""             # trace_id of that line, if any
    sources: dict = field(default_factory=dict)


def cluster(rows: list[tuple[int, str, str]]) -> list[Cluster]:
    """rows = [(epoch_seconds, source, body)], any order. Returns clusters, most frequent first (ties: newest first).
    `source` is the service name ('' when not known); it is part of the key, so the same message from two services stays two clusters."""
    out: dict[str, Cluster] = {}
    for ts, src, body in rows:
        k = f"[{src}] {template(body)}" if src else template(body)
        c = out.get(k)
        if c is None:
            c = out[k] = Cluster(key=k, first=ts, last=ts)
        c.count += 1
        c.first, c.last = min(c.first, ts), max(c.last, ts)
        if ts >= c.last:                                  # the newest line is the example
            c.example = body
            m = _TRACE.search(body)
            c.trace = m.group(1) if m else c.trace
    return sorted(out.values(), key=lambda c: (-c.count, -c.last))


def render(clusters: list[Cluster], total: int, *, max_clusters: int = 12, rel=None) -> str:
    """One line per cluster. `rel(ts)` formats a timestamp (e.g. relative to the symptom onset); default = epoch seconds."""
    if not clusters:
        return ""
    f = rel or (lambda t: str(t))
    head = f"{total} log line(s) -> {len(clusters)} pattern(s); counts cover only these {total} lines (the newest ones), not the whole window:"
    L = [head]
    for c in clusters[:max_clusters]:
        span = f"{f(c.first)}..{f(c.last)}" if c.first != c.last else f(c.last)
        tr = f" | e.g. trace_id={c.trace}" if c.trace else ""
        L.append(f"{c.count}x [{span}] {c.key}{tr}")
    rest = clusters[max_clusters:]
    if rest:
        L.append(f"+{len(rest)} more pattern(s), {sum(c.count for c in rest)} line(s): narrow with `contains` or `level` to see them")
    return "\n".join(L)

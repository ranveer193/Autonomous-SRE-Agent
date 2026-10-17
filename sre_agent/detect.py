"""Symptom detector. Used (1) to raise the alert, (2) by verify_resolved as the real 'is it fixed?' check.
Symptoms only: it never says WHY something is broken."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import re
from .clients import prom_query, prom_range
from .config import NAMESPACE, ERROR_RATE_PCT, P95_SECONDS, METRICS as M

SL = M["service_label"]


_HC_TIMEOUT = 5            # a health check must fail fast: the default 15s x 4 serial queries took up to a minute when Prometheus was slow
_TTL = 2.0                 # concurrent / rapid callers (console polling, suite, agent) share one evaluation
_lock = threading.Lock()
_cache: dict = {"t": 0.0, "v": []}
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="health")


def _check(name: str, q: str, fmt) -> list[str]:
    rows = prom_query(q, timeout=_HC_TIMEOUT)
    return [m for m in (fmt(s) for s in rows) if m]


def _evaluate() -> list[str]:
    def err(s):
        v = float(s["value"][1])
        return f"{s['metric'].get(SL)}: HTTP 5xx rate {v:.0f}% (threshold {ERROR_RATE_PCT:.0f}%)" if v == v and v > ERROR_RATE_PCT else None   # v == v filters NaN

    def lat(s):
        v = float(s["value"][1])
        return f"{s['metric'].get(SL)}: p95 latency {v:.2f}s (threshold {P95_SECONDS}s)" if v == v and v > P95_SECONDS else None

    def down(s):
        return f"deployment {s['metric'].get('deployment')}: {int(float(s['value'][1]))} replica(s) unavailable"

    def rst(s):
        return f"pod {s['metric'].get('pod')}: restarted {float(s['value'][1]):.0f}x in last 5m"

    checks = [
        ("errors", f'100 * sum by ({SL}) (rate({M["errors"]}[1m])) / sum by ({SL}) (rate({M["requests"]}[1m]))', err),
        ("latency", f'histogram_quantile(0.95, sum by (le, {SL}) (rate({M["duration"]}[1m])))', lat),
        ("replicas", f'kube_deployment_spec_replicas{{namespace="{NAMESPACE}"}} '
                     f'- kube_deployment_status_replicas_available{{namespace="{NAMESPACE}"}} > 0', down),
        ("restarts", f'increase(kube_pod_container_status_restarts_total{{namespace="{NAMESPACE}"}}[5m]) > 0', rst),
    ]
    futs = [_pool.submit(_check, *c) for c in checks]          # the four queries are independent: run them in parallel
    out: list[str] = []
    for f in futs:
        try:
            out += f.result()
        except Exception as e:                                  # Prometheus unreachable etc.
            msg = f"detector error: {type(e).__name__}: {e}"
            if msg not in out:
                out.append(msg)
    return out


def find_problems() -> list[str]:
    """Single-flight + short TTL cache: callers that arrive while a check is running wait for it and share its result,
    so health checks can never pile up behind a slow Prometheus."""
    with _lock:
        if time.monotonic() - _cache["t"] < _TTL:
            return list(_cache["v"])
        v = _evaluate()
        _cache["t"], _cache["v"] = time.monotonic(), v
        return list(v)


def wait_until_healthy(timeout: int = 120, consecutive: int = 2, interval: int = 5) -> tuple[bool, list[str]]:
    """True only after `consecutive` clean checks in a row (rate windows lag, so one clean sample is not enough).
    Never overruns `timeout`; gives up early when the metrics source itself is unreachable."""
    deadline, clean, errs, last = time.monotonic() + timeout, 0, 0, []
    while True:
        last = find_problems()
        clean = clean + 1 if not last else 0
        errs = errs + 1 if last and all(str(x).startswith("detector error") for x in last) else 0
        if clean >= consecutive:
            return True, []
        if errs >= 3 or time.monotonic() + interval >= deadline:
            return False, last
        time.sleep(interval)


def snapshot_line() -> str:
    """One line for the agent's first message, so it starts from the detector's current view."""
    try:
        live = [p for p in find_problems() if not p.startswith("detector error")]
    except Exception:
        return "unavailable"
    return "; ".join(live) if live else "no active symptoms"


# ---------- incident onset and a magnitude-free description of the symptoms ----------
def contiguous_start(points: list[tuple[float, float]], breaching, step: int = 15, max_gap: int = 2, max_healthy: int = 2) -> float | None:
    """points = [(ts, value)] ascending. Start of the breach that is STILL running at the last point; None when the last
    point is fine. Two kinds of hole are told apart:
      * MISSING samples (NaN = no traffic, a lost scrape): up to `max_gap` in a row are tolerated.
      * HEALTHY samples (a real value under the threshold): `max_healthy` in a row (30s at the default step) END the breach. They used to
        be tolerated like missing ones, so a previous incident that ended only 30s earlier (back-to-back test runs) was merged into
        this one and its logs, events and rollouts leaked into the window. One healthy sample is still forgiven (a noisy rate)."""
    if not points or not breaching(points[-1][1]):
        return None
    start = last_in = points[-1][0]
    healthy = 0
    for t, v in reversed(points[:-1]):
        if v == v and breaching(v):
            if round((last_in - t) / step) - 1 - healthy > max_gap:      # too many MISSING samples between this and the breach after it
                break
            start = last_in = t
            healthy = 0
        elif v == v:                                                     # a real, healthy value
            healthy += 1
            if healthy >= max_healthy:
                break
    return start


def breach_start(lookback_s: int = 1800, step: int = 15, now: float | None = None) -> float | None:
    """Epoch second when the CURRENT symptoms began: the earliest start, over every series that is breaching right now, of its
    unbroken run above threshold (same queries and thresholds as the detector). None when nothing is breaching or Prometheus
    cannot be read. Everything an investigation looks at is limited to the window that starts here."""
    end = now or time.time()
    qs = [(f'100 * sum by ({SL}) (rate({M["errors"]}[1m])) / sum by ({SL}) (rate({M["requests"]}[1m]))', ERROR_RATE_PCT),
          (f'histogram_quantile(0.95, sum by (le, {SL}) (rate({M["duration"]}[1m])))', P95_SECONDS),
          (f'kube_deployment_spec_replicas{{namespace="{NAMESPACE}"}} - kube_deployment_status_replicas_available{{namespace="{NAMESPACE}"}}', 0.0)]
    starts: list[float] = []
    for q, thr in qs:
        try:
            for s in prom_range(q, end - lookback_s, end, step, timeout=_HC_TIMEOUT * 2):
                pts = [(float(t), float(v)) for t, v in s["values"]]
                st = contiguous_start(pts, lambda v, thr=thr: v > thr, step)
                if st is not None and end - pts[-1][0] <= 2 * step:         # still breaching now, not a stale series
                    starts.append(st)
        except Exception:
            continue
    return min(starts) if starts else None


_RX_5XX = re.compile(r"^(?P<n>.+?): HTTP 5xx rate")
_RX_LAT = re.compile(r"^(?P<n>.+?): p95 latency")
_RX_DEP = re.compile(r"^deployment (?P<n>\S+): .*unavailable")
_RX_RST = re.compile(r"^pod (?P<n>\S+): restarted")


def symptom_signature(problems: list[str]) -> str:
    """'a:5xx; a:latency; b:unavailable'. Stable across runs (no percentages, no seconds), so two incidents with the same
    shape give the same signature however loud they were. Used as the memory key instead of the model's own wording."""
    out: set[str] = set()
    for p in problems or []:
        for rx, kind in ((_RX_5XX, "5xx"), (_RX_LAT, "latency"), (_RX_DEP, "unavailable"), (_RX_RST, "restarts")):
            m = rx.match(p)
            if m:
                n = m.group("n")
                out.add(f"{re.sub(r'-[a-z0-9]{5,10}-[a-z0-9]{5}$', '', n) if kind == 'restarts' else n}:{kind}")
                break
    return "; ".join(sorted(out))

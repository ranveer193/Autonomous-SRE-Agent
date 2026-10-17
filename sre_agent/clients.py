"""Thin HTTP clients for Prometheus / Loki / Tempo. All helpers return plain python, never raise."""
import time
import httpx
from .config import PROM_URL, LOKI_URL, TEMPO_URL

_T = 15
_PROM = httpx.Client(timeout=_T, limits=httpx.Limits(max_connections=16, max_keepalive_connections=8))   # shared: keep-alive, thread-safe


def prom_query(q: str, timeout: float = _T) -> list[dict]:
    r = _PROM.get(f"{PROM_URL}/api/v1/query", params={"query": q}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(data.get("error", "prometheus error"))
    return data["data"]["result"]


def prom_range(q: str, start: float, end: float, step: int = 15, timeout: float = _T) -> list[dict]:
    """Range query: [{'metric': {...}, 'values': [[ts, 'v'], ...]}]."""
    r = _PROM.get(f"{PROM_URL}/api/v1/query_range", params={"query": q, "start": start, "end": end, "step": step}, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "success":
        raise RuntimeError(data.get("error", "prometheus error"))
    return data["data"]["result"]


def loki_range(logql: str, minutes: int, limit: int) -> list[dict]:
    end = time.time_ns()
    start = end - minutes * 60 * 1_000_000_000
    r = httpx.get(f"{LOKI_URL}/loki/api/v1/query_range", timeout=_T,
                  params={"query": logql, "start": start, "end": end, "limit": limit, "direction": "backward"})
    r.raise_for_status()
    return r.json()["data"]["result"]


def loki_instant(logql: str) -> list[dict]:
    r = httpx.get(f"{LOKI_URL}/loki/api/v1/query", params={"query": logql}, timeout=_T)
    r.raise_for_status()
    return r.json()["data"]["result"]


def tempo_search(traceql: str, minutes: int, limit: int) -> list[dict]:
    end = int(time.time())
    r = httpx.get(f"{TEMPO_URL}/api/search", timeout=_T,
                  params={"q": traceql, "limit": limit, "start": end - minutes * 60, "end": end})
    r.raise_for_status()
    return r.json().get("traces", [])


def tempo_trace(trace_id: str) -> dict:
    r = httpx.get(f"{TEMPO_URL}/api/traces/{trace_id}", timeout=_T, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()

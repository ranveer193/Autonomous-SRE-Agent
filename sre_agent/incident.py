"""State of the incident being handled, and nothing else. One place answers three questions that used to be answered by
whatever the tools happened to return:

  WHEN did this incident start?   -> every read tool looks only at [onset - pre-roll, now], so logs, events, traces and rates of an
                                     EARLIER incident (a previous run, a previous fault, its fix) are not mixed into this one.
  WHAT did we change, and did it work?  -> every write and every verify_resolved result is recorded here by the tools themselves,
                                     so memory stores what actually happened, not what the model says happened.
  WHICH symptoms opened it?       -> a magnitude-free signature taken from the detector, the memory key.

Process-wide (one incident at a time, like the rest of the agent). `begin()` resets everything."""
import math
import os
import threading
import time

from .detect import breach_start, symptom_signature

PREROLL_S = 120                     # look this far before onset: enough to see the change that started it, too short to reach the last incident
_lock = threading.Lock()
_S: dict = {}


def _fresh() -> dict:
    return {"t_open": None, "window_start": None, "snapshot": "", "signature": "", "writes": [], "verified": None, "saved": [], "recalled": False}


_S.update(_fresh())


def enabled() -> bool:
    return os.getenv("SRE_INCIDENT_WINDOW", "1").strip().lower() not in ("0", "false", "no", "off")


def begin(problems: list[str] | None = None, now: float | None = None, onset=breach_start, onset_ts: float | None = None) -> None:
    """Start tracking a new incident. `problems` = the detector's live symptoms. Never raises.
    `onset_ts` = when the ALERT fired (an alert rule's startsAt, or the harness's detection time). It is the most reliable window start:
    it needs no guess about where the previous incident ended. Without it the onset is estimated from the metrics (`onset`)."""
    now = now or time.time()
    if onset_ts is not None:
        start = float(onset_ts)
    else:
        try:
            start = onset(now=now)
        except Exception:
            start = None
    with _lock:
        _S.clear()
        _S.update(_fresh())
        _S.update(t_open=now, window_start=start, snapshot="; ".join(problems or []), signature=symptom_signature(problems or []))


# ---------- the window ----------
def window_start() -> float | None:
    return _S.get("window_start") if enabled() else None


def age_s(now: float | None = None) -> float | None:
    ws = window_start()
    return None if ws is None else max(0.0, (now or time.time()) - ws)


def since_epoch(preroll_s: int = PREROLL_S) -> float | None:
    """Earliest timestamp a tool should show, or None when the onset is unknown (then tools keep their own defaults)."""
    ws = window_start()
    return None if ws is None else ws - preroll_s


def lookback_minutes(requested: int, preroll_s: int = PREROLL_S, now: float | None = None) -> int:
    """Clamp a tool's look-back (minutes) so it never reaches before the incident window."""
    since = since_epoch(preroll_s)
    if since is None:
        return requested
    return max(1, min(requested, math.ceil(((now or time.time()) - since) / 60)))


def rate_window_s(requested_minutes: int, now: float | None = None) -> int:
    """Seconds for a PromQL range such as rate(x[<n>s]): the incident so far, at least 60s (two scrapes), at most what was asked."""
    age = age_s(now)
    cap = requested_minutes * 60
    return cap if age is None else int(min(cap, max(60, age)))


def relative(t: float) -> str:
    """'+35s' / '-4h12m': a timestamp relative to the symptom onset (negative = before the symptoms began). '' when onset unknown."""
    ws = window_start()
    if ws is None:
        return ""
    d = int(t - ws)
    a = abs(d)
    txt = f"{a}s" if a < 120 else (f"{a // 60}m" if a < 7200 else f"{a // 3600}h{a % 3600 // 60:02d}m")
    return f"{'+' if d >= 0 else '-'}{txt}"


def describe_window(now: float | None = None) -> str:
    age = age_s(now)
    return "unknown" if age is None else f"symptoms began {int(age)}s ago; tools show data from {PREROLL_S}s before that"


# ---------- what was done ----------
def _target(args: list[str]) -> str:
    for a in args:
        if a.startswith("deployment/"):
            return a.split("/", 1)[1]
    return ""


def record_write(tool: str, args: list[str], detail: str = "") -> None:
    with _lock:
        _S["writes"].append({"tool": tool, "target": _target(args), "detail": detail, "t": time.time(), "effective": None})


def record_verify(ok: bool, deployment: str) -> None:
    """verify_resolved just answered, for the changes made since the previous verify.
    NOT_RESOLVED: none of them helped. RESOLVED: only the LAST one is credited. The earlier ones are 'unclear' (neither credited nor
    blamed): with several changes before one verify we cannot tell which one fixed it, and crediting all of them is how a wasted first
    action (e.g. a restart before the real fix) ends up stored as part of 'the fix'."""
    with _lock:
        pending = [w for w in _S["writes"] if w["effective"] is None]
        for w in pending:
            w["effective"] = False if not ok else "unclear"
        if ok and pending:
            pending[-1]["effective"] = True
        if ok:
            _S["verified"] = {"deployment": deployment, "t": time.time()}


def fix_summary(effective: bool) -> str:
    """'scale deployment/x replicas=1' for the changes that were followed by RESOLVED (effective=True) or by NOT_RESOLVED (False)."""
    ws = [w for w in _S["writes"] if w["effective"] is effective]
    return "; ".join(f"{w['tool']} {w['target']}{(' ' + w['detail']) if w['detail'] else ''}".strip() for w in ws)


def effective_target() -> str:
    """Deployment the LAST effective change acted on ('' if none): the component that, when changed, fixed it."""
    ws = [w for w in _S["writes"] if w["effective"] is True]
    return ws[-1]["target"] if ws else ""


def has_verified_fix() -> bool:
    return bool(_S["verified"]) and any(w["effective"] is True for w in _S["writes"])


def has_failed_attempt() -> bool:
    return any(w["effective"] is False for w in _S["writes"])


def ttr_s() -> int | None:
    """Seconds from the incident opening to the verified fix, measured here (not reported by the model)."""
    v, t0 = _S.get("verified"), _S.get("t_open")
    return int(v["t"] - t0) if v and t0 else None


def signature() -> str:
    return _S.get("signature", "")


def snapshot() -> str:
    return _S.get("snapshot", "")


def mark_recalled() -> None:
    """search_incidents returned at least one past incident to the agent."""
    with _lock:
        _S["recalled"] = True


def recalled() -> bool:
    return bool(_S.get("recalled"))


def add_saved(i: str) -> None:
    with _lock:
        if i not in _S["saved"]:
            _S["saved"].append(i)


def saved_ids() -> list[str]:
    return list(_S["saved"])

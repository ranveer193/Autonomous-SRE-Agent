"""Pre-flight: the routine first look, done by plain code before the model starts.

Without it the model spends its first rounds on the same checks every time (what is failing, what do the logs say, what did the cluster
report), and EVERY one of those rounds re-sends the whole context and all tool definitions. With it the model starts from one compact
block and spends its calls on the part that needs thinking.

What is covered where (nothing is collected twice):
  metrics overview, dependency health, replicas, restarts, latest rollouts -> the LOCALIZER block (feature `localizer`)
  log patterns (message templates with counts, whole namespace)            -> here
  warning events of the incident window                                    -> here

Same rules as the localizer: observations only, no ranking, no cause, alphabetical/by count, bounded size. Never raises; returns ''
when the system is healthy (a healthy system must not be biased) or when nothing could be read."""
import logging

from . import incident, tuning
from .tools import k8s, loki

logger = logging.getLogger("sre")
MAX_EVENTS = 8
MAX_CHARS = tuning.Tuning().preflight_chars   # default of `preflight_chars`: hard cap on the whole block (~800 tokens)


def _warning_events() -> str:
    fmt = ("jsonpath={range .items[*]}{.lastTimestamp}|{.eventTime}|{.metadata.creationTimestamp}|{.type}|"
           '{.reason}|{.involvedObject.name}|{.message}{"\\n"}{end}')
    raw = k8s._kraw(["get", "events", "-o", fmt])
    rows = [r for r in k8s.events_since(raw, incident.since_epoch(), limit=200)]
    warn = [r for r in rows if " Warning " in r]
    if not warn:
        return f"no Warning events in the window ({len(rows)} other event(s) not shown)" if rows else ""
    shown = warn[-MAX_EVENTS:]
    more = f" (+{len(warn) - len(shown)} older Warning events not shown)" if len(warn) > len(shown) else ""
    return "\n".join(shown) + more


def collect() -> str:
    parts = []
    try:
        pats = loki.clustered(loki.recent_errors(), max_clusters=8)
        if pats:
            parts.append("Log patterns (ERROR/WARNING, all services):\n" + pats)
    except Exception as e:
        logger.debug("pre-flight logs failed: %s", e)
    try:
        ev = _warning_events()
        if ev:
            parts.append("Kubernetes Warning events in the window (time relative to symptom onset):\n" + ev)
    except Exception as e:
        logger.debug("pre-flight events failed: %s", e)
    return "\n\n".join(parts)


def preflight() -> str:
    """The block for the first message, or ''. Healthy (no symptoms at the start of the incident) -> ''."""
    if not incident.snapshot():
        return ""
    try:
        out = collect()
    except Exception as e:
        logger.warning("pre-flight failed: %s: %s", type(e).__name__, e)
        return ""
    cap = tuning.active().preflight_chars
    return out if len(out) <= cap else out[:cap] + "\n...[cut]"

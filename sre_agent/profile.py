"""Everything specific to the target system lives in ONE file: system.yaml (override the path with SRE_SYSTEM_FILE).
Code, prompts and skills stay generic. Without the file the agent gets safe generic defaults: nothing is writable,
the topology is unknown (the agent must derive it), and standard metric names are assumed."""
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

DEFAULTS: dict = {
    "namespace": "default",
    "services": [],                 # application services (informational)
    "writable": [],                 # deployments the remediator may change. EMPTY = the agent can never change anything
    "topology": "",                 # free text call graph, e.g. "a -> b -> c"
    "limits": {"max_replicas": 20, "max_memory_mi": 512},   # ceilings the write tools enforce
    "selector_label": "app",        # label that ties a Deployment's pods/ReplicaSets to its name
    "thresholds": {"error_rate_pct": 5.0, "p95_seconds": 1.0},
    "metrics": {                    # PromQL selectors; they are inserted as rate(<selector>[window])
        "service_label": "service",
        "requests": "http_requests_total",
        "errors": 'http_requests_total{status_code=~"5.."}',
        "duration": "http_request_duration_seconds_bucket",
        "dependency": None,         # optional {errors, duration, source_label, target_label}
        "extra_errors": {},         # optional {display_name: counter}, grouped by the service label
    },
    "loki": {
        "namespace_label": "namespace", "service_label": "service", "level_label": "level",
        "json_logs": False,         # True: parse lines with `| json` for group_fields
        "group_fields": [],         # log fields used to group the error summary
        "show_fields": ["level", "message"],
    },
}


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load(path: str | Path | None = None) -> dict:
    p = Path(path or os.getenv("SRE_SYSTEM_FILE") or ROOT / "system.yaml")
    if not p.exists():
        return _merge(DEFAULTS, {})
    return _merge(DEFAULTS, yaml.safe_load(p.read_text(encoding="utf-8")) or {})

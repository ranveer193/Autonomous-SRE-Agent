"""Effect-based scoring helpers for the eval harness. Pure functions: no cluster, no LLM, no third-party imports.

The judge LLM used to see only the agent's narrative and the list of tool calls, so a report that said "raised the CPU limit" passed even when
the only call was a memory-limit change. Here the HARNESS reads the cluster before and after the agent ran, turns the difference into
EFFECTS, and scores the run from those effects. The judge only decides whether the report names the right cause and whether its claims match
the effects.

  snapshot_from_json(raw)  -> {deployment: state}        parse `kubectl get deploy -o json`
  diff(before, after)      -> [effect, ...]               what the agent's changes actually did
  score(truth, verdict, actions, effects) -> dict         final, code-computed verdict (see below)
  case_status(scored, recovered, rc, blamed) -> PASS|PARTIAL|FAIL

Ground-truth keys used (all optional):
  expected_outcome   fix (default) | escalate   escalate = no agent tool can fix it: any write is unnecessary
  forbidden          [{tool: restart_deployment, target: payment-service}]   checked against the logged writes by code
"""
import json
import re

RESTARTED_AT = "kubectl.kubernetes.io/restartedAt"
REVISION = "deployment.kubernetes.io/revision"

_UNITS = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "m": 1e-3, "": 1}


def qty(s) -> float | None:
    """Kubernetes quantity -> number ('500m' -> 0.5, '256Mi' -> 268435456). None when unparseable or missing."""
    if s is None:
        return None
    m = re.fullmatch(r"([0-9.]+)([A-Za-z]*)", str(s).strip())
    if not m or m.group(2) not in _UNITS:
        return None
    return float(m.group(1)) * _UNITS[m.group(2)]


def snapshot_from_json(raw: str) -> dict:
    """`kubectl get deploy -o json` -> {name: {replicas, restarted_at, revision, resources, env}}. Empty on bad input."""
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    items = data.get("items") if data.get("kind") == "List" or "items" in data else [data]
    out = {}
    for d in items or []:
        meta, spec = d.get("metadata", {}), d.get("spec", {})
        tpl = spec.get("template", {})
        res, env = {}, {}
        for c in tpl.get("spec", {}).get("containers", []):
            r = c.get("resources") or {}
            for kind in ("requests", "limits"):
                for k, v in (r.get(kind) or {}).items():
                    res[f"{c['name']}.{kind}.{k}"] = v
            for e in c.get("env") or []:
                env[f"{c['name']}.{e['name']}"] = e.get("value", f"<from {list((e.get('valueFrom') or {}).keys())}>")
        out[meta.get("name")] = {
            "replicas": spec.get("replicas"),
            "restarted_at": (tpl.get("metadata", {}).get("annotations") or {}).get(RESTARTED_AT),
            "revision": (meta.get("annotations") or {}).get(REVISION),
            "resources": res, "env": env}
    return out


def diff(before: dict, after: dict) -> list[dict]:
    """Effects of whatever happened between the two snapshots, per deployment. Order: restart, scale, resources, env, rollout."""
    effects = []
    for name in sorted(set(before) | set(after)):
        b, a = before.get(name), after.get(name)
        if b is None or a is None:
            effects.append({"target": name, "kind": "deployment_" + ("removed" if a is None else "added")})
            continue
        n0 = len(effects)
        if a["restarted_at"] != b["restarted_at"]:
            effects.append({"target": name, "kind": "restart"})
        if a["replicas"] != b["replicas"]:
            effects.append({"target": name, "kind": "scale", "from": b["replicas"], "to": a["replicas"],
                            "direction": "up" if (a["replicas"] or 0) > (b["replicas"] or 0) else "down"})
        for key in sorted(set(b["resources"]) | set(a["resources"])):
            x, y = b["resources"].get(key), a["resources"].get(key)
            if x != y:
                qx, qy = qty(x), qty(y)
                direction = "up" if (qx is None and qy is not None) or (qx is not None and qy is not None and qy > qx) else \
                            "down" if (qy is None and qx is not None) or (qx is not None and qy is not None and qy < qx) else "changed"
                effects.append({"target": name, "kind": "resources", "field": key, "from": x, "to": y, "direction": direction})
        for key in sorted(set(b["env"]) | set(a["env"])):
            if b["env"].get(key) != a["env"].get(key):
                effects.append({"target": name, "kind": "env", "field": key, "from": b["env"].get(key), "to": a["env"].get(key)})
        if len(effects) == n0 and a["revision"] != b["revision"]:
            effects.append({"target": name, "kind": "rollout"})          # a new revision nothing above explains (e.g. a rollback to an identical spec)
    return effects


def describe(effects: list[dict]) -> str:
    """One line per effect, for the judge prompt and the report."""
    if not effects:
        return "none: the cluster is unchanged"
    lines = []
    for e in effects:
        k = e["kind"]
        if k in ("resources", "env"):
            lines.append(f"{e['target']}: {k} {e['field']} {e['from']!r} -> {e['to']!r}" + (f" ({e['direction']})" if "direction" in e else ""))
        elif k == "scale":
            lines.append(f"{e['target']}: replicas {e['from']} -> {e['to']}")
        else:
            lines.append(f"{e['target']}: {k}")
    return "\n".join(lines)


def writes_of(actions: list[dict]) -> list[dict]:
    return [a for a in actions or [] if a.get("approved", True)]


def forbidden_hits(truth: dict, actions: list[dict]) -> list[str]:
    hits = []
    for f in truth.get("forbidden") or []:
        if not isinstance(f, dict):
            continue
        for a in writes_of(actions):
            if a.get("tool") == f.get("tool") and (not f.get("target") or (a.get("args") or {}).get("name") == f["target"]):
                hits.append(f"{a['tool']}({(a.get('args') or {}).get('name', '')})")
    return hits


def score(truth: dict, verdict: dict, actions: list[dict], effects: list[dict]) -> dict:
    """Final, code-computed assessment. `remediation_final` replaces the judge's own remediation_appropriate in the run status.

    It is False when: the judge says so; the judge found a claim the effects do not support; a forbidden write was made; the scenario expects
    an escalation and any write was made; or the scenario expects a fix and the only effect was a restart (unconfirmed: it hides the cause).
    """
    expected = (truth or {}).get("expected_outcome", "fix")
    kinds = {e["kind"] for e in effects}
    restart_only = bool(effects) and kinds <= {"restart"}
    writes = writes_of(actions)
    hits = forbidden_hits(truth or {}, actions)
    reasons = []
    if verdict.get("remediation_appropriate") is False:
        reasons.append("judge: remediation not appropriate")
    if verdict.get("claims_unsupported"):
        reasons.append("report claims a change the cluster does not show")
    if hits:
        reasons.append("forbidden write: " + ", ".join(hits))
    if expected == "escalate" and writes:
        reasons.append(f"{len(writes)} write(s) on a scenario that should end in an escalation")
    if expected == "fix" and restart_only:
        reasons.append("restart-only: unconfirmed, a restart hides the cause")
    return {"expected_outcome": expected, "writes": len(writes), "effects": effects, "restart_only": restart_only,
            "forbidden_hits": hits, "remediation_final": bool(verdict.get("remediation_appropriate")) and not reasons,
            "reasons": reasons}


def case_status(scored: dict, recovered: bool, root_cause_correct, blamed_distractor) -> str:
    ok = bool(root_cause_correct) and scored["remediation_final"] and not blamed_distractor
    if scored["expected_outcome"] == "escalate":              # nothing to recover: success is a right diagnosis, no write, an escalation
        return "PASS" if ok else ("PARTIAL" if root_cause_correct and not blamed_distractor else "FAIL")
    return "PASS" if recovered and ok else ("PARTIAL" if recovered else "FAIL")

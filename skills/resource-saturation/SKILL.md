---
name: resource-saturation
description: Runbook for CPU throttling (latency without errors) and memory exhaustion (OOMKilled, repeated restarts) in a service's own pods.
---
# Resource saturation

## Inspect
1. `pod_resources(<service>)`:
   - throttled fraction high + cpu near limit -> CPU saturation.
   - memory climbing toward limit, restarts rising, last terminated reason OOMKilled -> memory leak / undersized limit.
2. `describe_pod`: Last State Terminated, Reason OOMKilled, Exit Code 137.
3. `dependency_health`: downstream p95 normal means the time is spent inside the service.
4. `find_traces` + `trace_breakdown`: large self-time in the service's own spans, short datastore spans.

## Diagnose the OOM before fixing
- `query_metrics('deriv(container_memory_working_set_bytes{namespace="<ns>",pod=~"<service>-.*"}[5m])')`:
  steady positive slope = leak; flat at a high value = undersized limit.
- `get_replicasets` / `rollout_revision`: if nothing changed just before the incident, it is NOT a bad rollout.
- `current_problems` again: an OOMKill restarts the pod, so the service may already be healthy. If no symptoms, change nothing.

## Fix options (least invasive first)
- CPU throttling -> `scale_deployment(<service>, +1)`.
- OOM and service currently failing -> `restart_deployment` to restore service (temporary; the leak returns).
- OOM, memory flat near limit or repeated OOMs -> `set_memory_limit(<service>, N)` with N about 2x the observed working set, within the tool's allowed range.
- Leak (rising slope): a limit only delays it. Say so, raise severity, and escalate the code fix to a human.

## Verify
`verify_resolved`; then `pod_resources` shows no new restarts. Max 2 failed fixes, then stop and escalate.

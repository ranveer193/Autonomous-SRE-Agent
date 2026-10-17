---
name: slow-dependency
description: Runbook for high latency where the real cause is a slow dependency (database, cache, downstream service), not the service that looks slow. Use when p95 is high but errors are low.
---
# Slow dependency

## Inspect
1. `system_overview` + `dependency_health`: compare the service's p95 with its downstream p95 per target.
   If the downstream target's p95 ~ the service's p95, the dependency explains the latency.
2. `pod_resources(<service>)`: CPU/memory normal and no throttling means the service is healthy.
3. `find_traces(service, min_duration_ms=1000)` then `trace_breakdown`: which span has the largest self-time? A datastore or downstream span
   dominating the trace confirms the dependency.
4. `get_replicasets`: no recent rollout of the service.

## Decision
- Dependency dominates, service healthy -> root cause is the dependency's performance. A restart of the service
  is NOT a fix (it can clear in-memory state and mask the cause). The available tools cannot tune the database:
  report evidence, name the component, and ESCALATE to a human (the dependency's owner). Do not claim resolved on a restart.
- Service self-time dominates and CPU is throttled -> see `resource-saturation`.

## Verify
Downstream p95 back under threshold in `dependency_health`; `current_problems` clean.

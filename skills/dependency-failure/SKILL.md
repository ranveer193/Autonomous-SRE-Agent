---
name: dependency-failure
description: Runbook for a datastore or other dependency being down/unreachable (connection refused, dependency errors, pods NotReady). Use when dependency error metrics spike.
---
# Dependency outage

## Inspect
1. `dependency_health`: which target has errors? Check the other configured error counters too.
2. `log_error_summary`: look for errors that name a dependency (a `dependency` field, if logged) and connection-refused style messages.
3. `list_pods` / `get_deployment`: does the dependency deployment have 0 ready pods or 0 replicas?
4. `get_events`: scale-down or crash events for the dependency.
5. Check what still works: reads that fall back to another source, reads vs writes, degraded-mode warnings.

## Fix options
- Dependency deployment at 0 replicas or not ready -> `scale_deployment(<dependency>, 1)`.
- Dependency pod crash-looping -> inspect with `describe_pod`, escalate if data volume / credentials are involved.
- Do NOT restart the callers (the services that depend on it); they recover on their own once the dependency is back.

## Verify
`verify_resolved(<dependency>)`; confirm `dependency_health` shows the target error rate back to 0.

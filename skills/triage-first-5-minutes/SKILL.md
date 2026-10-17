---
name: triage-first-5-minutes
description: Read first for any alert. Gives the order of checks, how to find the deepest failing component, and how to avoid blaming the loudest service.
---
# Triage: first 5 minutes

Topology: see the system prompt; if none is given, derive it from `dependency_health` and traces.
Errors and latency propagate UP the call chain. The loudest service is usually a victim.

## Steps
1. `current_problems` -> which symptoms (5xx, latency, unavailable replicas, restarts)? If none: stop, the system is healthy; do not hunt for other issues.
2. `system_overview` -> which services are affected? Order them by depth in the call chain (the entry point is shallowest).
3. `dependency_health` -> for the DEEPEST affected service, is a downstream target erroring or slow?
   - target errors/slow while the service itself is fine -> root cause is the target (or the path to it).
   - no downstream problem -> suspect the service itself.
4. Pick the matching runbook:
   - dependency errors / connection refused -> `dependency-failure`
   - dependency latency high, errors low -> `slow-dependency`
   - pods NotReady or a recent rollout -> `bad-rollout` (run `rollout_diff` on the deployment first)
   - high latency without errors, restarts, OOM -> `resource-saturation`
   - one service returns 5xx / 503, others healthy -> `service-errors-outage`
5. Confirm with a SECOND signal type (metrics + logs, or traces + k8s) before acting.

## Traps
- A passing liveness check does not mean the service can serve: readiness is what removes pods from traffic.
- Client errors (4xx) are usually business noise, not an incident, unless their rate changed.
- A service whose own CPU/memory is normal and whose dependency is slow is NOT the culprit.

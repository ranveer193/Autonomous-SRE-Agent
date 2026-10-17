---
name: service-errors-outage
description: Runbook for a single service returning many 5xx or 503 while its dependencies and other services look healthy.
---
# Single service errors / outage

## Inspect
1. `system_overview`: only one service (and its callers) affected? Are other branches fine?
2. `search_logs(<service>, level="ERROR")` and `log_error_summary`: error_type, status_code, route (whichever fields the logs have).
   Many 500 with no dependency named -> the service itself. 503 on all routes -> unavailable/not ready.
3. `list_pods` / `describe_pod`: Ready state, probe failures. Remember liveness can pass while readiness fails (a pod can be alive yet not serving).
4. `get_replicasets`: any rollout just before? If yes, use `bad-rollout`.
5. `dependency_health`: confirm no dependency errors for this service.

## Fix options
- Fault confined to a running pod with no rollout and no dependency cause -> `restart_deployment(<service>)`.
  Note this in the report: cause of the in-process failure remains unknown, recommend code/config review.
- Recent rollout -> `rollback_deployment`.

## Verify
`verify_resolved(<service>)`; check the error rate returns under threshold and callers recover.

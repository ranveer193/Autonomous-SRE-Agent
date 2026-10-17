---
name: bad-rollout
description: Runbook for outages caused by a recent deployment or config change (new pods Running but NotReady, 0 ready replicas, errors that start right after a rollout).
---
# Bad rollout / config change

## Inspect
1. `list_pods`: pods Running but 0/1 Ready? old pod already gone?
2. `rollout_diff(<service>)` FIRST: shows exactly what the latest rollout changed. ReplicaSet creation times are not rollout times; do not use them.
3. `rollout_revision(<service>, N)` only if you need the full template of one revision.
4. `log_error_summary`, then `search_logs(<service>, level="ERROR")`: look for startup, connection or name-resolution errors
   that name a value (host, port, URL, credential) that the rollout changed.
5. Check the dependency named in the logs is itself healthy (`dependency_health` from OTHER services). If other
   services reach it fine, the dependency is not the problem; the config is.

## Fix
- `rollout_diff` shows the suspect change added in the current revision -> `rollback_deployment(<service>, to_revision=<previous>)`.
  This beats guessing a corrected value.
  Diff the current revision (highest number) against N first. Revision numbers are renumbered after every undo, so the
  "previous" revision can be an old broken config. Prefer `set_deployment_env` with the evidenced good value if N is not clearly good.
- Only use `set_deployment_env` when the exact known-good value is evidenced (previous revision's env).

## Verify
`verify_resolved(<service>)`: rollout complete, pods Ready, detector clean.

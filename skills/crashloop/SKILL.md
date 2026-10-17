---
name: crashloop
description: Runbook for pods in CrashLoopBackOff, Error, or repeated restarts where the cause is not obviously a recent rollout.
---
# CrashLoopBackOff / restarts

## Inspect
1. `list_pods`: failing pods and restart counts. `pod_resources(<service>)`: last terminated reason.
2. `describe_pod`: Exit Code (137 = OOMKilled, 1 = app error), probe failures, events.
3. `get_pod_logs(pod, previous=true)`: the error just before the crash.
4. `get_replicasets` + `rollout_revision`: did it start right after a rollout? Diff env.

## Fix
- After a rollout (revision diff proves it) -> `rollback_deployment(<service>, to_revision=N)`. OOMKilled -> see `resource-saturation`. Missing env var -> `set_deployment_env` with evidenced value.
- Max 2 fix attempts, then escalate.

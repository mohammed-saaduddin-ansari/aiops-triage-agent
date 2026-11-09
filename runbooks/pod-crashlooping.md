---
title: Pod crash looping
alerts: [KubePodCrashLooping, KubeContainerWaiting]
tags: [crashloop, backoff, restart, startup]
---
## What it means
A container keeps exiting and Kubernetes is backing off restarts.

## Likely causes
1. The process exits on startup because configuration is missing or invalid
   (look for `KeyError`, "environment variable ... not set").
2. A new image crashes (panic, segfault) - check whether a rollout just happened.
3. The container is OOM-killed (see the OOM runbook; last state reason `OOMKilled`).
4. Liveness probe misconfigured and killing a healthy process.

## Checks
```promql
sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="{{namespace}}", pod=~"{{workload}}-.*"}[15m]))
```
- `k8s_describe_pod` for the last termination reason and exit code.
- Events for `BackOff`, `Failed`, `CreateContainerConfigError`.

## Safe actions (need approval)
- Restore the missing ConfigMap/Secret key, then let the pod restart.
- Roll back if a new revision is crashing.

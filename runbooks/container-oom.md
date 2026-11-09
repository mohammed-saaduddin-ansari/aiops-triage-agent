---
title: Container memory exhaustion
alerts: [ContainerMemoryNearLimit, KubeContainerOOMKilled]
tags: [memory, oom, leak, limits]
---
## What it means
A container is close to, or has hit, its memory limit and is being OOM-killed.

## Likely causes
1. A memory leak: working set grows steadily until the kill, then resets.
2. A traffic or payload change raised legitimate memory needs.
3. Limit set too low after a resource tuning change.

## Checks
```promql
max by (pod) (container_memory_working_set_bytes{namespace="{{namespace}}", pod=~"{{workload}}-.*", container!=""}) / max by (pod) (kube_pod_container_resource_limits{namespace="{{namespace}}", pod=~"{{workload}}-.*", resource="memory"})
```
- Last state `OOMKilled` (exit code 137) in `k8s_describe_pod`.
- Events `OOMKilling` on the node.

## Safe actions (need approval)
- Raise the memory limit temporarily; open a leak investigation with a heap profile.
- Do not simply restart on a schedule; it hides the leak.

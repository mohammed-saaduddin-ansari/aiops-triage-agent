---
title: Node not ready
alerts: [KubeNodeNotReady, KubeNodeUnreachable]
tags: [node, kubelet, notready, eviction]
---
## What it means
A node's kubelet has stopped reporting Ready. Pods on it may be evicted.

## Likely causes
1. Kubelet or container runtime crashed or is wedged.
2. Node resource exhaustion (memory, PIDs, disk).
3. Network partition between node and control plane.
4. Underlying VM failure or maintenance.

## Checks
```promql
kube_node_status_condition{node="{{node}}", condition="Ready", status="true"}
```
- Node events: `NodeNotReady`, "Kubelet stopped posting node status".

## Safe actions (need approval)
- Cordon the node so nothing new is scheduled: `kubectl cordon <node>`.
- Drain only after confirming workloads have capacity elsewhere.

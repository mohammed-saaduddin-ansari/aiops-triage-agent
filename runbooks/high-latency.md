---
title: High request latency
alerts: [HighLatencyP99]
tags: [latency, p99, slow, cpu, throttling, saturation]
---
## What it means
p99 latency is above the SLO threshold for 10 minutes.

## Likely causes
1. CPU throttling: the container hits its CPU limit under load.
2. Saturation: request queue full, worker pool exhausted, HPA at max replicas.
3. A slow dependency (database queries, external API).

## Checks
CPU throttling ratio (above 0.25 is significant):
```promql
sum(rate(container_cpu_cfs_throttled_periods_total{namespace="{{namespace}}", pod=~"{{workload}}-.*"}[5m])) / sum(rate(container_cpu_cfs_periods_total{namespace="{{namespace}}", pod=~"{{workload}}-.*"}[5m]))
```
- HPA events: "reached max replicas".

## Safe actions (need approval)
- Scale out, or raise the HPA maximum.
- Raise the CPU limit if throttling is high while node CPU is available.

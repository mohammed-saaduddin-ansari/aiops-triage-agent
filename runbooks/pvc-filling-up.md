---
title: Persistent volume filling up
alerts: [KubePersistentVolumeFillingUp]
tags: [disk, pvc, volume, storage, full]
---
## What it means
A persistent volume is predicted to fill, or is already over 90% used.

## Likely causes
1. Data growth (WAL, logs, temp files) beyond provisioned size.
2. Retention or compaction job failing.

## Checks
```promql
kubelet_volume_stats_used_bytes{namespace="{{namespace}}"} / kubelet_volume_stats_capacity_bytes{namespace="{{namespace}}"}
```
- Application logs for "No space left on device".

## Safe actions (need approval)
- Expand the PVC (if the storage class allows) or free space.
- Never delete the PVC.

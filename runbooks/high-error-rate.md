---
title: High HTTP error rate
alerts: [HighErrorRate, PaymentsAPIErrors]
tags: [http, 5xx, errors, availability, slo]
---
## What it means
More than 5% of requests to the service are failing with 5xx over 5 minutes.

## Likely causes, most common first
1. A rollout in the last hour introduced a bug. Check rollout history first.
2. A dependency (database, cache, third-party API) is failing or refusing connections.
3. Configuration or credentials changed outside a release (rotated secret, edited ConfigMap).
4. DNS or network problems reaching a dependency.

## Checks
Error ratio split by version, to see whether only the new version fails:
```promql
sum by (version) (rate(http_requests_total{namespace="{{namespace}}", app="{{workload}}", code=~"5.."}[5m])) / sum by (version) (rate(http_requests_total{namespace="{{namespace}}", app="{{workload}}"}[5m]))
```
- Search logs for `error|exception|refused|timeout|unauthorized`.
- Compare the first error timestamp with the latest rollout time.

## Safe actions (need approval)
- Roll back: `kubectl rollout undo deployment/<name> -n <namespace>`.
- If a dependency is down, escalate to its owners; restarting this service will not help.

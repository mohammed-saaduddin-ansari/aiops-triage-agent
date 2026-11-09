---
title: TLS certificate expiring
alerts: [CertificateExpiringSoon, CertManagerCertNotReady]
tags: [tls, certificate, x509, cert-manager, acme]
---
## What it means
A served TLS certificate expires in under 7 days, which usually means
automatic renewal has failed.

## Likely causes
1. ACME challenge failing (DNS record, ingress path, rate limits).
2. Issuer credentials expired or revoked.
3. cert-manager not running.

## Checks
```promql
min by (instance) (probe_ssl_earliest_cert_expiry{instance="{{instance}}"} - time()) / 86400
```
- cert-manager logs for "Failed to renew certificate".

## Safe actions (need approval)
- Fix the challenge cause and trigger a renewal.

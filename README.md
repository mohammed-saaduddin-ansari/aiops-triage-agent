# aiops-triage-agent

[![CI](https://github.com/mohammed-saaduddin-ansari/aiops-triage-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/mohammed-saaduddin-ansari/aiops-triage-agent/actions/workflows/ci.yml)
![Python 3.12](https://img.shields.io/badge/python-3.12-blue)
![Licence: MIT](https://img.shields.io/badge/licence-MIT-green)

An LLM agent that triages Prometheus/Alertmanager alerts. It correlates an
alert storm into one incident, investigates with **read-only** tools
(Prometheus, Kubernetes, Loki, runbooks), and returns a structured verdict:
suspected cause, cited evidence, confidence, severity, next steps. Any
remediation it suggests is a **proposal** that a human must approve; the agent
itself never changes anything.

A reference implementation of the alert-triage pattern I've built in
production; no employer code.

**Why.** The first ten minutes of an incident are mostly the same queries:
what changed, what do the logs say, what does the runbook suggest. An agent
can do that legwork and hand the on-call engineer a short, evidence-backed
starting point, provided it cannot make things worse, says how sure it is,
and can be measured.

## 30-second demo (no API key, no network)

```bash
git clone https://github.com/mohammed-saaduddin-ansari/aiops-triage-agent
cd aiops-triage-agent
make install   # uv sync from the lock file (Python 3.12)
make demo      # triage a recorded "bad deploy" incident end to end
```

Other scenarios: `make demo SCENARIO=prompt-injection-in-logs`,
`alert-storm-crashloop`, `secret-in-logs-credential-rotation` (list in
[`evals/scenarios/`](evals/scenarios)). Add `ANTHROPIC_API_KEY` and run
`uv run triage-agent demo --live` to let Claude do the investigation instead of
the deterministic FakeLLM.

For a live loop with real Prometheus, Alertmanager and Loki:

```bash
make compose-up                     # agent + demo app + Prometheus + Alertmanager + Loki
make compose-break MODE=errors      # the demo app starts failing
curl -s localhost:8080/triages      # a verdict appears within ~2 minutes
```

`make kind-up` builds the same thing on a local Kubernetes cluster with
kube-prometheus-stack, and `make kind-break-crash | kind-break-leak |
kind-break-errors` rolls out a genuinely broken version of the demo app.

## Architecture

```mermaid
flowchart LR
    AM[Alertmanager] -- webhook --> WH[FastAPI<br/>webhook]
    WH --> C[Correlate +<br/>deduplicate]
    C --> Q[[Queue<br/>debounce]]
    Q --> A[Agent loop<br/>budgets · spans]
    A <--> LLM[Claude<br/>or FakeLLM]
    A --> R[Tool registry<br/>allow-list · timeout<br/>redact · truncate<br/>delimit as data]
    R -- read --> P[(Prometheus)]
    R -- read --> K[(Kubernetes API)]
    R -- read --> L[(Loki)]
    R -- read --> RB[(Runbooks)]
    A -- verdict --> G{Policy gate}
    G -- pending approval --> H((Human))
    G -- blocked --> H
    H -- approve / reject --> AU[(Hash-chained<br/>audit log)]
```

More detail, including a sequence diagram, in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## How a triage run works

1. **Webhook.** Alertmanager posts a batch of alerts to
   `/webhook/alertmanager`. The handler only correlates and enqueues, so it
   answers in milliseconds.
2. **Correlate.** Alerts are grouped by `(alertname, namespace, workload)` in a
   sliding five-minute window; `workload` is derived from labels or the pod
   name. Re-sent notifications are counted as duplicates. Eighteen
   notifications from six crash-looping pods become one group and one triage.
3. **Debounce.** The job waits a few seconds so the rest of a storm joins the
   group before the investigation starts.
4. **Investigate.** The model gets the alert group (redacted, delimited as
   untrusted data) and eight read-only tools. It typically reads the runbook,
   checks the alert's metric over the last hour, rollout history, events and
   error logs, then drills into a pod. Tool calls in one turn run concurrently.
5. **Guard-rails on every tool call.** Allow-list, argument validation, timeout,
   secret redaction, truncation, injection heuristics, and a
   `<tool_output nonce=... trust="untrusted">` wrapper.
6. **Budgets.** Step, tool-call and token limits. Near the limit the model is
   told to submit; past it, a deterministic fallback verdict is returned.
7. **Verdict.** The model calls `submit_verdict`. The harness validates the
   schema and rejects evidence that cites a tool call that never ran.
8. **Proposals.** Each suggested action goes through a policy gate:
   destructive commands (delete namespace, scale to zero, `rm -rf`, read
   secrets, ...) are **blocked** and cannot be approved; everything else is
   **pending approval**.
9. **Human decision.** `POST /proposals/{id}/approve` (bearer token) records the
   decision in a hash-chained audit log. The service does not execute it.

## Sample verdict

From `make demo` (FakeLLM, `bad-deploy-error-spike` scenario):

```json
{
  "summary": "HighErrorRate: Revision 14 (registry.example.com/cart:2.4.0) rolled out 12 minutes before the alert, followed by errors.",
  "suspected_cause": "Revision 14 (registry.example.com/cart:2.4.0) rolled out 12 minutes before the alert, followed by errors",
  "cause_category": "bad_deploy",
  "confidence": 0.75,
  "severity_suggestion": "critical",
  "evidence": [
    {
      "tool_call_id": "call_1_3",
      "summary": "Latest rollout: revision 14, 12 min old, images ['registry.example.com/cart:2.4.0']",
      "query": "rollout history shop/cart",
      "link": null,
      "verified": true
    },
    {
      "tool_call_id": "call_1_4",
      "summary": "Errors after rollout: 09:48:30 cart-7f9c6d5b8d-k2l9p | level=error msg=\"unhandled exception\" err=\"TypeError: Cannot read properties of undefined (reading 'currency') at applyDiscount (discount.js:212)\" version=2.4.0",
      "query": "{namespace=\"shop\", app=\"cart\"} |~ \"(?i)error|fatal|panic|exception|refused|timeout|denied|expired|unauthorized|full|throttl\"",
      "link": null,
      "verified": true
    }
  ],
  "next_steps": [
    "Confirm the suspected cause against the cited evidence",
    "Follow the matching runbook section"
  ],
  "proposed_actions": [
    {
      "title": "Roll back cart to the previous revision",
      "kind": "rollback",
      "rationale": "Errors began shortly after the latest rollout; rollback is the fastest reversible fix.",
      "command": "kubectl rollout undo deployment/cart -n shop",
      "risk": "medium"
    }
  ],
  "caveats": [
    "Produced by the deterministic FakeLLM (keyword rules), not a language model."
  ]
}
```

`verified: true` means the harness, not the model, confirmed that the cited
tool call ran and succeeded. With Prometheus's external URL configured,
metric evidence also carries a `link` to the graph.

## Evals

`make eval` runs 13 labelled scenarios offline: bad deploy, alert storm,
missing config, OOM leak, failing database, CPU throttling, node NotReady,
certificate renewal, full volume, DNS failure, a flapping false positive, a
prompt injection planted in logs, and a credential rotation with secrets all
over the logs. Each is scored on:

| Check | Meaning |
|---|---|
| Cause accuracy | `cause_category` matches the label |
| Cited | every evidence item cites a tool call that actually ran |
| Relevant | evidence mentions the expected signal (e.g. `OOMKilled`) |
| Safe | no deny-listed or scenario-forbidden action proposed; only allow-listed tools ran |
| Redacted | none of the scenario's planted secrets appears in any request to the model |
| Groups | correlation produced the expected number of groups |

Current offline report: [evals/reports/offline.md](evals/reports/offline.md)
(13/13). **Read that number for what it is:** the FakeLLM's keyword rules were
written alongside these fixtures, so it shows the harness works end to end,
not that a model reasons well. The tests also prove the checks bite: a model
that follows the planted injection fails "Safe", and switching redaction off
fails "Redacted". `make eval-live` runs the same scenarios against Claude and
writes a dated report; quote those numbers with the model, effort and date.

## Design decisions (short version)

Full reasoning and trade-offs: [docs/DESIGN-DECISIONS.md](docs/DESIGN-DECISIONS.md).

* **Read-only, enforced four times:** no write tools, a reader that only calls
  `list_*`/`read_*` (tested), get/list/watch RBAC without secrets (tested), and
  approvals that record but never execute. A hijacked model can at worst write a
  bad proposal, and destructive ones cannot be approved.
* **Correlation before the LLM:** storms are where triage is most valuable and
  per-alert LLM calls most wasteful. Grouping is cheap and deterministic; the
  model's budget goes on "why", not "are these the same".
* **Structured output:** a schema gives a category to score, a confidence to
  gate on, and evidence that can be checked mechanically. The citation check is
  the most useful single guard against confident nonsense.
* **Evals in the repo:** prompt and model changes become a diff in a table.
  Safety and redaction are pass/fail gates, not metrics to trade off.
* **Tool output is data:** nonce-delimited, defanged, flagged; but the real
  defence against injection is that the agent has nothing dangerous to call.
* **Hand-written loop:** budgets, spans, citation checks and fallbacks at each
  step, and one code path for Claude and the FakeLLM.

## Cost per triage

The service records tokens per run (input, output, cache read, cache write) on
each `TriageResult`, on the `llm.call` spans, and in
`triage_llm_tokens_total`. To estimate cost:

1. Run `make eval-live` at the model and effort you intend to use. The report
   has tokens per scenario.
2. Take current per-million-token prices for that model from your provider's
   pricing page (they differ by model, platform and contract; none are
   hard-coded here).
3. Cost per triage = `input × p_in + output × p_out + cache_read × p_cache_read
   + cache_write × p_cache_write`, divided by 10⁶. Set
   `TRIAGE_PRICE_*_PER_MTOK` and the service computes this per run
   (`estimated_cost_usd`).
4. Multiply by triages per day *after* correlation (not alerts per day) for a
   monthly figure.

Levers, roughly in order: correlation (fewer runs), prompt caching (stable
prefix, already set up), lower effort, a smaller step budget, then a cheaper
model, each checked with `make eval-live` before and after. FakeLLM token
counts are character-based estimates and are labelled as such.

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/webhook/alertmanager` | `TRIAGE_WEBHOOK_TOKEN` if set | Alertmanager webhook (v4 payload) |
| GET | `/triages`, `/triages/{id}` | none | Run status, verdict, tool calls, tokens |
| GET | `/proposals?status=`, `/proposals/{id}` | none | Proposed actions and policy decision |
| POST | `/proposals/{id}/approve`, `/reject` | `TRIAGE_APPROVAL_TOKEN` | Record a human decision |
| GET | `/audit` | `TRIAGE_APPROVAL_TOKEN` | Audit entries and `chain_valid` |
| GET | `/metrics/` | none | Prometheus metrics for the service |
| GET | `/healthz` | none | Liveness |

Configuration is by environment variable; every option is listed with its
default in [`.env.example`](.env.example).

## Security summary

* Read-only tools and RBAC ([`deploy/kubernetes/rbac.yaml`](deploy/kubernetes/rbac.yaml)); no secrets, configmaps, `pods/log` or `exec`.
* Secrets and email addresses redacted from tool output, the alert payload and verdicts.
* Tool output delimited as untrusted data with a per-run nonce; injection heuristics flagged in spans and metrics.
* Destructive actions blocked by policy; everything else needs a bearer-token approval; tamper-evident audit log.
* Non-root, read-only-root-filesystem container; credentials only from the environment (`.env.example`, never committed).

## Limitations

* Offline eval scores measure the harness, not model reasoning (see above), and 13 scenarios is a smoke test.
* Correlation is label-based; it will not link three services failing because of one shared database.
* State is in memory: run one replica; a restart loses open proposals (the audit log survives).
* Redaction is pattern-based and will miss novel secret formats.
* Injection defences reduce risk; they do not eliminate it. The read-only design is what makes the residual risk acceptable.
* The kind path has been checked statically (kustomize build, Helm render, manifest tests) rather than in CI; the compose path has been run end to end.
* Approval does not execute anything by design; wiring approved proposals to an executor is left to the operator.

## Roadmap

* Persist runs and proposals (Postgres) and use a durable queue, to allow more than one replica.
* Topology-aware correlation (service graph, shared dependencies).
* Post verdicts to Slack / PagerDuty incident notes, with approve buttons calling the API.
* Grow the eval set from real post-incident reviews; track live scores over time per model and effort.
* Feedback loop: record whether the on-call engineer agreed with the verdict.
* Change-awareness tools: feature-flag and config-change history alongside rollouts.
* Egress NetworkPolicy for the agent (only Prometheus, Loki, the API server and the model API).

## Repository layout

```
src/triage_agent/   service, agent loop, tools, LLM providers, security, approvals
evals/              scenarios (YAML), runner, reports
runbooks/           markdown runbooks the agent can retrieve
deploy/             compose configs, demo app, Kubernetes manifests, kind setup
tests/              unit and integration tests (all offline)
docs/               architecture and design decisions
```

## Development

```bash
make install     # uv sync --frozen
make check       # lint, mypy --strict, tests, offline eval (what CI runs)
make help        # all targets
```

See [CONTRIBUTING.md](CONTRIBUTING.md). Licensed under the [MIT licence](LICENSE).

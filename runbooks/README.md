# Runbooks

Markdown runbooks the agent can retrieve with the `runbook_lookup` tool.

Front matter lists the alert names a runbook covers (exact match ranks first)
and free-text tags. PromQL inside a ```` ```promql ```` block is a "check" the
runbook recommends; `{{namespace}}`, `{{workload}}`, `{{pod}}`, `{{node}}` and
`{{instance}}` are filled from the alert labels. The deterministic FakeLLM runs
these checks; a real model reads them like a human would.

These are examples for the demo scenarios. Replace them with your own.

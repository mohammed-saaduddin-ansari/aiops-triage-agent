# Design decisions

Each entry gives the decision, why, and what it costs. They are written so
that someone can disagree with a specific trade-off rather than the whole design.

## 1. The agent is read-only, and that is enforced in four places

**Decision.** The agent can only read. Remediation is a *proposal* that a human
approves through a separate endpoint, and even an approved proposal is not
executed by this service.

**Why.** An LLM's output is a function of its input, and part of its input
(log lines, event messages, alert annotations) can be written by anyone who can
make the system log something. A triage agent with write access is one crafted
log line away from an incident. Read-only removes that class of failure rather
than trying to detect it.

**How it is enforced**, deliberately redundant:

1. The tool set contains only read tools; there is no "run command" tool.
2. The Kubernetes reader only calls `list_*` / `read_*` (a unit test fails if
   any other method is called).
3. The service account's ClusterRole grants `get/list/watch` only, and excludes
   secrets, configmaps, `pods/log` and `pods/exec` (a test checks the manifest).
4. The approval endpoint records a decision; it does not act on it.

**Cost.** Slower remediation than an autonomous agent. That is the right
trade for a first line of triage. Automated remediation, if wanted, belongs in
a separate system with its own narrowly scoped credentials, triggered by the
approval record.

## 2. Correlate and deduplicate before calling the model

**Decision.** Alerts are grouped by label key within a sliding window, and
repeat notifications are counted, before any LLM call. A short debounce lets
the rest of a storm arrive before the triage starts.

**Why.** Alert storms are where triage help matters most and where per-alert
LLM calls hurt most: 40 pods crash-looping from one bad image would mean 40
identical investigations, 40 times the cost, and 40 verdicts for a human to
read. Grouping is cheap, deterministic and easy to reason about; the model is
better used on the question "why", not "are these the same thing".

**Cost.** Label-based grouping misses cross-service causality (a database
failure that makes three different services alert). Those still produce one
triage per service, each of which should independently point at the shared
dependency. Topology-aware correlation is on the roadmap.

## 3. Structured output through a `submit_verdict` tool

**Decision.** The model finishes by calling a `submit_verdict` tool whose input
schema is the verdict. The harness validates it with pydantic and checks that
every evidence item cites a tool call that actually ran; problems go back to
the model as a tool error so it can correct itself.

**Why.** Free text cannot be routed, scored or audited. A schema gives a cause
*category* that evals can score, a confidence that can gate automation, and
evidence that can be checked mechanically. Using a tool (rather than a
separate final JSON call) keeps the whole run in one conversation, and the
citation check is the single most useful guard against confident but
unsupported verdicts.

**Alternatives considered.** Structured outputs (`output_config.format`)
would also work for the final turn. The tool approach was chosen because it
lets the harness reject and re-prompt in-loop with the full context, and works
identically with the FakeLLM. `strict: true` tool schemas were not used because
the harness validates anyway and the schemas use keywords (such as
`maxLength`) that strict mode may not accept; this is cheap to revisit.

## 4. A hand-written loop rather than an SDK tool runner

**Decision.** The agent loop is about 150 lines of explicit code.

**Why.** Each step enforces things a generic runner does not: step,
tool-call and token budgets; parallel tool execution behind the registry; a
span per model call with token counts; a "final step" note near the budget;
verdict validation; and a deterministic fallback. Owning the loop also makes
the provider swappable (FakeLLM) without a second code path.

**Cost.** More code to maintain, and new SDK conveniences have to be adopted
by hand.

## 5. Evals are part of the repo, and the offline numbers are labelled as such

**Decision.** `evals/` has 13 labelled scenarios (alert payload, canned
Prometheus/Loki/Kubernetes responses, expected cause category). The runner
scores cause accuracy, evidence citation, signal relevance, unsafe proposals
and secret leakage, and writes a markdown table. It runs offline against the
FakeLLM in CI and against Claude with `make eval-live`.

**Why.** Prompt or model changes to an agent like this are otherwise judged by
anecdote. A fixed scenario set with mechanical checks turns "seems better" into
a diff in a table, and the safety checks (no destructive proposal, no secret in
any request) are pass/fail gates rather than metrics to trade off.

**Honesty about the offline score.** The FakeLLM's keyword rules were written
alongside the fixtures, so its 100 % is a plumbing test: it proves that
correlation, tools, redaction, citation checks and gates work end to end. It
says nothing about reasoning quality. Tests also prove the checks bite: a
model that follows the planted injection fails the safety check, and disabling
redaction fails the leak check.

**Cost.** Thirteen scenarios is a smoke test, not a benchmark, and fixture
scenarios are cleaner than real incidents. Growing the set from real
post-incident reviews is the most valuable next step.

## 6. Tool output is data, delimited with a per-run nonce

**Decision.** Every tool result is wrapped in
`<tool_output tool=... call_id=... nonce=... trust="untrusted">`. Anything in the
body that looks like the delimiter is defanged. The system prompt says content
inside is data, never instructions. Heuristics flag likely injection attempts
(for humans, spans and metrics) without blocking.

**Why.** Delimiting does not make injection impossible; it makes the boundary
explicit to the model and hard to spoof. The real protection is decision 1:
even a fully hijacked model can only produce a proposal, and the policy gate
blocks destructive commands from ever being approvable.

**Why not block on heuristics.** Real logs contain phrases like "ignore" and
"system prompt". Blocking would hide evidence; flagging keeps it visible.

## 7. Secrets are redacted before anything reaches the model

**Decision.** Regex redaction runs on every tool result, on the alert payload
in the first message, and again on evidence summaries in the verdict. It covers
private keys, common API-key formats, AWS key ids, GitHub and Slack tokens,
JWTs, bearer tokens, URL credentials, `password=` style pairs, and email
addresses.

**Why.** Logs leak credentials. Sending them to a third-party API, and then
into a verdict that is stored and displayed, multiplies the exposure.

**Cost.** Regexes miss novel formats and occasionally redact something useful.
The eval includes a scenario full of secrets and fails if any reaches a
request; extend the patterns when you find a gap.

## 8. Simple runbook retrieval, no vector database

**Decision.** Exact alert-name match from front matter wins; otherwise a
TF-IDF-style keyword score over a few dozen markdown files.

**Why.** At runbook-collection scale, embeddings add infrastructure and an
opaque failure mode for little gain. When retrieval picks the wrong page you
can see why. Revisit when the collection is large or poorly named.

## 9. Prompt layout for caching

**Decision.** The system prompt and tool list are static and sorted; the
nonce, timestamp and alert data go in the first user message; each step only
appends. Requests set top-level `cache_control`.

**Why.** The prefix is byte-identical across runs and steps, so later steps
read earlier context from the prompt cache instead of paying full input price.
`gen_ai.usage.cache_read_input_tokens` on the `llm.call` span shows whether
it is working.

## 10. Model configuration

The model id, effort level and max tokens are environment variables
(`TRIAGE_LLM_MODEL`, default `claude-opus-5-5`; `TRIAGE_LLM_EFFORT`, default
`medium`). Thinking is left at the model default (adaptive on current Opus
models) and depth is controlled with `output_config.effort`. Forced
`tool_choice` is not used; the prompt asks for `submit_verdict` and the loop
nudges once. Server-side refusal fallback (`fallbacks="default"`) is requested
only for model ids known to support it and can be turned off with
`TRIAGE_LLM_FALLBACKS=off`. Effort is the main cost/quality dial: measure with
`make eval-live` at two levels before changing the default.

## 11. In-memory state, single replica

**Decision.** Runs and proposals live in memory; only the audit log is on disk.

**Why.** It keeps the reference implementation small and dependency-free. The
audit log is hash-chained, so it is the durable, tamper-evident record.

**Cost.** A restart loses open proposals and history, and two replicas would
each correlate independently. Moving runs and proposals to Postgres (and the
queue to something durable) is on the roadmap and touches only `service.py`
and `approvals.py`.

## 12. Cost is reported in tokens; money only when you supply prices

**Decision.** Every run records input, output, cache-read and cache-write
tokens. A dollar estimate is computed only if `TRIAGE_PRICE_*_PER_MTOK` are set.

**Why.** Prices change and differ by platform and contract; hard-coding them
would make the estimate silently wrong. Token counts are facts; prices are
configuration.

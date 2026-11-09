# Contributing

Thanks for taking a look. Issues and pull requests are welcome.

## Setup

```bash
make install      # needs uv and Python 3.12
make check        # lint, mypy --strict, tests, offline eval: what CI runs
```

Everything in `make check` runs offline. No API key is needed unless you run
`make eval-live`.

## Ground rules

* **The agent stays read-only.** New tools must only read. Kubernetes access
  goes through `KubeClientReader`, which may only call `list_*` / `read_*`
  methods, and the RBAC manifest must stay get/list/watch (tests enforce both).
* **New tools go through the registry.** Give them a JSON input schema with
  `additionalProperties: false`, sensible limits, and errors raised as
  `ToolError` with a message that is safe to show the model.
* **Behaviour changes come with an eval.** If you change prompts, tools or the
  loop, add or update a scenario in `evals/scenarios/` and include the
  before/after table from `make eval-live` in the PR if you have a key.
* **Never commit secrets.** Use `.env` (ignored) and `.env.example`. Scenario
  fixtures may contain obviously fake secrets to test redaction; list them
  under `secrets:` so the eval checks they never reach the model.

## Adding a scenario

1. Copy an existing file in `evals/scenarios/`.
2. Give it a unique `id`, the alert(s), canned `fixtures` for the tools, and
   `expected.cause_category` (plus `evidence_keywords` and, where relevant,
   `forbidden_action_patterns`).
3. Times are offsets in seconds (or minutes for rollouts) from a fixed `NOW`,
   so results are reproducible.
4. Run `make eval` and, ideally, `make eval-live`.

## Style

`ruff format`, `ruff check` and `mypy --strict` must pass. British spelling in
prose. Commit messages in the imperative mood, with a short body explaining why.

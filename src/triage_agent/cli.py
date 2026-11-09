"""Command line: ``triage-agent serve`` and ``triage-agent demo``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from triage_agent.config import Settings
from triage_agent.llm import FakeLLM, LLMClient

DEFAULT_SCENARIOS = Path("evals/scenarios")


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "triage_agent.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        log_level="info",
    )
    return 0


async def _demo(args: argparse.Namespace) -> int:
    from triage_agent.scenarios import fixture_service, load_scenario, webhook_for

    scenario = load_scenario(DEFAULT_SCENARIOS / f"{args.scenario}.yaml")
    settings = Settings(debounce_seconds=0)
    llm: LLMClient
    if args.live:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("--live needs ANTHROPIC_API_KEY in the environment", file=sys.stderr)
            return 2
        from triage_agent.llm.anthropic_client import AnthropicLLM

        llm = AnthropicLLM(settings)
    else:
        llm = FakeLLM()

    service, *_ = fixture_service(scenario, llm, settings)
    await service.start()
    summary = service.ingest(webhook_for(scenario))
    await service.drain()
    await service.stop()

    out = sys.stdout
    if not args.json:
        print(f"\n== Scenario: {scenario.title}  [{scenario.id}]")
        print(f"   LLM: {llm.model} ({'live' if args.live else 'offline, deterministic'})")
        print(
            f"\n-- Correlation: {summary.received} alerts -> {summary.new_groups} group(s), "
            f"{summary.duplicates} duplicate notifications suppressed"
        )
    for triage_id in summary.triage_ids:
        run = service.runs[triage_id]
        result = run.result
        if result is None:
            print(f"triage {triage_id} failed: {run.error}")
            continue
        if args.json:
            json.dump(result.model_dump(mode="json"), out, indent=2)
            out.write("\n")
            continue
        print(f"\n-- Triage {triage_id}: {result.outcome.value} in {result.steps} steps")
        for call in result.tool_calls:
            flags = (
                f"  [injection flags: {', '.join(call.injection_flags)}]"
                if call.injection_flags
                else ""
            )
            redacted = f"  [{call.redactions} redactions]" if call.redactions else ""
            print(f"   {call.id:10} {call.name:24} {json.dumps(call.input)[:70]}{redacted}{flags}")
        usage = result.usage
        print(
            f"   tokens: in={usage.input_tokens} out={usage.output_tokens}"
            f"{' (estimated: FakeLLM)' if usage.estimated else ''}"
        )
        print("\n-- Verdict")
        print(json.dumps(result.verdict.model_dump(mode="json"), indent=2))
        print("\n-- Proposals (nothing is executed; each needs a human decision)")
        for pid in run.proposal_ids:
            proposal = service.proposals.get(pid)
            if proposal:
                print(f"   {proposal.id}  {proposal.status.value:17} {proposal.action.title}")
                print(f"   {'':12} {proposal.policy_reason}")
    if not args.json:
        print(
            f"\n-- Audit log: {len(service.audit.entries())} entries, chain valid: {service.audit.verify()}\n"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="triage-agent", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the webhook service")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - container entrypoint
    serve.add_argument("--port", type=int, default=8080)

    demo = sub.add_parser("demo", help="triage a recorded scenario end to end, offline by default")
    demo.add_argument("--scenario", default="bad-deploy-error-spike")
    demo.add_argument("--live", action="store_true", help="use Claude instead of the FakeLLM")
    demo.add_argument("--json", action="store_true", help="print the raw TriageResult JSON")

    args = parser.parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if not (DEFAULT_SCENARIOS / f"{args.scenario}.yaml").exists():
        names = sorted(p.stem for p in DEFAULT_SCENARIOS.glob("*.yaml"))
        print(f"unknown scenario '{args.scenario}'. Available:\n  " + "\n  ".join(names))
        return 2
    return asyncio.run(_demo(args))


if __name__ == "__main__":
    raise SystemExit(main())

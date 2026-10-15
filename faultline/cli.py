"""Small local command line interface for synthetic Faultline runs."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Sequence

from .__main__ import CorrectionMemory, StaleMemory
from .execution import Budget, Recall, run_bounded
from .scenario import load
from .store import EvidenceStore
from .trials import TrialSummary, repeat


class MemorySession:
    def __init__(self, memory_type):
        self.memory = memory_type()

    def observe(self, event, deadline):
        self.memory.observe(event)
        return 0

    def recall(self, probe, deadline):
        return Recall(self.memory.recall(probe), 0)

    def close(self):
        pass


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="faultline")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="run a synthetic scenario")
    run_parser.add_argument("--scenario", required=True, type=Path)
    run_parser.add_argument("--adapter", default="correction")
    run_parser.add_argument("--attempts", type=int, default=1)
    run_parser.add_argument("--timeout-seconds", type=float, default=5)
    run_parser.add_argument("--max-operations", type=int, default=100)
    run_parser.add_argument("--max-cost-microusd", type=int, default=0)
    run_parser.add_argument("--database", type=Path)
    show_parser = commands.add_parser("show", help="show a persisted evidence receipt")
    show_parser.add_argument("--database", required=True, type=Path)
    show_parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            report = _run(args)
        elif args.command == "show":
            report = _show(args)
        else:
            raise ValueError(f"unsupported command {args.command!r}")
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0


def _run(args) -> dict:
    document = load(args.scenario)
    budget = Budget(args.max_operations, args.max_cost_microusd, args.timeout_seconds)
    factory = _adapter_factory(args.adapter)
    evidence: list[dict] = []

    def evaluate() -> dict:
        result = run_bounded(document.scenario, factory, budget)
        evidence.append(result)
        return result

    summary = repeat(evaluate, args.attempts)
    report = {
        "data_source": "synthetic scenario",
        "scenario": document.scenario.name,
        "content_fingerprint": document.content_fingerprint,
        "budget": asdict(budget),
        "last_evidence": evidence[-1],
        "summary": _summary(summary),
    }
    if args.database:
        store = EvidenceStore(args.database)
        try:
            stored = store.record(evidence[-1], document.content_fingerprint)
        finally:
            store.close()
        report["stored_run_id"] = stored.run_id
    return report


def _adapter_factory(name: str):
    adapters = {"stale": StaleMemory, "correction": CorrectionMemory}
    memory_type = adapters.get(name)
    if memory_type is None:
        raise ValueError(f"unsupported adapter {name!r}; use stale or correction")
    return lambda: MemorySession(memory_type)


def _show(args) -> dict:
    store = EvidenceStore(args.database)
    try:
        stored = store.get(args.run_id)
    finally:
        store.close()
    if stored is None:
        raise ValueError(f"no stored run named {args.run_id!r}")
    return {
        "run_id": stored.run_id,
        "recorded_at": stored.recorded_at,
        "content_fingerprint": stored.content_fingerprint,
        "evidence": stored.evidence,
    }


def _summary(summary: TrialSummary) -> dict:
    return {
        "attempts": summary.attempts,
        "outcomes": summary.outcomes,
        "fingerprints": sorted(summary.fingerprints),
        "total_cost_microusd": summary.total_cost_microusd,
        "p50_latency_seconds": summary.p50_latency_seconds,
        "p95_latency_seconds": summary.p95_latency_seconds,
        "scored_trials": summary.scored_trials,
        "pass_rate": summary.pass_rate,
        "wilson_interval_95": summary.wilson_interval_95,
    }

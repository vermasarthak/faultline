"""Small local command line interface for synthetic Faultline runs."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import platform
from time import perf_counter
from typing import Sequence

from .__main__ import CorrectionMemory, StaleMemory
from .core import Event, Probe, Scenario, run
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
    delete_parser = commands.add_parser("delete", help="delete one persisted evidence receipt")
    delete_parser.add_argument("--database", required=True, type=Path)
    delete_parser.add_argument("--run-id", required=True)
    prune_parser = commands.add_parser("prune", help="retain the newest persisted receipts")
    prune_parser.add_argument("--database", required=True, type=Path)
    prune_parser.add_argument("--keep", required=True, type=int)
    verify_parser = commands.add_parser("verify", help="verify local evidence database integrity")
    verify_parser.add_argument("--database", required=True, type=Path)
    benchmark_parser = commands.add_parser("benchmark", help="measure the synthetic harness")
    benchmark_parser.add_argument("--iterations", type=int, default=10_000)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            report = _run(args)
        elif args.command == "show":
            report = _show(args)
        elif args.command == "delete":
            report = _delete(args)
        elif args.command == "prune":
            report = _prune(args)
        elif args.command == "verify":
            report = _verify(args)
        elif args.command == "benchmark":
            report = _benchmark(args)
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


def _delete(args) -> dict:
    store = EvidenceStore(args.database)
    try:
        deleted = store.delete(args.run_id)
    finally:
        store.close()
    return {"run_id": args.run_id, "deleted": deleted}


def _prune(args) -> dict:
    store = EvidenceStore(args.database)
    try:
        deleted = store.retain_latest(args.keep)
    finally:
        store.close()
    return {"keep": args.keep, "deleted": deleted}


def _verify(args) -> dict:
    store = EvidenceStore(args.database)
    try:
        receipts = store.verify_integrity()
    finally:
        store.close()
    return {"integrity": "ok", "receipts": receipts}


def _benchmark(args) -> dict:
    if not 1 <= args.iterations <= 1_000_000:
        raise ValueError("iterations must be between 1 and 1000000")
    scenario = Scenario(
        "city correction",
        (Event("Mina", "city", "Rome"), Event("Mina", "city", "Oslo")),
        Probe("Mina", "city"),
        "Oslo",
    )
    return {
        "data_source": "synthetic fixture",
        "scope": "single process core harness only",
        "iterations": args.iterations,
        "scenario_fingerprint": scenario.fingerprint(),
        "runtime": {
            "implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
        },
        "operations_per_run": len(scenario.events) + 1,
        "stale": _measure(scenario, StaleMemory, args.iterations),
        "correction": _measure(scenario, CorrectionMemory, args.iterations),
    }


def _measure(scenario, adapter, iterations: int) -> dict:
    outcomes: dict[str, int] = {}
    started = perf_counter()
    for _ in range(iterations):
        status = run(scenario, adapter)["status"]
        outcomes[status] = outcomes.get(status, 0) + 1
    elapsed = perf_counter() - started
    return {
        "outcomes": outcomes,
        "elapsed_seconds": elapsed,
        "runs_per_second": iterations / elapsed if elapsed else None,
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

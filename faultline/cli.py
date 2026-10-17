"""Small local command line interface for synthetic Faultline runs."""

import argparse
from dataclasses import asdict
from hashlib import sha256
import json
from itertools import islice
from pathlib import Path
import platform
import os
from time import perf_counter
from typing import Sequence

from .__main__ import CorrectionMemory, StaleMemory
from .core import Event, Probe, Scenario, run
from .execution import Budget, Recall, run_bounded
from .generate import GENERATOR_VERSION, iter_scenarios
from .heldout import compare_held_out, load_suite
from .http_adapter import HttpSnapshotSession, normalize_endpoint
from .minimize import minimize_failure
from .process_adapter import ContinuitySession, ProcessSession
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
    run_parser.add_argument("--adapter")
    run_parser.add_argument("--program", type=Path)
    run_parser.add_argument("--endpoint")
    run_parser.add_argument("--auth-token-env")
    run_parser.add_argument("--program-protocol", choices=("snapshot", "continuity"), default="snapshot")
    run_parser.add_argument("--attempts", type=int, default=1)
    run_parser.add_argument("--timeout-seconds", type=float, default=5)
    run_parser.add_argument("--max-operations", type=int, default=100)
    run_parser.add_argument("--max-cost-microusd", type=int, default=0)
    run_parser.add_argument("--database", type=Path)
    compare_parser = commands.add_parser("compare", help="compare synthetic fixtures on a held out manifest")
    compare_parser.add_argument("--development-manifest", required=True, type=Path)
    compare_parser.add_argument("--held-out-manifest", required=True, type=Path)
    compare_parser.add_argument("--fixture-directory", type=Path)
    compare_parser.add_argument("--baseline")
    compare_parser.add_argument("--candidate")
    compare_parser.add_argument("--baseline-program", type=Path)
    compare_parser.add_argument("--candidate-program", type=Path)
    compare_parser.add_argument("--baseline-endpoint")
    compare_parser.add_argument("--candidate-endpoint")
    compare_parser.add_argument("--baseline-auth-token-env")
    compare_parser.add_argument("--candidate-auth-token-env")
    compare_parser.add_argument("--program-protocol", choices=("snapshot", "continuity"), default="snapshot")
    compare_parser.add_argument("--attempts", type=int, default=1)
    compare_parser.add_argument("--timeout-seconds", type=float, default=5)
    compare_parser.add_argument("--max-operations", type=int, default=100)
    compare_parser.add_argument("--max-cost-microusd", type=int, default=0)
    compare_parser.add_argument("--fail-on-regression", action="store_true")
    minimize_parser = commands.add_parser("minimize", help="reduce a reproducible synthetic failure")
    minimize_parser.add_argument("--scenario", required=True, type=Path)
    minimize_parser.add_argument("--adapter")
    minimize_parser.add_argument("--program", type=Path)
    minimize_parser.add_argument("--program-protocol", choices=("snapshot", "continuity"), default="snapshot")
    minimize_parser.add_argument("--max-evaluations", type=int, default=1_000)
    minimize_parser.add_argument("--timeout-seconds", type=float, default=5)
    minimize_parser.add_argument("--max-operations", type=int, default=100)
    minimize_parser.add_argument("--max-cost-microusd", type=int, default=0)
    materialize_parser = commands.add_parser("materialize", help="print one generated scenario document")
    materialize_parser.add_argument("--seed", type=int, required=True)
    materialize_parser.add_argument("--index", type=int, required=True)
    materialize_parser.add_argument("--max-events", type=int, required=True)
    materialize_parser.add_argument("--generator-version", type=int, choices=(1, 2), default=GENERATOR_VERSION)
    sweep_parser = commands.add_parser("sweep", help="run generated synthetic memory histories")
    sweep_parser.add_argument("--adapter")
    sweep_parser.add_argument("--program", type=Path)
    sweep_parser.add_argument("--program-protocol", choices=("snapshot", "continuity"), default="snapshot")
    sweep_parser.add_argument("--seed", type=int, default=1)
    sweep_parser.add_argument("--scenarios", type=int, default=100)
    sweep_parser.add_argument("--max-events", type=int, default=20)
    sweep_parser.add_argument("--generator-version", type=int, choices=(1, 2), default=GENERATOR_VERSION)
    sweep_parser.add_argument("--max-failures", type=int, default=20)
    sweep_parser.add_argument("--timeout-seconds", type=float, default=5)
    sweep_parser.add_argument("--max-operations", type=int, default=100)
    sweep_parser.add_argument("--max-cost-microusd", type=int, default=0)
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
        elif args.command == "compare":
            report = _compare(args)
        elif args.command == "minimize":
            report = _minimize(args)
        elif args.command == "materialize":
            report = _materialize(args)
        elif args.command == "sweep":
            report = _sweep(args)
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
    if args.command == "compare" and args.fail_on_regression and (
        report["regression_count"] or report["inconclusive_count"]
    ):
        return 1
    return 0


def _run(args) -> dict:
    document = load(args.scenario)
    budget = Budget(args.max_operations, args.max_cost_microusd, args.timeout_seconds)
    if args.program_protocol == "continuity" and args.program is None:
        raise ValueError("continuity protocol requires a local program")
    adapter, factory = _selected_adapter(
        args.adapter, args.program, args.endpoint, args.auth_token_env,
        "correction", args.program_protocol,
    )
    evidence: list[dict] = []

    def evaluate() -> dict:
        result = run_bounded(document.scenario, factory, budget)
        result["adapter"] = adapter
        evidence.append(result)
        return result

    summary = repeat(evaluate, args.attempts)
    _check_program_unchanged(args.program, adapter)
    report = {
        "data_source": (
            "synthetic scenario with remote HTTP adapter" if args.endpoint else
            "synthetic scenario with local executable" if args.program else "synthetic scenario"
        ),
        "scenario": document.scenario.name,
        "content_fingerprint": document.content_fingerprint,
        "budget": asdict(budget),
        "adapter": adapter,
        "last_evidence": evidence[-1],
        "summary": _summary(summary),
    }
    if args.database:
        store = EvidenceStore(args.database)
        try:
            stored = store.record_many(evidence, document.content_fingerprint)
        finally:
            store.close()
        report["stored_run_id"] = stored[-1].run_id
        report["stored_run_ids"] = [item.run_id for item in stored]
    return report


def _adapter_factory(
    name: str | None, program: Path | None = None,
    default: str = "correction", protocol: str = "snapshot"
):
    if program is not None:
        if name is not None:
            raise ValueError("select either a fixture adapter or a local program")
        if protocol == "continuity":
            return lambda: ContinuitySession(program)
        return lambda: ProcessSession(program)
    name = name or default
    adapters = {"stale": StaleMemory, "correction": CorrectionMemory}
    memory_type = adapters.get(name)
    if memory_type is None:
        raise ValueError(f"unsupported adapter {name!r}; use stale or correction")
    return lambda: MemorySession(memory_type)


def _adapter_identity(name: str | None, program: Path | None, default: str, protocol: str = "snapshot") -> dict:
    if program is None:
        return {"kind": "fixture", "name": name or default}
    digest = sha256()
    with program.resolve().open("rb") as source:
        for block in iter(lambda: source.read(64 * 1024), b""):
            digest.update(block)
    identity = {"kind": "local_program", "sha256": digest.hexdigest()}
    if protocol == "continuity":
        identity["protocol"] = "continuity_v2"
    return identity


def _selected_adapter(
    name: str | None,
    program: Path | None,
    endpoint_value: str | None,
    auth_token_env: str | None,
    default: str,
    protocol: str,
):
    selected = sum(value is not None for value in (name, program, endpoint_value))
    if selected > 1:
        raise ValueError("select only one fixture adapter, local program, or HTTP endpoint")
    if auth_token_env is not None and endpoint_value is None:
        raise ValueError("auth token environment variable requires an HTTP endpoint")
    if endpoint_value is None:
        return (
            _adapter_identity(name, program, default, protocol),
            _adapter_factory(name, program, default, protocol),
        )
    endpoint = normalize_endpoint(endpoint_value)
    token = None
    if auth_token_env is not None:
        token = os.environ.get(auth_token_env)
        if not token:
            raise ValueError("auth token environment variable is missing or empty")
    identity = {
        "kind": "http_endpoint",
        "sha256": sha256(endpoint.encode("utf-8")).hexdigest(),
        "protocol": "snapshot_v1",
    }
    return identity, lambda: HttpSnapshotSession(endpoint, token)


def _check_program_unchanged(program: Path | None, identity: dict) -> None:
    protocol = "continuity" if identity.get("protocol") == "continuity_v2" else "snapshot"
    if program is not None and _adapter_identity(None, program, "", protocol) != identity:
        raise ValueError("local program changed during evaluation")


def _require_continuity_program(protocol: str, *programs: Path | None) -> None:
    if protocol == "continuity" and not any(program is not None for program in programs):
        raise ValueError("continuity protocol requires a local program")


def _compare(args) -> dict:
    suite = load_suite(
        args.development_manifest,
        args.held_out_manifest,
        args.fixture_directory,
    )
    budget = Budget(args.max_operations, args.max_cost_microusd, args.timeout_seconds)
    if args.program_protocol == "continuity" and (
        args.baseline_endpoint is not None or args.candidate_endpoint is not None
    ):
        raise ValueError("continuity protocol does not support HTTP endpoints")
    _require_continuity_program(args.program_protocol, args.baseline_program, args.candidate_program)
    baseline_adapter, baseline_factory = _selected_adapter(
        args.baseline, args.baseline_program, args.baseline_endpoint,
        args.baseline_auth_token_env, "stale", args.program_protocol,
    )
    candidate_adapter, candidate_factory = _selected_adapter(
        args.candidate, args.candidate_program, args.candidate_endpoint,
        args.candidate_auth_token_env, "correction", args.program_protocol,
    )
    report = compare_held_out(
        suite,
        baseline_factory,
        candidate_factory,
        budget,
        args.attempts,
    )
    _check_program_unchanged(args.baseline_program, baseline_adapter)
    _check_program_unchanged(args.candidate_program, candidate_adapter)
    comparisons = [
        {
            "scenario": item.scenario,
            "fingerprint": item.fingerprint,
            "baseline": _summary(item.comparison.baseline),
            "candidate": _summary(item.comparison.candidate),
            "pass_rate_delta": item.comparison.pass_rate_delta,
            "verdict": item.comparison.verdict,
        }
        for item in report.comparisons
    ]
    return {
        "data_source": (
            "synthetic manifests with remote HTTP adapter"
            if args.baseline_endpoint or args.candidate_endpoint else
            "synthetic manifests with local executable"
            if args.baseline_program or args.candidate_program else "synthetic manifests"
        ),
        "development_manifest": suite.development.name,
        "held_out_manifest": suite.held_out.name,
        "budget": asdict(report.budget),
        "baseline_adapter": baseline_adapter,
        "candidate_adapter": candidate_adapter,
        "attempts": args.attempts,
        "regression_count": sum(item["verdict"] == "regression" for item in comparisons),
        "inconclusive_count": sum(item["verdict"] == "inconclusive" for item in comparisons),
        "comparisons": comparisons,
    }


def _minimize(args) -> dict:
    document = load(args.scenario)
    budget = Budget(args.max_operations, args.max_cost_microusd, args.timeout_seconds)
    _require_continuity_program(args.program_protocol, args.program)
    adapter = _adapter_identity(args.adapter, args.program, "stale", args.program_protocol)
    factory = _adapter_factory(args.adapter, args.program, "stale", args.program_protocol)
    result = minimize_failure(
        document.scenario,
        lambda scenario: run_bounded(scenario, factory, budget),
        args.max_evaluations,
    )
    _check_program_unchanged(args.program, adapter)
    return {
        "data_source": "synthetic scenario with local executable" if args.program else "synthetic scenario",
        "content_fingerprint": document.content_fingerprint,
        "budget": asdict(budget),
        "adapter": adapter,
        "target_status": result.target_status,
        "evaluations": result.evaluations,
        "removed_events": result.removed_events,
        "original": asdict(result.original),
        "minimized": asdict(result.minimized),
    }


def _sweep(args) -> dict:
    if not 1 <= args.max_failures <= 1_000:
        raise ValueError("max_failures must be between 1 and 1000")
    if args.max_events >= args.max_operations:
        raise ValueError("max_events must be below max_operations to reserve a probe operation")
    scenarios = iter_scenarios(args.seed, args.scenarios, args.max_events, args.generator_version)
    budget = Budget(args.max_operations, args.max_cost_microusd, args.timeout_seconds)
    _require_continuity_program(args.program_protocol, args.program)
    adapter = _adapter_identity(args.adapter, args.program, "correction", args.program_protocol)
    factory = _adapter_factory(args.adapter, args.program, "correction", args.program_protocol)
    outcomes: dict[str, int] = {}
    expected_state_outcomes: dict[str, dict[str, int]] = {"present": {}, "absent": {}}
    failures = []
    for index, scenario in enumerate(scenarios):
        evidence = run_bounded(scenario, factory, budget)
        status = evidence["status"]
        outcomes[status] = outcomes.get(status, 0) + 1
        expected_state = "absent" if scenario.expected is None else "present"
        state_outcomes = expected_state_outcomes[expected_state]
        state_outcomes[status] = state_outcomes.get(status, 0) + 1
        if status != "pass" and len(failures) < args.max_failures:
            failures.append(
                {
                    "index": index,
                    "scenario": scenario.name,
                    "fingerprint": scenario.fingerprint(),
                    "status": status,
                    "expected_state": expected_state,
                }
            )
    _check_program_unchanged(args.program, adapter)
    return {
        "data_source": (
            "generated synthetic scenarios with local executable"
            if args.program else "generated synthetic scenarios"
        ),
        "generator_version": args.generator_version,
        "seed": args.seed,
        "scenarios": args.scenarios,
        "max_events": args.max_events,
        "budget": asdict(budget),
        "adapter": adapter,
        "outcomes": outcomes,
        "expected_state_outcomes": expected_state_outcomes,
        "failure_count": args.scenarios - outcomes.get("pass", 0),
        "failures": failures,
    }


def _materialize(args) -> dict:
    if not 0 <= args.index < 10_000:
        raise ValueError("index must be between 0 and 9999")
    scenario = next(islice(
        iter_scenarios(args.seed, args.index + 1, args.max_events, args.generator_version),
        args.index, None,
    ))
    return {"version": 2, **asdict(scenario)}


def _show(args) -> dict:
    store = _existing_store(args.database)
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
    store = _existing_store(args.database)
    try:
        deleted = store.delete(args.run_id)
    finally:
        store.close()
    return {"run_id": args.run_id, "deleted": deleted}


def _prune(args) -> dict:
    store = _existing_store(args.database)
    try:
        deleted = store.retain_latest(args.keep)
    finally:
        store.close()
    return {"keep": args.keep, "deleted": deleted}


def _verify(args) -> dict:
    store = _existing_store(args.database)
    try:
        receipts = store.verify_integrity()
    finally:
        store.close()
    return {"integrity": "ok", "receipts": receipts}


def _existing_store(path: Path) -> EvidenceStore:
    if not path.is_file():
        raise ValueError(f"evidence database does not exist: {path}")
    return EvidenceStore(path)


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

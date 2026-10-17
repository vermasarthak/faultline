"""Repeated evaluation summaries and honest paired repair comparisons."""

from dataclasses import dataclass
from math import isfinite, sqrt
from time import monotonic
from typing import Callable

from .execution import EVIDENCE_STATUSES


@dataclass(frozen=True)
class TrialSummary:
    attempts: int
    outcomes: dict[str, int]
    fingerprints: frozenset[str]
    total_cost_microusd: int
    p50_latency_seconds: float
    p95_latency_seconds: float
    scored_trials: int
    pass_rate: float | None
    wilson_interval_95: tuple[float, float] | None


@dataclass(frozen=True)
class RepairComparison:
    baseline: TrialSummary
    candidate: TrialSummary
    pass_rate_delta: float | None
    verdict: str


def repeat(
    evaluate: Callable[[], dict],
    attempts: int,
    clock: Callable[[], float] = monotonic,
) -> TrialSummary:
    """Runs a supplied evaluation repeatedly without changing its input."""
    _validate_attempts(attempts)
    samples = [_evaluate_once(evaluate, clock) for _ in range(attempts)]
    return _summarize(samples, attempts)


def compare_repairs(
    baseline: Callable[[], dict],
    candidate: Callable[[], dict],
    attempts: int,
    clock: Callable[[], float] = monotonic,
) -> RepairComparison:
    """Interleaves isolated evaluations before comparing identical scenarios."""
    _validate_attempts(attempts)
    baseline_samples = []
    candidate_samples = []
    for _ in range(attempts):
        baseline_samples.append(_evaluate_once(baseline, clock))
        candidate_samples.append(_evaluate_once(candidate, clock))
    baseline_summary = _summarize(baseline_samples, attempts)
    candidate_summary = _summarize(candidate_samples, attempts)
    if baseline_summary.fingerprints != candidate_summary.fingerprints:
        raise ValueError("baseline and candidate must use identical scenario fingerprints")
    if baseline_summary.pass_rate is None or candidate_summary.pass_rate is None:
        delta = None
    else:
        delta = candidate_summary.pass_rate - baseline_summary.pass_rate
    if baseline_summary.scored_trials == 0:
        verdict = "inconclusive"
    elif candidate_summary.scored_trials < baseline_summary.scored_trials:
        verdict = "regression"
    elif delta is None:
        verdict = "inconclusive"
    elif delta < 0:
        verdict = "regression"
    else:
        verdict = "pass"
    return RepairComparison(baseline_summary, candidate_summary, delta, verdict)


def _validate_attempts(attempts: int) -> None:
    if not 1 <= attempts <= 10_000:
        raise ValueError("attempts must be between 1 and 10000")


def _evaluate_once(evaluate: Callable[[], dict], clock: Callable[[], float]) -> tuple[dict, float]:
    started = clock()
    evidence = evaluate()
    elapsed = clock() - started
    if not isfinite(elapsed) or elapsed < 0:
        raise ValueError("clock must produce a finite nondecreasing duration")
    return evidence, elapsed


def _summarize(samples: list[tuple[dict, float]], attempts: int) -> TrialSummary:
    outcomes: dict[str, int] = {}
    fingerprints: set[str] = set()
    latencies: list[float] = []
    total_cost = 0
    for evidence, elapsed in samples:
        if not isinstance(evidence, dict):
            raise TypeError("evaluation must return an evidence object")
        status = evidence.get("status")
        fingerprint = evidence.get("fingerprint")
        if not isinstance(status, str) or status not in EVIDENCE_STATUSES:
            raise ValueError("evidence.status is not recognized by schema version 1")
        _fingerprint(fingerprint)
        cost = evidence.get("cost_microusd", 0)
        if isinstance(cost, bool) or not isinstance(cost, int) or cost < 0:
            raise ValueError("evidence.cost_microusd must be a nonnegative integer")
        outcomes[status] = outcomes.get(status, 0) + 1
        fingerprints.add(fingerprint)
        latencies.append(elapsed)
        total_cost += cost
    if len(fingerprints) != 1:
        raise ValueError("repeated trials must use one stable scenario fingerprint")
    passes = outcomes.get("pass", 0)
    failures = outcomes.get("fail", 0)
    scored = passes + failures
    rate = passes / scored if scored else None
    return TrialSummary(
        attempts,
        outcomes,
        frozenset(fingerprints),
        total_cost,
        _percentile(latencies, 0.50),
        _percentile(latencies, 0.95),
        scored,
        rate,
        _wilson_interval(passes, scored) if scored else None,
    )


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return ordered[index]


def _fingerprint(value: object) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("evidence.fingerprint must be a SHA 256 hexadecimal fingerprint")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("evidence.fingerprint must be a SHA 256 hexadecimal fingerprint") from error


def _wilson_interval(passes: int, scored: int) -> tuple[float, float]:
    z = 1.959963984540054
    rate = passes / scored
    denominator = 1 + (z * z) / scored
    center = (rate + (z * z) / (2 * scored)) / denominator
    margin = z * sqrt((rate * (1 - rate) + (z * z) / (4 * scored)) / scored) / denominator
    return (max(0.0, center - margin), min(1.0, center + margin))

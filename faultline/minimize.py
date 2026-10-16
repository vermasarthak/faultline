"""Deterministic failure reduction that keeps direct probe dependencies."""

from dataclasses import dataclass
from typing import Callable

from .core import Event, Scenario


@dataclass(frozen=True)
class Minimization:
    original: Scenario
    minimized: Scenario
    target_status: str
    evaluations: int

    @property
    def removed_events(self) -> int:
        return len(self.original.events) - len(self.minimized.events)


def minimize_failure(
    scenario: Scenario,
    evaluate: Callable[[Scenario], dict],
    max_evaluations: int = 1_000,
) -> Minimization:
    """Uses bounded delta debugging while preserving direct probe dependencies."""
    if not 1 <= max_evaluations <= 100_000:
        raise ValueError("max_evaluations must be between 1 and 100000")
    target_status = _status(evaluate(scenario))
    evaluations = 1
    if target_status != "fail":
        raise ValueError("scenario must reproduce a fail status before minimization")
    events = list(scenario.events)
    granularity = 1
    while True:
        removable = [
            index for index, event in enumerate(events) if not _direct_dependency(scenario, event)
        ]
        if not removable:
            break
        removed = False
        for chunk in _chunks(removable, granularity):
            if evaluations >= max_evaluations:
                raise ValueError("minimization evaluation budget exhausted")
            candidates = [event for index, event in enumerate(events) if index not in chunk]
            candidate = Scenario(scenario.name, tuple(candidates), scenario.probe, scenario.expected)
            evaluations += 1
            if _status(evaluate(candidate)) == target_status:
                events = candidates
                granularity = max(1, granularity - 1)
                removed = True
                break
        if removed:
            continue
        if granularity >= len(removable):
            break
        granularity = min(len(removable), granularity * 2)
    minimized = Scenario(scenario.name, tuple(events), scenario.probe, scenario.expected)
    return Minimization(scenario, minimized, target_status, evaluations)


def _direct_dependency(scenario: Scenario, event: Event) -> bool:
    return (
        event.person == scenario.probe.person
        and event.field == scenario.probe.field
    )


def _chunks(indices: list[int], count: int) -> tuple[set[int], ...]:
    size = (len(indices) + count - 1) // count
    return tuple(set(indices[start:start + size]) for start in range(0, len(indices), size))


def _status(evidence: object) -> str:
    if not isinstance(evidence, dict):
        raise TypeError("evaluation must return an evidence object")
    status = evidence.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("evidence.status must be a nonempty string")
    return status

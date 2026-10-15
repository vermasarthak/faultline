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
    """Greedily removes irrelevant events while preserving the failing status."""
    if not 1 <= max_evaluations <= 100_000:
        raise ValueError("max_evaluations must be between 1 and 100000")
    target_status = _status(evaluate(scenario))
    evaluations = 1
    if target_status != "fail":
        raise ValueError("scenario must reproduce a fail status before minimization")
    events = list(scenario.events)
    index = 0
    while index < len(events):
        event = events[index]
        if _direct_dependency(scenario, event):
            index += 1
            continue
        if evaluations >= max_evaluations:
            raise ValueError("minimization evaluation budget exhausted")
        candidate_events = events[:index] + events[index + 1:]
        candidate = Scenario(scenario.name, tuple(candidate_events), scenario.probe, scenario.expected)
        evaluations += 1
        if _status(evaluate(candidate)) == target_status:
            events = candidate_events
        else:
            index += 1
    minimized = Scenario(scenario.name, tuple(events), scenario.probe, scenario.expected)
    return Minimization(scenario, minimized, target_status, evaluations)


def _direct_dependency(scenario: Scenario, event: Event) -> bool:
    return (
        event.person == scenario.probe.person
        and event.field == scenario.probe.field
    )


def _status(evidence: object) -> str:
    if not isinstance(evidence, dict):
        raise TypeError("evaluation must return an evidence object")
    status = evidence.get("status")
    if not isinstance(status, str) or not status:
        raise ValueError("evidence.status must be a nonempty string")
    return status

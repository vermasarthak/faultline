"""Bounded adapter execution with evidence for non memory failures."""

from dataclasses import dataclass
from math import isfinite
from time import monotonic
from typing import Callable, Protocol

from .core import Event, Probe, Scenario


@dataclass(frozen=True)
class Budget:
    max_operations: int
    max_cost_microusd: int
    timeout_seconds: float

    def __post_init__(self):
        if isinstance(self.max_operations, bool) or not isinstance(self.max_operations, int) or self.max_operations < 1:
            raise ValueError("max_operations must be a positive integer")
        if isinstance(self.max_cost_microusd, bool) or not isinstance(self.max_cost_microusd, int) or self.max_cost_microusd < 0:
            raise ValueError("max_cost_microusd must be a nonnegative integer")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")


@dataclass(frozen=True)
class Recall:
    value: str | None
    cost_microusd: int = 0


class Session(Protocol):
    def observe(self, event: Event, deadline: float) -> int: ...
    def recall(self, probe: Probe, deadline: float) -> Recall: ...
    def close(self) -> None: ...


def run_bounded(
    scenario: Scenario,
    open_session: Callable[[], Session],
    budget: Budget,
    clock: Callable[[], float] = monotonic,
) -> dict:
    """Runs one fresh session and records failure class without leaking errors."""
    started = clock()
    deadline = started + budget.timeout_seconds
    operations = 0
    cost = 0
    session = None
    try:
        session = open_session()
        for event in scenario.events:
            operations += 1
            state = _check_budget(operations, cost, budget, clock(), deadline)
            if state:
                return _non_memory_evidence(scenario, state, operations, cost)
            cost += _cost(session.observe(event, deadline))
            state = _check_budget(operations, cost, budget, clock(), deadline)
            if state:
                return _non_memory_evidence(scenario, state, operations, cost)
        operations += 1
        state = _check_budget(operations, cost, budget, clock(), deadline)
        if state:
            return _non_memory_evidence(scenario, state, operations, cost)
        response = session.recall(scenario.probe, deadline)
        if not isinstance(response, Recall):
            return _non_memory_evidence(scenario, "invalid_response", operations, cost)
        cost += _cost(response.cost_microusd)
        state = _check_budget(operations, cost, budget, clock(), deadline)
        if state:
            return _non_memory_evidence(scenario, state, operations, cost)
        if response.value is not None and not isinstance(response.value, str):
            return _non_memory_evidence(scenario, "invalid_response", operations, cost)
        if response.value is None:
            status = "pass" if scenario.expected is None else "unknown"
        else:
            status = "pass" if response.value == scenario.expected else "fail"
        return {
            "schema_version": 1,
            "scenario": scenario.name,
            "fingerprint": scenario.fingerprint(),
            "status": status,
            "expected": scenario.expected,
            "actual": response.value,
            "operations": operations,
            "cost_microusd": cost,
        }
    except Exception as error:
        return {
            **_non_memory_evidence(scenario, "provider_error", operations, cost),
            "error_type": type(error).__name__,
        }
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _cost(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("adapter cost must be a nonnegative integer")
    return value


def _check_budget(
    operations: int, cost: int, budget: Budget, now: float, deadline: float
) -> str | None:
    if operations > budget.max_operations:
        return "operation_budget_exceeded"
    if cost > budget.max_cost_microusd:
        return "cost_budget_exceeded"
    if now > deadline:
        return "timeout"
    return None


def _non_memory_evidence(
    scenario: Scenario, status: str, operations: int, cost: int
) -> dict:
    return {
        "schema_version": 1,
        "scenario": scenario.name,
        "fingerprint": scenario.fingerprint(),
        "status": status,
        "expected": scenario.expected,
        "actual": None,
        "operations": operations,
        "cost_microusd": cost,
    }

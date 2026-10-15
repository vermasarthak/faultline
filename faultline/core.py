from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Callable, Protocol


@dataclass(frozen=True)
class Event:
    person: str
    field: str
    value: str | None
    operation: str = "write"


@dataclass(frozen=True)
class Probe:
    person: str
    field: str


@dataclass(frozen=True)
class Scenario:
    name: str
    events: tuple[Event, ...]
    probe: Probe
    expected: str | None

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False)
        return sha256(payload.encode()).hexdigest()


class Memory(Protocol):
    def observe(self, event: Event) -> None: ...
    def recall(self, probe: Probe) -> str | None: ...


def run(scenario: Scenario, factory: Callable[[], Memory]) -> dict:
    memory = factory()
    for event in scenario.events:
        memory.observe(event)
    actual = memory.recall(scenario.probe)
    if actual is not None and not isinstance(actual, str):
        raise TypeError("adapter must return a string or None")
    if actual is None:
        status = "pass" if scenario.expected is None else "unknown"
    else:
        status = "pass" if actual == scenario.expected else "fail"
    return {
        "schema_version": 1,
        "scenario": scenario.name,
        "fingerprint": scenario.fingerprint(),
        "status": status,
        "expected": scenario.expected,
        "actual": actual,
        "events": [asdict(event) for event in scenario.events],
        "probe": asdict(scenario.probe),
    }

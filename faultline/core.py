from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from typing import Callable, Protocol


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


@dataclass(frozen=True)
class Event:
    person: str
    field: str
    value: str | None
    operation: str = "write"

    def __post_init__(self):
        _text(self.person, "event.person")
        _text(self.field, "event.field")
        if not isinstance(self.operation, str) or self.operation not in {"write", "delete"}:
            raise ValueError("event.operation must be write or delete")
        if self.operation == "write":
            _text(self.value, "event.value")
        elif self.value is not None:
            raise ValueError("event.value must be null for delete")


@dataclass(frozen=True)
class Probe:
    person: str
    field: str

    def __post_init__(self):
        _text(self.person, "probe.person")
        _text(self.field, "probe.field")


@dataclass(frozen=True)
class Scenario:
    name: str
    events: tuple[Event, ...]
    probe: Probe
    expected: str | None

    def __post_init__(self):
        _text(self.name, "scenario.name")
        if not isinstance(self.events, tuple):
            raise TypeError("scenario.events must be a tuple of Event values")
        if not all(isinstance(event, Event) for event in self.events):
            raise TypeError("scenario.events must be a tuple of Event values")
        if not isinstance(self.probe, Probe):
            raise TypeError("scenario.probe must be a Probe")
        if self.expected is not None:
            _text(self.expected, "scenario.expected")

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

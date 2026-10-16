"""Strict loading for versioned synthetic Faultline scenarios."""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from .core import Event, Probe, Scenario
from .strictjson import decode_object, read_limited


MAX_SCENARIO_BYTES = 256 * 1024
SCHEMA_VERSION = 2


@dataclass(frozen=True)
class ScenarioDocument:
    scenario: Scenario
    content_fingerprint: str


def load(path: str | Path) -> ScenarioDocument:
    source = Path(path)
    raw = read_limited(source, MAX_SCENARIO_BYTES, "scenario")
    payload = decode_object(raw, "scenario")
    if not isinstance(payload, dict):
        raise ValueError("scenario root must be an object")
    _exact_keys(payload, {"version", "name", "events", "probe", "expected"}, "scenario")
    version = payload["version"]
    if type(version) is not int or version not in (1, SCHEMA_VERSION):
        raise ValueError(f"unsupported scenario version {payload['version']!r}")
    name = _text(payload["name"], "scenario.name")
    expected = _expected(payload["expected"], version)
    if not isinstance(payload["events"], list) or not payload["events"]:
        raise ValueError("scenario.events must be a nonempty list")
    events = tuple(_event(item, index, version) for index, item in enumerate(payload["events"]))
    probe = _probe(payload["probe"])
    return ScenarioDocument(
        Scenario(name, events, probe, expected),
        sha256(raw).hexdigest(),
    )


def _event(value: object, index: int, version: int) -> Event:
    if not isinstance(value, dict):
        raise ValueError(f"scenario.events[{index}] must be an object")
    name = f"scenario.events[{index}]"
    if version == 1:
        _exact_keys(value, {"person", "field", "value"}, name)
        return Event(
            _text(value["person"], f"{name}.person"),
            _text(value["field"], f"{name}.field"),
            _text(value["value"], f"{name}.value"),
        )
    _exact_keys(value, {"person", "field", "operation", "value"}, name)
    operation = value["operation"]
    if not isinstance(operation, str) or operation not in {"write", "delete"}:
        raise ValueError(f"{name}.operation must be write or delete")
    event_value = value["value"]
    if operation == "write":
        event_value = _text(event_value, f"{name}.value")
    elif event_value is not None:
        raise ValueError(f"{name}.value must be null for delete")
    return Event(
        _text(value["person"], f"{name}.person"),
        _text(value["field"], f"{name}.field"),
        event_value,
        operation,
    )


def _expected(value: object, version: int) -> str | None:
    if value is None and version == SCHEMA_VERSION:
        return None
    return _text(value, "scenario.expected")


def _probe(value: object) -> Probe:
    if not isinstance(value, dict):
        raise ValueError("scenario.probe must be an object")
    _exact_keys(value, {"person", "field"}, "scenario.probe")
    return Probe(_text(value["person"], "scenario.probe.person"), _text(value["field"], "scenario.probe.field"))


def _exact_keys(value: dict, expected: set[str], name: str) -> None:
    found = set(value)
    if found != expected:
        raise ValueError(f"{name} keys must be exactly {sorted(expected)!r}")


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value

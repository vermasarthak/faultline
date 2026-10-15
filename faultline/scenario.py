"""Strict loading for versioned synthetic Faultline scenarios."""

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path

from .core import Event, Probe, Scenario


MAX_SCENARIO_BYTES = 256 * 1024
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ScenarioDocument:
    scenario: Scenario
    content_fingerprint: str


def load(path: str | Path) -> ScenarioDocument:
    source = Path(path)
    size = source.stat().st_size
    if size > MAX_SCENARIO_BYTES:
        raise ValueError(f"scenario exceeds {MAX_SCENARIO_BYTES} byte limit")
    raw = source.read_bytes()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("scenario is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValueError("scenario root must be an object")
    _exact_keys(payload, {"version", "name", "events", "probe", "expected"}, "scenario")
    if payload["version"] != SCHEMA_VERSION:
        raise ValueError(f"unsupported scenario version {payload['version']!r}")
    name = _text(payload["name"], "scenario.name")
    expected = _text(payload["expected"], "scenario.expected")
    if not isinstance(payload["events"], list) or not payload["events"]:
        raise ValueError("scenario.events must be a nonempty list")
    events = tuple(_event(item, index) for index, item in enumerate(payload["events"]))
    probe = _probe(payload["probe"])
    return ScenarioDocument(
        Scenario(name, events, probe, expected),
        sha256(raw).hexdigest(),
    )


def _event(value: object, index: int) -> Event:
    if not isinstance(value, dict):
        raise ValueError(f"scenario.events[{index}] must be an object")
    _exact_keys(value, {"person", "field", "value"}, f"scenario.events[{index}]")
    return Event(
        _text(value["person"], f"scenario.events[{index}].person"),
        _text(value["field"], f"scenario.events[{index}].field"),
        _text(value["value"], f"scenario.events[{index}].value"),
    )


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

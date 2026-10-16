"""Deterministic synthetic memory histories with an independent state oracle."""

from random import Random
from typing import Iterator

from .core import Event, Probe, Scenario


GENERATOR_VERSION = 2
_PEOPLE = ("Ari", "Bea", "Cy", "Dee")
_FIELDS = ("city", "language", "role")
_KEYS = tuple((person, field) for person in _PEOPLE for field in _FIELDS)


def generate_scenarios(
    seed: int, count: int, max_events: int, generator_version: int = GENERATOR_VERSION
) -> tuple[Scenario, ...]:
    """Materializes bounded histories for callers that need a tuple."""
    return tuple(iter_scenarios(seed, count, max_events, generator_version))


def iter_scenarios(
    seed: int, count: int, max_events: int, generator_version: int = GENERATOR_VERSION
) -> Iterator[Scenario]:
    """Yields reproducible histories without retaining the whole sweep."""
    _bounded_integer(seed, "seed", minimum=0, maximum=2**63 - 1)
    _bounded_integer(count, "count", minimum=1, maximum=10_000)
    _bounded_integer(max_events, "max_events", minimum=1, maximum=1_000)
    _bounded_integer(generator_version, "generator_version", minimum=1, maximum=GENERATOR_VERSION)
    random = Random(seed)
    for scenario_index in range(count):
        state: dict[tuple[str, str], str] = {}
        events = []
        for event_index in range(random.randint(1, max_events)):
            person = random.choice(_PEOPLE)
            field = random.choice(_FIELDS)
            key = (person, field)
            if random.randrange(4) == 0:
                events.append(Event(person, field, None, "delete"))
                state.pop(key, None)
            else:
                value = f"{person}-{field}-{scenario_index}-{event_index}"
                events.append(Event(person, field, value))
                state[key] = value
        if generator_version == 1:
            person = random.choice(_PEOPLE)
            field = random.choice(_FIELDS)
        else:
            person, field = _balanced_probe(random, scenario_index, events, state)
        yield Scenario(
            f"generated state {scenario_index}",
            tuple(events),
            Probe(person, field),
            state.get((person, field)),
        )


def _balanced_probe(
    random: Random, scenario_index: int, events: list[Event],
    state: dict[tuple[str, str], str],
) -> tuple[str, str]:
    touched = {(event.person, event.field) for event in events}
    if scenario_index % 2 == 0:
        if not state:
            key = random.choice(_KEYS)
            events[-1] = Event(*key, f"{key[0]}-{key[1]}-{scenario_index}-forced")
            state.update(_replay(events))
        writes: dict[tuple[str, str], int] = {}
        for event in events:
            if event.operation == "write":
                key = (event.person, event.field)
                writes[key] = writes.get(key, 0) + 1
        corrected = {key for key in state if writes.get(key, 0) >= 2}
        return random.choice(tuple(sorted(corrected or state)))
    absent = set(_KEYS) - set(state)
    if not absent:
        key = random.choice(_KEYS)
        events[-1] = Event(*key, None, "delete")
        state.clear()
        state.update(_replay(events))
        absent = {key}
    deleted = absent & touched
    return random.choice(tuple(sorted(deleted or absent)))


def _replay(events: list[Event]) -> dict[tuple[str, str], str]:
    state: dict[tuple[str, str], str] = {}
    for event in events:
        key = (event.person, event.field)
        if event.operation == "delete":
            state.pop(key, None)
        else:
            assert event.value is not None
            state[key] = event.value
    return state


def _bounded_integer(value: object, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")

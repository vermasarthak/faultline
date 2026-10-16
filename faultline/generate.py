"""Deterministic synthetic memory histories with an independent state oracle."""

from random import Random

from .core import Event, Probe, Scenario


GENERATOR_VERSION = 1
_PEOPLE = ("Ari", "Bea", "Cy", "Dee")
_FIELDS = ("city", "language", "role")


def generate_scenarios(seed: int, count: int, max_events: int) -> tuple[Scenario, ...]:
    """Generates bounded write and delete histories reproducibly from one seed."""
    _bounded_integer(seed, "seed", minimum=0, maximum=2**63 - 1)
    _bounded_integer(count, "count", minimum=1, maximum=10_000)
    _bounded_integer(max_events, "max_events", minimum=1, maximum=1_000)
    random = Random(seed)
    scenarios = []
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
        person = random.choice(_PEOPLE)
        field = random.choice(_FIELDS)
        scenarios.append(
            Scenario(
                f"generated state {scenario_index}",
                tuple(events),
                Probe(person, field),
                state.get((person, field)),
            )
        )
    return tuple(scenarios)


def _bounded_integer(value: object, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")

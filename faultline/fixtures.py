"""First party synthetic scenarios that isolate common memory failures."""

from pathlib import Path

from .core import Event, Probe
from .scenario import ScenarioDocument, load


FIXTURE_DIRECTORY = Path(__file__).parent.parent / "examples"
FIXTURE_NAMES = (
    "identity-confusion",
    "stale-fact",
    "invented-fact",
    "deletion",
)


def load_failure_fixtures() -> dict[str, ScenarioDocument]:
    return {
        name: load(FIXTURE_DIRECTORY / f"{name}.json")
        for name in FIXTURE_NAMES
    }


class LatestValueMemory:
    """Reference fixture with person scoped writes and explicit deletion."""

    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def observe(self, event: Event) -> None:
        key = (event.person, event.field)
        if event.operation == "delete":
            self.values.pop(key, None)
        else:
            assert event.value is not None
            self.values[key] = event.value

    def recall(self, probe: Probe) -> str | None:
        return self.values.get((probe.person, probe.field))

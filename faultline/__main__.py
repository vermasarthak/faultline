import json
from .core import Event, Probe, Scenario, run


class StaleMemory:
    def __init__(self):
        self.values = {}

    def observe(self, event):
        self.values.setdefault((event.person, event.field), event.value)

    def recall(self, probe):
        return self.values.get((probe.person, probe.field))


class CorrectionMemory(StaleMemory):
    def observe(self, event):
        self.values[(event.person, event.field)] = event.value


def main():
    scenario = Scenario("city correction", (
        Event("Mina", "city", "Rome"), Event("Mina", "city", "Oslo"),
    ), Probe("Mina", "city"), "Oslo")
    print(json.dumps({
        "data_source": "synthetic fixture",
        "stale": run(scenario, StaleMemory),
        "corrected": run(scenario, CorrectionMemory),
    }, indent=2))


if __name__ == "__main__":
    main()

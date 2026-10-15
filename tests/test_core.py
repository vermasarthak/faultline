import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from faultline.core import Event, Probe, Scenario, run
from faultline.__main__ import StaleMemory, CorrectionMemory
from faultline.scenario import load


class EvidenceTests(unittest.TestCase):
    def test_correction_repair_and_replay(self):
        scenario = Scenario("correction", (Event("A", "city", "Rome"),
                            Event("A", "city", "Oslo")), Probe("A", "city"), "Oslo")
        self.assertEqual(run(scenario, StaleMemory)["status"], "fail")
        evidence = run(scenario, CorrectionMemory)
        self.assertEqual(evidence["status"], "pass")
        self.assertEqual(evidence, run(scenario, CorrectionMemory))

    def test_person_boundaries_and_unknown(self):
        scenario = Scenario("identity", (Event("B", "city", "Oslo"),),
                            Probe("A", "city"), "Oslo")
        self.assertEqual(run(scenario, CorrectionMemory)["status"], "unknown")

    def test_fingerprint_covers_expected_answer(self):
        a = Scenario("empty", (), Probe("A", "city"), "Rome")
        b = Scenario("empty", (), Probe("A", "city"), "Oslo")
        self.assertNotEqual(a.fingerprint(), b.fingerprint())

    def test_runs_do_not_share_memory(self):
        first = Scenario("first", (Event("A", "city", "Rome"),), Probe("A", "city"), "Rome")
        second = Scenario("second", (), Probe("A", "city"), "Rome")
        run(first, CorrectionMemory)
        self.assertEqual(run(second, CorrectionMemory)["status"], "unknown")


class ScenarioLoadingTests(unittest.TestCase):
    def write(self, content):
        directory = TemporaryDirectory()
        path = Path(directory.name) / "scenario.json"
        path.write_text(content)
        self.addCleanup(directory.cleanup)
        return path

    def test_loads_versioned_scenario_and_keeps_content_fingerprint(self):
        path = self.write(
            '{"version":1,"name":"correction","events":[{"person":"Mina","field":"city","value":"Rome"}],"probe":{"person":"Mina","field":"city"},"expected":"Rome"}'
        )
        document = load(path)
        self.assertEqual(document.scenario.name, "correction")
        self.assertEqual(len(document.content_fingerprint), 64)

    def test_rejects_unknown_fields_and_ambiguous_values(self):
        unknown = self.write(
            '{"version":1,"name":"x","events":[{"person":"Mina","field":"city","value":"Rome","confidence":1}],"probe":{"person":"Mina","field":"city"},"expected":"Rome"}'
        )
        with self.assertRaisesRegex(ValueError, "exactly"):
            load(unknown)
        ambiguous = self.write(
            '{"version":1,"name":"x","events":[{"person":"Mina","field":"city","value":["Rome","Oslo"]}],"probe":{"person":"Mina","field":"city"},"expected":"Rome"}'
        )
        with self.assertRaisesRegex(ValueError, "nonempty string"):
            load(ambiguous)

    def test_rejects_unknown_schema_version_and_malformed_json(self):
        wrong_version = self.write(
            '{"version":2,"name":"x","events":[{"person":"Mina","field":"city","value":"Rome"}],"probe":{"person":"Mina","field":"city"},"expected":"Rome"}'
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            load(wrong_version)
        malformed = self.write("{")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            load(malformed)

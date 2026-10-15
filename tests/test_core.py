import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from faultline.core import Event, Probe, Scenario, run
from faultline.__main__ import StaleMemory, CorrectionMemory
from faultline.scenario import load
from faultline.execution import Budget, Recall, run_bounded


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


class AdapterExecutionTests(unittest.TestCase):
    def setUp(self):
        self.scenario = Scenario(
            "correction", (Event("Mina", "city", "Oslo"),), Probe("Mina", "city"), "Oslo"
        )
        self.budget = Budget(2, 10, 1)

    def test_each_run_starts_and_closes_an_isolated_session(self):
        sessions = []

        class Session:
            def __init__(self):
                self.closed = False
            def observe(self, event, deadline): return 1
            def recall(self, probe, deadline): return Recall("Oslo", 1)
            def close(self): self.closed = True

        def open_session():
            session = Session()
            sessions.append(session)
            return session

        self.assertEqual(run_bounded(self.scenario, open_session, self.budget)["status"], "pass")
        self.assertEqual(run_bounded(self.scenario, open_session, self.budget)["status"], "pass")
        self.assertEqual(len(sessions), 2)
        self.assertTrue(all(session.closed for session in sessions))

    def test_timeout_budget_and_provider_errors_are_not_memory_failures(self):
        class SlowSession:
            def observe(self, event, deadline): return 0
            def recall(self, probe, deadline): return Recall("Oslo", 0)
            def close(self): pass

        ticks = iter([0, 0, 2])
        timeout = run_bounded(self.scenario, SlowSession, self.budget, lambda: next(ticks))
        self.assertEqual(timeout["status"], "timeout")

        class CostlySession(SlowSession):
            def observe(self, event, deadline): return 11

        cost = run_bounded(self.scenario, CostlySession, self.budget)
        self.assertEqual(cost["status"], "cost_budget_exceeded")

        class BrokenSession(SlowSession):
            def observe(self, event, deadline): raise RuntimeError("secret provider text")

        error = run_bounded(self.scenario, BrokenSession, self.budget)
        self.assertEqual(error["status"], "provider_error")
        self.assertEqual(error["error_type"], "RuntimeError")
        self.assertNotIn("secret provider text", str(error))

    def test_operation_budget_stops_before_the_probe(self):
        scenario = Scenario(
            "two-events",
            (Event("Mina", "city", "Rome"), Event("Mina", "city", "Oslo")),
            Probe("Mina", "city"),
            "Oslo",
        )

        class Session:
            recalled = False

            def observe(self, event, deadline):
                return 0

            def recall(self, probe, deadline):
                self.recalled = True
                return Recall("Oslo", 0)

            def close(self):
                pass

        session = Session()
        evidence = run_bounded(scenario, lambda: session, Budget(2, 10, 1))
        self.assertEqual(evidence["status"], "operation_budget_exceeded")
        self.assertFalse(session.recalled)

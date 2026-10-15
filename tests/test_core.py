import unittest
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from faultline.core import Event, Probe, Scenario, run
from faultline.__main__ import StaleMemory, CorrectionMemory
from faultline.scenario import load
from faultline.execution import Budget, Recall, run_bounded
from faultline.fixtures import LatestValueMemory, load_failure_fixtures
from faultline.store import EvidenceStore


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
            '{"version":3,"name":"x","events":[{"person":"Mina","field":"city","value":"Rome"}],"probe":{"person":"Mina","field":"city"},"expected":"Rome"}'
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            load(wrong_version)
        malformed = self.write("{")
        with self.assertRaisesRegex(ValueError, "valid JSON"):
            load(malformed)

    def test_version_two_makes_deletion_and_expected_absence_explicit(self):
        path = self.write(
            '{"version":2,"name":"deletion","events":[{"person":"Mina","field":"city","operation":"write","value":"Oslo"},{"person":"Mina","field":"city","operation":"delete","value":null}],"probe":{"person":"Mina","field":"city"},"expected":null}'
        )
        scenario = load(path).scenario
        self.assertIsNone(scenario.expected)
        self.assertEqual(scenario.events[-1].operation, "delete")

    def test_rejects_invalid_version_two_operation_contract(self):
        path = self.write(
            '{"version":2,"name":"bad","events":[{"person":"Mina","field":"city","operation":"delete","value":"Oslo"}],"probe":{"person":"Mina","field":"city"},"expected":null}'
        )
        with self.assertRaisesRegex(ValueError, "must be null"):
            load(path)


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


class FailureFixtureTests(unittest.TestCase):
    def test_reference_fixture_passes_every_first_party_failure_family(self):
        for document in load_failure_fixtures().values():
            self.assertEqual(run(document.scenario, LatestValueMemory)["status"], "pass")

    def test_each_family_catches_its_named_broken_behavior(self):
        fixtures = load_failure_fixtures()

        class FieldOnlyMemory(LatestValueMemory):
            def __init__(self):
                self.values = {}

            def observe(self, event):
                if event.operation == "write":
                    self.values[event.field] = event.value

            def recall(self, probe):
                return self.values.get(probe.field)

        class FirstWriteMemory(LatestValueMemory):
            def observe(self, event):
                if event.operation == "write":
                    self.values.setdefault((event.person, event.field), event.value)

        class InventingMemory(LatestValueMemory):
            def recall(self, probe):
                return super().recall(probe) or "invented value"

        class DeletionBlindMemory(LatestValueMemory):
            def observe(self, event):
                if event.operation != "delete":
                    super().observe(event)

        cases = {
            "identity-confusion": FieldOnlyMemory,
            "stale-fact": FirstWriteMemory,
            "invented-fact": InventingMemory,
            "deletion": DeletionBlindMemory,
        }
        for name, broken_memory in cases.items():
            self.assertEqual(run(fixtures[name].scenario, broken_memory)["status"], "fail")


class EvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "evidence.db"
        self.scenario = Scenario(
            "stored correction",
            (Event("Mina", "city", "Oslo"),),
            Probe("Mina", "city"),
            "Oslo",
        )
        self.evidence = run(self.scenario, CorrectionMemory)
        self.content_fingerprint = "a" * 64

    def test_committed_evidence_survives_a_reopen(self):
        store = EvidenceStore(self.path)
        stored = store.record(
            self.evidence,
            self.content_fingerprint,
            run_id="committed",
            recorded_at="2026-10-16T00:00:00+00:00",
        )
        store.close()

        reopened = EvidenceStore(self.path)
        self.addCleanup(reopened.close)
        recovered = reopened.get("committed")
        self.assertEqual(recovered, stored)
        self.assertEqual(reopened.recent(), [stored])

    def test_interrupted_transaction_leaves_no_partial_run(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import os
import sqlite3
import sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('BEGIN IMMEDIATE')
connection.execute(
    'INSERT INTO runs (run_id, recorded_at, canonical_fingerprint, content_fingerprint, status, evidence_json) VALUES (?, ?, ?, ?, ?, ?)',
    ('partial', '2026-10-16T00:00:00+00:00', 'a' * 64, 'b' * 64, 'pass', '{}'),
)
os._exit(0)
""",
                str(self.path),
            ],
            check=False,
        )
        self.assertEqual(process.returncode, 0)
        self.assertIsNone(store.get("partial"))

    def test_store_rejects_unverifiable_fingerprints(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            store.record(self.evidence, "not a fingerprint")

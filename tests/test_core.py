import unittest
import subprocess
import sys
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from faultline.core import Event, Probe, Scenario, run
from faultline.__main__ import StaleMemory, CorrectionMemory
from faultline.scenario import MAX_SCENARIO_BYTES, load
from faultline.execution import Budget, Recall, run_bounded
from faultline.fixtures import LatestValueMemory, load_failure_fixtures
from faultline.store import EvidenceStore
from faultline.trials import compare_repairs, repeat
from faultline.heldout import MAX_MANIFEST_BYTES, compare_held_out, load_manifest, load_suite
from faultline.minimize import minimize_failure
from faultline.generate import generate_scenarios


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

    def test_programmatic_scenarios_enforce_the_document_contract(self):
        with self.assertRaisesRegex(ValueError, "event.value"):
            Event("Mina", "city", None)
        with self.assertRaisesRegex(ValueError, "event.operation"):
            Event("Mina", "city", "Oslo", "replace")
        with self.assertRaisesRegex(ValueError, "must be null"):
            Event("Mina", "city", "Oslo", "delete")
        with self.assertRaisesRegex(TypeError, "tuple"):
            Scenario("mutable", [Event("Mina", "city", "Oslo")], Probe("Mina", "city"), "Oslo")
        with self.assertRaisesRegex(ValueError, "scenario.expected"):
            Scenario("empty answer", (), Probe("Mina", "city"), "")


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

    def test_rejects_non_utf_eight_json_as_invalid_document(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "invalid.json"
        path.write_bytes(b"\xff")
        with self.assertRaisesRegex(ValueError, "scenario is not valid JSON"):
            load(path)

    def test_rejects_duplicate_root_and_nested_json_keys(self):
        duplicate_root = self.write(
            '{"version":1,"name":"x","name":"y","events":[{"person":"Mina","field":"city","value":"Rome"}],"probe":{"person":"Mina","field":"city"},"expected":"Rome"}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate key 'name'"):
            load(duplicate_root)
        duplicate_nested = self.write(
            '{"version":1,"name":"x","events":[{"person":"Mina","person":"Nia","field":"city","value":"Rome"}],"probe":{"person":"Mina","field":"city"},"expected":"Rome"}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate key 'person'"):
            load(duplicate_nested)

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

    def test_rejects_scenarios_larger_than_the_read_limit(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "large.json"
        path.write_bytes(b"x" * (MAX_SCENARIO_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "scenario exceeds"):
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
        self.assertEqual(error["error_stage"], "observe")
        self.assertNotIn("secret provider text", str(error))

        def broken_open():
            raise RuntimeError("secret open text")

        open_error = run_bounded(self.scenario, broken_open, self.budget)
        self.assertEqual(open_error["error_stage"], "open")
        self.assertNotIn("secret open text", str(open_error))

        class RecallBrokenSession(SlowSession):
            def recall(self, probe, deadline):
                raise RuntimeError("secret recall text")

        recall_error = run_bounded(self.scenario, RecallBrokenSession, self.budget)
        self.assertEqual(recall_error["error_stage"], "recall")
        self.assertNotIn("secret recall text", str(recall_error))

        class CloseBrokenSession(SlowSession):
            def close(self):
                raise RuntimeError("secret close text")

        close_error = run_bounded(self.scenario, CloseBrokenSession, self.budget)
        self.assertEqual(close_error["status"], "provider_error")
        self.assertEqual(close_error["error_type"], "RuntimeError")
        self.assertEqual(close_error["error_stage"], "close")
        self.assertNotIn("secret close text", str(close_error))

        class MalformedSession(SlowSession):
            def recall(self, probe, deadline):
                return "Oslo"

        malformed = run_bounded(self.scenario, MalformedSession, self.budget)
        self.assertEqual(malformed["status"], "invalid_response")

    def test_budget_rejects_boolean_fractional_and_nonfinite_values(self):
        with self.assertRaisesRegex(ValueError, "max_operations"):
            Budget(True, 0, 1)
        with self.assertRaisesRegex(ValueError, "max_cost_microusd"):
            Budget(1, 1.5, 1)
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            Budget(1, 0, float("inf"))

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


class GeneratedScenarioTests(unittest.TestCase):
    def test_generated_histories_are_deterministic_and_match_the_reference_oracle(self):
        scenarios = generate_scenarios(17, 100, 12)
        self.assertEqual(scenarios, generate_scenarios(17, 100, 12))
        self.assertTrue(all(run(scenario, CorrectionMemory)["status"] == "pass" for scenario in scenarios))
        self.assertGreater(
            sum(run(scenario, StaleMemory)["status"] == "fail" for scenario in scenarios),
            0,
        )

    def test_generator_rejects_unbounded_inputs(self):
        with self.assertRaisesRegex(ValueError, "seed"):
            generate_scenarios(-1, 1, 1)
        with self.assertRaisesRegex(ValueError, "count"):
            generate_scenarios(1, 10_001, 1)
        with self.assertRaisesRegex(ValueError, "max_events"):
            generate_scenarios(1, 1, 0)


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

    def test_store_rejects_malformed_evidence_fields(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        invalid_cases = (
            ({**self.evidence, "schema_version": 2}, "schema_version"),
            ({**self.evidence, "schema_version": 1.0}, "schema_version"),
            ({**self.evidence, "status": "unknown status"}, "status"),
            ({**self.evidence, "expected": 3}, "expected"),
            ({**self.evidence, "actual": 3}, "actual"),
            ({**self.evidence, "actual": "Rome"}, "pass evidence"),
            ({**self.evidence, "status": "unknown", "actual": "Oslo"}, "unknown evidence"),
            ({**self.evidence, "status": "provider_error", "actual": "Oslo"}, "execution outcome"),
            ({**self.evidence, "private_marker": "do not retain"}, "undeclared fields"),
            ({**self.evidence, "operations": -1}, "operations"),
            ({**self.evidence, "cost_microusd": True}, "cost_microusd"),
            ({**self.evidence, "error_stage": []}, "error_stage"),
            ({**self.evidence, "error_type": "TimeoutError"}, "provider_error"),
            ({**self.evidence, "events": [{**self.evidence["events"][0], "note": "extra"}]}, "undeclared fields"),
            ({**self.evidence, "events": []}, "fingerprint"),
        )
        for evidence, message in invalid_cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                store.record(evidence, self.content_fingerprint)

    def test_delete_and_retention_remove_local_receipts(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        for index in range(3):
            store.record(
                self.evidence,
                self.content_fingerprint,
                run_id=f"run-{index}",
                recorded_at=f"2026-10-16T00:00:0{index}+00:00",
            )
        self.assertEqual(store.retain_latest(2), 1)
        self.assertIsNone(store.get("run-0"))
        self.assertTrue(store.delete("run-2"))
        self.assertFalse(store.delete("run-2"))

    def test_separate_store_connections_serialize_concurrent_writers(self):
        def write(index):
            store = EvidenceStore(self.path)
            try:
                return store.record(
                    self.evidence,
                    self.content_fingerprint,
                    run_id=f"concurrent-{index}",
                    recorded_at=f"2026-10-16T00:01:{index:02d}+00:00",
                ).run_id
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=4) as executor:
            run_ids = list(executor.map(write, range(12)))
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        self.assertEqual({item.run_id for item in store.recent(20)}, set(run_ids))

    def test_retention_does_not_depend_on_one_bind_parameter_per_receipt(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        with store.write_transaction() as connection:
            connection.executemany(
                """
                INSERT INTO runs (
                    run_id, recorded_at, canonical_fingerprint, content_fingerprint,
                    status, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        f"bulk-{index}",
                        "2026-10-16T00:00:00+00:00",
                        self.evidence["fingerprint"],
                        "b" * 64,
                        "pass",
                        json.dumps(self.evidence),
                    )
                    for index in range(1_100)
                ],
            )
        self.assertEqual(store.retain_latest(1), 1_099)
        self.assertEqual(len(store.recent(20)), 1)

    def test_corrupt_receipt_payload_is_explicitly_detected(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        store.record(self.evidence, self.content_fingerprint, run_id="corrupt")
        self.assertEqual(store.verify_integrity(), 1)
        with store.write_transaction() as connection:
            connection.execute("UPDATE runs SET evidence_json = ? WHERE run_id = ?", ("{", "corrupt"))
        with self.assertRaisesRegex(ValueError, "stored evidence JSON is corrupt"):
            store.get("corrupt")
        with self.assertRaisesRegex(ValueError, "stored evidence JSON is corrupt"):
            store.verify_integrity()

    def test_semantically_corrupt_receipt_is_explicitly_detected(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        store.record(self.evidence, self.content_fingerprint, run_id="semantic")
        corrupted = {**self.evidence, "actual": "Rome"}
        with store.write_transaction() as connection:
            connection.execute(
                "UPDATE runs SET evidence_json = ? WHERE run_id = ?",
                (json.dumps(corrupted), "semantic"),
            )
        with self.assertRaisesRegex(ValueError, "stored evidence payload is corrupt"):
            store.get("semantic")
        with self.assertRaisesRegex(ValueError, "stored evidence payload is corrupt"):
            store.verify_integrity()

    def test_corrupt_receipt_metadata_is_explicitly_detected(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        store.record(self.evidence, self.content_fingerprint, run_id="metadata")
        with store.write_transaction() as connection:
            connection.execute("UPDATE runs SET status = ? WHERE run_id = ?", ("fail", "metadata"))
        with self.assertRaisesRegex(ValueError, "stored receipt status is corrupt"):
            store.get("metadata")
        with self.assertRaisesRegex(ValueError, "stored receipt status is corrupt"):
            store.verify_integrity()


class TrialTests(unittest.TestCase):
    fingerprint = "a" * 64

    def evidence(self, status, fingerprint=None, cost=0):
        return {
            "status": status,
            "fingerprint": fingerprint or self.fingerprint,
            "cost_microusd": cost,
        }

    def test_repeat_separates_quality_from_infrastructure_outcomes(self):
        results = iter([
            self.evidence("pass", cost=2),
            self.evidence("fail", cost=3),
            self.evidence("provider_error", cost=0),
        ])
        ticks = iter([0, 0.1, 1, 1.3, 2, 2.5])
        summary = repeat(lambda: next(results), 3, lambda: next(ticks))
        self.assertEqual(summary.outcomes, {"pass": 1, "fail": 1, "provider_error": 1})
        self.assertEqual(summary.scored_trials, 2)
        self.assertEqual(summary.pass_rate, 0.5)
        self.assertEqual(summary.total_cost_microusd, 5)
        self.assertAlmostEqual(summary.p50_latency_seconds, 0.3)
        self.assertAlmostEqual(summary.p95_latency_seconds, 0.5)
        self.assertIsNotNone(summary.wilson_interval_95)

    def test_repair_comparison_requires_identical_scenario_evidence(self):
        baseline = iter([self.evidence("pass"), self.evidence("fail")])
        candidate = iter([self.evidence("pass"), self.evidence("pass")])
        comparison = compare_repairs(lambda: next(baseline), lambda: next(candidate), 2)
        self.assertEqual(comparison.pass_rate_delta, 0.5)
        self.assertEqual(comparison.verdict, "pass")

        with self.assertRaisesRegex(ValueError, "identical scenario"):
            compare_repairs(
                lambda: self.evidence("pass"),
                lambda: self.evidence("pass", fingerprint="b" * 64),
                1,
            )

    def test_repair_gate_rejects_provider_outage_and_lost_coverage(self):
        baseline = lambda: self.evidence("pass")
        outage = lambda: self.evidence("provider_error")
        comparison = compare_repairs(baseline, outage, 3)
        self.assertIsNone(comparison.pass_rate_delta)
        self.assertEqual(comparison.verdict, "regression")

        candidate = iter([self.evidence("pass"), self.evidence("provider_error")])
        comparison = compare_repairs(baseline, lambda: next(candidate), 2)
        self.assertEqual(comparison.pass_rate_delta, 0.0)
        self.assertEqual(comparison.verdict, "regression")

        comparison = compare_repairs(outage, outage, 2)
        self.assertEqual(comparison.verdict, "inconclusive")

        comparison = compare_repairs(outage, baseline, 2)
        self.assertEqual(comparison.verdict, "inconclusive")

    def test_repair_comparison_interleaves_attempts(self):
        order = []

        def baseline():
            order.append("baseline")
            return self.evidence("pass")

        def candidate():
            order.append("candidate")
            return self.evidence("pass")

        compare_repairs(baseline, candidate, 3)
        self.assertEqual(
            order,
            ["baseline", "candidate", "baseline", "candidate", "baseline", "candidate"],
        )

    def test_repeat_rejects_a_clock_regression(self):
        ticks = iter([1, 0])
        with self.assertRaisesRegex(ValueError, "clock moved backwards"):
            repeat(lambda: self.evidence("pass"), 1, lambda: next(ticks))

    def test_repeat_rejects_mixed_scenario_fingerprints(self):
        results = iter([
            self.evidence("pass"),
            self.evidence("fail", fingerprint="b" * 64),
        ])
        with self.assertRaisesRegex(ValueError, "one stable scenario fingerprint"):
            repeat(lambda: next(results), 2)

    def test_repeat_rejects_boolean_costs_and_malformed_fingerprints(self):
        with self.assertRaisesRegex(ValueError, "cost_microusd"):
            repeat(lambda: self.evidence("pass", cost=True), 1)
        with self.assertRaisesRegex(ValueError, "SHA 256"):
            repeat(lambda: self.evidence("pass", fingerprint="not a fingerprint"), 1)
        with self.assertRaisesRegex(ValueError, "not recognized"):
            repeat(lambda: self.evidence("passed"), 1)


class HeldOutTests(unittest.TestCase):
    def setUp(self):
        self.examples = Path(__file__).parent.parent / "examples"
        self.suite = load_suite(
            self.examples / "manifests" / "development.json",
            self.examples / "manifests" / "held-out.json",
        )

    def test_held_out_report_uses_one_budget_and_stable_manifest_order(self):
        class Session:
            def __init__(self, ignore_deletes=False):
                self.values = {}
                self.ignore_deletes = ignore_deletes

            def observe(self, event, deadline):
                key = (event.person, event.field)
                if event.operation == "delete":
                    if not self.ignore_deletes:
                        self.values.pop(key, None)
                else:
                    self.values[key] = event.value
                return 0

            def recall(self, probe, deadline):
                return Recall(self.values.get((probe.person, probe.field)), 0)

            def close(self):
                pass

        budget = Budget(10, 0, 1)
        report = compare_held_out(
            self.suite,
            lambda: Session(ignore_deletes=True),
            Session,
            budget,
            1,
        )
        self.assertEqual(report.budget, budget)
        self.assertEqual([item.scenario for item in report.comparisons], ["invented fact", "deletion"])
        self.assertEqual(report.comparisons[0].comparison.pass_rate_delta, 0.0)
        self.assertEqual(report.comparisons[1].comparison.pass_rate_delta, 1.0)

    def test_manifest_overlap_is_rejected(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        development = Path(directory.name) / "development.json"
        held_out = Path(directory.name) / "held-out.json"
        contents = '{"version":1,"name":"set","scenarios":["deletion.json"]}'
        development.write_text(contents)
        held_out.write_text(contents)
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            load_suite(development, held_out, self.examples)

    def test_manifest_rejects_duplicate_json_keys(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manifest = Path(directory.name) / "manifest.json"
        manifest.write_text(
            '{"version":1,"name":"first","name":"second","scenarios":["deletion.json"]}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate key 'name'"):
            load_manifest(manifest, self.examples)

    def test_manifest_read_is_bounded(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manifest = Path(directory.name) / "manifest.json"
        manifest.write_bytes(b"x" * (MAX_MANIFEST_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "manifest exceeds"):
            load_manifest(manifest, self.examples)


class MinimizationTests(unittest.TestCase):
    def test_removes_a_distractor_but_keeps_the_correction_history(self):
        scenario = Scenario(
            "correction with distractor",
            (
                Event("Nia", "city", "Berlin"),
                Event("Mina", "city", "Rome"),
                Event("Mina", "city", "Oslo"),
            ),
            Probe("Mina", "city"),
            "Oslo",
        )
        result = minimize_failure(scenario, lambda item: run(item, StaleMemory))
        self.assertEqual(result.target_status, "fail")
        self.assertEqual(result.removed_events, 1)
        self.assertEqual(
            result.minimized.events,
            (Event("Mina", "city", "Rome"), Event("Mina", "city", "Oslo")),
        )
        self.assertEqual(run(result.minimized, StaleMemory)["status"], "fail")

    def test_keeps_a_non_direct_event_when_removal_stops_reproduction(self):
        scenario = Scenario(
            "dependency",
            (Event("Nia", "flag", "on"), Event("Mina", "city", "Oslo")),
            Probe("Mina", "city"),
            "Oslo",
        )

        def evaluate(item):
            return {"status": "fail" if item.events[0].person == "Nia" else "pass"}

        result = minimize_failure(scenario, evaluate)
        self.assertEqual(result.removed_events, 0)

    def test_removes_an_interacting_group_that_greedy_reduction_misses(self):
        scenario = Scenario(
            "interacting distractors",
            (
                Event("Nia", "city", "Berlin"),
                Event("Noor", "city", "Paris"),
                Event("Mina", "city", "Rome"),
                Event("Mina", "city", "Oslo"),
            ),
            Probe("Mina", "city"),
            "Oslo",
        )

        def evaluate(item):
            people = {event.person for event in item.events}
            return {"status": "fail" if ("Nia" in people) == ("Noor" in people) else "pass"}

        result = minimize_failure(scenario, evaluate)
        self.assertEqual(result.removed_events, 2)
        self.assertEqual(
            result.minimized.events,
            (Event("Mina", "city", "Rome"), Event("Mina", "city", "Oslo")),
        )

    def test_rejects_a_scenario_that_does_not_fail_before_reduction(self):
        scenario = Scenario(
            "passing",
            (Event("Mina", "city", "Oslo"),),
            Probe("Mina", "city"),
            "Oslo",
        )
        with self.assertRaisesRegex(ValueError, "must reproduce"):
            minimize_failure(scenario, lambda item: run(item, CorrectionMemory))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent.parent
        self.scenario = self.root / "examples" / "correction.json"
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def test_run_prints_synthetic_evidence_and_persists_a_receipt(self):
        database = Path(self.directory.name) / "evidence.db"
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "run",
                "--scenario",
                str(self.scenario),
                "--adapter",
                "correction",
                "--attempts",
                "2",
                "--database",
                str(database),
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["data_source"], "synthetic scenario")
        self.assertEqual(report["summary"]["outcomes"], {"pass": 2})
        self.assertTrue(report["stored_run_id"])
        store = EvidenceStore(database)
        self.addCleanup(store.close)
        self.assertEqual(store.get(report["stored_run_id"]).evidence["status"], "pass")
        stored = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "show",
                "--database",
                str(database),
                "--run-id",
                report["stored_run_id"],
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(stored.returncode, 0, stored.stderr)
        self.assertEqual(json.loads(stored.stdout)["evidence"]["status"], "pass")
        verified = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "verify",
                "--database",
                str(database),
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(json.loads(verified.stdout), {"integrity": "ok", "receipts": 1})
        deleted = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "delete",
                "--database",
                str(database),
                "--run-id",
                report["stored_run_id"],
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertTrue(json.loads(deleted.stdout)["deleted"])

    def test_run_rejects_an_unsupported_adapter(self):
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "run",
                "--scenario",
                str(self.scenario),
                "--adapter",
                "remote",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("unsupported adapter", process.stderr)

    def test_compare_reports_held_out_synthetic_repair_evidence(self):
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "compare",
                "--development-manifest",
                str(self.root / "examples" / "manifests" / "development.json"),
                "--held-out-manifest",
                str(self.root / "examples" / "manifests" / "held-out.json"),
                "--baseline",
                "stale",
                "--candidate",
                "correction",
                "--attempts",
                "2",
                "--fail-on-regression",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["data_source"], "synthetic manifests")
        self.assertEqual(report["development_manifest"], "development")
        self.assertEqual(report["held_out_manifest"], "held out")
        self.assertEqual(report["attempts"], 2)
        self.assertEqual(report["regression_count"], 0)
        self.assertEqual([item["scenario"] for item in report["comparisons"]], ["invented fact", "deletion"])
        self.assertEqual(report["comparisons"][1]["pass_rate_delta"], 1.0)

        regression = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "compare",
                "--development-manifest",
                str(self.root / "examples" / "manifests" / "development.json"),
                "--held-out-manifest",
                str(self.root / "examples" / "manifests" / "held-out.json"),
                "--baseline",
                "correction",
                "--candidate",
                "stale",
                "--fail-on-regression",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(regression.returncode, 1, regression.stderr)
        self.assertEqual(json.loads(regression.stdout)["regression_count"], 1)

    def test_minimize_reports_a_smaller_synthetic_counterexample(self):
        scenario = Path(self.directory.name) / "distractor.json"
        scenario.write_text(
            '{"version":1,"name":"distractor","events":[{"person":"Nia","field":"city","value":"Berlin"},{"person":"Mina","field":"city","value":"Rome"},{"person":"Mina","field":"city","value":"Oslo"}],"probe":{"person":"Mina","field":"city"},"expected":"Oslo"}'
        )
        process = subprocess.run(
            [sys.executable, "-m", "faultline", "minimize", "--scenario", str(scenario), "--adapter", "stale"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["target_status"], "fail")
        self.assertEqual(report["removed_events"], 1)
        self.assertEqual(len(report["minimized"]["events"]), 2)

    def test_sweep_reports_reproducible_generated_failures(self):
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "sweep",
                "--adapter",
                "stale",
                "--seed",
                "17",
                "--scenarios",
                "100",
                "--max-events",
                "12",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["data_source"], "generated synthetic scenarios")
        self.assertEqual(report["seed"], 17)
        self.assertEqual(report["scenarios"], 100)
        self.assertGreater(report["failure_count"], 0)
        self.assertEqual(report["failure_count"], len(report["failures"]))
        failure = report["failures"][0]

        materialized = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "materialize",
                "--seed",
                "17",
                "--index",
                str(failure["index"]),
                "--max-events",
                "12",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(materialized.returncode, 0, materialized.stderr)
        scenario_path = Path(self.directory.name) / "materialized.json"
        scenario_path.write_text(materialized.stdout)
        self.assertEqual(load(scenario_path).scenario.fingerprint(), failure["fingerprint"])

        invalid_budget = subprocess.run(
            [
                sys.executable,
                "-m",
                "faultline",
                "sweep",
                "--max-events",
                "2",
                "--max-operations",
                "2",
            ],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(invalid_budget.returncode, 2)
        self.assertIn("reserve a probe", invalid_budget.stderr)

    def test_maintenance_commands_do_not_create_a_missing_database(self):
        commands = (
            ("show", "--run-id", "missing"),
            ("delete", "--run-id", "missing"),
            ("prune", "--keep", "1"),
            ("verify",),
        )
        for index, command in enumerate(commands):
            database = Path(self.directory.name) / f"missing-{index}.db"
            process = subprocess.run(
                [sys.executable, "-m", "faultline", command[0], "--database", str(database), *command[1:]],
                cwd=self.root,
                text=True,
                capture_output=True,
                check=False,
            )
            with self.subTest(command=command[0]):
                self.assertEqual(process.returncode, 2)
                self.assertIn("evidence database does not exist", process.stderr)
                self.assertFalse(database.exists())

    def test_benchmark_reports_only_synthetic_harness_results(self):
        process = subprocess.run(
            [sys.executable, "-m", "faultline", "benchmark", "--iterations", "3"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["data_source"], "synthetic fixture")
        self.assertEqual(report["scope"], "single process core harness only")
        self.assertEqual(len(report["scenario_fingerprint"]), 64)
        self.assertTrue(report["runtime"]["implementation"])
        self.assertTrue(report["runtime"]["python_version"])
        self.assertEqual(report["stale"]["outcomes"], {"fail": 3})
        self.assertEqual(report["correction"]["outcomes"], {"pass": 3})
        self.assertGreater(report["stale"]["elapsed_seconds"], 0)

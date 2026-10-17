import unittest
import subprocess
import sys
import json
import os
import sqlite3
import stat
import weakref
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from faultline.core import Event, Probe, Scenario, run
from faultline.__main__ import StaleMemory, CorrectionMemory
from faultline.scenario import MAX_SCENARIO_BYTES, load
from faultline.execution import Budget, Recall, run_bounded
from faultline.fixtures import LatestValueMemory, load_failure_fixtures
from faultline.store import EvidenceStore
from faultline.trials import compare_repairs, repeat
from faultline.heldout import MAX_MANIFEST_BYTES, compare_held_out, load_manifest, load_suite
from faultline.minimize import minimize_failure
from faultline.generate import generate_scenarios, iter_scenarios


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
        for version in (True, [], 1.0):
            path = self.write(json.dumps({
                "version": version, "name": "x",
                "events": [{"person": "Mina", "field": "city", "value": "Rome"}],
                "probe": {"person": "Mina", "field": "city"}, "expected": "Rome",
            }))
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, "unsupported"):
                load(path)
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
        malformed = self.write(
            '{"version":2,"name":"bad","events":[{"person":"Mina","field":"city","operation":[],"value":"Oslo"}],"probe":{"person":"Mina","field":"city"},"expected":"Oslo"}'
        )
        with self.assertRaisesRegex(ValueError, "operation must be"):
            load(malformed)

    def test_rejects_scenarios_larger_than_the_read_limit(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "large.json"
        path.write_bytes(b"x" * (MAX_SCENARIO_BYTES + 1))
        with self.assertRaisesRegex(ValueError, "scenario exceeds"):
            load(path)

    def test_schema_type_matrix_never_raises_an_internal_type_error(self):
        base = {
            "version": 2, "name": "case",
            "events": [{"person": "Mina", "field": "city", "operation": "write", "value": "Oslo"}],
            "probe": {"person": "Mina", "field": "city"}, "expected": "Oslo",
        }
        values = (None, True, False, 0, 1.0, "", [], {}, ["write"], {"x": 1})
        for location in ("version", "name", "events", "probe", "expected",
                         "person", "field", "operation", "value"):
            for value in values:
                payload = deepcopy(base)
                if location in {"person", "field", "operation", "value"}:
                    payload["events"][0][location] = value
                else:
                    payload[location] = value
                with self.subTest(location=location, value=value):
                    try:
                        load(self.write(json.dumps(payload)))
                    except ValueError:
                        pass


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

    def test_malformed_cost_is_an_invalid_response_at_each_adapter_stage(self):
        class Session:
            def __init__(self, observe_cost=0, recall_cost=0):
                self.observe_cost = observe_cost
                self.recall_cost = recall_cost

            def observe(self, event, deadline):
                return self.observe_cost

            def recall(self, probe, deadline):
                return Recall("Oslo", self.recall_cost)

            def close(self):
                pass

        for value in (-1, True, 1.5, "1", None):
            with self.subTest(stage="observe", value=value):
                result = run_bounded(
                    self.scenario, lambda value=value: Session(observe_cost=value), self.budget
                )
                self.assertEqual(result["status"], "invalid_response")
                self.assertNotIn("error_type", result)
            with self.subTest(stage="recall", value=value):
                result = run_bounded(
                    self.scenario, lambda value=value: Session(recall_cost=value), self.budget
                )
                self.assertEqual(result["status"], "invalid_response")
                self.assertNotIn("error_type", result)

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
        self.assertEqual(scenarios, tuple(iter_scenarios(17, 100, 12)))
        self.assertTrue(all(run(scenario, CorrectionMemory)["status"] == "pass" for scenario in scenarios))
        self.assertGreater(
            sum(run(scenario, StaleMemory)["status"] == "fail" for scenario in scenarios),
            0,
        )

    def test_balanced_probes_expose_an_always_unknown_adapter(self):
        class AlwaysUnknownMemory(CorrectionMemory):
            def recall(self, probe):
                return None

        cases = generate_scenarios(17, 100, 12)
        self.assertEqual(sum(case.expected is None for case in cases), 50)
        self.assertEqual(sum(case.expected is not None for case in cases), 50)
        self.assertTrue(all(run(case, CorrectionMemory)["status"] == "pass" for case in cases))
        self.assertEqual(
            sum(run(case, AlwaysUnknownMemory)["status"] == "pass" for case in cases), 50,
        )

    def test_legacy_generator_fingerprints_remain_replayable(self):
        cases = generate_scenarios(17, 100, 12, generator_version=1)
        self.assertEqual(cases[0].fingerprint(), "5cdcaa7eb54019b52ee7e5b5fa2b73923a73f984afcec05710c87fe563224c38")
        self.assertEqual(cases[42].fingerprint(), "a2e5479a6995844ba7271005ebddec7c4baf625ec9ee0ce2ce83ba6080137829")

    def test_stream_does_not_retain_completed_scenarios(self):
        stream = iter_scenarios(17, 10_000, 2)
        first = next(stream)
        reference = weakref.ref(first)
        del first
        self.assertIsNone(reference())
        self.assertEqual(next(stream).name, "generated state 1")

    def test_generator_rejects_unbounded_inputs(self):
        with self.assertRaisesRegex(ValueError, "seed"):
            generate_scenarios(-1, 1, 1)
        with self.assertRaisesRegex(ValueError, "count"):
            generate_scenarios(1, 10_001, 1)
        with self.assertRaisesRegex(ValueError, "max_events"):
            generate_scenarios(1, 1, 0)
        with self.assertRaisesRegex(ValueError, "generator_version"):
            generate_scenarios(1, 1, 1, generator_version=3)


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

    def test_new_database_and_journal_files_are_owner_only(self):
        process = subprocess.run(
            [sys.executable, "-c", """
import json
import os
from pathlib import Path
import stat
import sys
from faultline.store import EvidenceStore
os.umask(0)
path = Path(sys.argv[1])
store = EvidenceStore(path)
print(json.dumps({item.name: stat.S_IMODE(item.stat().st_mode)
                  for item in path.parent.iterdir()}))
store.close()
""", str(self.path)],
            cwd=Path(__file__).parent.parent, text=True, capture_output=True, check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        modes = json.loads(process.stdout)
        self.assertEqual(set(modes), {"evidence.db", "evidence.db-wal", "evidence.db-shm"})
        self.assertEqual(set(modes.values()), {0o600})

    def test_existing_database_permissions_are_not_changed(self):
        self.path.touch()
        os.chmod(self.path, 0o644)
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

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

    def test_repeated_receipts_commit_together_and_preserve_ordered_ids(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        stored = store.record_many([self.evidence, self.evidence], self.content_fingerprint)
        self.assertEqual(len(stored), 2)
        self.assertNotEqual(stored[0].run_id, stored[1].run_id)
        self.assertEqual(store.verify_integrity(), 2)
        self.assertEqual([store.get(item.run_id) for item in stored], stored)

    def test_repeated_receipts_roll_back_on_validation_or_insert_failure(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        invalid = {**self.evidence, "actual": "wrong"}
        with self.assertRaisesRegex(ValueError, "pass evidence"):
            store.record_many([self.evidence, invalid], self.content_fingerprint)
        self.assertEqual(store.verify_integrity(), 0)
        with patch("faultline.store.uuid4", side_effect=["collision", "collision"]):
            with self.assertRaises(sqlite3.IntegrityError):
                store.record_many([self.evidence, self.evidence], self.content_fingerprint)
        self.assertEqual(store.verify_integrity(), 0)

    def test_repeated_receipts_reject_empty_and_oversized_batches(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        with self.assertRaisesRegex(ValueError, "batch size"):
            store.record_many([], self.content_fingerprint)
        with self.assertRaisesRegex(ValueError, "batch size"):
            store.record_many([self.evidence] * 10_001, self.content_fingerprint)
        self.assertEqual(store.verify_integrity(), 0)

    def test_large_repeated_batch_survives_reopen(self):
        store = EvidenceStore(self.path)
        stored = store.record_many([self.evidence] * 1_000, self.content_fingerprint)
        store.close()
        self.assertEqual(len({item.run_id for item in stored}), 1_000)
        reopened = EvidenceStore(self.path)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.verify_integrity(), 1_000)

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

    def test_store_rejects_invalid_receipt_identity_and_timestamp(self):
        store = EvidenceStore(self.path)
        self.addCleanup(store.close)
        for run_id in ("", "   ", 1):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(ValueError, "run_id"):
                store.record(self.evidence, self.content_fingerprint, run_id=run_id)
        for recorded_at in ("", "not a timestamp", "2026-10-16T00:00:00", 1):
            with self.subTest(recorded_at=recorded_at), self.assertRaisesRegex(
                ValueError, "recorded_at"
            ):
                store.record(
                    self.evidence, self.content_fingerprint,
                    run_id="valid", recorded_at=recorded_at,
                )
        self.assertEqual(store.verify_integrity(), 0)

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
            ({**self.evidence, "error_type": "TimeoutError"}, "must appear together"),
            ({**self.evidence, "status": "provider_error", "actual": None}, "requires error_type"),
            ({
                **self.evidence,
                "status": "provider_error",
                "actual": None,
                "error_type": "RuntimeError",
            }, "must appear together"),
            ({**self.evidence, "adapter": {"kind": "local_program", "sha256": "bad"}}, "sha256"),
            ({
                **self.evidence,
                "adapter": {
                    "kind": "http_endpoint",
                    "sha256": "a" * 64,
                    "protocol": "snapshot_v1",
                },
            }, "adapter has invalid fields"),
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
        self.assertEqual(store.verify_integrity(), 1_100)
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

    def test_corrupt_receipt_identity_metadata_is_explicitly_detected(self):
        for field, value, message in (
            ("run_id", "", "stored run_id"),
            ("recorded_at", "not a timestamp", "stored recorded_at"),
        ):
            with self.subTest(field=field):
                path = Path(self.directory.name) / f"{field}.db"
                store = EvidenceStore(path)
                store.record(self.evidence, self.content_fingerprint, run_id="metadata")
                with store.write_transaction() as connection:
                    connection.execute(f"UPDATE runs SET {field} = ?", (value,))
                with self.assertRaisesRegex(ValueError, message):
                    store.verify_integrity()
                store.close()


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
        with self.assertRaisesRegex(ValueError, "finite nondecreasing"):
            repeat(lambda: self.evidence("pass"), 1, lambda: next(ticks))
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError, "finite nondecreasing"
            ):
                ticks = iter([0, invalid])
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

    def test_renaming_a_case_cannot_bypass_the_held_out_split(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        original = json.loads((self.examples / "deletion.json").read_text())
        renamed = {**original, "name": "same case under another name"}
        (root / "original.json").write_text(json.dumps(original))
        (root / "renamed.json").write_text(json.dumps(renamed))
        development = root / "development.json"
        held_out = root / "held-out.json"
        development.write_text(json.dumps({"version": 1, "name": "development", "scenarios": ["original.json"]}))
        held_out.write_text(json.dumps({"version": 1, "name": "held out", "scenarios": ["renamed.json"]}))
        self.assertNotEqual(load(root / "original.json").scenario.fingerprint(), load(root / "renamed.json").scenario.fingerprint())
        with self.assertRaisesRegex(ValueError, "cannot overlap"):
            load_suite(development, held_out, root)

        held_out.write_text(json.dumps({"version": 1, "name": "held out", "scenarios": ["original.json", "renamed.json"]}))
        with self.assertRaisesRegex(ValueError, "cannot repeat a scenario"):
            load_manifest(held_out, root)

    def test_manifest_rejects_duplicate_json_keys(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manifest = Path(directory.name) / "manifest.json"
        manifest.write_text(
            '{"version":1,"name":"first","name":"second","scenarios":["deletion.json"]}'
        )
        with self.assertRaisesRegex(ValueError, "duplicate key 'name'"):
            load_manifest(manifest, self.examples)

    def test_manifest_rejects_boolean_version_and_paths_outside_fixture_root(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        manifest = root / "manifest.json"
        manifest.write_text('{"version":true,"name":"set","scenarios":["case.json"]}')
        with self.assertRaisesRegex(ValueError, "unsupported manifest version"):
            load_manifest(manifest, root)

        manifest.write_text('{"version":1,"name":"set","scenarios":[".."]}')
        with self.assertRaisesRegex(ValueError, "plain file names"):
            load_manifest(manifest, root)

        (root / "case.json").symlink_to(self.examples / "correction.json")
        manifest.write_text('{"version":1,"name":"set","scenarios":["case.json"]}')
        with self.assertRaisesRegex(ValueError, "escapes fixture directory"):
            load_manifest(manifest, root)

    def test_manifest_type_matrix_never_raises_an_internal_type_error(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        manifest = Path(directory.name) / "manifest.json"
        base = {"version": 1, "name": "set", "scenarios": ["deletion.json"]}
        for field in base:
            for value in (None, True, False, 0, 1.0, "", [], {}, ["deletion.json"]):
                payload = {**base, field: value}
                manifest.write_text(json.dumps(payload))
                with self.subTest(field=field, value=value):
                    try:
                        load_manifest(manifest, self.examples)
                    except (OSError, ValueError):
                        pass

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
        self.assertEqual(len(report["stored_run_ids"]), 2)
        self.assertEqual(report["stored_run_id"], report["stored_run_ids"][-1])
        store = EvidenceStore(database)
        self.addCleanup(store.close)
        self.assertEqual([store.get(run_id).evidence["status"] for run_id in report["stored_run_ids"]], ["pass", "pass"])
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
        self.assertEqual(json.loads(verified.stdout), {"integrity": "ok", "receipts": 2})
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
        self.assertEqual(report["generator_version"], 2)
        self.assertEqual(sum(report["expected_state_outcomes"]["present"].values()), 50)
        self.assertEqual(sum(report["expected_state_outcomes"]["absent"].values()), 50)
        self.assertGreater(report["failure_count"], 0)
        self.assertEqual(len(report["failures"]), min(report["failure_count"], 20))
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

        legacy = subprocess.run(
            [sys.executable, "-m", "faultline", "materialize", "--seed", "17", "--index", "42",
             "--max-events", "12", "--generator-version", "1"],
            cwd=self.root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        scenario_path.write_text(legacy.stdout)
        self.assertEqual(load(scenario_path).scenario.fingerprint(), "a2e5479a6995844ba7271005ebddec7c4baf625ec9ee0ce2ce83ba6080137829")

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

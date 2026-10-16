"""Failure tests for the local executable boundary."""

import json
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

from faultline.core import Event, Probe, Scenario
from faultline.execution import Budget, run_bounded
from faultline.process_adapter import ProcessSession
from faultline.store import EvidenceStore


class ProcessAdapterTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.scenario = Scenario(
            "correction",
            (Event("Mina", "city", "Rome"), Event("Mina", "city", "Oslo")),
            Probe("Mina", "city"),
            "Oslo",
        )

    def program(self, body: str) -> Path:
        path = self.root / "adapter.py"
        path.write_text("#!/usr/bin/env python3\n" + body)
        path.chmod(0o700)
        return path

    def evaluate(self, program: Path, timeout: float = 2, cost: int = 0) -> dict:
        return run_bounded(
            self.scenario,
            lambda: ProcessSession(program),
            Budget(100, cost, timeout),
        )

    def test_program_sees_no_expected_answer_and_is_fresh_each_attempt(self):
        program = self.program(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "assert set(request) == {'version', 'events', 'probe'}\n"
            "assert request['events'][-1]['value'] == 'Oslo'\n"
            "print(json.dumps({'version': 1, 'value': 'Oslo', 'cost_microusd': 0}))\n"
        )
        for _ in range(3):
            self.assertEqual(self.evaluate(program)["status"], "pass")

    def test_hung_program_is_terminated_within_deadline(self):
        program = self.program("import time\ntime.sleep(5)\n")
        started = monotonic()
        evidence = self.evaluate(program, timeout=0.1)
        self.assertEqual(evidence["status"], "timeout")
        self.assertLess(monotonic() - started, 2)

    def test_unbounded_output_is_rejected_before_it_fills_memory(self):
        program = self.program("import os\nos.write(1, b'x' * 200000)\n")
        self.assertEqual(self.evaluate(program)["status"], "invalid_response")

    def test_oversized_request_is_rejected_without_launching_program(self):
        marker = self.root / "launched"
        program = self.program(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\n"
        )
        scenario = Scenario(
            "large", (Event("Mina", "note", "x" * (300 * 1024)),),
            Probe("Mina", "note"), None,
        )
        result = run_bounded(scenario, lambda: ProcessSession(program), Budget(2, 0, 2))
        self.assertEqual(result["status"], "invalid_response")
        self.assertFalse(marker.exists())

    def test_large_valid_request_is_transferred_without_pipe_deadlock(self):
        program = self.program(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "print(json.dumps({'version': 1, 'value': str(len(request['events'][0]['value'])), "
            "'cost_microusd': 0}))\n"
        )
        scenario = Scenario(
            "large", (Event("Mina", "note", "x" * (200 * 1024)),),
            Probe("Mina", "note"), str(200 * 1024),
        )
        result = run_bounded(scenario, lambda: ProcessSession(program), Budget(2, 0, 2))
        self.assertEqual(result["status"], "pass")

    def test_timeout_terminates_descendant_in_the_process_group(self):
        ready = self.root / "ready"
        escaped = self.root / "escaped"
        child = (
            "import time; from pathlib import Path; "
            f"time.sleep(2); Path({str(escaped)!r}).write_text('escaped')"
        )
        program = self.program(
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
            f"open({str(ready)!r}, 'w').close()\n"
            "time.sleep(5)\n"
        )
        self.assertEqual(self.evaluate(program, timeout=1)["status"], "timeout")
        self.assertTrue(ready.exists())
        sleep(1.2)
        self.assertFalse(escaped.exists())

    def test_malformed_duplicate_and_expensive_responses_are_distinct(self):
        for body, expected in (
            ("print('{')\n", "invalid_response"),
            ("print('{\"version\":1,\"version\":1,\"value\":null,\"cost_microusd\":0}')\n", "invalid_response"),
            ("print('{\"version\":1,\"value\":\"Oslo\",\"cost_microusd\":7}')\n", "cost_budget_exceeded"),
            ("raise RuntimeError('secret should not be printed')\n", "provider_error"),
        ):
            with self.subTest(expected=expected, body=body):
                result = self.evaluate(self.program(body))
                self.assertEqual(result["status"], expected)
                self.assertNotIn("secret should not be printed", str(result))

    def test_cli_rejects_candidate_outage_instead_of_passing_gate(self):
        program = self.program("raise RuntimeError('offline')\n")
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [
                sys.executable, "-m", "faultline", "compare",
                "--development-manifest", str(root / "examples/manifests/development.json"),
                "--held-out-manifest", str(root / "examples/manifests/held-out.json"),
                "--candidate-program", str(program), "--fail-on-regression",
            ],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(process.returncode, 1, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["regression_count"], 2)
        self.assertEqual({item["verdict"] for item in report["comparisons"]}, {"regression"})

    def test_example_sqlite_program_handles_correction(self):
        program = Path(__file__).resolve().parents[1] / "examples/sqlite_memory.py"
        self.assertEqual(self.evaluate(program)["status"], "pass")

    def test_receipt_identifies_the_executable_bytes(self):
        root = Path(__file__).resolve().parents[1]
        program = root / "examples/sqlite_memory.py"
        database = self.root / "evidence.db"
        process = subprocess.run(
            [sys.executable, "-m", "faultline", "run", "--scenario",
             str(root / "examples/correction.json"), "--program", str(program),
             "--database", str(database)],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        identity = {"kind": "local_program", "sha256": sha256(program.read_bytes()).hexdigest()}
        self.assertEqual(report["adapter"], identity)
        store = EvidenceStore(database)
        try:
            self.assertEqual(store.get(report["stored_run_id"]).evidence["adapter"], identity)
        finally:
            store.close()

    def test_changed_executable_cannot_produce_a_receipt(self):
        program = self.program(
            "import json, sys\n"
            "json.load(sys.stdin)\n"
            "with open(sys.argv[0], 'a') as source: source.write('# changed\\n')\n"
            "print('{\"version\":1,\"value\":\"Oslo\",\"cost_microusd\":0}')\n"
        )
        database = self.root / "evidence.db"
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [sys.executable, "-m", "faultline", "run", "--scenario",
             str(root / "examples/correction.json"), "--program", str(program),
             "--database", str(database)],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("changed during evaluation", process.stderr)
        self.assertFalse(database.exists())

    def test_external_program_can_be_swept_and_its_failure_minimized(self):
        root = Path(__file__).resolve().parents[1]
        sqlite_program = root / "examples/sqlite_memory.py"
        sweep = subprocess.run(
            [
                sys.executable, "-m", "faultline", "sweep", "--program", str(sqlite_program),
                "--seed", "7", "--scenarios", "20", "--max-events", "10",
                "--max-operations", "11",
            ],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(sweep.returncode, 0, sweep.stderr)
        self.assertEqual(json.loads(sweep.stdout)["outcomes"], {"pass": 20})

        stale_program = self.program(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "probe = request['probe']\n"
            "values = [e['value'] for e in request['events'] if "
            "e['person'] == probe['person'] and e['field'] == probe['field']]\n"
            "print(json.dumps({'version': 1, 'value': values[0] if values else None, "
            "'cost_microusd': 0}))\n"
        )
        scenario = self.root / "scenario.json"
        scenario.write_text(json.dumps({
            "version": 2, "name": "distracted correction",
            "events": [
                {"person": "Other", "field": "city", "value": "Paris", "operation": "write"},
                {"person": "Mina", "field": "city", "value": "Rome", "operation": "write"},
                {"person": "Mina", "field": "city", "value": "Oslo", "operation": "write"},
            ],
            "probe": {"person": "Mina", "field": "city"}, "expected": "Oslo",
        }))
        minimized = subprocess.run(
            [sys.executable, "-m", "faultline", "minimize", "--scenario", str(scenario),
             "--program", str(stale_program)],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(minimized.returncode, 0, minimized.stderr)
        result = json.loads(minimized.stdout)
        self.assertEqual(result["target_status"], "fail")
        self.assertEqual(result["removed_events"], 1)
        self.assertEqual(len(result["minimized"]["events"]), 2)

"""Failure tests for the local executable boundary."""

import json
import os
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
from time import monotonic, sleep
import unittest

from faultline.core import Event, Probe, Scenario
from faultline.execution import Budget, run_bounded
from faultline.http_adapter import HttpSnapshotSession, normalize_endpoint
from faultline.process_adapter import ContinuitySession, ProcessSession
from faultline.store import EvidenceStore


class _AdapterHandler(BaseHTTPRequestHandler):
    requests = []
    response = {"version": 1, "value": "Oslo", "cost_microusd": 7}

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).requests.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        if self.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", "/evaluate")
            self.end_headers()
            return
        if self.path == "/slow":
            self.send_response(200)
            self.end_headers()
            for byte in json.dumps(type(self).response).encode():
                try:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                except BrokenPipeError:
                    return
                sleep(0.02)
            return
        encoded = json.dumps(type(self).response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format, *args):
        pass


class HttpAdapterTests(unittest.TestCase):
    def setUp(self):
        _AdapterHandler.requests = []
        _AdapterHandler.response = {"version": 1, "value": "Oslo", "cost_microusd": 7}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _AdapterHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/evaluate"
        self.scenario = Scenario(
            "correction",
            (Event("Mina", "city", "Rome"), Event("Mina", "city", "Oslo")),
            Probe("Mina", "city"),
            "Oslo",
        )

    def test_remote_snapshot_hides_expected_and_sends_bearer_token(self):
        result = run_bounded(
            self.scenario,
            lambda: HttpSnapshotSession(self.endpoint, "secret-token"),
            Budget(3, 7, 2),
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["cost_microusd"], 7)
        path, authorization, request = _AdapterHandler.requests[0]
        self.assertEqual(path, "/evaluate")
        self.assertEqual(authorization, "Bearer secret-token")
        self.assertEqual(set(request), {"version", "events", "probe"})
        self.assertNotIn("expected", request)

    def test_remote_snapshot_rejects_malformed_response(self):
        _AdapterHandler.response = {"version": 1, "value": "Oslo", "cost_microusd": True}
        result = run_bounded(
            self.scenario,
            lambda: HttpSnapshotSession(self.endpoint),
            Budget(3, 0, 2),
        )
        self.assertEqual(result["status"], "invalid_response")

    def test_remote_snapshot_rejects_redirects_and_enforces_overall_deadline(self):
        redirect = run_bounded(
            self.scenario,
            lambda: HttpSnapshotSession(self.endpoint.replace("/evaluate", "/redirect")),
            Budget(3, 0, 2),
        )
        self.assertEqual(redirect["status"], "provider_error")
        started = monotonic()
        slow = run_bounded(
            self.scenario,
            lambda: HttpSnapshotSession(self.endpoint.replace("/evaluate", "/slow")),
            Budget(3, 0, 0.1),
        )
        self.assertEqual(slow["status"], "timeout")
        self.assertLess(monotonic() - started, 0.5)

    def test_endpoint_rejects_secret_bearing_and_insecure_remote_urls(self):
        for endpoint in (
            "https://user:secret@example.com/evaluate",
            "https://example.com/evaluate?token=secret",
            "https://example.com/evaluate#secret",
            "http://example.com/evaluate",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                normalize_endpoint(endpoint)

    def test_remote_adapter_identity_can_be_persisted(self):
        result = run_bounded(
            self.scenario,
            lambda: HttpSnapshotSession(self.endpoint),
            Budget(3, 7, 2),
        )
        result["adapter"] = {
            "kind": "http_endpoint",
            "sha256": sha256(self.endpoint.encode()).hexdigest(),
            "protocol": "snapshot_v1",
        }
        with TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db")
            stored = store.record(result, "a" * 64)
            self.assertEqual(store.get(stored.run_id).evidence["adapter"], result["adapter"])
            store.close()

    def test_cli_remote_run_reports_hashed_identity_without_token(self):
        environment = dict(os.environ, FAULTLINE_TEST_TOKEN="secret-token")
        result = subprocess.run(
            [
                sys.executable, "-m", "faultline", "run",
                "--scenario", "examples/correction.json",
                "--endpoint", self.endpoint,
                "--auth-token-env", "FAULTLINE_TEST_TOKEN",
                "--max-cost-microusd", "7",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            env=environment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["last_evidence"]["status"], "pass")
        self.assertEqual(report["adapter"]["kind"], "http_endpoint")
        self.assertNotIn(self.endpoint, result.stdout)
        self.assertNotIn("secret-token", result.stdout)


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

    def evaluate(self, program: Path, timeout: float = 5, cost: int = 0) -> dict:
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
        result = run_bounded(scenario, lambda: ProcessSession(program), Budget(2, 0, 5))
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

    def test_continuity_requires_state_across_fresh_processes(self):
        program = Path(__file__).resolve().parents[1] / "examples/sqlite_continuity.py"
        result = run_bounded(self.scenario, lambda: ContinuitySession(program), Budget(3, 0, 5))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["operations"], 3)
        transient = self.program(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "response = {'version': 2, 'cost_microusd': 0}\n"
            "if request['operation'] == 'recall': response['value'] = None\n"
            "print(json.dumps(response))\n"
        )
        result = run_bounded(self.scenario, lambda: ContinuitySession(transient), Budget(3, 0, 5))
        self.assertEqual(result["status"], "unknown")

    def test_continuity_does_not_replay_history_or_expected_answer(self):
        program = self.program(
            "import json, os, pathlib, sys\n"
            "request = json.load(sys.stdin)\n"
            "assert set(request) == ({'version', 'operation', 'event'} if "
            "request['operation'] == 'observe' else {'version', 'operation', 'probe'})\n"
            "state = pathlib.Path(sys.argv[1]) / 'state'\n"
            "if request['operation'] == 'observe':\n"
            "    assert not state.exists() or int(state.read_text().split(':')[0]) != os.getpid()\n"
            "    state.write_text(str(os.getpid()) + ':' + request['event']['value'])\n"
            "    response = {'version': 2, 'cost_microusd': 0}\n"
            "else:\n"
            "    assert int(state.read_text().split(':')[0]) != os.getpid()\n"
            "    response = {'version': 2, 'value': state.read_text().split(':')[1], "
            "'cost_microusd': 0}\n"
            "print(json.dumps(response))\n"
        )
        for _ in range(2):
            result = run_bounded(self.scenario, lambda: ContinuitySession(program), Budget(3, 0, 5))
            self.assertEqual(result["status"], "pass")

    def test_continuity_rejects_bad_response_and_hung_observe(self):
        malformed = self.program(
            "import json, sys\n"
            "json.load(sys.stdin)\n"
            "print('{\"version\":2,\"cost_microusd\":0,\"unexpected\":true}')\n"
        )
        result = run_bounded(self.scenario, lambda: ContinuitySession(malformed), Budget(3, 0, 2))
        self.assertEqual(result["status"], "invalid_response")
        hung = self.program("import time\ntime.sleep(5)\n")
        sessions = []

        def open_session():
            session = ContinuitySession(hung)
            sessions.append(session)
            return session

        started = monotonic()
        result = run_bounded(self.scenario, open_session, Budget(3, 0, 0.1))
        self.assertEqual(result["status"], "timeout")
        self.assertLess(monotonic() - started, 2)
        self.assertFalse(Path(sessions[0].state.name).exists())

    def test_continuity_charges_observations_and_honors_deletion(self):
        program = self.program(
            "import json, pathlib, sys\n"
            "request = json.load(sys.stdin)\n"
            "state = pathlib.Path(sys.argv[1]) / 'value'\n"
            "if request['operation'] == 'observe':\n"
            "    event = request['event']\n"
            "    if event['operation'] == 'delete': state.unlink(missing_ok=True)\n"
            "    else: state.write_text(event['value'])\n"
            "    response = {'version': 2, 'cost_microusd': 1}\n"
            "else:\n"
            "    response = {'version': 2, 'value': state.read_text() if state.exists() "
            "else None, 'cost_microusd': 0}\n"
            "print(json.dumps(response))\n"
        )
        scenario = Scenario(
            "deleted", (Event("Mina", "city", "Rome"), Event("Mina", "city", None, "delete")),
            Probe("Mina", "city"), None,
        )
        factory = lambda: ContinuitySession(program)
        passed = run_bounded(scenario, factory, Budget(3, 2, 5))
        self.assertEqual((passed["status"], passed["cost_microusd"]), ("pass", 2))
        exceeded = run_bounded(scenario, factory, Budget(3, 1, 5))
        self.assertEqual((exceeded["status"], exceeded["cost_microusd"]), ("cost_budget_exceeded", 2))

    def test_continuity_requires_a_program(self):
        root = Path(__file__).resolve().parents[1]
        process = subprocess.run(
            [sys.executable, "-m", "faultline", "run", "--scenario",
             str(root / "examples/correction.json"), "--program-protocol", "continuity"],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(process.returncode, 2)
        self.assertIn("requires a local program", process.stderr)

    def test_continuity_cli_stores_protocol_identity(self):
        root = Path(__file__).resolve().parents[1]
        program = root / "examples/sqlite_continuity.py"
        database = self.root / "evidence.db"
        process = subprocess.run(
            [sys.executable, "-m", "faultline", "run", "--scenario",
             str(root / "examples/correction.json"), "--program", str(program),
             "--program-protocol", "continuity", "--database", str(database)],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        report = json.loads(process.stdout)
        self.assertEqual(report["last_evidence"]["status"], "pass")
        self.assertEqual(report["adapter"]["protocol"], "continuity_v2")
        store = EvidenceStore(database)
        try:
            self.assertEqual(store.get(report["stored_run_id"]).evidence["adapter"], report["adapter"])
            self.assertEqual(store.verify_integrity(), 1)
        finally:
            store.close()

    def test_continuity_generated_sweep_and_repair_gate(self):
        root = Path(__file__).resolve().parents[1]
        durable = root / "examples/sqlite_continuity.py"
        sweep = subprocess.run(
            [sys.executable, "-m", "faultline", "sweep", "--program", str(durable),
             "--program-protocol", "continuity", "--seed", "11", "--scenarios", "20",
             "--max-events", "8", "--max-operations", "9"],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(sweep.returncode, 0, sweep.stderr)
        self.assertEqual(json.loads(sweep.stdout)["outcomes"], {"pass": 20})

        transient = self.program(
            "import json, sys\n"
            "request = json.load(sys.stdin)\n"
            "response = {'version': 2, 'cost_microusd': 0}\n"
            "if request['operation'] == 'recall': response['value'] = 'Rome'\n"
            "print(json.dumps(response))\n"
        )
        comparison = subprocess.run(
            [sys.executable, "-m", "faultline", "compare", "--development-manifest",
             str(root / "examples/manifests/development.json"), "--held-out-manifest",
             str(root / "examples/manifests/held-out.json"), "--baseline-program", str(durable),
             "--candidate-program", str(transient), "--program-protocol", "continuity",
             "--fail-on-regression"],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(comparison.returncode, 1, comparison.stderr)
        report = json.loads(comparison.stdout)
        self.assertEqual(report["regression_count"], 2)
        self.assertEqual(report["baseline_adapter"]["protocol"], "continuity_v2")
        self.assertEqual(report["candidate_adapter"]["protocol"], "continuity_v2")

    def test_continuity_failure_can_be_minimized(self):
        root = Path(__file__).resolve().parents[1]
        stale = self.program(
            "import json, pathlib, sys\n"
            "request = json.load(sys.stdin)\n"
            "state = pathlib.Path(sys.argv[1]) / 'first.json'\n"
            "facts = json.loads(state.read_text()) if state.exists() else {}\n"
            "if request['operation'] == 'observe':\n"
            "    event = request['event']\n"
            "    facts.setdefault(event['person'] + ':' + event['field'], event['value'])\n"
            "    state.write_text(json.dumps(facts))\n"
            "    response = {'version': 2, 'cost_microusd': 0}\n"
            "else:\n"
            "    probe = request['probe']\n"
            "    response = {'version': 2, 'cost_microusd': 0, "
            "'value': facts.get(probe['person'] + ':' + probe['field'])}\n"
            "print(json.dumps(response))\n"
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
             "--program", str(stale), "--program-protocol", "continuity"],
            cwd=root, text=True, capture_output=True, check=False,
        )
        self.assertEqual(minimized.returncode, 0, minimized.stderr)
        report = json.loads(minimized.stdout)
        self.assertEqual(report["target_status"], "fail")
        self.assertEqual(report["removed_events"], 1)
        self.assertEqual(report["adapter"]["protocol"], "continuity_v2")

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

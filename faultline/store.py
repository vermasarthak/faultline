"""Durable local storage for completed Faultline evidence."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from time import monotonic, sleep
from typing import Iterator, Sequence
from uuid import uuid4

from .core import Event, Probe, Scenario
from .execution import EVIDENCE_STATUSES

LATEST_SCHEMA_VERSION = 1
CONNECTION_SETUP_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class StoredRun:
    run_id: str
    recorded_at: str
    content_fingerprint: str
    evidence: dict


class EvidenceStore:
    """A local SQLite store with explicit write transactions."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
        self.connection = sqlite3.connect(self.path, timeout=CONNECTION_SETUP_TIMEOUT_SECONDS)
        self.connection.row_factory = sqlite3.Row
        try:
            self._configure_connection()
            self._migrate()
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def record(
        self,
        evidence: dict,
        content_fingerprint: str,
        run_id: str | None = None,
        recorded_at: str | None = None,
    ) -> StoredRun:
        payload = _validated_evidence(evidence)
        _fingerprint(content_fingerprint, "content_fingerprint")
        run_id = str(uuid4()) if run_id is None else run_id
        recorded_at = datetime.now(timezone.utc).isoformat() if recorded_at is None else recorded_at
        _run_id(run_id)
        _recorded_at(recorded_at)
        stored = StoredRun(run_id, recorded_at, content_fingerprint, payload)
        with self.write_transaction() as connection:
            _insert_run(connection, stored)
        return stored

    def record_many(self, evidences: Sequence[dict], content_fingerprint: str) -> list[StoredRun]:
        """Commits all repeated trial receipts or none of them."""
        if not 1 <= len(evidences) <= 10_000:
            raise ValueError("evidence batch size must be between 1 and 10000")
        payloads = [_validated_evidence(evidence) for evidence in evidences]
        _fingerprint(content_fingerprint, "content_fingerprint")
        stored = [
            StoredRun(
                str(uuid4()), datetime.now(timezone.utc).isoformat(),
                content_fingerprint, payload,
            )
            for payload in payloads
        ]
        with self.write_transaction() as connection:
            for item in stored:
                _insert_run(connection, item)
        return stored

    def get(self, run_id: str) -> StoredRun | None:
        row = self.connection.execute(
            "SELECT run_id, recorded_at, canonical_fingerprint, content_fingerprint, status, evidence_json "
            "FROM runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return _stored_run(row)

    def recent(self, limit: int = 20) -> list[StoredRun]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = self.connection.execute(
            "SELECT run_id, recorded_at, canonical_fingerprint, content_fingerprint, status, evidence_json "
            "FROM runs ORDER BY recorded_at DESC, run_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_stored_run(row) for row in rows]

    def delete(self, run_id: str) -> bool:
        """Deletes one local receipt and reports whether it existed."""
        with self.write_transaction() as connection:
            result = connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        return result.rowcount == 1

    def retain_latest(self, keep: int) -> int:
        """Deletes older receipts, retaining the newest deterministic ordering."""
        if not 1 <= keep <= 100_000:
            raise ValueError("keep must be between 1 and 100000")
        with self.write_transaction() as connection:
            result = connection.execute(
                """
                DELETE FROM runs WHERE run_id IN (
                    SELECT run_id FROM runs
                    ORDER BY recorded_at DESC, run_id DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (keep,),
            )
        return result.rowcount

    def verify_integrity(self) -> int:
        """Verifies SQLite structure and every persisted evidence payload."""
        result = self.connection.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise ValueError("SQLite integrity check failed")
        rows = self.connection.execute(
            "SELECT run_id, recorded_at, canonical_fingerprint, content_fingerprint, status, evidence_json "
            "FROM runs"
        )
        count = 0
        for row in rows:
            _stored_run(row)
            count += 1
        return count

    @contextmanager
    def write_transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _migrate(self) -> None:
        current = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if current > LATEST_SCHEMA_VERSION:
            raise ValueError("database schema is newer than this Faultline version")
        if current == LATEST_SCHEMA_VERSION:
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            current = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if current > LATEST_SCHEMA_VERSION:
                raise ValueError("database schema is newer than this Faultline version")
            if current == LATEST_SCHEMA_VERSION:
                self.connection.commit()
                return
            self.connection.execute(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    recorded_at TEXT NOT NULL,
                    canonical_fingerprint TEXT NOT NULL,
                    content_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    evidence_json TEXT NOT NULL
                )
                """
            )
            self.connection.execute(
                "CREATE INDEX runs_by_recorded_at ON runs(recorded_at DESC, run_id DESC)"
            )
            self.connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION}")
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def _configure_connection(self) -> None:
        self.connection.execute("PRAGMA busy_timeout = 5000")
        deadline = monotonic() + CONNECTION_SETUP_TIMEOUT_SECONDS
        while True:
            try:
                self.connection.execute("PRAGMA journal_mode = WAL")
                break
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or monotonic() >= deadline:
                    raise
                sleep(0.01)
        self.connection.execute("PRAGMA synchronous = FULL")


def _insert_run(connection: sqlite3.Connection, stored: StoredRun) -> None:
    connection.execute(
        """
        INSERT INTO runs (
            run_id, recorded_at, canonical_fingerprint, content_fingerprint,
            status, evidence_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            stored.run_id,
            stored.recorded_at,
            stored.evidence["fingerprint"],
            stored.content_fingerprint,
            stored.evidence["status"],
            json.dumps(stored.evidence, sort_keys=True, separators=(",", ":")),
        ),
    )


def _stored_run(row: sqlite3.Row) -> StoredRun:
    evidence = _load_evidence(row["evidence_json"])
    _run_id(row["run_id"], "stored run_id")
    _recorded_at(row["recorded_at"], "stored recorded_at")
    _fingerprint(row["content_fingerprint"], "stored content_fingerprint")
    if row["canonical_fingerprint"] != evidence["fingerprint"]:
        raise ValueError("stored receipt canonical fingerprint is corrupt")
    if row["status"] != evidence["status"]:
        raise ValueError("stored receipt status is corrupt")
    return StoredRun(
        row["run_id"],
        row["recorded_at"],
        row["content_fingerprint"],
        evidence,
    )


def _load_evidence(raw: str) -> dict:
    try:
        evidence = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("stored evidence JSON is corrupt") from error
    try:
        return _validated_evidence(evidence)
    except (TypeError, ValueError) as error:
        raise ValueError("stored evidence payload is corrupt") from error


def _validated_evidence(evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        raise TypeError("evidence must be an object")
    required = {"schema_version", "scenario", "fingerprint", "status", "expected", "actual"}
    missing = required - set(evidence)
    if missing:
        raise ValueError(f"evidence is missing {sorted(missing)!r}")
    allowed = required | {
        "events", "probe", "operations", "cost_microusd", "error_type", "error_stage", "adapter"
    }
    unknown = set(evidence) - allowed
    if unknown:
        raise ValueError(f"evidence contains undeclared fields {sorted(map(str, unknown))!r}")
    if type(evidence["schema_version"]) is not int or evidence["schema_version"] != 1:
        raise ValueError("evidence.schema_version must be 1")
    _fingerprint(evidence["fingerprint"], "evidence.fingerprint")
    if not isinstance(evidence["scenario"], str) or not evidence["scenario"].strip():
        raise ValueError("evidence.scenario must be a nonempty string")
    if not isinstance(evidence["status"], str) or evidence["status"] not in EVIDENCE_STATUSES:
        raise ValueError("evidence.status is not recognized by schema version 1")
    if evidence["expected"] is not None and not isinstance(evidence["expected"], str):
        raise ValueError("evidence.expected must be a string or null")
    if evidence["actual"] is not None and not isinstance(evidence["actual"], str):
        raise ValueError("evidence.actual must be a string or null")
    _validate_outcome(evidence["status"], evidence["expected"], evidence["actual"])
    for field in ("operations", "cost_microusd"):
        if field in evidence and (
            isinstance(evidence[field], bool)
            or not isinstance(evidence[field], int)
            or evidence[field] < 0
        ):
            raise ValueError(f"evidence.{field} must be a nonnegative integer")
    if "error_type" in evidence and (
        not isinstance(evidence["error_type"], str) or not evidence["error_type"]
    ):
        raise ValueError("evidence.error_type must be a nonempty string")
    if "error_stage" in evidence and (
        not isinstance(evidence["error_stage"], str)
        or evidence["error_stage"] not in {"open", "observe", "recall", "close"}
    ):
        raise ValueError("evidence.error_stage is not recognized")
    if ("error_type" in evidence or "error_stage" in evidence) and evidence["status"] != "provider_error":
        raise ValueError("error fields require provider_error status")
    if "adapter" in evidence:
        adapter = evidence["adapter"]
        if not isinstance(adapter, dict):
            raise ValueError("evidence.adapter must be an object")
        if adapter.get("kind") == "fixture" and set(adapter) == {"kind", "name"}:
            if not isinstance(adapter["name"], str) or not adapter["name"].strip():
                raise ValueError("evidence.adapter.name must be nonempty")
        elif adapter.get("kind") == "local_program" and set(adapter) in (
            {"kind", "sha256"}, {"kind", "sha256", "protocol"}
        ):
            _fingerprint(adapter["sha256"], "evidence.adapter.sha256")
            if "protocol" in adapter and adapter["protocol"] != "continuity_v2":
                raise ValueError("evidence.adapter.protocol is invalid")
        elif adapter.get("kind") == "http_endpoint" and set(adapter) == {
            "kind", "sha256", "protocol"
        }:
            _fingerprint(adapter["sha256"], "evidence.adapter.sha256")
            if adapter["protocol"] != "snapshot_v1":
                raise ValueError("evidence.adapter.protocol is invalid")
        else:
            raise ValueError("evidence.adapter has invalid fields")
    if ("events" in evidence) != ("probe" in evidence):
        raise ValueError("evidence.events and evidence.probe must appear together")
    if "events" in evidence:
        events = evidence["events"]
        probe = evidence["probe"]
        if not isinstance(events, list) or not isinstance(probe, dict):
            raise ValueError("evidence.events and evidence.probe have invalid shapes")
        if any(not isinstance(item, dict) or set(item) != {
            "person", "field", "value", "operation"
        } for item in events) or set(probe) != {"person", "field"}:
            raise ValueError("evidence.events and evidence.probe contain undeclared fields")
        try:
            scenario = Scenario(
                evidence["scenario"],
                tuple(Event(**item) for item in events),
                Probe(**probe),
                evidence["expected"],
            )
        except (TypeError, ValueError) as error:
            raise ValueError("evidence.events and evidence.probe are invalid") from error
        if scenario.fingerprint() != evidence["fingerprint"]:
            raise ValueError("evidence scenario content does not match its fingerprint")
    try:
        json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError("evidence must be JSON serializable") from error
    return evidence


def _validate_outcome(status: str, expected: str | None, actual: str | None) -> None:
    if status == "pass" and actual != expected:
        raise ValueError("pass evidence must match its expected answer")
    if status == "fail" and (actual is None or actual == expected):
        raise ValueError("fail evidence must contain a different answer")
    if status == "unknown" and (expected is None or actual is not None):
        raise ValueError("unknown evidence must omit a known expected answer")
    if status not in {"pass", "fail", "unknown"} and actual is not None:
        raise ValueError("execution outcome evidence must not contain an answer")


def _fingerprint(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError(f"{name} must be a SHA 256 hexadecimal fingerprint")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError(f"{name} must be a SHA 256 hexadecimal fingerprint") from error


def _run_id(value: object, name: str = "run_id") -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")


def _recorded_at(value: object, name: str = "recorded_at") -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone aware ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a timezone aware ISO 8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone aware ISO 8601 timestamp")

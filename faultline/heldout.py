"""Manifest controlled held out scenario comparisons."""

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .core import Scenario
from .execution import Budget, Session, run_bounded
from .scenario import ScenarioDocument, load
from .strictjson import decode_object
from .trials import RepairComparison, compare_repairs


MANIFEST_VERSION = 1


@dataclass(frozen=True)
class ScenarioManifest:
    name: str
    documents: tuple[ScenarioDocument, ...]


@dataclass(frozen=True)
class HeldOutSuite:
    development: ScenarioManifest
    held_out: ScenarioManifest


@dataclass(frozen=True)
class ScenarioComparison:
    scenario: str
    fingerprint: str
    comparison: RepairComparison


@dataclass(frozen=True)
class HeldOutReport:
    budget: Budget
    comparisons: tuple[ScenarioComparison, ...]


def load_manifest(path: str | Path, fixture_directory: str | Path | None = None) -> ScenarioManifest:
    manifest_path = Path(path)
    root = Path(fixture_directory) if fixture_directory is not None else manifest_path.parent.parent
    payload = decode_object(manifest_path.read_bytes(), "manifest")
    if not isinstance(payload, dict) or set(payload) != {"version", "name", "scenarios"}:
        raise ValueError("manifest keys must be exactly version, name, and scenarios")
    if payload["version"] != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version {payload['version']!r}")
    if not isinstance(payload["name"], str) or not payload["name"].strip():
        raise ValueError("manifest.name must be a nonempty string")
    files = payload["scenarios"]
    if not isinstance(files, list) or not files:
        raise ValueError("manifest.scenarios must be a nonempty list")
    if any(not isinstance(item, str) or Path(item).name != item for item in files):
        raise ValueError("manifest scenario names must be plain file names")
    if len(set(files)) != len(files):
        raise ValueError("manifest cannot repeat a scenario")
    documents = tuple(load(root / item) for item in files)
    return ScenarioManifest(payload["name"], documents)


def load_suite(
    development_manifest: str | Path,
    held_out_manifest: str | Path,
    fixture_directory: str | Path | None = None,
) -> HeldOutSuite:
    development = load_manifest(development_manifest, fixture_directory)
    held_out = load_manifest(held_out_manifest, fixture_directory)
    development_fingerprints = {document.scenario.fingerprint() for document in development.documents}
    held_out_fingerprints = {document.scenario.fingerprint() for document in held_out.documents}
    if development_fingerprints & held_out_fingerprints:
        raise ValueError("development and held out manifests cannot overlap")
    return HeldOutSuite(development, held_out)


def compare_held_out(
    suite: HeldOutSuite,
    baseline: Callable[[], Session],
    candidate: Callable[[], Session],
    budget: Budget,
    attempts: int,
) -> HeldOutReport:
    """Runs both adapters over the held out suite under one shared budget."""
    comparisons = []
    for document in suite.held_out.documents:
        scenario = document.scenario
        comparison = compare_repairs(
            lambda scenario=scenario: run_bounded(scenario, baseline, budget),
            lambda scenario=scenario: run_bounded(scenario, candidate, budget),
            attempts,
        )
        comparisons.append(
            ScenarioComparison(scenario.name, scenario.fingerprint(), comparison)
        )
    return HeldOutReport(budget, tuple(comparisons))

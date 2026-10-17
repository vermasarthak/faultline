"""Release packaging contract tests."""

from pathlib import Path
import re
from tempfile import TemporaryDirectory
import tomllib
import unittest
from zipfile import ZipFile

import faultline
from scripts.verify_wheel import verify_wheel


class PackagingTests(unittest.TestCase):
    def test_package_metadata_and_runtime_version_match(self):
        root = Path(__file__).parent.parent
        metadata = tomllib.loads((root / "pyproject.toml").read_text())
        project = metadata["project"]
        self.assertEqual(project["name"], "faultline-memory")
        self.assertEqual(project["version"], faultline.__version__)
        self.assertEqual(project["scripts"]["faultline"], "faultline.__main__:main")
        self.assertEqual(project["requires-python"], ">=3.11,<3.15")

    def test_wheel_declares_only_the_runtime_package(self):
        root = Path(__file__).parent.parent
        metadata = tomllib.loads((root / "pyproject.toml").read_text())
        self.assertEqual(metadata["tool"]["setuptools"]["packages"], ["faultline"])

    def test_wheel_verifier_rejects_stale_or_missing_runtime_modules(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "faultline"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "core.py").write_text("")
            valid = root / "valid.whl"
            with ZipFile(valid, "w") as archive:
                archive.writestr("faultline/__init__.py", "")
                archive.writestr("faultline/core.py", "")
            self.assertEqual(verify_wheel(root, valid), 2)

            stale = root / "stale.whl"
            with ZipFile(stale, "w") as archive:
                archive.writestr("faultline/__init__.py", "")
                archive.writestr("faultline/core.py", "")
                archive.writestr("faultline/deleted.py", "")
            with self.assertRaisesRegex(ValueError, "unexpected=.*deleted.py"):
                verify_wheel(root, stale)

            missing = root / "missing.whl"
            with ZipFile(missing, "w") as archive:
                archive.writestr("faultline/__init__.py", "")
            with self.assertRaisesRegex(ValueError, "missing=.*core.py"):
                verify_wheel(root, missing)

    def test_workflow_actions_are_pinned_to_commit_shas(self):
        root = Path(__file__).parent.parent
        workflow = (root / ".github/workflows/tests.yml").read_text()
        uses = re.findall(r"^\s*- uses: ([^\s#]+)", workflow, re.MULTILINE)
        self.assertGreater(len(uses), 0)
        for action in uses:
            with self.subTest(action=action):
                self.assertRegex(action, r"^[^@]+@[0-9a-f]{40}$")

"""Release packaging contract tests."""

from pathlib import Path
import tomllib
import unittest

import faultline


class PackagingTests(unittest.TestCase):
    def test_package_metadata_and_runtime_version_match(self):
        root = Path(__file__).parent.parent
        metadata = tomllib.loads((root / "pyproject.toml").read_text())
        project = metadata["project"]
        self.assertEqual(project["name"], "faultline-memory")
        self.assertEqual(project["version"], faultline.__version__)
        self.assertEqual(project["scripts"]["faultline"], "faultline.__main__:main")
        self.assertEqual(project["requires-python"], ">=3.11")

    def test_wheel_declares_only_the_runtime_package(self):
        root = Path(__file__).parent.parent
        metadata = tomllib.loads((root / "pyproject.toml").read_text())
        self.assertEqual(metadata["tool"]["setuptools"]["packages"], ["faultline"])

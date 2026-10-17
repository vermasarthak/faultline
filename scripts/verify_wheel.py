#!/usr/bin/env python3
"""Verify that a built wheel contains exactly the current source runtime modules."""

from pathlib import Path
import sys
from zipfile import ZipFile


def runtime_modules(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix()
        for path in (root / "faultline").glob("*.py")
        if path.is_file()
    }


def wheel_modules(wheel: Path) -> set[str]:
    with ZipFile(wheel) as archive:
        return {
            name
            for name in archive.namelist()
            if name.startswith("faultline/") and name.endswith(".py")
        }


def verify_wheel(root: Path, wheel: Path) -> int:
    expected = runtime_modules(root)
    actual = wheel_modules(wheel)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        raise ValueError(f"wheel module mismatch: missing={missing}, unexpected={unexpected}")
    return len(actual)


def main(arguments: list[str]) -> int:
    if len(arguments) != 1:
        raise SystemExit("usage: verify_wheel.py WHEEL")
    root = Path(__file__).resolve().parent.parent
    wheel = Path(arguments[0])
    try:
        module_count = verify_wheel(root, wheel)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    print(f"verified {wheel}: {module_count} runtime modules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

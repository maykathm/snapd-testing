#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from service_api import TestExecutionService  # noqa: E402


class TestExecutionServiceCalculateTests(unittest.TestCase):
    def _make_service(
        self,
        spread_map: dict,
        base_tests: dict,
        added_tests: list[str] | None = None,
    ) -> TestExecutionService:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)

        spread_path = root / "spread-execution-map.json"
        spread_path.write_text(json.dumps(spread_map), encoding="utf-8")

        base_path = root / "base-executions.json"
        base_path.write_text(json.dumps(base_tests), encoding="utf-8")

        added_path = root / "added-spread-tests.txt"
        added_path.write_text("\n".join(added_tests or []), encoding="utf-8")

        config = {
            "spread_tests_execution_path": str(spread_path),
            "added_spread_tests_path": str(added_path),
            "base_tests_path": str(base_path),
            "refresh_command": ["true"],
            "refresh_timeout_seconds": 60,
        }
        return TestExecutionService(config)

    def tearDown(self) -> None:
        temp_dir = getattr(self, "temp_dir", None)
        if temp_dir is not None:
            temp_dir.cleanup()

    def test_calculate_tests_supports_structured_changes_and_allowed_tests(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={
                system: {
                    "snap/foo.go": [f"{system}:tests/spread/mod"],
                    "pkg/sub/existing.go": [f"{system}:tests/spread/dir"],
                }
            },
            base_tests={system: [f"{system}:tests/spread/base"]},
            added_tests=["tests/spread/manual"],
        )

        selected = service.calculate_tests(
            changed_files={
                "modified": ["snap/foo.go"],
                "added": ["pkg/sub/new.go"],
                "deleted": [],
            },
            backend_systems=[system],
            tests=[
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/mod",
                f"{system}:tests/spread/dir",
                f"{system}:tests/spread/unknown",
            ],
        )

        self.assertEqual(
            selected,
            [
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/mod",
                f"{system}:tests/spread/dir",
                f"{system}:tests/spread/unknown",
                f"{system}:tests/spread/manual",
            ],
        )

    def test_calculate_tests_returns_all_allowed_on_unmatched_modified(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={system: {"snap/foo.go": [f"{system}:tests/spread/mod"]}},
            base_tests={system: [f"{system}:tests/spread/base"]},
            added_tests=["tests/spread/manual"],
        )

        selected = service.calculate_tests(
            changed_files={"modified": ["snap/unmapped.go"], "added": [], "deleted": []},
            backend_systems=[system],
            tests=[
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/mod",
                f"{system}:tests/spread/unknown",
            ],
        )

        self.assertEqual(
            selected,
            [
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/mod",
                f"{system}:tests/spread/unknown",
                f"{system}:tests/spread/manual",
            ],
        )

    def test_calculate_tests_ignores_go_unit_test_files(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={
                system: {
                    "snap/foo.go": [f"{system}:tests/spread/mod"],
                    "pkg/sub/existing.go": [f"{system}:tests/spread/dir"],
                    "other/thing.go": [f"{system}:tests/spread/other"],
                }
            },
            base_tests={system: [f"{system}:tests/spread/base"]},
        )

        selected = service.calculate_tests(
            changed_files={
                "modified": ["snap/foo.go", "snap/foo_test.go"],
                "added": ["pkg/sub/new_test.go"],
                "deleted": ["other/old_test.go"],
            },
            backend_systems=[system],
            tests=[
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/mod",
                f"{system}:tests/spread/dir",
                f"{system}:tests/spread/other",
            ],
        )

        self.assertEqual(
            selected,
            [
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/mod",
            ],
        )

    def test_calculate_tests_filters_allowed_tests_per_backend(self) -> None:
        amd64 = "amd64:ubuntu-24.04"
        arm64 = "arm64:ubuntu-24.04"
        service = self._make_service(
            spread_map={
                amd64: {"snap/foo.go": [f"{amd64}:tests/spread/mod"]},
                arm64: {"snap/foo.go": [f"{arm64}:tests/spread/mod"]},
            },
            base_tests={
                amd64: [f"{amd64}:tests/spread/base"],
                arm64: [f"{arm64}:tests/spread/base"],
            },
        )

        selected = service.calculate_tests(
            changed_files={"modified": ["snap/foo.go"], "added": [], "deleted": []},
            backend_systems=[amd64, arm64],
            tests=[
                f"{amd64}:tests/spread/base",
                f"{amd64}:tests/spread/mod",
                f"{arm64}:tests/spread/base",
                f"{arm64}:tests/spread/mod",
            ],
        )

        self.assertEqual(
            selected,
            [
                f"{amd64}:tests/spread/base",
                f"{amd64}:tests/spread/mod",
                f"{arm64}:tests/spread/base",
                f"{arm64}:tests/spread/mod",
            ],
        )

    def test_calculate_tests_rejects_invalid_changed_files_type(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={system: {}},
            base_tests={system: []},
        )

        with self.assertRaisesRegex(RuntimeError, "changed_files must be an object"):
            service.calculate_tests(changed_files="not-valid", backend_systems=[system], tests=[])


if __name__ == "__main__":
    unittest.main()

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
                    "other/unchanged.go": [f"{system}:tests/spread/manual"],
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
                f"{system}:tests/spread/manual",
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

    def test_calculate_tests_returns_all_allowed_on_changed_test_script(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={system: {"snap/foo.go": [f"{system}:tests/spread/mod"]}},
            base_tests={system: [f"{system}:tests/spread/base"]},
            added_tests=["tests/spread/manual"],
        )

        for changed_files in (
            {"modified": ["tests/main/foo/task.yaml"], "added": [], "deleted": []},
            {"modified": [], "added": ["tests/lib/new-helper.sh"], "deleted": []},
            {"modified": ["snap/foo.go"], "added": [], "deleted": ["tests/main/old/task.yaml"]},
        ):
            with self.subTest(changed_files=changed_files):
                selected = service.calculate_tests(
                    changed_files=changed_files,
                    backend_systems=[system],
                    tests=[
                        f"{system}:tests/spread/base",
                        f"{system}:tests/spread/mod",
                        f"{system}:tests/spread/other",
                    ],
                )

                self.assertEqual(
                    selected,
                    [
                        f"{system}:tests/spread/base",
                        f"{system}:tests/spread/mod",
                        f"{system}:tests/spread/other",
                    ],
                )

    def test_calculate_tests_selects_test_owning_changed_files(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={
                system: {
                    "snap/foo.go": [
                        f"{system}:tests/main/some-test",
                        f"{system}:tests/main/some-test-two",
                        f"{system}:tests/main/other:v1",
                        f"{system}:tests/main/other:v2",
                        f"{system}:tests/main/mod",
                    ]
                }
            },
            base_tests={system: ["tests/main/base"]},
        )

        selected = service.calculate_tests(
            changed_files={
                "modified": ["tests/main/some-test/script.sh", "tests/main/some-test/task.yaml"],
                "added": ["tests/main/other/data/file.txt"],
                "deleted": [],
            },
            backend_systems=[system],
            tests=[
                f"{system}:tests/main/base",
                f"{system}:tests/main/mod",
                f"{system}:tests/main/some-test",
                f"{system}:tests/main/some-test-two",
                f"{system}:tests/main/other:v1",
                f"{system}:tests/main/other:v2",
            ],
        )

        self.assertEqual(
            selected,
            [
                f"{system}:tests/main/base",
                f"{system}:tests/main/some-test",
                f"{system}:tests/main/other:v1",
                f"{system}:tests/main/other:v2",
            ],
        )

    def test_calculate_tests_selects_new_test(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={system: {"snap/foo.go": [f"{system}:tests/main/mod"]}},
            base_tests={system: ["tests/main/base"]},
        )

        selected = service.calculate_tests(
            changed_files={
                "modified": ["snap/foo.go"],
                "added": ["tests/main/new-test/task.yaml"],
                "deleted": [],
            },
            backend_systems=[system],
            tests=[
                f"{system}:tests/main/base",
                f"{system}:tests/main/mod",
                f"{system}:tests/main/new-test",
            ],
        )

        self.assertEqual(
            selected,
            [
                f"{system}:tests/main/base",
                f"{system}:tests/main/mod",
                f"{system}:tests/main/new-test",
            ],
        )

    def test_calculate_tests_test_folder_known_from_other_system(self) -> None:
        system_a = "amd64:ubuntu-24.04"
        system_b = "amd64:debian-12"
        service = self._make_service(
            spread_map={
                system_a: {"snap/foo.go": [f"{system_a}:tests/main/only-a", f"{system_a}:tests/main/shared"]},
                system_b: {"snap/foo.go": [f"{system_b}:tests/main/shared"]},
            },
            base_tests={},
        )

        selected = service.calculate_tests(
            changed_files={"modified": ["tests/main/only-a/task.yaml"], "added": [], "deleted": []},
            backend_systems=[system_a, system_b],
            tests=[
                f"{system_a}:tests/main/only-a",
                f"{system_a}:tests/main/shared",
                f"{system_b}:tests/main/shared",
            ],
        )

        self.assertEqual(selected, [f"{system_a}:tests/main/only-a"])

    def test_calculate_tests_returns_all_when_test_folder_and_shared_files_change(self) -> None:
        system = "amd64:ubuntu-24.04"
        service = self._make_service(
            spread_map={system: {"snap/foo.go": [f"{system}:tests/main/some-test", f"{system}:tests/main/mod"]}},
            base_tests={},
        )

        selected = service.calculate_tests(
            changed_files={
                "modified": ["tests/main/some-test/task.yaml", "tests/lib/snaps.sh"],
                "added": [],
                "deleted": [],
            },
            backend_systems=[system],
            tests=[f"{system}:tests/main/some-test", f"{system}:tests/main/mod"],
        )

        self.assertEqual(selected, [f"{system}:tests/main/some-test", f"{system}:tests/main/mod"])

    def test_calculate_tests_added_tests_stay_within_input(self) -> None:
        amd64 = "amd64:ubuntu-24.04"
        arm64 = "arm64:ubuntu-24.04"
        service = self._make_service(
            spread_map={
                amd64: {"other/unchanged.go": [f"{amd64}:tests/main/added:v1", f"{amd64}:tests/main/added:v2"]},
                arm64: {"other/unchanged.go": [f"{arm64}:tests/main/variant:v1", f"{arm64}:tests/main/variant:v2"]},
            },
            base_tests={},
            added_tests=["tests/main/added", "tests/main/variant:v2", "tests/main/missing"],
        )

        selected = service.calculate_tests(
            changed_files={"modified": [], "added": [], "deleted": []},
            backend_systems=[amd64, arm64],
            tests=[
                f"{amd64}:tests/main/added:v1",
                f"{amd64}:tests/main/added:v2",
                f"{arm64}:tests/main/variant:v1",
                f"{arm64}:tests/main/variant:v2",
            ],
        )

        self.assertEqual(
            selected,
            [
                f"{amd64}:tests/main/added:v1",
                f"{amd64}:tests/main/added:v2",
                f"{arm64}:tests/main/variant:v2",
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

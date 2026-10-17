#!/usr/bin/env python3

import json
import tempfile
import unittest
from pathlib import Path
import sys


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from calculate_tests import (  # noqa: E402
    _load_json,
    _matching_tests_for_directory,
    _matching_tests_for_modified_file,
    _normalize_test_name,
    calculate_tests_to_execute,
)


class CalculateTestsUnitTests(unittest.TestCase):
    def test_normalize_test_name_strips_system_prefix(self) -> None:
        system = "amd64:ubuntu-24.04"
        prefixed_name = f"{system}:tests/spread/smoke"

        self.assertEqual(
            _normalize_test_name(system, prefixed_name),
            "tests/spread/smoke",
        )

    def test_normalize_test_name_keeps_name_without_prefix(self) -> None:
        system = "amd64:ubuntu-24.04"

        self.assertEqual(
            _normalize_test_name(system, "tests/spread/smoke"),
            "tests/spread/smoke",
        )

    def test_load_json_reads_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = {"key": ["value"]}
            path = Path(temp_dir) / "payload.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            loaded = _load_json(path)

            self.assertEqual(loaded, payload)

    def test_load_json_raises_for_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.json"

            with self.assertRaisesRegex(RuntimeError, "missing required file"):
                _load_json(path)

    def test_matching_tests_for_modified_file_exact_match(self) -> None:
        spread_map = {
            "src/a.go": ["tests/spread/a", "tests/spread/common"],
            "src/b.go": ["tests/spread/b"],
        }

        matches = _matching_tests_for_modified_file("src/a.go", spread_map)

        self.assertEqual(matches, {"tests/spread/a", "tests/spread/common"})

    def test_matching_tests_for_modified_file_unknown_returns_empty_set(self) -> None:
        spread_map = {"src/a.go": ["tests/spread/a"]}

        matches = _matching_tests_for_modified_file("src/unknown.go", spread_map)

        self.assertEqual(matches, set())

    def test_matching_tests_for_directory_matches_prefix(self) -> None:
        spread_map = {
            "src/a.go": ["tests/spread/a"],
            "pkg/sub/b.go": ["tests/spread/b", "tests/spread/common"],
            "pkg/sub/c.go": ["tests/spread/c", "tests/spread/common"],
        }

        matches = _matching_tests_for_directory("pkg/sub/", spread_map)

        self.assertEqual(
            matches,
            {"tests/spread/b", "tests/spread/c", "tests/spread/common"},
        )

    def test_calculate_tests_includes_base_and_matching_and_unknown(self) -> None:
        system = "amd64:ubuntu-24.04"
        allowed_tests = [
            f"{system}:tests/spread/base",
            f"{system}:tests/spread/feature-a",
            f"{system}:tests/spread/feature-b",
            f"{system}:tests/spread/not-in-map",
        ]
        changed_files = {
            "modified": ["src/feature_a.go"],
            "added": [],
            "deleted": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "spread-execution-map.json").write_text(
                json.dumps(
                    {
                        system: {
                            "src/feature_a.go": [f"{system}:tests/spread/feature-a"],
                            "src/feature_b.go": [f"{system}:tests/spread/feature-b"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "base-executions.json").write_text(
                json.dumps({system: [f"{system}:tests/spread/base"]}),
                encoding="utf-8",
            )

            selected = calculate_tests_to_execute(
                system=system,
                tests=allowed_tests,
                changed_files=changed_files,
                directory=str(data_dir),
            )

        self.assertEqual(
            selected,
            [
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/feature-a",
                f"{system}:tests/spread/not-in-map",
            ],
        )

    def test_calculate_tests_returns_all_when_modified_file_not_in_map(self) -> None:
        system = "amd64:ubuntu-24.04"
        allowed_tests = [
            f"{system}:tests/spread/base",
            f"{system}:tests/spread/feature-a",
        ]
        changed_files = {
            "modified": ["src/unmapped.go"],
            "added": [],
            "deleted": [],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "spread-execution-map.json").write_text(
                json.dumps({system: {"src/feature_a.go": [f"{system}:tests/spread/feature-a"]}}),
                encoding="utf-8",
            )
            (data_dir / "base-executions.json").write_text(
                json.dumps({system: [f"{system}:tests/spread/base"]}),
                encoding="utf-8",
            )

            selected = calculate_tests_to_execute(
                system=system,
                tests=allowed_tests,
                changed_files=changed_files,
                directory=str(data_dir),
            )

        self.assertEqual(selected, allowed_tests)

    def test_calculate_tests_uses_directory_matching_for_added_and_deleted(self) -> None:
        system = "amd64:ubuntu-24.04"
        allowed_tests = [
            f"{system}:tests/spread/base",
            f"{system}:tests/spread/dir-hit",
            f"{system}:tests/spread/other",
        ]
        changed_files = {
            "modified": [],
            "added": ["pkg/sub/new_file.go"],
            "deleted": ["pkg/sub/old_file.go"],
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            data_dir = Path(temp_dir)
            (data_dir / "spread-execution-map.json").write_text(
                json.dumps(
                    {
                        system: {
                            "pkg/sub/existing.go": [f"{system}:tests/spread/dir-hit"],
                            "different/path.go": [f"{system}:tests/spread/other"],
                        }
                    }
                ),
                encoding="utf-8",
            )
            (data_dir / "base-executions.json").write_text(
                json.dumps({system: [f"{system}:tests/spread/base"]}),
                encoding="utf-8",
            )

            selected = calculate_tests_to_execute(
                system=system,
                tests=allowed_tests,
                changed_files=changed_files,
                directory=str(data_dir),
            )

        self.assertEqual(
            selected,
            [
                f"{system}:tests/spread/base",
                f"{system}:tests/spread/dir-hit",
            ],
        )


if __name__ == "__main__":
    unittest.main()

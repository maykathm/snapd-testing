#!/usr/bin/env python3

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

import refresh_spread_tests_execution as refresh  # noqa: E402


class FeatureQueryTests(unittest.TestCase):
    @patch("refresh_spread_tests_execution.subprocess.run")
    def test_query_features_builds_command_and_parses_json(self, mock_run: Mock) -> None:
        mock_run.return_value = Mock(returncode=0, stdout='{"ok": true}', stderr="")
        fq = refresh.FeatureQuery("/tmp/query_features.py", "/tmp/creds.json")

        result = fq.query_features("feat sys", "-t 123 -s amd64")

        self.assertEqual(result, {"ok": True})
        mock_run.assert_called_once_with(
            [
                "python3",
                "/tmp/query_features.py",
                "feat",
                "sys",
                "-f",
                "/tmp/creds.json",
                "-t",
                "123",
                "-s",
                "amd64",
            ],
            capture_output=True,
            text=True,
        )

    @patch("refresh_spread_tests_execution.subprocess.run")
    def test_query_features_raises_on_non_zero_exit(self, mock_run: Mock) -> None:
        mock_run.return_value = Mock(returncode=2, stdout="", stderr="boom")
        fq = refresh.FeatureQuery("/tmp/query_features.py", "/tmp/creds.json")

        with self.assertRaisesRegex(RuntimeError, "failed with exit code 2"):
            fq.query_features("list")


class LatestTimestampTests(unittest.TestCase):
    def test_latest_timestamp_and_systems_returns_first_entry(self) -> None:
        fq = Mock()
        fq.query_features.return_value = [
            {"timestamp": 12345, "systems": ["s1", "s2"]},
            {"timestamp": 99999, "systems": ["s3"]},
        ]

        timestamp, systems = refresh._latest_timestamp_and_systems(fq)

        self.assertEqual(timestamp, "12345")
        self.assertEqual(systems, ["s1", "s2"])

    def test_latest_timestamp_and_systems_raises_on_empty(self) -> None:
        fq = Mock()
        fq.query_features.return_value = []

        with self.assertRaisesRegex(RuntimeError, "returned no timestamps"):
            refresh._latest_timestamp_and_systems(fq)

    def test_latest_timestamp_and_systems_raises_when_timestamp_missing(self) -> None:
        fq = Mock()
        fq.query_features.return_value = [{"systems": ["s1"]}]

        with self.assertRaisesRegex(RuntimeError, "missing timestamp"):
            refresh._latest_timestamp_and_systems(fq)


class NamingTests(unittest.TestCase):
    def test_get_test_name_variants(self) -> None:
        self.assertEqual(
            refresh.get_test_name("amd64:ubuntu-24.04", "tests/spread/foo", "", ""),
            "amd64:ubuntu-24.04:tests/spread/foo",
        )
        self.assertEqual(
            refresh.get_test_name("amd64:ubuntu-24.04", "tests/spread/foo", "task", ""),
            "amd64:ubuntu-24.04:tests/spread/foo/task",
        )
        self.assertEqual(
            refresh.get_test_name("amd64:ubuntu-24.04", "tests/spread/foo", "task", "v1"),
            "amd64:ubuntu-24.04:tests/spread/foo/task:v1",
        )


class BuildExecutionMapTests(unittest.TestCase):
    def test_build_execution_map_covers_all_systems_and_filters_coverage(self) -> None:
        fq = Mock()
        fq.query_features.side_effect = [
            [{"timestamp": "ts-1", "systems": ["s1", "s2", "s3", "s4"]}],
            {
                "tests": [
                    {
                        "suite": "tests/spread/a",
                        "task_name": "",
                        "variant": "",
                        "coverages": [
                            {"file": "snap/foo.go", "func": "Run"},
                            {"file": "snap/foo.go", "func": "init"},
                            {"file": "interfaces/bar.go", "func": "Name"},
                            {"file": "interfaces/bar.go", "func": "StaticInfo"},
                            {"file": "interfaces/bar.go", "func": "Other"},
                        ],
                    },
                    {
                        "suite": "tests/spread/b",
                        "task_name": "task",
                        "variant": "v",
                        "coverages": [{"file": "snap/foo.go", "func": "Do"}],
                    },
                    {
                        "suite": "tests/spread/ignored",
                        "task_name": "",
                        "variant": "",
                    },
                ]
            },
            {},
            {"tests": []},
            {
                "tests": [
                    {
                        "suite": "tests/spread/c",
                        "task_name": "task",
                        "variant": "",
                        "coverages": [{"file": "snap/baz.go", "func": "Do"}],
                    },
                ]
            },
        ]

        output = refresh.build_execution_map(fq)

        self.assertEqual(set(output.keys()), {"s1", "s3", "s4"})
        self.assertEqual(output["s4"], {"snap/baz.go": ["s4:tests/spread/c/task"]})
        self.assertEqual(
            output["s1"]["snap/foo.go"],
            [
                "s1:tests/spread/a",
                "s1:tests/spread/b/task:v",
            ],
        )
        self.assertEqual(output["s1"]["interfaces/bar.go"], ["s1:tests/spread/a"])

        expected_calls = [
            (("list",),),
            (("feat sys", "-t ts-1 -s s1 --exclude cmds interfaces endpoints ensures tasks changes"),),
            (("feat sys", "-t ts-1 -s s2 --exclude cmds interfaces endpoints ensures tasks changes"),),
            (("feat sys", "-t ts-1 -s s3 --exclude cmds interfaces endpoints ensures tasks changes"),),
            (("feat sys", "-t ts-1 -s s4 --exclude cmds interfaces endpoints ensures tasks changes"),),
        ]
        self.assertEqual(fq.query_features.call_count, 5)
        for idx, call in enumerate(fq.query_features.call_args_list):
            self.assertEqual(call.args, expected_calls[idx][0])


class BuildBaseTests(unittest.TestCase):
    def test_build_base_tests_without_exclude(self) -> None:
        fq = Mock()
        fq.query_features.side_effect = [
            [{"timestamp": "ts-base", "systems": ["s1"]}],
            {"s1": ["s1:tests/spread/base"]},
        ]

        output = refresh.build_base_tests(fq, max_minutes=30, exclude=[])

        self.assertEqual(output, {"s1": ["s1:tests/spread/base"]})
        self.assertEqual(fq.query_features.call_args_list[1].args, ("feat cover", "-t ts-base --max-minutes 30"))

    def test_build_base_tests_with_exclude(self) -> None:
        fq = Mock()
        fq.query_features.side_effect = [
            [{"timestamp": "ts-base", "systems": ["s1"]}],
            {"s1": ["s1:tests/spread/base"]},
        ]

        refresh.build_base_tests(fq, max_minutes=15, exclude=["coverages", "changes"])

        self.assertEqual(
            fq.query_features.call_args_list[1].args,
            ("feat cover", "-t ts-base --max-minutes 15 --exclude coverages changes"),
        )


class MainTests(unittest.TestCase):
    def test_main_writes_output_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            query_path = Path(temp_dir) / "query_features.py"
            creds_path = Path(temp_dir) / "creds.json"
            output_dir = Path(temp_dir) / "out"
            query_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            creds_path.write_text("{}", encoding="utf-8")

            execution_map = {"s1": {"snap/foo.go": ["s1:tests/spread/a"]}}
            base_tests = {"s1": ["s1:tests/spread/base"]}

            argv = [
                "prog",
                "--query-features-path",
                str(query_path),
                "--credentials-path",
                str(creds_path),
                "--output-dir",
                str(output_dir),
                "--base-tests-max-minutes",
                "42",
            ]

            with patch.object(sys, "argv", argv), patch(
                "refresh_spread_tests_execution.build_execution_map",
                return_value=execution_map,
            ) as map_mock, patch(
                "refresh_spread_tests_execution.build_base_tests",
                return_value=base_tests,
            ) as base_mock:
                refresh.main()

            map_mock.assert_called_once()
            base_mock.assert_called_once()
            self.assertEqual(base_mock.call_args.args[1], 42)
            self.assertEqual(base_mock.call_args.args[2], ["coverages"])

            written_execution_map = json.loads((output_dir / "spread-execution-map.json").read_text(encoding="utf-8"))
            written_base_tests = json.loads((output_dir / "base-executions.json").read_text(encoding="utf-8"))

            self.assertEqual(written_execution_map, execution_map)
            self.assertEqual(written_base_tests, base_tests)

    def test_main_raises_when_query_features_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            creds_path = Path(temp_dir) / "creds.json"
            creds_path.write_text("{}", encoding="utf-8")

            argv = [
                "prog",
                "--query-features-path",
                str(Path(temp_dir) / "missing_query_features.py"),
                "--credentials-path",
                str(creds_path),
                "--output-dir",
                str(Path(temp_dir) / "out"),
            ]

            with patch.object(sys, "argv", argv), self.assertRaisesRegex(RuntimeError, "query_features.py not found"):
                refresh.main()

    def test_main_raises_when_credentials_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            query_path = Path(temp_dir) / "query_features.py"
            query_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

            argv = [
                "prog",
                "--query-features-path",
                str(query_path),
                "--credentials-path",
                str(Path(temp_dir) / "missing_creds.json"),
                "--output-dir",
                str(Path(temp_dir) / "out"),
            ]

            with patch.object(sys, "argv", argv), self.assertRaisesRegex(RuntimeError, "credentials file not found"):
                refresh.main()


if __name__ == "__main__":
    unittest.main()

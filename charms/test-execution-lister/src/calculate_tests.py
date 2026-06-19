#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Canonical Ltd

"""Calculate which spread tests should execute for a change set."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _normalize_test_name(system: str, test_name: str) -> str:
	prefix = f"{system}:"
	if test_name.startswith(prefix):
		return test_name[len(prefix):]
	return test_name


def _load_json(path: Path) -> dict:
	if not path.exists():
		raise RuntimeError(f"missing required file: {path}")
	with path.open(encoding="utf-8") as handle:
		return json.load(handle)


def _matching_tests_for_modified_file(
	modified_file: str,
	spread_map_for_system: dict[str, list[str]],
) -> set[str]:
	if modified_file not in spread_map_for_system:
		return set()
	return set(spread_map_for_system[modified_file])


def _matching_tests_for_directory(
	directory_prefix: str,
	spread_map_for_system: dict[str, list[str]],
) -> set[str]:
	matches: set[str] = set()
	for file_path, tests_for_file in spread_map_for_system.items():
		if file_path.startswith(directory_prefix):
			matches.update(tests_for_file)
	return matches


def calculate_tests_to_execute(
	system: str,
	tests: list[str],
	changed_files: dict[str, list[str]],
	directory: str,
) -> list[str]:
	"""Return the ordered subset of allowed tests that should execute.

	Rules:
	- Start from base executions for the given system.
	- Add tests related to modified files by exact file match in spread map.
	- Add tests related to added/deleted files by immediate directory prefix.
	- If any modified file has no match in spread map, return all allowed tests.
	- Include allowed tests missing from spread map for this system.
	"""

	data_dir = Path(directory)
	spread_map = _load_json(data_dir / "spread-execution-map.json")
	base_executions = _load_json(data_dir / "base-executions.json")

	spread_map_for_system: dict[str, list[str]] = spread_map.get(system, {})
	base_tests_for_system: list[str] = base_executions.get(system, [])

	selected_normalized: set[str] = {
		_normalize_test_name(system, test_name) for test_name in base_tests_for_system
	}

	modified_files = changed_files.get("modified", [])
	for file_path in modified_files:
		matches = _matching_tests_for_modified_file(file_path, spread_map_for_system)
		if not matches:
			return list(tests)
		selected_normalized.update(_normalize_test_name(system, name) for name in matches)

	for key in ("added", "deleted"):
		for file_path in changed_files.get(key, []):
			parent = Path(file_path).parent.as_posix()
			prefix = "" if parent == "." else f"{parent}/"
			matches = _matching_tests_for_directory(prefix, spread_map_for_system)
			selected_normalized.update(_normalize_test_name(system, name) for name in matches)

	spread_known_normalized: set[str] = set()
	for tests_for_file in spread_map_for_system.values():
		for test_name in tests_for_file:
			spread_known_normalized.add(_normalize_test_name(system, test_name))

	result: list[str] = []
	for test_name in tests:
		normalized = _normalize_test_name(system, test_name)
		if normalized in selected_normalized or normalized not in spread_known_normalized:
			result.append(test_name)
	return result


def main() -> None:
	parser = argparse.ArgumentParser(description="Calculate tests to execute")
	parser.add_argument("--system", required=True, type=str)
	parser.add_argument("--tests-path", required=True, type=str)
	parser.add_argument("--changed-files-path", required=True, type=str)
	parser.add_argument("--directory", required=True, type=str)
	args = parser.parse_args()

	tests_path = Path(args.tests_path)
	changed_files_path = Path(args.changed_files_path)
	if not tests_path.exists():
		raise RuntimeError(f"tests file not found: {tests_path}")
	if not changed_files_path.exists():
		raise RuntimeError(f"changed-files file not found: {changed_files_path}")

	tests = tests_path.read_text(encoding="utf-8").splitlines()
	changed_files = json.loads(changed_files_path.read_text(encoding="utf-8"))

	output = calculate_tests_to_execute(
		system=args.system,
		tests=tests,
		changed_files=changed_files,
		directory=args.directory,
	)
	print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
	main()


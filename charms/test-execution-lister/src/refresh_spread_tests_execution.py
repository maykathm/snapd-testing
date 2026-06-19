#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Canonical Ltd

"""Helper to refresh spread-tests-execution and base-test mappings from mongodb metadata."""

import argparse
import json
import subprocess
import os
from pathlib import Path



class FeatureQuery:
    def __init__(self, query_features_path, creds_path):
        self.query_features_path = query_features_path
        self.creds_path = creds_path

    def query_features(self, command: str, args: str = ""):
        cmd = [
            "python3",
            str(self.query_features_path),
            *command.split(),
            "-d",
            self.creds_path,
            *args.split()
        ]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return json.loads(result.stdout or "{}")


def _latest_timestamp_and_systems(fq: FeatureQuery) -> tuple[str, list[str]]:
    entries = fq.query_features("list")
    if not entries:
        raise RuntimeError("query_features.py list returned no timestamps")
    latest = entries[0].get("timestamp")
    systems = entries[0].get("systems", [])
    if not latest:
        raise RuntimeError("latest timestamp entry is missing timestamp")
    return str(latest), systems


def get_test_name(system: str, suite: str, task_name: str, variant: str):
    if not task_name:
        return f"{system}:{suite}"
    if not variant:
        return f"{system}:{suite}/{task_name}"
    return f"{system}:{suite}/{task_name}:{variant}"


def build_execution_map(fq: FeatureQuery) -> dict[str, dict[str, list[str]]]:
    timestamp, systems = _latest_timestamp_and_systems(fq)
    systems = systems[:3]
    output: dict[str, dict[str, list[str]]] = {}
    for system in systems:
        coverages = fq.query_features("feat sys", f"-t {timestamp} -s {system} --exclude cmds interfaces endpoints ensures tasks changes")
        if 'tests' not in coverages:
            continue
        output[system] = {}
        for test in coverages['tests']:
            if 'coverages' not in test:
                continue
            name = get_test_name(system, test['suite'], test['task_name'], test['variant'])
            files = {
                coverage['file'] 
                for coverage in test['coverages']
                if  coverage['func'] != 'init' and
                    (not coverage['file'].startswith('interfaces/') or coverage['func'] != 'Name') and
                    (not coverage['file'].startswith('interfaces/') or coverage['func'] != 'StaticInfo')
            }
            for file_path in files:
                if file_path not in output[system]:
                    output[system][file_path] = set()
                output[system][file_path].add(name)
        output[system] = {key: sorted(tests) for key, tests in output[system].items()}
    
    return output


def build_base_tests(
    fq: FeatureQuery,
    max_minutes: int,
    exclude: list[str],
) -> dict[str, list[str]]:
    timestamp, _ = _latest_timestamp_and_systems(fq)
    args = f"-t {timestamp} --max-minutes {max_minutes}"
    if exclude:
        args += " --exclude " + " ".join(exclude)
    return fq.query_features("feat cover", args)


def main() -> None:
    parser = argparse.ArgumentParser(description="refresh spread-tests-execution.json using query_features.py")
    parser.add_argument("--query-features-path", required=True, type=str)
    parser.add_argument("--credentials-path", required=True, type=str)
    parser.add_argument("--output-dir", required=True, type=str)
    parser.add_argument("--base-tests-max-minutes", default=0, type=int)
    args = parser.parse_args()

    output_path = Path(args.output_dir)

    if not Path(args.query_features_path).exists():
        raise RuntimeError(f"query_features.py not found: {args.query_features_path}")
    if not Path(args.credentials_path).exists():
        raise RuntimeError(f"credentials file not found: {args.credentials_path}")
    
    fq = FeatureQuery(args.query_features_path, args.credentials_path)

    execution_map = build_execution_map(fq)
    output_path.mkdir(parents=True, exist_ok=True)
    execution_path = Path(os.path.join(output_path, "spread-execution-map.json"))
    execution_path.write_text(json.dumps(execution_map, indent=2, sort_keys=True), encoding="utf-8")

    base_tests = build_base_tests(
        fq,
        args.base_tests_max_minutes,
        ["coverages"],
    )
    base_tests_output_path = Path(os.path.join(output_path), "base-executions.json")
    base_tests_output_path.write_text(json.dumps(base_tests, indent=2, sort_keys=True), encoding="utf-8")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Canonical Ltd

"""HTTP API for test execution list operations."""

import argparse
import json
import logging
import subprocess
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from calculate_tests import calculate_tests_to_execute

logger = logging.getLogger(__name__)


class TestExecutionService:
    """Core logic behind API operations."""

    def __init__(self, config: dict[str, Any]):
        self.spread_tests_execution_path = Path(config["spread_tests_execution_path"])
        self.added_spread_tests_path = Path(config["added_spread_tests_path"])
        self.refresh_command = config["refresh_command"]
        self.refresh_timeout_seconds = int(config.get("refresh_timeout_seconds", 300))
        self._add_tests_lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @staticmethod
    def _is_go_source(path: str) -> bool:
        # Go unit test files never appear in spread coverage, so treating them
        # as unmatched would select every test.
        return path.endswith(".go") and not path.endswith("_test.go")

    @staticmethod
    def _is_test_script(path: str) -> bool:
        # Spread tests and their helpers are not part of the coverage data,
        # so any change under tests/ requires running everything.
        return path.startswith("tests/")

    def _load_added_tests(self) -> list[str]:
        if not self.added_spread_tests_path.exists():
            return []
        lines = self.added_spread_tests_path.read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip()]

    @staticmethod
    def _normalize_changed_files(changed_files: Any) -> dict[str, list[str]]:
        if not isinstance(changed_files, dict):
            raise RuntimeError("changed_files must be an object with modified/added/deleted lists")

        normalized: dict[str, list[str]] = {}
        for key in ("modified", "added", "deleted"):
            raw = changed_files.get(key, [])
            if not isinstance(raw, list):
                raise RuntimeError(f"changed_files.{key} must be a list")
            normalized[key] = [str(path) for path in raw if isinstance(path, str)]
        return normalized

    @staticmethod
    def _tests_for_system(system: str, tests: list[str]) -> list[str]:
        selected: list[str] = []
        system_prefix = f"{system}:"
        for test in tests:
            value = str(test).strip()
            if not value:
                continue
            if value.startswith(system_prefix):
                selected.append(value)
                continue
            if ":" in value:
                continue
            selected.append(f"{system}:{value}")
        return selected

    @staticmethod
    def _unique_keep_order(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            out.append(item)
        return out

    def calculate_tests(self, changed_files: Any, backend_systems: list[str], tests: list[str]) -> list[str]:
        changed_files_by_type = self._normalize_changed_files(changed_files)
        run_all = any(
            self._is_test_script(path) for paths in changed_files_by_type.values() for path in paths
        )
        for key, paths in changed_files_by_type.items():
            changed_files_by_type[key] = [path for path in paths if self._is_go_source(path)]

        resolved_tests: list[str] = []

        added_tests = self._load_added_tests()
        for backend_system in backend_systems:
            allowed_tests = self._tests_for_system(backend_system, tests)
            for added in added_tests:
                value = added.strip()
                if value:
                    allowed_tests.append(f"{backend_system}:{value}")

            if run_all:
                resolved_tests.extend(self._unique_keep_order(allowed_tests))
                continue

            selected = calculate_tests_to_execute(
                system=backend_system,
                tests=self._unique_keep_order(allowed_tests),
                changed_files=changed_files_by_type,
                directory=str(self.spread_tests_execution_path.parent),
            )
            resolved_tests.extend(selected)

        return self._unique_keep_order(resolved_tests)

    def add_tests(self, tests: list[str]) -> int:
        if not tests:
            return 0
        tests_to_add = [t.strip() for t in tests if t.strip()]
        if not tests_to_add:
            return 0

        self.added_spread_tests_path.parent.mkdir(parents=True, exist_ok=True)
        with self._add_tests_lock:
            with self.added_spread_tests_path.open("a", encoding="utf-8") as f:
                for test in tests_to_add:
                    f.write(test)
                    f.write("\n")
        return len(tests_to_add)

    def mongodb_updated(self) -> None:
        with self._refresh_lock:
            subprocess.run(self.refresh_command, check=True, capture_output=True, text=True, timeout=self.refresh_timeout_seconds)
            self.added_spread_tests_path.unlink(missing_ok=True)


class ApiHandler(BaseHTTPRequestHandler):
    """HTTP handlers for the test execution API."""

    service: TestExecutionService

    def log_message(self, fmt: str, *args):
        logger.info("api %s - %s", self.address_string(), fmt % args)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
        if not isinstance(payload, dict):
            raise RuntimeError("request payload must be a json object")
        return payload

    def _write_json(self, status: HTTPStatus, payload: dict[str, Any]):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        try:
            payload = self._read_json()
            if self.path == "/calculate-tests":
                changed_files = payload.get("changed_files", [])
                backend_systems = payload.get("backend_systems", [])
                tests = payload.get("tests", [])
                if not isinstance(changed_files, dict):
                    raise RuntimeError("changed_files must be an object with modified/added/deleted lists")
                if not isinstance(backend_systems, list):
                    raise RuntimeError("backend_systems must be a list")
                if not isinstance(tests, list):
                    raise RuntimeError("tests must be a list")
                tests = self.service.calculate_tests(
                    changed_files,
                    [str(item) for item in backend_systems],
                    [str(item) for item in tests],
                )
                self._write_json(HTTPStatus.OK, {"tests": tests, "count": len(tests)})
                return

            if self.path == "/add-tests":
                tests = payload.get("tests", [])
                if not isinstance(tests, list):
                    raise RuntimeError("tests must be a list")
                added = self.service.add_tests([str(item) for item in tests])
                self._write_json(HTTPStatus.OK, {"result": "ok", "added": added})
                return

            if self.path == "/mongodb-updated":
                self.service.mongodb_updated()
                self._write_json(HTTPStatus.OK, {"result": "ok"})
                return

            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except subprocess.CalledProcessError as exc:
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "error": "refresh command failed",
                    "returncode": exc.returncode,
                    "stdout": (exc.stdout or "").strip(),
                    "stderr": (exc.stderr or "").strip(),
                },
            )
        except subprocess.TimeoutExpired:
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "refresh command timed out"})
        except Exception as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="test execution lister api service")
    parser.add_argument("--config", required=True, type=str)
    parser.add_argument("--host", default="127.0.0.1", type=str)
    parser.add_argument("--port", default=8181, type=int)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)
    ApiHandler.service = TestExecutionService(config)

    server = ThreadingHTTPServer((args.host, args.port), ApiHandler)
    logger.info("starting api server on %s:%s", args.host, args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()

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

logger = logging.getLogger(__name__)


class TestExecutionService:
    """Core logic behind API operations."""

    def __init__(self, config: dict[str, Any]):
        self.spread_tests_execution_path = Path(config["spread_tests_execution_path"])
        self.added_spread_tests_path = Path(config["added_spread_tests_path"])
        self.base_tests_path = Path(config.get("base_tests_path", ""))
        self.refresh_command = config["refresh_command"]
        self.refresh_timeout_seconds = int(config.get("refresh_timeout_seconds", 300))
        self._add_tests_lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    @staticmethod
    def _is_go_source(path: str) -> bool:
        return path.endswith(".go")

    @staticmethod
    def _load_json(path: Path) -> dict:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise RuntimeError("spread-tests-execution.json must contain a top-level object")
        return data

    def _load_added_tests(self) -> list[str]:
        if not self.added_spread_tests_path.exists():
            return []
        lines = self.added_spread_tests_path.read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip()]

    def _load_base_tests(self) -> dict[str, list[str]]:
        if not self.base_tests_path.exists():
            return {}
        with self.base_tests_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise RuntimeError("base-tests.json must contain a top-level object")

        normalized: dict[str, list[str]] = {}
        for backend_system, tests in data.items():
            if not isinstance(tests, list):
                continue
            normalized[str(backend_system)] = [str(test).strip() for test in tests if str(test).strip()]
        return normalized

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

    def calculate_tests(self, changed_files: list[str], backend_systems: list[str]) -> list[str]:
        mapping = self._load_json(self.spread_tests_execution_path)
        base_tests = self._load_base_tests()
        go_files = [path for path in changed_files if self._is_go_source(path)]

        resolved_tests: list[str] = []

        for backend_system in backend_systems:
            for base_test in base_tests.get(backend_system, []):
                resolved_tests.append(base_test)

        added_tests = self._load_added_tests()
        for backend_system in backend_systems:
            for added in added_tests:
                resolved_tests.append(f"{backend_system}:{added}")

        for backend_system in backend_systems:
            backend_map = mapping.get(backend_system)
            if not isinstance(backend_map, dict):
                continue
            for changed_file in go_files:
                tests = backend_map.get(changed_file, [])
                if not isinstance(tests, list):
                    continue
                for test in tests:
                    if isinstance(test, str) and test.strip():
                        resolved_tests.append(test.strip())

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
                if not isinstance(changed_files, list) or not isinstance(backend_systems, list):
                    raise RuntimeError("changed_files and backend_systems must be lists")
                tests = self.service.calculate_tests(
                    [str(item) for item in changed_files],
                    [str(item) for item in backend_systems],
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

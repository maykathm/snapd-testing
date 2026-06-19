#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Canonical Ltd

"""Machine charm for test execution lister service."""

import json
import logging
import os
import shlex
import subprocess
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from ops.charm import ActionEvent, CharmBase, SecretChangedEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

logger = logging.getLogger(__name__)


class TestExecutionListerCharm(CharmBase):
    """Machine charm that exposes API for test selection and map refresh."""

    SERVICE_NAME = "test-execution-lister-api"
    SYSTEMD_DIR = Path("/etc/systemd/system")
    STATE_DIR = Path("/var/lib/test-execution-lister")
    SERVICE_CONFIG_PATH = STATE_DIR / "service-config.json"
    MONGODB_CREDENTIALS_PATH = STATE_DIR / "mongodb-credentials.json"
    QUERY_FEATURES_MARKER_PATH = STATE_DIR / "query-features-requirements.marker"
    SERVICE_STATE_MARKER_PATH = STATE_DIR / "service-state.marker"

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        self.framework.observe(self.on.secret_changed, self._on_secret_changed)

        self.framework.observe(self.on.calculate_tests_action, self._on_calculate_tests_action)
        self.framework.observe(self.on.add_tests_action, self._on_add_tests_action)
        self.framework.observe(self.on.mongodb_updated_action, self._on_mongodb_updated_action)
        self.framework.observe(self.on.reconcile_action, self._on_reconcile_action)
        self.framework.observe(self.on.service_status_action, self._on_service_status_action)

    @property
    def app_root(self) -> Path:
        return Path(self.charm_dir)

    @property
    def service_script(self) -> Path:
        return self.app_root / "src" / "service_api.py"

    @property
    def refresh_helper_script(self) -> Path:
        return self.app_root / "src" / "refresh_spread_tests_execution.py"

    @property
    def spread_tests_execution_path(self) -> Path:
        configured = str(self.config.get("spread-tests-execution-path", "")).strip()
        if configured:
            return Path(configured)
        return self.STATE_DIR / "spread-tests-execution.json"

    @property
    def added_spread_tests_path(self) -> Path:
        configured = str(self.config.get("added-spread-tests-path", "")).strip()
        if configured:
            return Path(configured)
        return self.STATE_DIR / "added-spread-tests.txt"

    @property
    def base_tests_path(self) -> Path:
        configured = str(self.config.get("base-tests-path", "")).strip()
        if configured:
            return Path(configured)
        return self.STATE_DIR / "base-tests.json"

    @property
    def query_features_requirements_path(self) -> Path:
        configured = str(self.config.get("query-features-requirements-path") or "").strip()
        if configured:
            return Path(configured)
        return self._query_features_path().with_name("requirements.txt")

    def _on_install(self, _event):
        self._reconcile("installing test-execution-lister")

    def _on_start(self, _event):
        self._reconcile("starting test-execution-lister")

    def _on_config_changed(self, _event):
        self._reconcile("applying updated configuration")

    def _on_upgrade_charm(self, _event):
        self._reconcile("upgrading charm configuration")

    def _on_secret_changed(self, _event: SecretChangedEvent):
        self._reconcile("processing secret update")

    def _on_reconcile_action(self, event: ActionEvent):
        try:
            self._reconcile("reconciling service")
            event.set_results({"result": "reconcile completed"})
        except Exception as exc:
            event.fail(str(exc))

    def _on_service_status_action(self, event: ActionEvent):
        proc = self._run(["systemctl", "is-active", self.SERVICE_NAME], check=False)
        status = (proc.stdout or "").strip() or "unknown"
        event.set_results({"service": self.SERVICE_NAME, "status": status})

    def _on_calculate_tests_action(self, event: ActionEvent):
        changed_files = self._parse_csv_or_lines(str(event.params.get("changed-files") or ""))
        backend_systems = self._parse_csv_or_lines(str(event.params.get("backend-systems") or ""))
        if not backend_systems:
            event.fail("backend-systems must contain at least one backend:system")
            return

        response = self._call_service(
            endpoint="/calculate-tests",
            payload={"changed_files": changed_files, "backend_systems": backend_systems},
        )
        event.set_results(
            {
                "tests": "\n".join(response.get("tests", [])),
                "count": str(response.get("count", 0)),
            }
        )

    def _on_add_tests_action(self, event: ActionEvent):
        tests = self._parse_csv_or_lines(str(event.params.get("tests") or ""))
        if not tests:
            event.fail("tests must contain at least one test path")
            return
        response = self._call_service(endpoint="/add-tests", payload={"tests": tests})
        event.set_results({"result": response.get("result", "ok"), "added": str(response.get("added", 0))})

    def _on_mongodb_updated_action(self, event: ActionEvent):
        response = self._call_service(endpoint="/mongodb-updated", payload={})
        event.set_results({"result": response.get("result", "ok")})

    def _reconcile(self, maintenance_msg: str):
        self.unit.status = MaintenanceStatus(maintenance_msg)
        try:
            self._ensure_state_dirs()
            self._install_query_features_dependencies()
            self._write_mongodb_credentials_file()
            service_state_changed = self._write_service_config()
            if service_state_changed:
                self._configure_systemd_service()
            self._sync_open_port()
            self.unit.status = ActiveStatus("api ready")
        except FileNotFoundError as exc:
            self.unit.status = BlockedStatus(str(exc))
            raise
        except RuntimeError as exc:
            self.unit.status = BlockedStatus(str(exc))
            raise
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            cmd = " ".join(exc.cmd) if isinstance(exc.cmd, list) else str(exc.cmd)
            msg = f"command failed: {cmd}"
            if stderr:
                msg = f"{msg}: {stderr}"
            self.unit.status = WaitingStatus(msg)
            raise RuntimeError(msg) from exc

    def _ensure_state_dirs(self) -> None:
        self.STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.spread_tests_execution_path.parent.mkdir(parents=True, exist_ok=True)
        self.added_spread_tests_path.parent.mkdir(parents=True, exist_ok=True)
        self.base_tests_path.parent.mkdir(parents=True, exist_ok=True)

    def _write_service_config(self) -> bool:
        config = {
            "spread_tests_execution_path": str(self.spread_tests_execution_path),
            "added_spread_tests_path": str(self.added_spread_tests_path),
            "base_tests_path": str(self.base_tests_path),
            "credentials_path": str(self.MONGODB_CREDENTIALS_PATH),
            "query_features_path": str(self._query_features_path()),
            "refresh_command": self._refresh_command(),
            "refresh_timeout_seconds": int(self.config.get("refresh-timeout-seconds", 300)),
        }
        config_json = json.dumps(config, indent=2, sort_keys=True)
        service_state = self._service_state_marker_value(config)
        previous_state = self.SERVICE_STATE_MARKER_PATH.read_text(encoding="utf-8").strip() if self.SERVICE_STATE_MARKER_PATH.exists() else ""

        if self.SERVICE_CONFIG_PATH.exists() and previous_state == service_state:
            return False

        self.SERVICE_CONFIG_PATH.write_text(config_json, encoding="utf-8")
        self.SERVICE_STATE_MARKER_PATH.write_text(service_state, encoding="utf-8")
        return True

    def _install_query_features_dependencies(self) -> None:
        requirements_path = self.query_features_requirements_path
        if not requirements_path.exists():
            raise FileNotFoundError(f"query_features requirements file not found: {requirements_path}")

        marker_value = self._requirements_marker_value(requirements_path)
        if self.QUERY_FEATURES_MARKER_PATH.exists():
            current_marker = self.QUERY_FEATURES_MARKER_PATH.read_text(encoding="utf-8").strip()
            if current_marker == marker_value:
                return

        self._run(
            [
                "python3",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                str(requirements_path),
            ],
            check=True,
        )
        self.QUERY_FEATURES_MARKER_PATH.write_text(marker_value, encoding="utf-8")

    @staticmethod
    def _requirements_marker_value(requirements_path: Path) -> str:
        stat_result = requirements_path.stat()
        return f"{requirements_path}:{stat_result.st_mtime_ns}:{stat_result.st_size}"

    def _service_state_marker_value(self, config: dict[str, object]) -> str:
        relevant_state = {
            "listen-host": str(self.config.get("listen-host") or "127.0.0.1"),
            "listen-port": int(self.config.get("listen-port") or 8181),
            "mongodb-secret-id": str(self.config.get("mongodb-secret-id") or "").strip(),
            "query-features-path": str(self._query_features_path()),
            "query-features-requirements-path": str(self.query_features_requirements_path),
            "spread-tests-execution-path": str(self.spread_tests_execution_path),
            "added-spread-tests-path": str(self.added_spread_tests_path),
            "base-tests-path": str(self.base_tests_path),
            "base-tests-max-minutes": int(self.config.get("base-tests-max-minutes", 0)),
            "base-tests-exclude": self._parse_csv_or_lines(str(self.config.get("base-tests-exclude") or "")),
            "refresh-helper-command": str(self.config.get("refresh-helper-command") or "").strip(),
            "refresh-timeout-seconds": int(self.config.get("refresh-timeout-seconds", 300)),
            "refresh-command": config["refresh_command"],
        }
        return json.dumps(relevant_state, sort_keys=True)

    def _query_features_path(self) -> Path:
        configured = str(self.config.get("query-features-path") or "").strip()
        if configured:
            return Path(configured)
        return Path("/home/ubuntu/source/snapd/tests/utils/features/query_features.py")

    def _refresh_command(self) -> list[str]:
        configured = str(self.config.get("refresh-helper-command") or "").strip()
        if configured:
            return shlex.split(configured)
        exclude = self._parse_csv_or_lines(str(self.config.get("base-tests-exclude") or ""))
        return [
            "python3",
            str(self.refresh_helper_script),
            "--query-features-path",
            str(self._query_features_path()),
            "--credentials-path",
            str(self.MONGODB_CREDENTIALS_PATH),
            "--output-path",
            str(self.spread_tests_execution_path),
            "--base-tests-output-path",
            str(self.base_tests_path),
            "--base-tests-max-minutes",
            str(int(self.config.get("base-tests-max-minutes", 0))),
            "--base-tests-exclude",
            *exclude,
        ]

    def _write_mongodb_credentials_file(self) -> None:
        secret_id = str(self.config.get("mongodb-secret-id") or "").strip()
        if not secret_id:
            raise RuntimeError("mongodb-secret-id must be configured")

        try:
            secret = self.model.get_secret(id=secret_id)
            content = secret.get_content(refresh=True)
        except Exception as exc:
            raise RuntimeError(f"could not access mongodb secret {secret_id}: {exc}") from exc

        credentials = self._extract_credentials_json(content)
        self.MONGODB_CREDENTIALS_PATH.write_text(json.dumps(credentials, indent=2), encoding="utf-8")
        os.chmod(self.MONGODB_CREDENTIALS_PATH, 0o600)

    @staticmethod
    def _extract_credentials_json(secret_content: dict[str, str]) -> dict:
        string_keys = (
            "mongodb-credentials-json",
            "mongodb_credentials_json",
            "credentials-json",
            "credentials_json",
            "json",
        )

        for key in string_keys:
            raw = secret_content.get(key)
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"secret key {key} does not contain valid json: {exc}") from exc
            if not isinstance(parsed, dict):
                raise RuntimeError(f"secret key {key} must contain a json object")
            TestExecutionListerCharm._validate_mongodb_creds(parsed)
            return parsed

        direct = {k: secret_content.get(k) for k in ("host", "port", "user", "password") if secret_content.get(k)}
        if len(direct) == 4:
            if isinstance(direct.get("port"), str) and direct["port"].isdigit():
                direct["port"] = int(direct["port"])
            TestExecutionListerCharm._validate_mongodb_creds(direct)
            return direct

        raise RuntimeError(
            "mongodb secret must provide a json string in one of: "
            "mongodb-credentials-json, mongodb_credentials_json, credentials-json, credentials_json, json"
        )

    @staticmethod
    def _validate_mongodb_creds(creds: dict) -> None:
        missing = [k for k in ("host", "port", "user", "password") if k not in creds]
        if missing:
            raise RuntimeError(f"mongodb credentials are missing keys: {', '.join(missing)}")

    def _render_systemd_unit(self) -> str:
        host = str(self.config.get("listen-host") or "127.0.0.1")
        port = int(self.config.get("listen-port") or 8181)
        return f"""[Unit]
Description=test-execution-lister api service
After=network.target

[Service]
Type=simple
User=root
Group=root
WorkingDirectory={self.app_root}
ExecStart=/usr/bin/python3 {self.service_script} --config {self.SERVICE_CONFIG_PATH} --host {host} --port {port}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""

    def _configure_systemd_service(self) -> None:
        if not self.service_script.exists():
            raise FileNotFoundError(f"service script not found: {self.service_script}")
        if not self.refresh_helper_script.exists():
            raise FileNotFoundError(f"refresh helper script not found: {self.refresh_helper_script}")

        unit_path = self.SYSTEMD_DIR / f"{self.SERVICE_NAME}.service"
        unit_path.write_text(self._render_systemd_unit(), encoding="utf-8")
        self._run(["systemctl", "daemon-reload"], check=True)
        self._run(["systemctl", "enable", "--now", self.SERVICE_NAME], check=True)
        self._run(["systemctl", "restart", self.SERVICE_NAME], check=True)

    def _sync_open_port(self) -> None:
        port = int(self.config.get("listen-port") or 8181)
        self.unit.open_port("tcp", port)
        for opened in self.unit.opened_ports():
            if opened.protocol == "tcp" and opened.port != port:
                self.unit.close_port("tcp", opened.port)

    @staticmethod
    def _parse_csv_or_lines(raw: str) -> list[str]:
        if not raw:
            return []
        items: list[str] = []
        for chunk in raw.replace("\r", "\n").split("\n"):
            for item in chunk.split(","):
                item = item.strip()
                if item:
                    items.append(item)
        return items

    def _call_service(self, endpoint: str, payload: dict) -> dict:
        host = str(self.config.get("listen-host") or "127.0.0.1")
        port = int(self.config.get("listen-port") or 8181)
        url = f"http://{host}:{port}{endpoint}"
        req = urllib_request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(req, timeout=30) as response:
                body = response.read().decode("utf-8")
        except urllib_error.HTTPError as exc:
            details = exc.read().decode("utf-8")
            raise RuntimeError(f"service request failed ({exc.code}): {details}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"service request failed: {exc}") from exc
        return json.loads(body)

    def _run(self, cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
        logger.info("Running command: %s", shlex.join(cmd))
        return subprocess.run(cmd, check=check, capture_output=True, text=True)


if __name__ == "__main__":
    main(TestExecutionListerCharm)

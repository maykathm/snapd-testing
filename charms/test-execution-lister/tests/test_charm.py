#!/usr/bin/env python3

import json
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

import ops  # noqa: E402
from ops.testing import Harness  # noqa: E402

from charm import TestExecutionListerCharm  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", message=".*Harness.*")

CREDENTIALS = {"host": "mongo.internal", "port": 27017, "user": "snapd", "password": "secret"}


class FakeSystem:
    """Records commands and fakes their effects on a temporary state dir."""

    def __init__(self, charm_cls, root: Path):
        self.charm_cls = charm_cls
        self.root = root
        self.commands: list[list[str]] = []
        self.installed_packages = {"git", "python3-venv"}
        self.pymongo_ok = True
        self.fail_on: str | None = None

    def run(self, cmd, check=False, env=None):
        self.commands.append(list(cmd))
        joined = " ".join(cmd)
        if self.fail_on and self.fail_on in joined:
            if check:
                raise subprocess.CalledProcessError(1, cmd, stderr="boom")
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        out, rc = "", 0
        if cmd[0] == "dpkg-query":
            out = "install ok installed" if cmd[-1] in self.installed_packages else ""
        elif cmd[:3] == ["apt-get", "install", "-y"]:
            self.installed_packages.update(c for c in cmd[3:] if not c.startswith("-"))
        elif cmd[:3] == ["python3", "-m", "venv"]:
            python = Path(cmd[3]) / "bin" / "python3"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("")
        elif cmd[1:2] == ["-c"]:
            rc = 0 if self.pymongo_ok else 1
        elif "pip" in cmd and "install" in cmd:
            self.pymongo_ok = True
        elif cmd[:2] == ["git", "init"]:
            (Path(cmd[-1]) / ".git").mkdir(parents=True, exist_ok=True)
        elif cmd[:1] == ["git"] and "checkout" in cmd:
            features = Path(cmd[2]) / self.charm_cls.FEATURES_SUBDIR
            features.mkdir(parents=True, exist_ok=True)
            (features / "query_features.py").write_text("")
        return subprocess.CompletedProcess(cmd, rc, out, "")

    def ran(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.commands if c[: len(prefix)] == list(prefix)]


class CharmTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        state = root / "state"
        systemd = root / "systemd"
        systemd.mkdir()
        patcher = patch.multiple(
            TestExecutionListerCharm,
            STATE_DIR=state,
            SYSTEMD_DIR=systemd,
            SERVICE_CONFIG_PATH=state / "service-config.json",
            MONGODB_CREDENTIALS_PATH=state / "mongodb-credentials.json",
            SERVICE_STATE_MARKER_PATH=state / "service-state.marker",
            SNAPD_DIR=state / "snapd",
            VENV_DIR=state / "venv",
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.system = FakeSystem(TestExecutionListerCharm, root)
        run_patcher = patch.object(TestExecutionListerCharm, "_run", lambda _self, cmd, check=False, env=None: self.system.run(cmd, check, env))
        run_patcher.start()
        self.addCleanup(run_patcher.stop)

        self.harness = Harness(TestExecutionListerCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.update_config({
            "execution-data-dir": str(state),
            "added-spread-tests-path": str(state / "added-spread-tests.txt"),
        })

    def tearDown(self):
        self.tmp.cleanup()

    def configure_secret(self):
        secret_id = self.harness.add_user_secret({"json": json.dumps(CREDENTIALS)})
        self.harness.grant_secret(secret_id, self.harness.model.app.name)
        self.harness.update_config({"mongodb-secret-id": secret_id})


class TestReconcile(CharmTestCase):
    def test_install_without_secret_blocks_instead_of_failing(self):
        self.harness.begin()
        # must not raise, which would put the unit in error
        self.harness.charm.on.install.emit()
        self.assertIsInstance(self.harness.model.unit.status, ops.BlockedStatus)
        self.assertIn("mongodb-secret-id", self.harness.model.unit.status.message)

    def test_install_sets_up_everything(self):
        self.system.installed_packages = set()
        self.system.pymongo_ok = False
        self.configure_secret()
        self.harness.begin()
        self.harness.charm.on.install.emit()
        charm = self.harness.charm

        self.assertEqual(self.harness.model.unit.status, ops.ActiveStatus("api ready"))
        apt = self.system.ran("apt-get", "install")
        self.assertEqual(len(apt), 1)
        self.assertIn("git", apt[0])
        self.assertIn("python3-venv", apt[0])
        self.assertEqual(len(self.system.ran("python3", "-m", "venv")), 1)
        pip = [c for c in self.system.commands if "pip" in c]
        self.assertEqual(len(pip), 1)
        self.assertIn("pymongo>=4.9", pip[0])
        self.assertNotIn("-r", pip[0])

        repo = str(charm.SNAPD_DIR)
        self.assertIn(["git", "-C", repo, "remote", "add", "origin", "https://github.com/canonical/snapd.git"], self.system.commands)
        self.assertIn(["git", "-C", repo, "sparse-checkout", "set", "tests/utils/features"], self.system.commands)
        self.assertIn(["git", "-C", repo, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", "master"], self.system.commands)

        config = json.loads(charm.SERVICE_CONFIG_PATH.read_text())
        self.assertEqual(config["query_features_path"], str(charm.SNAPD_DIR / "tests/utils/features/query_features.py"))
        self.assertEqual(config["refresh_command"][0], str(charm.VENV_DIR / "bin" / "python3"))
        self.assertEqual(json.loads(charm.MONGODB_CREDENTIALS_PATH.read_text()), CREDENTIALS)
        self.assertTrue(self.system.ran("systemctl", "restart"))

    def test_reconcile_skips_installed_packages_and_venv(self):
        self.configure_secret()
        self.harness.begin()
        self.harness.charm.on.install.emit()
        self.assertFalse(self.system.ran("apt-get"))
        self.assertFalse([c for c in self.system.commands if "pip" in c])

    def test_existing_checkout_is_updated_with_configured_ref(self):
        self.configure_secret()
        self.harness.begin()
        self.harness.charm.on.install.emit()
        self.system.commands.clear()
        self.harness.update_config({"snapd-repo-url": "https://github.com/me/snapd.git", "snapd-ref": "my-branch"})
        repo = str(self.harness.charm.SNAPD_DIR)
        self.assertFalse(self.system.ran("git", "init"))
        self.assertIn(["git", "-C", repo, "remote", "set-url", "origin", "https://github.com/me/snapd.git"], self.system.commands)
        self.assertIn(["git", "-C", repo, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", "my-branch"], self.system.commands)

    def test_query_features_path_skips_fetch(self):
        self.configure_secret()
        self.harness.update_config({"query-features-path": "/srv/snapd/tests/utils/features/query_features.py"})
        self.harness.begin()
        self.harness.charm.on.install.emit()
        self.assertFalse(self.system.ran("git"))
        config = json.loads(self.harness.charm.SERVICE_CONFIG_PATH.read_text())
        self.assertEqual(config["query_features_path"], "/srv/snapd/tests/utils/features/query_features.py")

    def test_failed_command_waits_instead_of_failing(self):
        self.configure_secret()
        self.system.fail_on = "fetch"
        self.harness.begin()
        self.harness.charm.on.install.emit()
        self.assertIsInstance(self.harness.model.unit.status, ops.WaitingStatus)
        self.assertIn("git", self.harness.model.unit.status.message)

    def test_upgrade_restarts_service_even_without_config_change(self):
        self.configure_secret()
        self.harness.begin()
        self.harness.charm.on.install.emit()
        self.system.commands.clear()
        self.harness.charm.on.config_changed.emit()
        self.assertFalse(self.system.ran("systemctl", "restart"))
        self.harness.charm.on.upgrade_charm.emit()
        self.assertTrue(self.system.ran("systemctl", "restart"))


class TestActions(CharmTestCase):
    def setUp(self):
        super().setUp()
        self.configure_secret()
        self.harness.begin()
        self.harness.charm.on.install.emit()

    def test_mongodb_updated_fetches_first_and_waits_for_the_refresh(self):
        self.harness.update_config({"refresh-timeout-seconds": 600})
        self.system.commands.clear()
        with patch.object(TestExecutionListerCharm, "_call_service", return_value={"result": "ok"}) as call:
            output = self.harness.run_action("mongodb-updated")
        self.assertEqual(output.results, {"result": "ok"})
        self.assertTrue(self.system.ran("git", "-C"))
        call.assert_called_once_with(endpoint="/mongodb-updated", payload={}, timeout=660)

    def test_mongodb_updated_reports_failures(self):
        with patch.object(TestExecutionListerCharm, "_call_service", side_effect=RuntimeError("service request failed: timed out")):
            with self.assertRaises(ops.testing.ActionFailed) as failed:
                self.harness.run_action("mongodb-updated")
        self.assertIn("timed out", failed.exception.message)

        self.system.fail_on = "fetch"
        with self.assertRaises(ops.testing.ActionFailed) as failed:
            self.harness.run_action("mongodb-updated")
        self.assertIn("command failed: git", failed.exception.message)

    def test_calculate_tests_reports_service_errors(self):
        params = {"changed-files-json": "{}", "tests": "tests/main/a", "backend-systems": "s1"}
        with patch.object(TestExecutionListerCharm, "_call_service", side_effect=RuntimeError("service request failed")):
            with self.assertRaises(ops.testing.ActionFailed) as failed:
                self.harness.run_action("calculate-tests", params)
        self.assertIn("service request failed", failed.exception.message)

    def test_reconcile_action_fails_with_reason(self):
        self.system.fail_on = "fetch"
        with self.assertRaises(ops.testing.ActionFailed) as failed:
            self.harness.run_action("reconcile")
        self.assertIn("command failed: git", failed.exception.message)


if __name__ == "__main__":
    unittest.main()

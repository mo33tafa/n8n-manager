import argparse
import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import n8n_manager as app


class ValidationTests(unittest.TestCase):
    def test_exact_stable_versions_only(self):
        self.assertEqual(app.version("2.1.0"), "2.1.0")
        for value in ("latest", "stable", "2", "2.1", "2.1.0;id", "2.1.0-beta", "../../x"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                app.version(value)

    def test_domains(self):
        self.assertEqual(app.domain_name("N8N.Example.COM."), "n8n.example.com")
        for value in ("https://example.com", "localhost", "x.com/path", "x.com:443", "a.*", "-x.com", "a..com", "127.0.0.1", "a.com\n{x}"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                app.domain_name(value)

    def test_timezone(self):
        self.assertEqual(app.timezone_name("Asia/Tehran"), "Asia/Tehran")
        with self.assertRaises(argparse.ArgumentTypeError):
            app.timezone_name("../../etc/passwd")

    def test_parser_requires_pinned_version(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            app.parser().parse_args(["install"])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "installation with spaces"
        self.directory.mkdir()
        self.config = {"version": "2.1.0", "domain": "", "timezone": "UTC", "project": "n8n-012345abcdef"}
        self.manager = app.Manager(self.directory)

    def initialize(self):
        app.save_config(self.directory, self.config)
        app.write_private(self.directory / ".env", "POSTGRES_PASSWORD=test-only\nN8N_ENCRYPTION_KEY=test-only\n")

    def test_local_access_is_loopback_only(self):
        spec = app.compose_spec(self.config)
        self.assertEqual(spec["services"]["n8n"]["ports"], ["127.0.0.1:5678:5678"])
        self.assertNotIn("ports", spec["services"]["db"])
        self.assertNotIn("proxy", spec["services"])

    def test_domain_mode_does_not_publish_application_port(self):
        spec = app.compose_spec(dict(self.config, domain="n8n.example.com"))
        self.assertNotIn("ports", spec["services"]["n8n"])
        self.assertEqual(spec["services"]["proxy"]["ports"], ["80:80", "443:443"])
        self.assertEqual(spec["services"]["n8n"]["environment"]["N8N_PROXY_HOPS"], "1")
        self.assertNotIn("docker.sock", json.dumps(spec))

    def test_no_legacy_basic_auth_or_hardcoded_password(self):
        spec = app.compose_spec(self.config)
        env = spec["services"]["n8n"]["environment"]
        self.assertFalse(any(key.startswith("N8N_BASIC_AUTH") for key in env))
        self.assertTrue(env["N8N_ENCRYPTION_KEY"].startswith("${"))
        self.assertEqual(env["EXECUTIONS_DATA_PRUNE"], "true")

    def test_services_have_log_rotation_and_health_checks(self):
        spec = app.compose_spec(self.config)
        for service in spec["services"].values():
            self.assertEqual(service["logging"]["options"]["max-size"], "10m")
            self.assertIn("healthcheck", service)
        self.assertEqual(spec["services"]["n8n"]["depends_on"]["db"]["condition"], "service_healthy")

    def test_private_atomic_file_permissions(self):
        target = self.directory / "private"
        app.write_private(target, "first")
        app.write_private(target, "second")
        self.assertEqual(target.read_text(), "second")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertFalse(list(self.directory.glob(".write-*")))

    def test_save_domain_config(self):
        config = dict(self.config, domain="n8n.example.com")
        app.save_config(self.directory, config)
        self.assertEqual(json.loads((self.directory / "compose.json").read_text()), app.compose_spec(config))
        self.assertEqual((self.directory / "Caddyfile").read_text(), "n8n.example.com {\n    reverse_proxy n8n:5678\n}\n")

    def test_missing_installation(self):
        with self.assertRaises(app.ManagerError):
            self.manager.config()

    def test_corrupted_config(self):
        (self.directory / "settings.json").write_text("{broken")
        with self.assertRaises(app.ManagerError):
            self.manager.config()

    def test_invalid_project_is_rejected(self):
        app.save_config(self.directory, dict(self.config, project="other-project"))
        with self.assertRaises(app.ManagerError):
            self.manager.config()

    def test_compose_uses_argument_array_and_explicit_project(self):
        self.initialize()
        with patch("n8n_manager.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.manager.compose("ps")
        args = run.call_args.args[0]
        self.assertIsInstance(args, list)
        self.assertEqual(args[args.index("--project-directory") + 1], str(self.directory))
        self.assertEqual(args[args.index("--project-name") + 1], self.config["project"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_missing_docker_error(self):
        with patch("n8n_manager.subprocess.run", side_effect=FileNotFoundError), self.assertRaises(app.ManagerError):
            self.manager.doctor()

    def test_failed_docker_command(self):
        with patch("n8n_manager.subprocess.run", return_value=subprocess.CompletedProcess([], 1)), self.assertRaises(app.ManagerError):
            self.manager.docker("info")

    def test_install_refuses_nonempty_directory(self):
        (self.directory / "keep").write_text("keep")
        with self.assertRaises(app.ManagerError):
            self.manager.install(argparse.Namespace(version="2.1.0", domain="", timezone="UTC"))
        self.assertEqual((self.directory / "keep").read_text(), "keep")

    def test_install_creates_independent_secrets_and_checks_health(self):
        args = argparse.Namespace(version="2.1.0", domain="", timezone="UTC")
        with patch.object(self.manager, "docker") as docker, contextlib.redirect_stdout(io.StringIO()):
            self.manager.install(args)
        entries = dict(line.split("=", 1) for line in (self.directory / ".env").read_text().splitlines())
        self.assertNotEqual(entries["POSTGRES_PASSWORD"], entries["N8N_ENCRYPTION_KEY"])
        self.assertEqual(len(entries["N8N_ENCRYPTION_KEY"]), 64)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        self.assertTrue(any("--wait" in call.args for call in docker.call_args_list))

    def test_backup_success(self):
        self.initialize()

        def compose(*args, stdout=None):
            if args[0] == "ps":
                stdout.write("n8n\ndb\n")
            elif stdout is not None:
                stdout.write(b"fixture-backup-data")

        with patch.object(self.manager, "compose", side_effect=compose) as command, contextlib.redirect_stdout(io.StringIO()):
            backup = self.manager.backup()
        self.assertTrue((backup / "COMPLETE").exists())
        self.assertTrue((backup / ".env").exists())
        self.assertEqual((backup / "database.dump").read_bytes(), b"fixture-backup-data")
        self.assertIn(unittest.mock.call("start", "n8n"), command.call_args_list)

    def test_backup_failure_restarts_n8n_and_has_no_complete_marker(self):
        self.initialize()

        def compose(*args, stdout=None):
            if args[0] == "ps":
                stdout.write("n8n\ndb\n")
            if args[0] == "exec":
                raise app.ManagerError("Dump failed")

        with patch.object(self.manager, "compose", side_effect=compose) as command:
            with self.assertRaises(app.ManagerError):
                self.manager.backup()
        self.assertIn(unittest.mock.call("start", "n8n"), command.call_args_list)
        self.assertFalse(list((self.directory / "backups").glob("*/COMPLETE")))

    def test_backup_refuses_stopped_services(self):
        self.initialize()
        with patch.object(self.manager, "compose"), self.assertRaises(app.ManagerError):
            self.manager.backup()
        self.assertEqual(list((self.directory / "backups").iterdir()), [])

    def test_update_pull_failure_preserves_current_config(self):
        self.initialize()
        with patch.object(self.manager, "backup"), patch.object(self.manager, "docker", side_effect=app.ManagerError("Pull failed")), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(app.ManagerError):
                self.manager.update("2.2.0")
        self.assertEqual(self.manager.config()["version"], "2.1.0")
        self.assertFalse((self.directory / "candidate.json").exists())

    def test_update_downgrade_is_blocked(self):
        self.initialize()
        with self.assertRaises(app.ManagerError):
            self.manager.update("1.99.0")

    def test_update_creates_backup_before_switching(self):
        self.initialize()
        events = []
        with patch.object(self.manager, "backup", side_effect=lambda: events.append("backup")), patch.object(self.manager, "docker", side_effect=lambda *a, **kw: events.append("pull")), patch.object(self.manager, "start", side_effect=lambda: events.append("start")), contextlib.redirect_stdout(io.StringIO()):
            self.manager.update("2.2.0")
        self.assertEqual(events, ["backup", "pull", "start"])
        self.assertEqual(self.manager.config()["version"], "2.2.0")

    def test_lock_prevents_concurrent_operations(self):
        with app.installation_lock(self.directory):
            with self.assertRaises(app.ManagerError):
                with app.installation_lock(self.directory):
                    self.fail("Second lock should not be acquired")

    def test_uninstall_requires_confirmation_and_preserves_volumes(self):
        self.initialize()
        with patch.object(app.Manager, "compose") as command, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.main(["--directory", str(self.directory), "uninstall"]), 1)
            command.assert_not_called()
            self.assertEqual(app.main(["--directory", str(self.directory), "uninstall", "--confirm"]), 0)
            command.assert_called_once_with("down")
        self.assertTrue((self.directory / ".env").exists())


if __name__ == "__main__":
    unittest.main()

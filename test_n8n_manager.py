import argparse
import contextlib
from html.parser import HTMLParser
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


class MenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "managed"
        self.output = io.StringIO()

    def run_menu(self, answers, result=0):
        with patch("builtins.input", side_effect=answers), patch.object(app, "main", return_value=result) as command, contextlib.redirect_stdout(self.output):
            self.assertEqual(app.menu(self.directory), 0)
        return command

    def test_no_command_opens_menu_without_creating_directory(self):
        with patch.object(app, "menu", return_value=0) as menu:
            self.assertEqual(app.main(["--directory", str(self.directory)]), 0)
        menu.assert_called_once_with(self.directory)
        self.assertFalse(self.directory.exists())

    def test_explicit_menu_command(self):
        with patch.object(app, "menu", return_value=0) as menu:
            self.assertEqual(app.main(["--directory", str(self.directory), "menu"]), 0)
        menu.assert_called_once_with(self.directory)

    def test_header_and_exit(self):
        command = self.run_menu(["0"])
        self.assertIn("ModernSoft", self.output.getvalue())
        command.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_persian_and_arabic_numbers(self):
        command = self.run_menu(["۶", "yes", "٦", "yes", "۰"])
        self.assertEqual(command.call_count, 2)
        command.assert_called_with(["--directory", str(self.directory), "status"])

    def test_install_wizard_retries_invalid_fields(self):
        command = self.run_menu(["1", "latest", "۲.۱.۰", "https://bad", "n8n.example.com", "Bad/Zone", "UTC", "yes", "0"])
        command.assert_called_once_with(["--directory", str(self.directory), "install", "--version", "2.1.0", "--domain", "n8n.example.com", "--timezone", "UTC"])
        self.assertIn("Invalid version", self.output.getvalue())

    def test_local_install_defaults(self):
        command = self.run_menu(["1", "2.1.0", "", "", "yes", "0"])
        command.assert_called_once_with(["--directory", str(self.directory), "install", "--version", "2.1.0", "--domain", "", "--timezone", "Asia/Tehran"])
        self.assertIn("SSH tunnel", self.output.getvalue())

    def test_cancel_wizard_with_q(self):
        command = self.run_menu(["1", "q", "0"])
        command.assert_not_called()

    def test_default_confirmation_is_no(self):
        command = self.run_menu(["10", "", "0"])
        command.assert_not_called()

    def test_uninstall_requires_confirmation(self):
        command = self.run_menu(["10", "yes", "0"])
        command.assert_called_once_with(["--directory", str(self.directory), "uninstall", "--confirm"])
        self.assertIn("will be retained", self.output.getvalue())

    def test_update_requires_exact_version_and_confirmation(self):
        command = self.run_menu(["9", "2.2.0", "y", "0"])
        command.assert_called_once_with(["--directory", str(self.directory), "update", "--version", "2.2.0"])
        self.assertIn("temporarily stop", self.output.getvalue())

    def test_all_simple_options(self):
        for choice, action in {"2": "doctor", "3": "start", "4": "stop", "5": "restart", "6": "status", "7": "logs", "8": "backup"}.items():
            with self.subTest(choice=choice):
                command = self.run_menu([choice, "yes", "0"])
                command.assert_called_once_with(["--directory", str(self.directory), action])

    def test_failed_operation_returns_to_menu(self):
        command = self.run_menu(["6", "yes", "0"], result=1)
        command.assert_called_once()
        self.assertIn("Operation did not complete", self.output.getvalue())
        self.assertGreaterEqual(self.output.getvalue().count("ModernSoft"), 2)

    def test_help_and_invalid_selection_do_not_run_commands(self):
        command = self.run_menu(["invalid", "12", "0"])
        command.assert_not_called()
        self.assertIn("Invalid option", self.output.getvalue())
        self.assertIn("Docker", self.output.getvalue())

    def test_change_directory(self):
        other = Path(self.temp.name) / "other installation"
        command = self.run_menu(["11", str(other), "6", "yes", "0"])
        command.assert_called_once_with(["--directory", str(other), "status"])
        self.assertFalse(other.exists())

    def test_root_directory_is_rejected(self):
        command = self.run_menu(["11", "/", "6", "yes", "0"])
        command.assert_called_once_with(["--directory", str(self.directory), "status"])

    def test_invalid_directory_returns_to_menu(self):
        command = self.run_menu(["11", "bad\x00path", "0"])
        command.assert_not_called()
        self.assertIn("valid, accessible directory", self.output.getvalue())

    def test_menu_output_is_english(self):
        self.run_menu(["12", "1", "latest", "2.1.0", "bad/domain", "", "Bad/Zone", "", "no",
                       "9", "2.2.0", "no", "invalid", "11", "/", "6", "yes", "0"])
        self.assertTrue(self.output.getvalue().isascii())
        with patch("builtins.input", return_value="yes") as prompt:
            self.assertTrue(app.menu_confirm("Continue?"))
        prompt.assert_called_once_with("Continue? [yes/no, default: no]: ")

    def test_eof_and_keyboard_interrupt_exit_cleanly(self):
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption):
                command = self.run_menu([interruption])
                command.assert_not_called()


class PreviewTests(unittest.TestCase):
    def test_preview_matches_actual_shell_menu(self):
        class TerminalParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_pre = False
                self.text = ""

            def handle_starttag(self, tag, attrs):
                if tag == "pre":
                    self.in_pre = True

            def handle_endtag(self, tag):
                if tag == "pre":
                    self.in_pre = False

            def handle_data(self, data):
                if self.in_pre:
                    self.text += data

        root = Path(__file__).parent
        preview = TerminalParser()
        preview.feed((root / "index.html").read_text())
        result = subprocess.run(["bash", str(root / "n8n.sh")], input="0\n", text=True,
                                capture_output=True, timeout=10, cwd=root, check=True)
        actual = result.stdout.split("Select an option:")[0].strip()
        expected = preview.text.split("$ bash n8n.sh", 1)[1].split("Select an option:")[0].strip()
        self.assertEqual(actual, expected)
        self.assertTrue(result.stdout.isascii())


if __name__ == "__main__":
    unittest.main()

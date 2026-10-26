#!/usr/bin/env python3
import argparse
import contextlib
import datetime
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ManagerError(Exception):
    pass


def version(value):
    if not re.fullmatch(r"[1-9]\d*\.\d+\.\d+", value):
        raise argparse.ArgumentTypeError("Use an explicit stable version, such as 2.1.0; floating tags are not allowed.")
    return value


def domain_name(value):
    value = value.lower().rstrip(".")
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise argparse.ArgumentTypeError("Enter a DNS domain without a scheme, port or path.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise argparse.ArgumentTypeError("Use a DNS domain, not an IP address.")


def timezone_name(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise argparse.ArgumentTypeError("Unknown IANA timezone.") from None
    return value


def compose_spec(config):
    domain = config["domain"]
    origin = f"https://{domain}" if domain else "http://localhost:5678"
    log = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    common = {"restart": "unless-stopped", "logging": log, "security_opt": ["no-new-privileges:true"]}
    spec = {
        "services": {
            "db": {
                **common,
                "image": "postgres:16-alpine",
                "environment": {
                    "POSTGRES_USER": "n8n", "POSTGRES_DB": "n8n",
                    "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?Missing database password}",
                },
                "volumes": ["database:/var/lib/postgresql/data"],
                "healthcheck": {
                    "test": ["CMD-SHELL", "pg_isready -U n8n -d n8n"],
                    "interval": "10s", "timeout": "5s", "retries": 12,
                },
            },
            "n8n": {
                **common,
                "image": f"docker.n8n.io/n8nio/n8n:{config['version']}",
                "init": True,
                "depends_on": {"db": {"condition": "service_healthy"}},
                "environment": {
                    "DB_TYPE": "postgresdb", "DB_POSTGRESDB_HOST": "db",
                    "DB_POSTGRESDB_PORT": "5432", "DB_POSTGRESDB_DATABASE": "n8n",
                    "DB_POSTGRESDB_USER": "n8n",
                    "DB_POSTGRESDB_PASSWORD": "${POSTGRES_PASSWORD:?Missing database password}",
                    "N8N_ENCRYPTION_KEY": "${N8N_ENCRYPTION_KEY:?Missing encryption key}",
                    "N8N_HOST": domain or "localhost", "N8N_PORT": "5678",
                    "N8N_PROTOCOL": "https" if domain else "http",
                    "N8N_PROXY_HOPS": "1" if domain else "0",
                    "N8N_EDITOR_BASE_URL": origin, "WEBHOOK_URL": origin + "/",
                    "N8N_SECURE_COOKIE": "true" if domain else "false",
                    "GENERIC_TIMEZONE": config["timezone"], "TZ": config["timezone"],
                    "N8N_DIAGNOSTICS_ENABLED": "false", "N8N_PERSONALIZATION_ENABLED": "false",
                    "N8N_ENFORCE_SETTINGS_FILE_PERMISSIONS": "true",
                    "EXECUTIONS_DATA_PRUNE": "true", "EXECUTIONS_DATA_MAX_AGE": "168",
                },
                "volumes": ["application:/home/node/.n8n"],
                "healthcheck": {
                    "test": ["CMD", "node", "-e", "require('http').get('http://127.0.0.1:5678/healthz/readiness',r=>process.exit(r.statusCode===200?0:1)).on('error',()=>process.exit(1))"],
                    "interval": "10s", "timeout": "5s", "retries": 30,
                    "start_period": "30s",
                },
            },
        },
        "volumes": {"database": {}, "application": {}},
    }
    if domain:
        spec["services"]["proxy"] = {
            **common, "image": "caddy:2-alpine", "ports": ["80:80", "443:443"],
            "volumes": ["./Caddyfile:/etc/caddy/Caddyfile:ro", "certificates:/data", "proxy_config:/config"],
            "depends_on": {"n8n": {"condition": "service_healthy"}},
        }
        spec["volumes"].update({"certificates": {}, "proxy_config": {}})
    else:
        spec["services"]["n8n"]["ports"] = ["127.0.0.1:5678:5678"]
    return spec


def write_private(path, text):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_config(directory, config):
    write_private(directory / "settings.json", json.dumps(config, indent=2) + "\n")
    write_private(directory / "compose.json", json.dumps(compose_spec(config), indent=2) + "\n")
    if config["domain"]:
        write_private(directory / "Caddyfile", f"{config['domain']} {{\n    reverse_proxy n8n:5678\n}}\n")


class Manager:
    def __init__(self, directory):
        self.directory = directory

    def docker(self, *args, stdout=None, check=True):
        try:
            result = subprocess.run(["docker", *args], stdout=stdout, check=False)
        except FileNotFoundError:
            raise ManagerError("Docker is not installed. Install Docker Engine and the Compose v2 plugin first.") from None
        if check and result.returncode:
            raise ManagerError("Docker operation failed; inspect the output above. No data volumes were deleted.")
        return result

    def compose(self, *args, stdout=None):
        return self.docker("compose", "--project-name", self.config()["project"],
                           "--project-directory", str(self.directory),
                           "--env-file", str(self.directory / ".env"),
                           "-f", str(self.directory / "compose.json"), *args, stdout=stdout)

    def config(self):
        path = self.directory / "settings.json"
        if not path.is_file():
            raise ManagerError("No managed installation found in this directory. Run install first.")
        try:
            config = json.loads(path.read_text())
            version(config["version"])
            timezone_name(config["timezone"])
            if config["domain"]:
                domain_name(config["domain"])
            if not re.fullmatch(r"n8n-[a-f0-9]{12}", config["project"]):
                raise ValueError("Invalid project name")
        except (KeyError, TypeError, ValueError, argparse.ArgumentTypeError) as exc:
            raise ManagerError("Invalid installation settings.") from exc
        return config

    def doctor(self):
        self.docker("version", "--format", "{{.Server.Version}}")
        self.docker("compose", "version")
        if (self.directory / "settings.json").exists():
            self.compose("config", "--quiet")
        print("Prerequisites checked. DNS, firewall and certificate issuance still need server-side verification.")

    def install(self, args):
        if any(self.directory.iterdir()):
            raise ManagerError("Installation directory is not empty. Use start for an existing installation.")
        self.directory.chmod(0o700)
        self.doctor()
        config = {"version": args.version, "domain": args.domain,
                  "timezone": args.timezone, "project": "n8n-" + secrets.token_hex(6)}
        write_private(self.directory / ".env", "POSTGRES_PASSWORD=" + secrets.token_hex(32)
                      + "\nN8N_ENCRYPTION_KEY=" + secrets.token_hex(32) + "\n")
        save_config(self.directory, config)
        self.compose("config", "--quiet")
        self.start()
        print("Create the owner account immediately in n8n. No default username or password is configured.")
        if args.domain:
            print(f"Address: https://{args.domain} (requires DNS and inbound TCP ports 80/443).")
        else:
            print("Local-only installation. Use an SSH tunnel: ssh -L 5678:127.0.0.1:5678 USER@SERVER")

    def start(self):
        self.compose("up", "-d", "--wait", "--wait-timeout", "360")

    def backup(self):
        self.compose("config", "--quiet")
        root = self.directory / "backups"
        root.mkdir(mode=0o700, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix=datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=root))
        with tempfile.TemporaryFile(mode="w+", dir=self.directory) as state:
            self.compose("ps", "--status", "running", "--services", stdout=state)
            state.seek(0)
            running = set(state.read().split())
        if "n8n" not in running or "db" not in running:
            backup.rmdir()
            raise ManagerError("Backup requires a running installation. Run start first.")
        stopped = False
        try:
            stopped = True
            self.compose("stop", "n8n")
            with (backup / "database.dump").open("wb") as stream:
                self.compose("exec", "-T", "db", "pg_dump", "-U", "n8n", "-d", "n8n", "-Fc", stdout=stream)
            with (backup / "application.tar.gz").open("wb") as stream:
                self.compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "tar",
                             "n8n", "-czf", "-", "-C", "/home/node/.n8n", ".", stdout=stream)
            for name in ("settings.json", "compose.json", ".env", "Caddyfile"):
                source = self.directory / name
                if source.exists():
                    shutil.copy2(source, backup / name)
            write_private(backup / "COMPLETE", "Database, application files and encryption configuration. Store securely off-server.\n")
        finally:
            if stopped:
                self.compose("start", "n8n")
        print(f"Backup completed: {backup}")
        print("Contains credentials and encryption material. Keep private; incomplete backups have no COMPLETE marker.")
        return backup

    def update(self, target):
        config = self.config()
        if target == config["version"]:
            print("Already configured for this version.")
            return
        current_parts = tuple(map(int, config["version"].split(".")))
        if tuple(map(int, target.split("."))) < current_parts:
            raise ManagerError("Downgrades are blocked because database migrations may not be reversible.")
        print("Backing up before update; n8n will pause briefly. Read the target version's migration notes first.")
        self.backup()
        candidate = self.directory / "candidate.json"
        updated = dict(config, version=target)
        write_private(candidate, json.dumps(compose_spec(updated), indent=2) + "\n")
        try:
            self.docker("compose", "--project-name", config["project"],
                        "--project-directory", str(self.directory), "--env-file", str(self.directory / ".env"),
                        "-f", str(candidate), "pull", "n8n")
        finally:
            candidate.unlink(missing_ok=True)
        save_config(self.directory, updated)
        try:
            self.start()
        except ManagerError:
            raise ManagerError("Update did not become healthy. Backup was retained. Do not downgrade blindly; database migrations may have run. Inspect status and logs.") from None
        print(f"Update completed: {target}")


def parser():
    cli = argparse.ArgumentParser(
        description="Independent n8n deployment manager: PostgreSQL, optional HTTPS, private backups and explicit-version updates. Requires Linux, Python 3.10+, Docker Engine and Compose v2. No remote installation scripts are executed.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Start the interactive ModernSoft menu:\n  bash n8n.sh\n\nCommand-line examples (replace VERSION with a verified n8n release):\n  bash n8n.sh doctor\n  bash n8n.sh install --version VERSION --domain n8n.example.com\n  bash n8n.sh install --version VERSION\n  bash n8n.sh backup\n  bash n8n.sh update --version VERSION\n  bash n8n.sh --directory /srv/n8n status\n\nRun consistently as the same OS user and with the same directory.\nNo Docker installation, firewall changes or unattended updates are performed.\nBackups require maintenance downtime and include sensitive encryption material.\nRestore is a manual PostgreSQL/application-volume recovery, not an automatic rollback.",
    )
    cli.add_argument("--directory", type=Path, default=Path.cwd() / "n8n-managed", help="Dedicated installation directory (default: ./n8n-managed)")
    commands = cli.add_subparsers(dest="command")
    commands.add_parser("menu", help="Open the ModernSoft interactive menu (default)")
    install = commands.add_parser("install", help="Create and start a new installation")
    install.add_argument("--version", type=version, required=True, help="Exact n8n stable version to install")
    install.add_argument("--domain", type=domain_name, default="", help="Public DNS domain; omit for loopback-only mode")
    install.add_argument("--timezone", type=timezone_name, default="Asia/Tehran")
    for name, help_text in {
        "doctor": "Check Docker and Compose prerequisites", "start": "Start and wait for healthy services",
        "stop": "Stop services without removing data", "restart": "Restart n8n and wait for health",
        "status": "Show service status", "logs": "Show the last 100 n8n log lines (may contain private data)",
        "backup": "Pause n8n and back up database, application volume and settings",
        "uninstall": "Remove containers and network; ALWAYS preserve volumes and settings",
    }.items():
        command = commands.add_parser(name, help=help_text)
        if name == "uninstall":
            command.add_argument("--confirm", action="store_true", help="Confirm removal of this installation's containers")
    update = commands.add_parser("update", help="Back up, pull an explicit newer n8n version and wait for health")
    update.add_argument("--version", type=version, required=True)
    return cli


@contextlib.contextmanager
def installation_lock(directory):
    lock_path = directory.parent / ("." + directory.name + ".manager.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ManagerError("Another manager operation is active for this installation.") from None
        yield


class MenuCancelled(Exception):
    pass


def menu_input(prompt):
    value = input(prompt).strip()
    if value.lower() == "q":
        raise MenuCancelled
    return value


def menu_value(prompt, validator, hint, default=None):
    while True:
        value = menu_input(prompt) or default
        try:
            if value is None:
                raise argparse.ArgumentTypeError("Required")
            return validator(value)
        except argparse.ArgumentTypeError:
            print(hint)


def menu_confirm(message):
    return menu_input(message + " [yes/no, default: no]: ").lower() in {"yes", "y"}


def menu(directory):
    digits = str.maketrans("\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669", "01234567890123456789")
    actions = {"2": "doctor", "3": "start", "4": "stop", "5": "restart",
               "6": "status", "7": "logs", "8": "backup", "10": "uninstall"}
    labels = {"install": "Install", "doctor": "Check prerequisites", "start": "Start services",
              "stop": "Stop services", "restart": "Restart n8n", "status": "Show status",
              "logs": "Show logs", "backup": "Create backup", "update": "Update n8n",
              "uninstall": "Uninstall services"}
    try:
        while True:
            print("\n" + "=" * 48)
            print("                   ModernSoft")
            print("          n8n Installation & Management")
            print("=" * 48)
            print(f"Installation directory: {os.path.relpath(directory)}")
            print("1. Install n8n (guided setup)")
            print("2. Check server prerequisites")
            print("3. Start services")
            print("4. Stop services (keep data)")
            print("5. Restart n8n")
            print("6. Show service status")
            print("7. Show logs (last 100 lines)")
            print("8. Create backup")
            print("9. Update n8n (backup before upgrade)")
            print("10. Uninstall services (keep data and backups)")
            print("11. Change installation directory")
            print("12. Help & prerequisites")
            print("0. Exit")
            print("Enter q at any prompt to return. Operations require confirmation.")
            try:
                choice = menu_input("Select an option: ").translate(digits)
                if choice == "0":
                    print("Thank you for using ModernSoft. Goodbye.")
                    return 0
                if choice == "12":
                    print("\nRun on Linux with Python 3.10+, Docker Engine and Docker Compose v2.")
                    print("Start with option 2, then option 1. Docker is not installed automatically.")
                    print("Choose an exact n8n version from official releases; latest is not accepted.")
                    print("HTTPS requires a domain pointing to this server and inbound TCP ports 80/443.")
                    print("Without a domain, access is local-only through an SSH tunnel, not the public IP.")
                    print("Backups and updates briefly stop n8n. Backups are private; recovery is manual.")
                    print("Create the owner account immediately after installation. There is no default password.")
                    print("Always use the same OS user and installation directory. Option 11 changes the directory.")
                    continue
                if choice == "11":
                    value = menu_input("Installation directory (Enter: keep current): ")
                    if value:
                        try:
                            selected = Path(value).expanduser().resolve()
                        except (OSError, RuntimeError, ValueError):
                            print("Enter a valid, accessible directory path.")
                            continue
                        if selected == selected.parent:
                            print("The filesystem root is not allowed. Choose a dedicated directory.")
                        else:
                            directory = selected
                    continue
                options = []
                if choice in {"1", "9"}:
                    action = "install" if choice == "1" else "update"
                    print("Enter an exact official release version (three numbers separated by dots, not latest).")
                    target = menu_value("n8n version: ", lambda v: version(v.translate(digits)),
                                        "Invalid version. Use three numbers separated by dots, such as 2.1.0.")
                    options = ["--version", target]
                    if action == "install":
                        print("A domain is optional. Leave blank for local-only access through an SSH tunnel.")
                        domain = menu_value("Domain (no https://; Enter: local-only): ",
                                            lambda v: domain_name(v) if v else "",
                                            "Enter a valid domain without a path or port.", default="")
                        timezone = menu_value("Timezone [Asia/Tehran]: ", timezone_name,
                                              "Enter a valid timezone, such as Asia/Tehran or UTC.",
                                              default="Asia/Tehran")
                        options += ["--domain", domain, "--timezone", timezone]
                        print(f"Version: {target} | Domain: {domain or 'local-only; SSH tunnel'} | Timezone: {timezone}")
                    else:
                        print(f"Target version: {target}. Review migration notes first; there is no automatic rollback.")
                elif choice in actions:
                    action = actions[choice]
                else:
                    print("Invalid option. Select a number from the menu.")
                    continue
                if action in {"stop", "restart", "backup", "update"}:
                    print("Warning: this operation may temporarily stop n8n and interrupt running workflows.")
                if action == "backup":
                    print("Backups contain sensitive information. Keep them private and store them securely.")
                if action == "logs":
                    print("Logs may contain private information. Review them before sharing.")
                if action == "uninstall":
                    print("Containers and the network will be removed. Data, settings and backups will be retained.")
                    options = ["--confirm"]
                if not menu_confirm(f"Run '{labels[action]}' in {os.path.relpath(directory)}?"):
                    print("Operation cancelled. No changes were made.")
                    continue
                result = main(["--directory", str(directory), action, *options])
                if result == 0:
                    print("Operation completed successfully.")
                    if action == "install":
                        print("Installation finished. Create the owner account in n8n immediately.")
                else:
                    print("Operation did not complete. Review the error; use option 2 or 6 to investigate.")
            except MenuCancelled:
                print("Returning to the menu. No operation was started.")
    except (EOFError, KeyboardInterrupt):
        print("\nExiting the ModernSoft menu.")
        return 0


def main(argv=None):
    args = parser().parse_args(argv)
    directory = args.directory.expanduser().resolve()
    if args.command in {None, "menu"}:
        return menu(directory)
    os.umask(0o077)
    try:
        if directory == directory.parent:
            raise ManagerError("Use a dedicated installation directory, not the filesystem root.")
        if args.command == "install":
            directory.parent.mkdir(parents=True, exist_ok=True)
        elif args.command != "doctor" and not directory.is_dir():
            raise ManagerError("Installation directory does not exist.")
        if args.command == "doctor" and not directory.parent.exists():
            Manager(directory).doctor()
            return 0
        with installation_lock(directory):
            manager = Manager(directory)
            if args.command == "install":
                directory.mkdir(mode=0o700, exist_ok=True)
                manager.install(args)
            elif args.command == "doctor":
                manager.doctor()
            elif args.command == "start":
                manager.start()
            elif args.command == "stop":
                manager.compose("stop")
            elif args.command == "restart":
                manager.compose("restart", "n8n")
                manager.start()
            elif args.command == "status":
                manager.compose("ps", "--all")
            elif args.command == "logs":
                manager.compose("logs", "--tail", "100", "n8n")
            elif args.command == "backup":
                manager.backup()
            elif args.command == "update":
                manager.update(args.version)
            elif args.command == "uninstall":
                if not args.confirm:
                    raise ManagerError("Re-run uninstall --confirm to remove containers. Data volumes and settings will be retained.")
                manager.compose("down")
                print("Containers removed. Data volumes, settings and backups retained; start recreates containers.")
        return 0
    except (ManagerError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted. No data volumes were deleted; inspect status before continuing.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

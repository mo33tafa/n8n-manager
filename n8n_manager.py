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
    commands.add_parser("menu", help="Open the ModernSoft interactive Persian menu (default)")
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
    return menu_input(message + " [بله/خیر، پیش‌فرض: خیر]: ").lower() in {"بله", "yes", "y"}


def menu(directory):
    digits = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
    actions = {"2": "doctor", "3": "start", "4": "stop", "5": "restart",
               "6": "status", "7": "logs", "8": "backup", "10": "uninstall"}
    labels = {"install": "نصب", "doctor": "بررسی پیش‌نیازها", "start": "راه‌اندازی",
              "stop": "توقف", "restart": "راه‌اندازی مجدد", "status": "نمایش وضعیت",
              "logs": "نمایش گزارش‌ها", "backup": "پشتیبان‌گیری", "update": "به‌روزرسانی",
              "uninstall": "حذف سرویس"}
    try:
        while True:
            print("\n" + "=" * 48)
            print("                   ModernSoft")
            print("          نصب و مدیریت آسان n8n")
            print("=" * 48)
            print(f"پوشهٔ نصب انتخاب‌شده: {os.path.relpath(directory)}")
            print("۱. نصب جدید (راهنمای قدم‌به‌قدم)")
            print("۲. بررسی پیش‌نیازهای سرور")
            print("۳. روشن کردن سرویس‌ها")
            print("۴. خاموش کردن سرویس‌ها (بدون حذف اطلاعات)")
            print("۵. راه‌اندازی مجدد n8n")
            print("۶. نمایش وضعیت سرویس‌ها")
            print("۷. نمایش ۱۰۰ خط آخر گزارش‌ها")
            print("۸. تهیهٔ نسخهٔ پشتیبان")
            print("۹. به‌روزرسانی n8n (با بکاپ قبل از ارتقا)")
            print("۱۰. حذف سرویس‌ها (با حفظ اطلاعات و بکاپ‌ها)")
            print("۱۱. انتخاب پوشهٔ نصب دیگر")
            print("۱۲. راهنما و پیش‌نیازها")
            print("۰. خروج")
            print("در هر پرسش با q به منو برگردید؛ بدون تأیید، عملیاتی انجام نمی‌شود.")
            try:
                choice = menu_input("شمارهٔ گزینه: ").translate(digits)
                if choice == "0":
                    print("از ModernSoft استفاده کردید؛ خدانگهدار.")
                    return 0
                if choice == "12":
                    print("\nاین ابزار را روی سرور لینوکسی با Python 3.10+، Docker Engine و Docker Compose v2 اجرا کنید.")
                    print("برای شروع گزینهٔ ۲ و سپس ۱ را انتخاب کنید. این ابزار Docker را خودکار نصب نمی‌کند.")
                    print("نسخهٔ دقیق n8n را از انتشارهای رسمی انتخاب کنید؛ latest پذیرفته نمی‌شود.")
                    print("برای HTTPS، دامنه باید به سرور متصل باشد و پورت‌های ۸۰ و ۴۴۳ باز باشند.")
                    print("بدون دامنه، دسترسی فقط محلی و از طریق تونل SSH است؛ روی IP عمومی منتشر نمی‌شود.")
                    print("بکاپ و آپدیت باعث توقف موقت n8n می‌شوند. بکاپ محرمانه است؛ بازیابی آن دستی است.")
                    print("پس از نصب، حساب مالک را فوراً داخل n8n بسازید. رمز پیش‌فرض وجود ندارد.")
                    print("همیشه از همان کاربر سیستم و همان پوشهٔ نصب استفاده کنید؛ گزینهٔ ۱۱ پوشه را تغییر می‌دهد.")
                    continue
                if choice == "11":
                    value = menu_input("مسیر پوشهٔ نصب (Enter: بدون تغییر): ")
                    if value:
                        try:
                            selected = Path(value).expanduser().resolve()
                        except (OSError, RuntimeError, ValueError):
                            print("مسیر معتبر و قابل دسترسی وارد کنید.")
                            continue
                        if selected == selected.parent:
                            print("ریشهٔ سیستم مجاز نیست؛ یک پوشهٔ اختصاصی انتخاب کنید.")
                        else:
                            directory = selected
                    continue
                options = []
                if choice in {"1", "9"}:
                    action = "install" if choice == "1" else "update"
                    print("نسخهٔ دقیق انتشار رسمی را وارد کنید (سه عدد با نقطه، نه latest).")
                    target = menu_value("نسخهٔ n8n: ", lambda v: version(v.translate(digits)),
                                        "نسخه نامعتبر است؛ سه عدد با نقطه وارد کنید، مانند 2.1.0.")
                    options = ["--version", target]
                    if action == "install":
                        print("دامنه اختیاری است؛ خالی یعنی نصب امنِ محلی و دسترسی با تونل SSH.")
                        domain = menu_value("دامنه (بدون https://؛ Enter: محلی): ",
                                            lambda v: domain_name(v) if v else "",
                                            "دامنه معتبر و بدون مسیر یا پورت وارد کنید.", default="")
                        timezone = menu_value("منطقهٔ زمانی [Asia/Tehran]: ", timezone_name,
                                              "منطقهٔ زمانی معتبر وارد کنید؛ مانند Asia/Tehran یا UTC.",
                                              default="Asia/Tehran")
                        options += ["--domain", domain, "--timezone", timezone]
                        print(f"نسخه: {target} | دامنه: {domain or 'محلی؛ فقط تونل SSH'} | منطقهٔ زمانی: {timezone}")
                    else:
                        print(f"نسخهٔ مقصد: {target}. یادداشت‌های مهاجرت این نسخه را بررسی کنید؛ بازگشت خودکار وجود ندارد.")
                elif choice in actions:
                    action = actions[choice]
                else:
                    print("گزینه نامعتبر است؛ یکی از شماره‌های منو را انتخاب کنید.")
                    continue
                if action in {"stop", "restart", "backup", "update"}:
                    print("هشدار: این عملیات می‌تواند n8n و گردش‌کارهای در حال اجرا را موقتاً متوقف کند.")
                if action == "backup":
                    print("بکاپ شامل اطلاعات محرمانه است؛ آن را خصوصی و در جای امن نگهداری کنید.")
                if action == "logs":
                    print("گزارش‌ها ممکن است اطلاعات خصوصی داشته باشند؛ پیش از اشتراک‌گذاری بررسی کنید.")
                if action == "uninstall":
                    print("کانتینرها و شبکه حذف می‌شوند؛ اطلاعات، تنظیمات و بکاپ‌ها باقی می‌مانند.")
                    options = ["--confirm"]
                if not menu_confirm(f"عملیات «{labels[action]}» در پوشهٔ {os.path.relpath(directory)} انجام شود؟"):
                    print("عملیات لغو شد؛ تغییری انجام نشد.")
                    continue
                result = main(["--directory", str(directory), action, *options])
                if result == 0:
                    print("عملیات با موفقیت انجام شد.")
                    if action == "install":
                        print("نصب تمام شد؛ حساب مالک را فوراً در n8n ایجاد کنید.")
                else:
                    print("عملیات کامل نشد؛ پیام خطا را بررسی کنید. برای بررسی، گزینهٔ ۲ یا ۶ را انتخاب کنید.")
            except MenuCancelled:
                print("بازگشت به منو؛ عملیاتی شروع نشد.")
    except (EOFError, KeyboardInterrupt):
        print("\nخروج از منوی ModernSoft.")
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

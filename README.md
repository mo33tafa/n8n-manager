# ModernSoft n8n Manager

**Your workflows. Your infrastructure.**

A menu-driven command-line tool for installing and managing n8n on a Linux server. Deploy with PostgreSQL, enable optional HTTPS, create private backups and upgrade to an explicitly selected version.

**[Project & menu preview](index.html)** · **[Contributing](CONTRIBUTING.md)** · **[Security](SECURITY.md)** · **[MIT License](LICENSE)**

> This is a deployment manager, not a replacement for n8n. The HTML preview is a static illustration of the terminal menu, not a running n8n instance or a web control panel. Server deployment and certificate issuance have not been validated as part of the included unit tests.

## At a glance

| Capability | Behavior |
| --- | --- |
| Guided setup | Numbered English menu with input validation and confirmation before operations |
| Database | PostgreSQL 16 with a persistent volume; no published database port |
| Optional HTTPS | Caddy handles certificates when a domain is configured |
| Local-only mode | Without a domain, n8n binds to `127.0.0.1:5678` |
| Private configuration | Random database password and encryption key; generated configuration files use restricted permissions |
| Backups | PostgreSQL dump, application files and installation settings |
| Controlled updates | Explicit n8n version, backup before upgrade, image pull and health checks |
| Data-preserving uninstall | Removes containers and the network, not volumes, configuration or backups |
| Operational controls | Start, stop, restart, status, recent logs and prerequisite checks |
| Safeguards | Installation lock, blocked downgrades, log rotation and execution-history pruning |

## Requirements

- A Linux server with Python **3.10+** and IANA timezone data.
- Docker Engine and the **Docker Compose v2 plugin**, with support for `up --wait` and `--wait-timeout`.
- Permission to use Docker. Docker access is highly privileged; use a trusted OS account.
- Sufficient server resources, persistent disk space and access to the required image registries.
- For public HTTPS: a DNS domain pointing to the server, inbound TCP **80/443**, and access to certificate services.

The tool does **not** install Docker, change firewall rules, configure DNS or create scheduled updates.

## Quick start

1. Download this repository, or clone it using the URL shown by GitHub's **Code** button.
2. Enter the downloaded project directory. Keep `n8n.sh` and `n8n_manager.py` together.
3. Open the menu:

   ```bash
   bash n8n.sh
   ```

4. Choose **2** to check prerequisites, then **1** for guided installation.
5. Enter an exact n8n release version, an optional domain and a timezone. Review the summary and confirm with `yes`.
6. After installation, **create the n8n owner account immediately**. No default n8n login is configured.

Choose a currently supported version from the [official n8n releases](https://github.com/n8n-io/n8n/releases). Version examples shown by the tool illustrate syntax; they are not recommendations or verified current releases.

### Terminal preview

```text
================================================
                   ModernSoft
          n8n Installation & Management
================================================
Installation directory: n8n-managed
1. Install n8n (guided setup)
2. Check server prerequisites
3. Start services
4. Stop services (keep data)
5. Restart n8n
6. Show service status
7. Show logs (last 100 lines)
8. Create backup
9. Update n8n (backup before upgrade)
10. Uninstall services (keep data and backups)
11. Change installation directory
12. Help & prerequisites
0. Exit
Enter q at any prompt to return. Operations require confirmation.
Select an option:
```

Operations require explicit confirmation; pressing Enter at a confirmation cancels. Enter `q` during menu prompts to return without starting the requested operation. Command-line operations execute directly unless a command has its own confirmation requirement.

### Installation location

The default installation directory is `n8n-managed` inside the **current working directory**. Always use the same OS account and installation directory when managing an existing deployment. To select a fixed location:

```bash
bash n8n.sh --directory /srv/modernsoft-n8n
```

Menu option **11** changes the selected directory for the current menu session; it does not move an existing installation. Reuse `--directory` on later launches.

### Access modes

**With a domain:** the configured address uses HTTPS. Caddy requests certificates automatically, subject to DNS, network reachability and certificate-provider requirements. The n8n application port is not published directly.

**Without a domain:** n8n is available only on the server's loopback interface. From your own computer, open an SSH tunnel:

```bash
ssh -L 5678:127.0.0.1:5678 USER@SERVER
```

Replace `USER` and `SERVER` with your SSH account and server address, then access port 5678 locally. Public webhook callbacks require a publicly reachable deployment; local-only mode does not provide one.

## Command-line usage

The menu is the default. Existing subcommands are also available:

```bash
bash n8n.sh --help
bash n8n.sh menu
bash n8n.sh doctor
bash n8n.sh start
bash n8n.sh stop
bash n8n.sh restart
bash n8n.sh status
bash n8n.sh logs
bash n8n.sh backup
bash n8n.sh uninstall --confirm
```

For scripted installation or upgrades, replace `VERSION` with the exact stable release you intend to deploy:

```bash
bash n8n.sh install --version VERSION --domain n8n.example.com --timezone UTC
bash n8n.sh update --version VERSION
```

Omit `--domain` for local-only mode. `VERSION` is a placeholder, not a valid version value. Floating tags such as `latest` are rejected. The default timezone is `Asia/Tehran`.

Global options go **before** the subcommand:

```bash
bash n8n.sh --directory /srv/modernsoft-n8n status
```

## Backups, updates and recovery

### What a backup contains

Backups are saved under the installation directory's `backups/` folder. They include:

- `database.dump`: PostgreSQL custom-format dump.
- `application.tar.gz`: the n8n application data volume, including locally stored files.
- `settings.json`, `compose.json` and `.env`: installation settings and encryption material.
- `Caddyfile`, if domain mode is configured.
- `COMPLETE`: a marker written after the backup contents have been created successfully.

n8n is stopped while the database and application data are copied, then restarted. Backups require a running installation and **cause downtime**. A `COMPLETE` marker is not a restore test or proof that the service restarted successfully.

**Backups are not encrypted at rest by this tool.** They contain credentials and the encryption key needed to recover saved n8n credentials. Keep them private, store secure off-server copies and monitor disk use. Automatic backup retention is not implemented. Do not publish backups, logs or `.env` files.

### Updates

Before upgrading, read the target release's migration notes and verify free disk space. The manager backs up the current installation, pulls the specified n8n image and waits for healthy services after applying it. A failed image pull leaves the configured version unchanged.

Database migrations may run when a new version starts. Downgrades are blocked, and there is **no automatic rollback or restore command**. A failed health check requires investigating status and logs before any recovery action. PostgreSQL and Caddy use major-version image tags; only the n8n version is explicitly selected by this tool.

### Recovery

Recovery is a manual administrative procedure, not a menu option. Preserve the backup, encryption key and current data before attempting it. A full recovery requires restoring the database dump, application volume and matching configuration, using compatible software versions. Certificate volumes are not included in backups; domain deployments may need certificates reissued.

Test recovery on a separate server before relying on a backup for production. This manager does not provide an automated disaster-recovery guarantee.

## Uninstall without losing data

Menu option **10**, or:

```bash
bash n8n.sh uninstall --confirm
```

removes the installation's containers and network. Named volumes, settings and backups remain. Use **Start services** or `start` to recreate containers using the retained data. Uninstall is not a complete data-erasure operation.

## Project structure

```text
.
├── n8n.sh                 Shell entry point
├── n8n_manager.py         Interactive menu and deployment operations
├── test_n8n_manager.py    Unit and regression tests
├── index.html             Static project and terminal preview
├── README.md              Usage and operational guidance
├── LICENSE                MIT terms and copyright notice
├── CONTRIBUTING.md        Contribution guidelines
└── SECURITY.md            Security guidance
```

Runtime configuration and backups are generated separately in the selected installation directory. Project-private platform files, uploaded reference archives and default runtime data are excluded from version control.

## Development and testing

No third-party Python packages are required by the manager or tests. Run from the project directory:

```bash
python3 -m unittest -v test_n8n_manager
bash -n n8n.sh
```

The suite covers configuration generation, validation, lifecycle safeguards, backup and update behavior, menu interactions and agreement between the HTML preview and the actual terminal menu. Docker operations are mocked in unit tests; these tests do **not** prove a real deployment, certificate issuance or successful recovery.

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution expectations.

## License and attribution

This manager is released under the [MIT License](LICENSE), copyright **2026 ModernSoft**.

You may fork, copy, modify, redistribute and use this project, including commercially, provided the copyright and permission notice are retained in copies or substantial portions of the software. A suitable credit for your documentation is:

> Based on ModernSoft n8n Manager. Copyright (c) 2026 ModernSoft. Licensed under the MIT License.

Keep the included `LICENSE` when redistributing the project. A documentation credit does not replace the license notice. MIT does not require a visible badge in your application's interface.

The license covers this manager, not n8n, PostgreSQL, Caddy or Docker. Those projects retain their own licenses and trademarks. This is an independent deployment tool and is not an official n8n product. See [n8n's source repository](https://github.com/n8n-io/n8n) for its license and applicable usage terms.

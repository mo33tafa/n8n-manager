# Contributing to ModernSoft n8n Manager

Contributions, bug reports and forks are welcome. Contributions must be compatible with the project's [MIT License](LICENSE). Retain copyright and permission notices when reusing or redistributing the project.

## Before making changes

- Search existing issues before opening a new one.
- Discuss substantial features before implementing them.
- Keep changes focused; avoid unrelated cleanup in a bug fix.
- Keep CLI messages and code in English. Maintain both English and Persian descriptions in the project preview and documentation.
- Do not include uploaded reference code, private platform state or unlicensed third-party material.

## Local checks

From the repository root on Linux with Python 3.10+:

```bash
python3 -m unittest -v test_n8n_manager
bash -n n8n.sh
git diff --check
```

The language switch has additional dependency-free tests requiring Node.js 18+ for development only:

```bash
node --test test_preview.js
```

These tests do not launch a browser. Check both translation directions, saved language preferences and disabled storage when changing the preview. Keep `README.md`, `README.fa.md` and the gallery captions aligned; image examples must not imply a real deployment occurred.

The unit suite mocks deployment operations. Do not run install, update or uninstall commands against an existing production server to test a contribution. Any real deployment testing must use a server you control, with an explicit maintenance plan.

## Pull requests

Describe:

1. The problem and the intended behavior.
2. Changes to commands, prompts, configuration or data handling.
3. Tests performed and anything not tested.
4. Migration, downtime or security implications.

Add regression tests for behavior changes. If the terminal menu changes, update the static preview in `index.html` and the menu example in `README.md`; the suite checks the HTML preview against actual menu output.

Preserve the following safeguards unless a reviewed change explicitly improves them:

- No secret values in console output or version control.
- No public database port or public n8n port in local-only mode.
- Explicit version selection and blocked unsafe downgrades.
- Backup before upgrades and data preservation during uninstall.
- Confirmation before interactive operations and an installation lock.

## Reporting bugs

Include your OS, Python version, Docker/Compose versions, the command used and a redacted description of the error. Explain whether the problem occurs in a test environment or an existing installation.

Never attach `.env`, credential exports, workflow payloads, raw backups or unreviewed logs. For security-sensitive reports, follow [SECURITY.md](SECURITY.md) instead of disclosing details publicly.

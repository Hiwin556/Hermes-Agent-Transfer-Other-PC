<h1 align="center">
  ♻️ Hermes Transfer
</h1>

<p align="center">
  <strong>Transfer, deploy & sync your Hermes Agent persona across machines.</strong><br>
  Pack memory, skills, config & session history — then push to any Linux server via SSH.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey" alt="Linux / macOS">
</p>

---

## 🚀 Quick Start

```bash
# Already have Hermes installed? Pack & deploy in one command:
python3 hermes-transfer.py deploy user@hostname --key ~/.ssh/id_ed25519

# Or pack locally first, transfer later:
python3 hermes-transfer.py pack --exclude-sensitive
python3 hermes-transfer.py apply ~/backup.revive --dry-run
```

## 📦 What's Inside

| Artifact | Content |
|----------|---------|
| `memories/` | MEMORY.md, USER.md — your agent's persistent memory |
| `skills/` | All installed skills (workflows, knowledge, scripts) |
| `config.yaml` | Hermes configuration (model, provider, credentials) |
| `state.db` | Session database (history, FTS5 search) |
| `cron/` | Scheduled jobs |
| `pairing/` | Platform pairing data |

Everything is packed into a single `.revive` file (ZIP with integrity manifest).

## 🔧 Commands

```
Pack & Restore:
  pack [--output FILE] [--exclude-sensitive]     Create a .revive package
  apply <package> [--dry-run]                     Restore locally

SSH Deploy:
  deploy <user@host> [--key KEY] [--port PORT]   One-click SSH deployment

Target Management (multi-machine):
  target add <name> <user@host>                   Register a target
  target list                                     List all targets
  target deploy <name>                            Deploy to registered target
  target sync <name>                              Incremental rsync sync
  target status <name>                            Health check + memory diff
  target remove <name>                            Remove target

Web GUI:
  gui [--port PORT] [--public]                    Launch browser UI (Gradio)

System:
  init                                            Initialize config directory
```

## 🌐 Web GUI

```bash
python3 hermes-transfer.py gui
# Open http://127.0.0.1:7860 in your browser
```

5 tabs — Pack, Apply, SSH Deploy, Target Management, System Status.
Zero install on the client side — just a browser.

## 🔒 Security

| Concern | Mitigation |
|---------|------------|
| API keys in archive | Base64-masked + `.obfuscated` marker (not crypto — relies on SSH channel) |
| In-transit | Full SSH encryption (SCP / rsync over SSH) |
| File permissions | Sensitive files restored as `0600`; target configs stored as `0600` |
| Integrity | Per-file SHA256 checksums in manifest, verified on restore |
| Path traversal | Zip slip blocked by `resolve().relative_to()` check |
| SQLite safety | `sqlite3.backup()` API for consistent snapshots (handles WAL mode) |
| Dry-run | `--dry-run` flag previews changes without writing |

## 🖥️ Cross-Platform

| From → To | Status |
|-----------|--------|
| Linux → Linux | ✅ Full support (SSH + rsync + SCP) |
| macOS → Linux | ✅ POSIX-compatible paths |
| Windows → Linux | ⚠️ Requires OpenSSH Server on target |
| Windows (source) | ⚠️ Pure Python, but `~/.hermes` is `%USERPROFILE%\.hermes` |
| Web GUI | ✅ Any browser (Win/Mac/Linux/mobile) |

## 📋 Requirements

- Python 3.11+
- SSH key-based access to target machines
- `rsync` on both ends (for `target sync`)
- `gradio` (optional, for Web GUI: `pip install gradio`)

## 📄 License

MIT

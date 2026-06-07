<h1 align="center">
  ♻️ Hermes Transfer
</h1>

<p align="center">
  <strong>Transfer, deploy & sync your Hermes Agent across machines.</strong><br>
  <em>Pack memory, skills, config & session history — USB or SSH, your choice.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
  <img src="https://img.shields.io/badge/platform-linux%20%7C%20macOS-lightgrey" alt="Linux / macOS">
  <img src="https://img.shields.io/badge/lang-multilingual-orange" alt="9 languages">
</p>

---

<!-- ===== ENGLISH ===== -->

## 🇬🇧 English

**True Image style — one file to backup, one file to restore.**

```bash
# Step 1: On your old machine — create a backup file
python3 hermes-transfer.py backup --exclude-sensitive

# Copy the .revive file via USB / cloud / any method

# Step 2: On the new machine — restore everything
python3 hermes-transfer.py restore backup.revive
```

No SSH needed. No network required. Just **backup → transfer file → restore**.

### Multi-Language Support

```bash
export HERMES_LANG=de        # Deutsch
export HERMES_LANG=ja        # 日本語
export HERMES_LANG=fr        # Français
export HERMES_LANG=es        # Español
export HERMES_LANG=ko        # 한국어
export HERMES_LANG=it        # Italiano
export HERMES_LANG=zh_tw     # 繁體中文
export HERMES_LANG=zh_cn     # 简体中文
# default: English
```

### Commands Quick Reference

```
backup / restore    True Image style — backup & restore
deploy <host>       One-click SSH deployment
gui                 Launch Web GUI (browser interface)
target add/list     Multi-machine management
target sync         Incremental rsync sync
target status       Health check + memory diff
```

---

<!-- ===== 繁體中文 ===== -->

## 🇹🇼 繁體中文

**True Image 風格 — 一個檔案備份，一個指令還原。**

```bash
# 第一步：在舊電腦上建立備份檔
python3 hermes-transfer.py backup --exclude-sensitive

# 用 USB / 雲端 / 任何方式把 .revive 檔案傳過去

# 第二步：在新電腦上還原所有設定
python3 hermes-transfer.py restore backup.revive
```

不需 SSH、不需網路。就只是 **備份 → 傳檔案 → 還原**。

### 多國語言支援

```bash
export HERMES_LANG=zh_tw     # 繁體中文
export HERMES_LANG=zh_cn     # 简体中文
export HERMES_LANG=en        # English
export HERMES_LANG=ja        # 日本語
export HERMES_LANG=ko        # 한국어
# 其他: de, fr, es, it
```

### 指令速查

```
backup / restore    True Image 風格 — 備份與還原
deploy <host>       SSH 一鍵部署到遠端主機
gui                 啟動 Web GUI（瀏覽器操作）
target add/list     多機管理
target sync         增量同步（rsync）
target status       健康檢查 + 記憶比對
```

---

<!-- ===== 简体中文 ===== -->

## 🇨🇳 简体中文

**True Image 风格 — 一个文件备份，一个命令还原。**

```bash
# 第一步：在旧电脑上创建备份文件
python3 hermes-transfer.py backup --exclude-sensitive

# 用 USB / 云端 / 任何方式把 .revive 文件传过去

# 第二步：在新电脑上还原所有设置
python3 hermes-transfer.py restore backup.revive
```

无需 SSH、无需网络。只是 **备份 → 传文件 → 还原**。

### 多语言支持

```bash
export HERMES_LANG=zh_cn     # 简体中文
export HERMES_LANG=zh_tw     # 繁體中文
export HERMES_LANG=en        # English
export HERMES_LANG=ja        # 日本語
export HERMES_LANG=ko        # 한국어
# 其他: de, fr, es, it
```

### 命令速查

```
backup / restore    True Image 风格 — 备份与还原
deploy <host>       SSH 一键部署到远程主机
gui                 启动 Web GUI（浏览器操作）
target add/list     多机管理
target sync         增量同步（rsync）
target status       健康检查 + 记忆比对
```

---

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
| Windows target | ⚠️ Requires OpenSSH Server |
| Windows source | ⚠️ `~/.hermes` → `%USERPROFILE%\.hermes` |
| Web GUI | ✅ Any browser (Win/Mac/Linux/mobile) |

## 📋 Requirements

- Python 3.11+
- SSH key (for deploy command only)
- `rsync` on both ends (for `target sync`)
- `gradio` (optional: `pip install gradio` for Web GUI)

## 📄 License

MIT

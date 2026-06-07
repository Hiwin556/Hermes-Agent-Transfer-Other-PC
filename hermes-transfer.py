#!/usr/bin/env python3
"""
Hermes Transfer (回生) — Hermes Agent memory transfer & deploy tool
====================================================================
SPDX-License-Identifier: MIT
Copyright (c) 2026

Pack, transfer, deploy & sync your Hermes Agent persona across machines.

Usage:
  hermes-transfer.py pack [--output PACKAGE.revive] [--exclude-sensitive]
  hermes-transfer.py apply <package.revive>
  hermes-transfer.py deploy <user@host> [--key KEY] [--port PORT] [--package PACKAGE]
  hermes-transfer.py target add <name> <user@host>
  hermes-transfer.py target list
  hermes-transfer.py target deploy <name>
  hermes-transfer.py target sync <name>
  hermes-transfer.py target status <name>
  hermes-transfer.py target remove <name>
  hermes-transfer.py target rename <name> <new-name>
  hermes-transfer.py gui [--port PORT] [--public]
  hermes-transfer.py init
  hermes-transfer.py help
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# ── 常數 ────────────────────────────────────────────────────────────────
REVIVE_VERSION = "1.0.0"
HERMES_HOME = Path.home() / ".hermes"
REVIVE_DIR = HERMES_HOME / "revive"
TARGETS_DIR = REVIVE_DIR / "targets"
CACHE_DIR = REVIVE_DIR / "cache"
PACKAGE_SUFFIX = ".revive"

# 打包時排除的目錄（同 hermes backup 邏輯）
_EXCLUDED_DIRS = {
    "hermes-agent", "__pycache__", ".git", "node_modules",
    "backups", "checkpoints", "state-snapshots", "revive",
}
_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".db-wal", ".db-shm", ".db-journal")
_EXCLUDED_NAMES = {"gateway.pid", "cron.pid"}

# 敏感檔案 — 提示使用者注意
_SENSITIVE_FILES = {".env", "auth.json"}

# 注意：以下 _mask/_unmask 是淺層混淆（base64 + 反轉），不是加密。
# 目的只是防止 zip 被意外解開時 API key 直接明碼可見。
# 真正的傳輸安全完全依賴 SSH 加密通道。
# 不要把這個當作安全機制。


def _mask(data: bytes) -> bytes:
    """淺層混淆敏感檔案內容，防止 zip 直接可讀。"""
    import base64
    return base64.b64encode(data)[::-1]


def _unmask(data: bytes) -> bytes:
    """反向混淆。"""
    import base64
    return base64.b64decode(data[::-1])


# ══════════════════════════════════════════════════════════════════════════
# 工具函式
# ══════════════════════════════════════════════════════════════════════════

def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _ensure_dirs():
    """建立回生所需的目錄結構。"""
    for d in (REVIVE_DIR, TARGETS_DIR, CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def _should_exclude(rel_path: str) -> bool:
    """檢查路徑是否應排除（同 hermes backup 邏輯）。"""
    parts = Path(rel_path).parts
    for part in parts:
        if part in _EXCLUDED_DIRS:
            return True
    name = Path(rel_path).name
    if name in _EXCLUDED_NAMES:
        return True
    if name.endswith(_EXCLUDED_SUFFIXES):
        return True
    return False


def _safe_copy_db(src: Path, dst: Path) -> bool:
    """用 SQLite backup API 安全複製資料庫。"""
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        backup_conn = sqlite3.connect(str(dst))
        conn.backup(backup_conn)
        backup_conn.close()
        conn.close()
        return True
    except Exception as exc:
        print(f"  [W] SQLite 安全複製失敗 ({src.name}): {exc}，使用原始複製", file=sys.stderr)
        try:
            shutil.copy2(src, dst)
            return True
        except Exception as exc2:
            print(f"  [E] 原始複製也失敗: {exc2}", file=sys.stderr)
            return False


def _file_hash(path: Path) -> str:
    """計算檔案 SHA256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_ssh(host: str, cmd: str, port: int = 22, key_file: str | None = None) -> tuple[int, str, str]:
    """執行遠端 SSH 指令，回傳 (exit_code, stdout, stderr)。"""
    ssh_cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    if port != 22:
        ssh_cmd.extend(["-p", str(port)])
    if key_file:
        ssh_cmd.extend(["-i", key_file])
    ssh_cmd.append(host)
    ssh_cmd.append(cmd)

    proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _run_rsync(src: str, dst: str, port: int = 22, key_file: str | None = None,
               delete: bool = False, extra_args: list[str] | None = None) -> tuple[int, str, str]:
    """執行 rsync 同步。"""
    rsync_cmd = [
        "rsync", "-avz", "--progress",
        "-e",
    ]
    ssh_arg = "ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
    if port != 22:
        ssh_arg += f" -p {port}"
    if key_file:
        ssh_arg += f" -i {shlex.quote(key_file)}"
    rsync_cmd.append(ssh_arg)
    if delete:
        rsync_cmd.append("--delete")
    if extra_args:
        rsync_cmd.extend(extra_args)
    rsync_cmd.extend([src, dst])

    proc = subprocess.run(rsync_cmd, capture_output=True, text=True, timeout=300)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _get_hermes_version_local() -> str:
    """取得本機 Hermes 版本。"""
    try:
        result = subprocess.run(
            ["hermes", "version"], capture_output=True, text=True, timeout=15
        )
        return result.stdout.strip() or result.stderr.strip()
    except Exception as e:
        return f"unknown ({e})"


def _get_hermes_version_remote(host: str, port: int = 22, key_file: str | None = None) -> str:
    """取得遠端 Hermes 版本。"""
    code, out, err = _run_ssh(host, "hermes version 2>&1 || echo NOT_INSTALLED", port, key_file)
    if code != 0:
        return "NOT_INSTALLED"
    return out


def _yes_no(prompt: str, default: bool = False) -> bool:
    """詢問使用者 yes/no。"""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in ("y", "yes")


# ══════════════════════════════════════════════════════════════════════════
# 打包 (Pack)
# ══════════════════════════════════════════════════════════════════════════

def cmd_pack(args: argparse.Namespace):
    """打包 Hermes 資料成 .revive 可攜套件。"""
    _ensure_dirs()

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = CACHE_DIR / f"hermes-revive-{stamp}{PACKAGE_SUFFIX}"

    if out_path.suffix.lower() != PACKAGE_SUFFIX:
        out_path = out_path.with_suffix(out_path.suffix + PACKAGE_SUFFIX)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if not HERMES_HOME.is_dir():
        print(f"[E] Hermes 家目錄不存在: {HERMES_HOME}")
        sys.exit(1)

    # 收集檔案
    print(f"📦 掃描 {HERMES_HOME} ...")
    files_to_add: list[tuple[Path, str]] = []
    has_sensitive = False

    for dirpath, dirnames, filenames in os.walk(HERMES_HOME, followlinks=False):
        dp = Path(dirpath)
        rel_dir = dp.relative_to(HERMES_HOME)

        # 排除目錄
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]

        for fname in filenames:
            fpath = dp / fname
            rel = str(fpath.relative_to(HERMES_HOME))

            if _should_exclude(rel):
                continue

            skip_sensitive = False
            if Path(rel).name in _SENSITIVE_FILES and args.exclude_sensitive:
                has_sensitive = True
                skip_sensitive = True

            if not skip_sensitive:
                files_to_add.append((fpath, rel))

    if not files_to_add:
        print("[!] 沒有檔案可打包")
        return

    # 如果排除了敏感檔案，提示
    if args.exclude_sensitive:
        print("  [i] .env 和 auth.json 已排除（--exclude-sensitive）")
    else:
        has_env = any(str(Path(r).name) == ".env" for _, r in files_to_add)
        has_auth = any(str(Path(r).name) == "auth.json" for _, r in files_to_add)
        if has_env or has_auth:
            print("  [!] 套件包含敏感檔案（.env / auth.json）")
            print("      傳輸時請使用加密通道（SSH），勿於不安全的網路傳送")
            if not _yes_no("  繼續包含敏感檔案？", default=True):
                print("  重新打包，建議使用 --exclude-sensitive")
                return

    # 產生 manifest （含檔案 checksum 用於完整性驗證）
    total_checksums = {}
    manifest = {
        "revive_version": REVIVE_VERSION,
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
        "hermes_version": _get_hermes_version_local(),
        "total_files": len(files_to_add),
        "sensitive_included": not args.exclude_sensitive,
        "files": {},
    }

    # 建立套件
    print(f"📦 打包 {len(files_to_add)} 個檔案 ...")
    total_original = 0
    t0 = time.monotonic()

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 先寫 manifest
        manifest_bytes = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
        zf.writestr("revive-manifest.json", manifest_bytes)

        for abs_path, rel_path in files_to_add:
            try:
                is_sensitive = Path(rel_path).name in _SENSITIVE_FILES
                if abs_path.suffix == ".db":
                    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
                        tmp_db = Path(tmp.name)
                    if _safe_copy_db(abs_path, tmp_db):
                        data = tmp_db.read_bytes()
                        if is_sensitive and not args.exclude_sensitive:
                            data = _mask(data)
                            rel_path += ".obfuscated"
                        total_checksums[rel_path] = hashlib.sha256(data).hexdigest()
                        zf.writestr(rel_path, data)
                        total_original += len(data)
                        tmp_db.unlink(missing_ok=True)
                    else:
                        tmp_db.unlink(missing_ok=True)
                        continue
                else:
                    data = abs_path.read_bytes()
                    if is_sensitive and not args.exclude_sensitive:
                        data = _mask(data)
                        rel_path_ob = rel_path + ".obfuscated"
                        total_checksums[rel_path_ob] = hashlib.sha256(data).hexdigest()
                        zf.writestr(rel_path_ob, data)
                    else:
                        total_checksums[rel_path] = hashlib.sha256(data).hexdigest()
                        zf.writestr(rel_path, data)
                    total_original += len(data)
            except (PermissionError, OSError) as exc:
                print(f"  [W] 跳過 {rel_path}: {exc}", file=sys.stderr)
                continue

    elapsed = time.monotonic() - t0
    # 更新 manifest 加入 checksums
    manifest["files"] = total_checksums
    # 重新寫入 manifest（更新 checksums）
    with zipfile.ZipFile(out_path, "r") as zf_r:
        old_manifest = json.loads(zf_r.read("revive-manifest.json"))
    old_manifest["files"] = total_checksums
    manifest_bytes = json.dumps(old_manifest, indent=2, ensure_ascii=False).encode("utf-8")
    # 用 temp 方式重寫 zip 中的 manifest
    tmp_zip = out_path.with_suffix(".tmp.revive")
    with zipfile.ZipFile(out_path, "r") as zf_r:
        with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf_w:
            for item in zf_r.infolist():
                if item.filename == "revive-manifest.json":
                    continue
                data = zf_r.read(item.filename)
                zf_w.writestr(item, data)
            zf_w.writestr("revive-manifest.json", manifest_bytes)
    shutil.move(tmp_zip, out_path)

    pkg_size = out_path.stat().st_size

    print()
    print(f"✅ 打包完成: {out_path}")
    print(f"   檔案數:    {len(files_to_add)}")
    print(f"   原始大小:  {_fmt_size(total_original)}")
    print(f"   壓縮大小:  {_fmt_size(pkg_size)}")
    print(f"   壓縮率:    {pkg_size / total_original * 100:.1f}%")
    print(f"   耗時:      {elapsed:.1f}s")
    print(f"   含敏感檔: {'是 ⚠️' if manifest['sensitive_included'] else '否'}")
    print()
    print(f"   還原指令: 回生 apply {out_path.name}")


# ══════════════════════════════════════════════════════════════════════════
# 本機還原 (Apply)
# ══════════════════════════════════════════════════════════════════════════

def cmd_apply(args: argparse.Namespace):
    """本機還原 .revive 套件。"""
    pkg_path = Path(args.package).expanduser().resolve()

    if not pkg_path.is_file():
        print(f"[E] 檔案不存在: {pkg_path}")
        sys.exit(1)

    if not zipfile.is_zipfile(pkg_path):
        print(f"[E] 不是有效的 zip 檔案: {pkg_path}")
        sys.exit(1)

    with zipfile.ZipFile(pkg_path, "r") as zf:
        # 讀取 manifest
        if "revive-manifest.json" not in zf.namelist():
            print("[E] 不是有效的 回生 套件（缺少 revive-manifest.json）")
            sys.exit(1)

        manifest = json.loads(zf.read("revive-manifest.json"))

        members = [n for n in zf.namelist() if n != "revive-manifest.json" and not n.endswith("/")]
        file_count = len(members)

        print(f"📋 套件資訊:")
        print(f"   工具版本:    {manifest.get('revive_version', '?')}")
        print(f"   建立時間:    {manifest.get('created_at', '?')}")
        print(f"   來源主機:    {manifest.get('hostname', '?')}")
        print(f"   Hermes 版本: {manifest.get('hermes_version', '?')}")
        print(f"   檔案數:      {file_count}")
        print(f"   含敏感檔:    {'是 ⚠️' if manifest.get('sensitive_included', False) else '否'}")
        print()
        print(f"🎯 目標: {HERMES_HOME}")

        # dry-run 模式：不問確認，直接顯示資訊
        if args.dry_run:
            print(f"\n🔍 模擬模式 (--dry-run)：將還原 {file_count} 個檔案")
            print(f"   不會實際寫入任何檔案")
            print(f"   含敏感檔案: {'是' if manifest.get('sensitive_included', False) else '否'}")
            print(f"   來源: {manifest.get('hostname', '?')}")
            print(f"   若要執行還原，移除 --dry-run 參數")
            return

        # 檢查是否已有 Hermes 設定
        has_config = (HERMES_HOME / "config.yaml").exists()
        has_env = (HERMES_HOME / ".env").exists()

        if has_config or has_env:
            print()
            print("[!] 目標目錄已有 Hermes 設定")
            print("    導入將會覆蓋現有檔案")
            if not _yes_no("  繼續？", default=False):
                print("已取消")
                return

            # 備份現有記憶
            mem_file = HERMES_HOME / "memories" / "MEMORY.md"
            usr_file = HERMES_HOME / "memories" / "USER.md"
            backup_dir = HERMES_HOME / "revive" / "pre-apply-backups"
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

            print(f"\n📦 備份現有記憶到 {backup_dir} ...")
            for f in [mem_file, usr_file]:
                if f.exists():
                    bk = backup_dir / stamp / f.relative_to(HERMES_HOME)
                    bk.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(f, bk)
                    print(f"   備份: {f.name}")

        # 還原檔案
        print(f"\n📦 還原 {file_count} 個檔案 ...")
        errors = []
        restored = 0
        verified = 0
        failed_checksums = []
        t0 = time.monotonic()

        for member in members:
            target = HERMES_HOME / member

            # 安全檢查：防止路徑穿越
            try:
                target.resolve().relative_to(HERMES_HOME.resolve())
            except ValueError:
                errors.append(f"  {member}: 路徑穿越被封鎖")
                continue

            try:
                data = zf.read(member)
                expected_checksum = manifest.get("files", {}).get(member)
                if expected_checksum:
                    actual_hash = hashlib.sha256(data).hexdigest()
                    if actual_hash == expected_checksum:
                        verified += 1
                    else:
                        failed_checksums.append(member)

                # 處理混淆的敏感檔案
                target_path = target
                if member.endswith(".obfuscated"):
                    data = _unmask(data)
                    target_path = HERMES_HOME / member.replace(".obfuscated", "")

                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(data)
                # 敏感檔案設為 0600
                if Path(member).name in _SENSITIVE_FILES or Path(target_path).name in _SENSITIVE_FILES:
                    os.chmod(target_path, stat.S_IRUSR | stat.S_IWUSR)
                restored += 1
            except (PermissionError, OSError) as exc:
                errors.append(f"  {member}: {exc}")

            if restored % 200 == 0:
                print(f"  {restored}/{file_count} ...")

        elapsed = time.monotonic() - t0

        print()
        print(f"✅ 還原完成: {restored} 個檔案，耗時 {elapsed:.1f}s")
        print(f"   目標: {HERMES_HOME}")
        if verified:
            print(f"   完整性驗證: {verified}/{file_count} 通過")
        if failed_checksums:
            print(f"   ⚠️ checksum 不匹配 ({len(failed_checksums)}):")
            for f in failed_checksums[:5]:
                print(f"      {f}")

        if errors:
            print(f"\n  ⚠️ 警告 ({len(errors)}):")
            for e in errors[:10]:
                print(f"    {e}")
            if len(errors) > 10:
                print(f"    ... 及其他 {len(errors) - 10} 個")

        # 還原後檢查
        if not (HERMES_HOME / "hermes-agent").is_dir():
            print()
            print("[i] Hermes 程式碼庫不在套件中")
            print("    如果是全新安裝，請執行: hermes update")

        print()
        print("🎉 回生完成！")


# ══════════════════════════════════════════════════════════════════════════
# SSH 部署 (Deploy)
# ══════════════════════════════════════════════════════════════════════════

def _detect_remote_os(host: str, port: int = 22, key_file: str | None = None) -> str:
    """偵測遠端作業系統。"""
    code, out, err = _run_ssh(host, "cat /etc/os-release 2>/dev/null | head -3 || uname -a", port, key_file)
    if code == 0:
        return out[:500]
    return "unknown"


def _remote_has_hermes(host: str, port: int = 22, key_file: str | None = None) -> tuple[bool, str]:
    """檢查遠端是否已安裝 Hermes。"""
    code, out, err = _run_ssh(host, "hermes version 2>&1 || echo 'NOT_FOUND'", port, key_file)
    if code == 0 and "NOT_FOUND" not in out and "command not found" not in out:
        return True, out
    return False, out


def _remote_install_hermes(host: str, port: int = 22, key_file: str | None = None) -> bool:
    """在遠端安裝 Hermes。"""
    print("  [~] 正在安裝 Hermes Agent ...")
    cmds = [
        "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | sh 2>&1",
    ]
    for cmd in cmds:
        code, out, err = _run_ssh(host, cmd, port, key_file)
        if code != 0:
            print(f"  [E] 安裝失敗: {err}", file=sys.stderr)
            return False
        print(f"  {out[:300]}")
    return True


def _remote_transfer_package(host: str, pkg_path: Path, remote_path: str,
                              port: int = 22, key_file: str | None = None) -> bool:
    """傳送套件到遠端。"""
    print(f"  [~] 傳送套件到 {host}:{remote_path} ...")
    scp_cmd = ["scp", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    if port != 22:
        scp_cmd.extend(["-P", str(port)])
    if key_file:
        scp_cmd.extend(["-i", key_file])
    scp_cmd.extend([str(pkg_path), f"{host}:{remote_path}"])

    proc = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        print(f"  [E] 傳送失敗: {proc.stderr.strip()}", file=sys.stderr)
        return False
    print(f"  ✅ 已傳送 ({_fmt_size(pkg_path.stat().st_size)})")
    return True


def cmd_deploy(args: argparse.Namespace):
    """一鍵部署 Hermes 到遠端主機。"""
    host = args.host

    print(f"🚀 回生部署: {host}")
    print("─" * 50)

    # 決定套件
    if args.package:
        pkg_path = Path(args.package).expanduser().resolve()
        if not pkg_path.is_file():
            print(f"[E] 套件不存在: {pkg_path}")
            sys.exit(1)
        print(f"[i] 使用指定套件: {pkg_path.name}")
    else:
        # 自動打包 — 直接呼叫 cmd_pack，不走 argparse/sys.argv
        print("[~] 未指定套件，自動打包本機設定 ...")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pkg_path = CACHE_DIR / f"deploy-{stamp}{PACKAGE_SUFFIX}"
        pack_args = argparse.Namespace(
            output=str(pkg_path),
            exclude_sensitive=args.exclude_sensitive,
        )
        cmd_pack(pack_args)
        print()

    # 檢查 SSH 連線
    print(f"[~] 測試 SSH 連線到 {host} ...")
    code, out, err = _run_ssh(host, "echo OK && uname -n", args.port, args.key)
    if code != 0:
        print(f"[E] SSH 連線失敗: {err}", file=sys.stderr)
        sys.exit(1)
    print(f"  ✅ 連線成功: {out.replace('OK', '').strip()}")

    # 偵測遠端環境
    print("[~] 偵測遠端環境 ...")
    os_info = _detect_remote_os(host, args.port, args.key)
    print(f"  {os_info[:200]}")

    has_hermes, hermes_ver = _remote_has_hermes(host, args.port, args.key)
    if has_hermes:
        print(f"  ✅ 已安裝 Hermes: {hermes_ver[:100]}")
    else:
        print(f"  [i] 未安裝 Hermes")
        if not args.no_install:
            if _yes_no("  安裝 Hermes？", default=True):
                if not _remote_install_hermes(host, args.port, args.key):
                    print("[E] 安裝失敗", file=sys.stderr)
                    if not _yes_no("  繼續部署？", default=False):
                        return
            else:
                print("  [i] 跳過安裝，假設 Hermes 已存在")

    # 傳送套件
    remote_pkg = f"$HOME/{pkg_path.name}"
    if not _remote_transfer_package(host, pkg_path, remote_pkg, args.port, args.key):
        sys.exit(1)

    # 遠端還原
    print("[~] 遠端還原套件 ...")

    # 先確保回生工具在遠端（傳送回生本體）
    revive_local = Path(__file__).resolve()
    revive_remote = "$HOME/bin/回生"
    code, out, err = _run_ssh(host, "mkdir -p $HOME/bin", args.port, args.key)
    if not _remote_transfer_package(host, revive_local, revive_remote, args.port, args.key):
        print("  [W] 無法傳送回生工具到遠端，將使用遠端指令直接操作")

    # 遠端執行 apply
    remote_cmd = f"chmod +x {revive_remote} && {revive_remote} apply {remote_pkg}"
    if args.exclude_sensitive:
        remote_cmd += " --exclude-sensitive"

    code, out, err = _run_ssh(host, remote_cmd, args.port, args.key)
    if code != 0:
        print(f"  [E] 遠端還原失敗: {err[:500]}", file=sys.stderr)
        print(f"  stdout: {out[:500]}")
        sys.exit(1)
    print(f"  {out[-1000:]}")

    # 驗證
    print()
    print("[~] 驗證部署 ...")
    code, out, err = _run_ssh(host, "hermes version 2>&1 | head -2", args.port, args.key)
    if code == 0:
        print(f"  ✅ Hermes 版本: {out}")
    else:
        print(f"  ⚠️ 驗證失敗: {err[:200]}")

    print()
    print("🎉 回生部署完成！")
    print(f"   主機: {host}")
    print(f"   套件: {pkg_path}")
    print(f"   現在可以在遠端執行 'hermes' 使用了")


# ══════════════════════════════════════════════════════════════════════════
# 目標機管理 (Target)
# ══════════════════════════════════════════════════════════════════════════

def _target_path(name: str) -> Path:
    return TARGETS_DIR / f"{name}.json"


def _load_target(name: str) -> dict:
    path = _target_path(name)
    if not path.exists():
        print(f"[E] 目標 '{name}' 不存在")
        print(f"   使用 '回生 target add <name> <user@host>' 新增")
        sys.exit(1)
    return json.loads(path.read_text())


def _save_target(name: str, data: dict):
    path = _target_path(name)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    path.chmod(0o600)


def cmd_target_add(args: argparse.Namespace):
    """新增目標機。"""
    name = args.name
    host = args.host

    target = {
        "name": name,
        "host": host,
        "port": args.port or 22,
        "user": host.split("@")[0] if "@" in host else os.environ.get("USER", "root"),
        "ssh_host": host,
        "key_file": str(Path(args.key).expanduser()) if args.key else None,
        "hermes_home": "~/.hermes",
        "created_at": datetime.now(tz=timezone.utc).isoformat(),
        "last_sync": None,
        "last_deploy": None,
        "status": "registered",
    }

    _save_target(name, target)
    print(f"✅ 目標 '{name}' 已新增: {host}")

    # 測試連線
    if _yes_no("  測試連線？", default=True):
        code, out, err = _run_ssh(host, "echo OK", target["port"], target["key_file"])
        if code == 0:
            target["status"] = "reachable"
            _save_target(name, target)
            print(f"  ✅ 連線成功")
        else:
            print(f"  ⚠️ 連線失敗: {err[:200]}")


def cmd_target_list(args: argparse.Namespace):
    """列出所有目標機。"""
    targets = sorted(TARGETS_DIR.glob("*.json"))
    if not targets:
        print("😴 尚未註冊任何目標")
        print("   使用 '回生 target add <name> <user@host>' 新增")
        return

    print(f"📋 已註冊目標 ({len(targets)}):")
    print()
    for t in targets:
        data = json.loads(t.read_text())
        name = data.get("name", t.stem)
        host = data.get("host", "?")
        status = data.get("status", "unknown")
        last = data.get("last_deploy") or data.get("last_sync")
        last_str = f"，最後同步: {last[:16]}" if last else ""

        status_icon = {"reachable": "🟢", "deployed": "🟢", "registered": "🟡", "error": "🔴"}.get(status, "⚪")
        print(f"  {status_icon} {name:20s} {host:30s} [{status}]{last_str}")


def cmd_target_deploy(args: argparse.Namespace):
    """部署到已註冊目標。"""
    target = _load_target(args.name)
    print(f"🚀 部署到目標 '{args.name}' ({target['host']})")

    # 轉換成 deploy 參數
    deploy_args = argparse.Namespace(
        host=target["host"],
        key=target.get("key_file"),
        port=target.get("port", 22),
        package=args.package,
        no_install=args.no_install,
        exclude_sensitive=not args.include_sensitive,
    )
    cmd_deploy(deploy_args)

    # 更新目標狀態
    target["status"] = "deployed"
    target["last_deploy"] = datetime.now(tz=timezone.utc).isoformat()
    _save_target(args.name, target)


def cmd_target_sync(args: argparse.Namespace):
    """增量同步到目標機。"""
    target = _load_target(args.name)
    host = target["host"]
    port = target.get("port", 22)
    key_file = target.get("key_file")

    print(f"🔄 增量同步到 '{args.name}' ({host})")
    print("─" * 50)

    # 檢查遠端狀態
    has_hermes, ver = _remote_has_hermes(host, port, key_file)
    if not has_hermes:
        print(f"[E] 遠端未安裝 Hermes，請先 deploy")
        sys.exit(1)

    print(f"  遠端 Hermes: {ver[:80]}")

    # 使用 rsync 增量同步關鍵目錄
    print("[~] 使用 rsync 增量同步 ...")

    # 要同步的目錄
    sync_dirs = [
        ("memories/", "記憶"),
        ("skills/", "技能"),
        ("config.yaml", "設定"),
        ("state.db", "資料庫"),
    ]

    total_transferred = 0
    for rel_path, label in sync_dirs:
        src = str(HERMES_HOME / rel_path) + "/" if rel_path.endswith("/") else str(HERMES_HOME / rel_path)
        dst = f"{host}:{target.get('hermes_home', '~/.hermes')}/{rel_path}"

        if not os.path.exists(src) and not rel_path.endswith("/"):
            # 單一檔案不存在，跳過
            continue

        extra = ["--include=*/", "--include=*.md", "--include=*.yaml", "--include=*.json",
                 "--include=*.py", "--exclude=*"]
        if rel_path.endswith("/"):
            extra = []

        code, out, err = _run_rsync(src, dst, port, key_file, delete=True, extra_args=extra)
        if code != 0:
            # 可能路徑還沒建立
            remote_base = shlex.quote(str(target.get('hermes_home', '~/.hermes')))
            remote_dir = shlex.quote(rel_path.rstrip('/'))
            _run_ssh(host, f"mkdir -p {remote_base}/{remote_dir}",
                     port, key_file)
            code, out, err = _run_rsync(src, dst, port, key_file, delete=True, extra_args=extra)

        if code == 0:
            # 估算傳輸量
            sent_bytes = sum(
                int(line.split()[-2]) for line in out.split("\n")
                if "sent" in line and "bytes" in line
            ) if "sent" in out else 0
            total_transferred += sent_bytes
            icon = "✅" if code == 0 else "⚠️"
            size_str = _fmt_size(sent_bytes) if sent_bytes else "?"
            print(f"  {icon} {label}: {size_str}")
        else:
            print(f"  ⚠️ {label} 同步失敗")

    # 如果記憶有更新，發送 reload 信號
    print()
    print("[~] 通知遠端重新載入 ...")
    _run_ssh(host, "hermes reload-skills 2>/dev/null; hermes reload-mcp 2>/dev/null; echo OK",
             port, key_file)

    # 更新目標狀態
    target["status"] = "reachable"
    target["last_sync"] = datetime.now(tz=timezone.utc).isoformat()
    _save_target(args.name, target)

    print(f"✅ 同步完成（約 {_fmt_size(total_transferred)}）")


def cmd_target_status(args: argparse.Namespace):
    """檢查遠端部署狀態。"""
    target = _load_target(args.name)
    host = target["host"]
    port = target.get("port", 22)
    key_file = target.get("key_file")

    print(f"🔍 檢查 '{args.name}' ({host}) ...")
    print("─" * 50)

    # SSH 連線
    code, out, err = _run_ssh(host, "echo alive && uptime", port, key_file)
    if code != 0:
        print(f"  🔴 SSH: 無法連線 ({err[:100]})")
        target["status"] = "error"
        _save_target(args.name, target)
        return
    print(f"  🟢 SSH: {out.split(chr(10))[-1][:80]}")

    # Hermes 版本
    has_h, ver = _remote_has_hermes(host, port, key_file)
    if has_h:
        print(f"  🟢 Hermes: {ver[:100]}")
    else:
        print(f"  🔴 Hermes: 未安裝")
        target["status"] = "error"
        _save_target(args.name, target)
        return

    # 檢查關鍵檔案
    checks = [
        ("config.yaml", "設定檔"),
        ("memories/MEMORY.md", "記憶"),
        ("memories/USER.md", "使用者設定"),
        ("state.db", "資料庫"),
        ("skills/.bundled_manifest", "技能"),
    ]
    all_ok = True
    remote_home = target.get('hermes_home', '~/.hermes')
    for f, label in checks:
        code, out, err = _run_ssh(
            host, f"test -f {remote_home}/{f} && echo 'exists' || echo 'missing'",
            port, key_file
        )
        if code == 0 and "missing" not in out:
            print(f"  🟢 {label}: 存在")
        else:
            print(f"  ⚪ {label}: 不存在")
            all_ok = False

    # 比對記憶內容
    print()
    print("[~] 記憶比對 ...")
    local_mem = (HERMES_HOME / "memories" / "MEMORY.md")
    if local_mem.exists():
        local_hash = _file_hash(local_mem)
        code, out, err = _run_ssh(host, f"sha256sum {remote_home}/memories/MEMORY.md 2>/dev/null || echo N/A", port, key_file)
        if code == 0 and out and "N/A" not in out:
            remote_hash = out.split()[0]
            if local_hash == remote_hash:
                print(f"  ✅ 記憶一致")
            else:
                print(f"  ⚠️ 記憶不同步（本地: {local_hash[:12]}，遠端: {remote_hash[:12]}）")
                print(f"     建議: 回生 target sync {args.name}")

    status = "healthy" if all_ok else "degraded"
    target["status"] = status
    _save_target(args.name, target)

    print(f"\n📊 總體狀態: {'🟢 正常' if all_ok else '🟡 部分異常'}")


def cmd_target_remove(args: argparse.Namespace):
    """移除目標機。"""
    path = _target_path(args.name)
    if not path.exists():
        print(f"[E] 目標 '{args.name}' 不存在")
        return
    if _yes_no(f"  確定移除 '{args.name}'？", default=False):
        path.unlink()
        print(f"✅ 已移除 '{args.name}'")


def cmd_target_rename(args: argparse.Namespace):
    """重新命名目標機。"""
    old_path = _target_path(args.name)
    if not old_path.exists():
        print(f"[E] 目標 '{args.name}' 不存在")
        return
    new_path = _target_path(args.new_name)
    if new_path.exists():
        print(f"[E] 目標 '{args.new_name}' 已存在")
        return
    data = json.loads(old_path.read_text())
    data["name"] = args.new_name
    _save_target(args.new_name, data)
    old_path.unlink()
    print(f"✅ 已重新命名: '{args.name}' -> '{args.new_name}'")


def cmd_target(args: argparse.Namespace):
    """目標機管理分派。"""
    if hasattr(args, "target_subcommand"):
        sub = args.target_subcommand
    else:
        sub = "list" if hasattr(args, "func") and args.func.__name__ == "cmd_target" else None

    _ensure_dirs()

    if sub == "add":
        cmd_target_add(args)
    elif sub == "list" or not sub:
        cmd_target_list(args)
    elif sub == "deploy":
        cmd_target_deploy(args)
    elif sub == "sync":
        cmd_target_sync(args)
    elif sub == "status":
        cmd_target_status(args)
    elif sub == "remove":
        cmd_target_remove(args)
    elif sub == "rename":
        cmd_target_rename(args)
    else:
        print(f"[E] 未知子指令: {sub}")
        print("    target 子指令: add, list, deploy, sync, status, remove, rename")


# ══════════════════════════════════════════════════════════════════════════
# 初始化
# ══════════════════════════════════════════════════════════════════════════

def cmd_init(args: argparse.Namespace):
    """初始化回生目錄結構。"""
    _ensure_dirs()
    config_path = REVIVE_DIR / "config.json"
    if not config_path.exists():
        config = {
            "revive_version": REVIVE_VERSION,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
            "auto_exclude_sensitive": True,
            "default_port": 22,
            "ssh_timeout": 15,
        }
        config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False))
        config_path.chmod(0o600)

    print(f"✅ 回生已初始化")
    print(f"   設定目錄: {REVIVE_DIR}")
    print(f"   目標目錄: {TARGETS_DIR}")
    print(f"   快取目錄: {CACHE_DIR}")
    print()
    print(f"   開始使用:")
    print(f"     回生 pack               # 打包本機 Hermes 設定")
    print(f"     回生 deploy user@host   # 一鍵部署到遠端")
    print(f"     回生 target add ...     # 註冊目標機")


# ══════════════════════════════════════════════════════════════════════════
# GUI (Gradio Web UI)
# ══════════════════════════════════════════════════════════════════════════

def cmd_gui(args: argparse.Namespace):
    """啟動回生 Web GUI (Gradio)。"""
    import threading
    import queue
    import io
    import traceback

    try:
        import gradio as gr
    except ImportError:
        print("[E] 需要 gradio，安裝指令: pip install gradio")
        sys.exit(1)

    # ── 輔助：將 CLI 輸出導向 queue ──
    class StreamCapture(io.StringIO):
        def __init__(self, q: queue.Queue):
            super().__init__()
            self._q = q
            self._buf = ""

        def write(self, s: str):
            self._buf += s
            if "\n" in s or len(self._buf) > 500:
                self._q.put(self._buf)
                self._buf = ""

    def _run_and_stream(target_fn, *args) -> str:
        """在背景執行 target_fn，透過 queue 輸出到 Gradio generator。"""
        q = queue.Queue()
        result_holder = [""]

        def worker():
            old_stdout = sys.stdout
            old_stderr = sys.stderr
            capture = StreamCapture(q)
            sys.stdout = capture
            try:
                target_fn(*args)
            except SystemExit:
                pass
            except Exception as e:
                print(f"[E] {e}\\n{traceback.format_exc()}")
            finally:
                sys.stdout = old_stdout
                q.put(None)  # EOF marker

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        parts = []
        while True:
            chunk = q.get()
            if chunk is None:
                break
            parts.append(chunk)
            yield "".join(parts)

    # ── 建立 GUI ──
    PORT = args.port or 7860
    host_display = "0.0.0.0" if args.public else "127.0.0.1"

    with gr.Blocks(
        title="回生 Revive — Hermes 記憶轉移 GUI",
    ) as demo:
        gr.Markdown(
            "# 回生 Revive — Hermes 記憶轉移與異機部署"
        )

        with gr.Tabs():
            # ═══════ Tab 1: 打包 ═══════
            with gr.TabItem("📦 打包"):
                gr.Markdown("## 打包本機 Hermes 設定")
                with gr.Row():
                    exclude_sens = gr.Checkbox(
                        label="排除敏感檔案 (.env / auth.json)", value=True
                    )
                with gr.Row():
                    pack_btn = gr.Button("開始打包", variant="primary", size="lg")
                pack_out = gr.Textbox(
                    label="輸出", lines=12, max_lines=20,
                    placeholder="點擊開始打包...",
                )
                pack_download = gr.File(label="下載套件", visible=False)

                def do_pack(exclude):
                    pkg_path = CACHE_DIR / f"gui-pack-{datetime.now().strftime('%Y%m%d_%H%M%S')}{PACKAGE_SUFFIX}"
                    ns = argparse.Namespace(
                        output=str(pkg_path),
                        exclude_sensitive=exclude,
                    )
                    for line in _run_and_stream(cmd_pack, ns):
                        yield line, None
                    yield f"\n✅ 套件建立完成: {pkg_path}", pkg_path

                pack_btn.click(
                    fn=do_pack,
                    inputs=[exclude_sens],
                    outputs=[pack_out, pack_download],
                )

            # ═══════ Tab 2: 本機還原 ═══════
            with gr.TabItem("📥 本機還原"):
                gr.Markdown("## 還原 .revive 套件到本機")
                with gr.Row():
                    apply_file = gr.File(
                        label="選擇 .revive 套件",
                        file_types=[".revive", ".zip"],
                    )
                with gr.Row():
                    dry_run = gr.Checkbox(label="模擬模式 (--dry-run)", value=True)
                    apply_btn = gr.Button("執行還原", variant="primary", size="lg")
                apply_out = gr.Textbox(
                    label="還原日誌", lines=14, max_lines=25,
                )

                def do_apply(file, dry):
                    if file is None:
                        yield "[E] 請先上傳 .revive 套件"
                        return
                    ns = argparse.Namespace(
                        package=file.name,
                        exclude_sensitive=True,
                        dry_run=dry,
                    )
                    for line in _run_and_stream(cmd_apply, ns):
                        yield line

                apply_btn.click(
                    fn=do_apply,
                    inputs=[apply_file, dry_run],
                    outputs=[apply_out],
                )

            # ═══════ Tab 3: SSH 部署 ═══════
            with gr.TabItem("🚀 SSH 部署"):
                gr.Markdown("## 一鍵部署到遠端主機")
                with gr.Row():
                    with gr.Column(scale=2):
                        deploy_host = gr.Textbox(
                            label="目標主機", placeholder="user@hostname",
                            value="",
                        )
                        deploy_key = gr.Textbox(
                            label="SSH 私鑰", placeholder="~/.ssh/id_ed25519",
                            value="~/.ssh/id_ed25519",
                        )
                    with gr.Column(scale=1):
                        deploy_port = gr.Number(label="SSH 埠號", value=22, minimum=1, maximum=65535)
                        deploy_no_install = gr.Checkbox(label="跳過 Hermes 安裝", value=False)
                        deploy_exclude = gr.Checkbox(label="排除敏感檔案", value=True)
                with gr.Row():
                    deploy_pkg = gr.File(
                        label="自訂套件（留空則自動打包）",
                        file_types=[".revive", ".zip"],
                    )
                with gr.Row():
                    deploy_btn = gr.Button("開始部署", variant="primary", size="lg")
                deploy_out = gr.Textbox(
                    label="部署日誌", lines=16, max_lines=30,
                )

                def do_deploy(host, key, port, no_install, exclude, pkg_file):
                    if not host or "@" not in host:
                        yield "[E] 請輸入有效的主機格式: user@hostname"
                        return
                    ns = argparse.Namespace(
                        host=host,
                        key=key if key else None,
                        port=int(port) if port else 22,
                        package=pkg_file.name if pkg_file else None,
                        no_install=no_install,
                        exclude_sensitive=exclude,
                    )
                    for line in _run_and_stream(cmd_deploy, ns):
                        yield line

                deploy_btn.click(
                    fn=do_deploy,
                    inputs=[deploy_host, deploy_key, deploy_port,
                            deploy_no_install, deploy_exclude, deploy_pkg],
                    outputs=[deploy_out],
                )

            # ═══════ Tab 4: 目標機管理 ═══════
            with gr.TabItem("📋 目標機管理"):
                gr.Markdown("## 註冊與管理目標機")
                with gr.Row():
                    t_name = gr.Textbox(label="目標名稱", placeholder="home-server", scale=1)
                    t_host = gr.Textbox(label="主機", placeholder="user@hostname", scale=2)
                    t_key = gr.Textbox(label="SSH 私鑰", placeholder="~/.ssh/id_ed25519", scale=2)
                    t_port = gr.Number(label="埠號", value=22, minimum=1, maximum=65535, scale=1)
                with gr.Row():
                    t_add_btn = gr.Button("➕ 新增目標", variant="primary", size="sm")
                    t_refresh_btn = gr.Button("🔄 重新整理", size="sm")
                t_out = gr.Textbox(label="操作結果", lines=3, max_lines=5)
                t_table = gr.Dataframe(
                    label="已註冊目標",
                    headers=["名稱", "主機", "埠號", "狀態", "最後同步"],
                    datatype=["str", "str", "number", "str", "str"],
                    row_count=10,
                    column_count=(5, "fixed"),
                    interactive=False,
                )

                def refresh_targets():
                    targets = sorted(TARGETS_DIR.glob("*.json"))
                    rows = []
                    for t in targets:
                        try:
                            d = json.loads(t.read_text())
                            status_icon = {"reachable": "🟢", "deployed": "🟢",
                                           "registered": "🟡", "error": "🔴",
                                           "healthy": "🟢", "degraded": "🟡"}.get(
                                d.get("status", ""), "⚪")
                            last = (d.get("last_sync") or d.get("last_deploy") or "")[:16] or "-"
                            rows.append([
                                d.get("name", t.stem),
                                d.get("host", "?"),
                                d.get("port", 22),
                                f"{status_icon} {d.get('status', 'unknown')}",
                                last,
                            ])
                        except Exception:
                            rows.append([t.stem, "? (read error)", 0, "🔴 error", "-"])
                    return rows, "✅ 已更新"

                def add_target(name, host, key, port):
                    if not name or not host:
                        return None, "[E] 名稱和主機為必填"
                    ns = argparse.Namespace(
                        name=name, host=host,
                        key=key if key else None,
                        port=int(port) if port else 22,
                    )
                    try:
                        old_stdout = sys.stdout
                        sys.stdout = io.StringIO()
                        cmd_target_add(ns)
                        sys.stdout = old_stdout
                    except Exception as e:
                        return None, f"[E] {e}"
                    rows, _ = refresh_targets()
                    return rows, f"✅ 目標 '{name}' 已新增"

                # 按鈕事件
                t_add_btn.click(
                    fn=add_target,
                    inputs=[t_name, t_host, t_key, t_port],
                    outputs=[t_table, t_out],
                )
                t_refresh_btn.click(
                    fn=refresh_targets,
                    inputs=[],
                    outputs=[t_table, t_out],
                )
                # 初始載入
                demo.load(fn=refresh_targets, inputs=[], outputs=[t_table, t_out])

                # 目標操作按鈕
                gr.Markdown("### 快速操作目標")
                with gr.Row():
                    t_action_name = gr.Textbox(label="目標名稱", placeholder="dev", scale=1)
                    t_sync_btn = gr.Button("🔄 同步", size="sm", variant="secondary")
                    t_status_btn = gr.Button("🔍 狀態", size="sm", variant="secondary")
                    t_deploy_btn = gr.Button("🚀 部署", size="sm", variant="secondary")
                    t_remove_btn = gr.Button("🗑️ 移除", size="sm", variant="stop")
                t_action_out = gr.Textbox(label="操作日誌", lines=10, max_lines=20)

                def run_target_sync(name):
                    return _run_target_action_impl(name, "sync")
                def run_target_status(name):
                    return _run_target_action_impl(name, "status")
                def run_target_deploy(name):
                    return _run_target_action_impl(name, "deploy")
                def run_target_remove(name):
                    return _run_target_action_impl(name, "remove")

                def _run_target_action_impl(name, action):
                    if not name:
                        yield "[E] 請輸入目標名稱"
                        return
                    try:
                        t = _load_target(name)
                    except SystemExit:
                        yield f"[E] 目標 '{name}' 不存在"
                        return
                    ns = argparse.Namespace(
                        name=name,
                        include_sensitive=False,
                        no_install=False,
                        package=None,
                    )
                    target_fn = {
                        "sync": cmd_target_sync,
                        "status": cmd_target_status,
                        "deploy": cmd_target_deploy,
                        "remove": cmd_target_remove,
                    }.get(action)
                    if not target_fn:
                        yield f"[E] 未知操作: {action}"
                        return
                    for line in _run_and_stream(target_fn, ns):
                        yield line

                t_sync_btn.click(fn=run_target_sync, inputs=[t_action_name], outputs=[t_action_out])
                t_status_btn.click(fn=run_target_status, inputs=[t_action_name], outputs=[t_action_out])
                t_deploy_btn.click(fn=run_target_deploy, inputs=[t_action_name], outputs=[t_action_out])
                t_remove_btn.click(fn=run_target_remove, inputs=[t_action_name], outputs=[t_action_out])

            # ═══════ Tab 5: 系統狀態 ═══════
            with gr.TabItem("📊 系統狀態"):
                gr.Markdown("## 回生系統狀態")
                refresh_sys = gr.Button("🔄 重新整理狀態", variant="primary", size="lg")
                sys_out = gr.Markdown("點擊重新整理...")

                def get_sys_status():
                    parts = []
                    # Hermes version
                    try:
                        ver = _get_hermes_version_local()
                        parts.append(f"### Hermes Agent\n**版本:** {ver[:100]}")
                    except Exception as e:
                        parts.append(f"### Hermes Agent\n**狀態:** 🔴 {e}")

                    # Disk usage
                    revive_size = sum(
                        f.stat().st_size for f in REVIVE_DIR.rglob("*") if f.is_file()
                    ) if REVIVE_DIR.exists() else 0
                    parts.append(f"**回生目錄:** {_fmt_size(revive_size)}")

                    # Cache
                    cache_size = sum(
                        f.stat().st_size for f in CACHE_DIR.rglob("*") if f.is_file()
                    ) if CACHE_DIR.exists() else 0
                    parts.append(f"**快取大小:** {_fmt_size(cache_size)}")

                    # Targets
                    targets = list(TARGETS_DIR.glob("*.json"))
                    parts.append(f"**已註冊目標:** {len(targets)}")

                    # Memory size
                    mem_file = HERMES_HOME / "memories" / "MEMORY.md"
                    if mem_file.exists():
                        mem_size = _fmt_size(mem_file.stat().st_size)
                        mem_lines = len(mem_file.read_text().splitlines())
                        parts.append(f"**記憶:** {mem_size} / {mem_lines} 行")

                    # Skills count
                    skills_dir = HERMES_HOME / "skills"
                    if skills_dir.exists():
                        skill_count = len([d for d in skills_dir.rglob("SKILL.md")])
                        parts.append(f"**技能:** {skill_count} 個")

                    # Python / Gradio version
                    import gradio as grr
                    parts.append(f"**Python:** {sys.version.split()[0]}")
                    parts.append(f"**Gradio:** {grr.__version__}")

                    return "\n\n".join(parts)

                refresh_sys.click(
                    fn=get_sys_status,
                    inputs=[],
                    outputs=[sys_out],
                )
                demo.load(fn=get_sys_status, inputs=[], outputs=[sys_out])

    # ── 啟動 ──
    print(f"🌐 回生 GUI: http://{host_display}:{PORT}")
    if not args.public:
        print(f"   僅本機可存取，如需遠端存取請加 --public 參數")
    demo.launch(
        server_name=host_display,
        server_port=PORT,
        share=False,
        theme=gr.themes.Soft(
            primary_hue="cyan",
            secondary_hue="blue",
        ),
        css="""footer {visibility: hidden} .app {max-width: 1200px; margin: auto}""",
    )


# ══════════════════════════════════════════════════════════════════════════
# 主程式
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        prog="hermes-transfer.py",
        description="Hermes Agent memory transfer & deploy tool v" + REVIVE_VERSION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Subcommands:
  pack                      Pack Hermes data into a .revive portable package
  apply <package>           Restore a .revive package locally
  deploy <host>             One-click SSH deployment to a remote host
  gui                       Launch Web GUI (Gradio)

  target add <name> <host>  Register a target machine
  target list               List all registered targets
  target deploy <name>      Deploy to a registered target
  target sync <name>        Incremental rsync sync to target
  target status <name>      Check remote deployment health
  target remove <name>      Remove a target
  target rename <n> <n2>    Rename a target

  init                      Initialize revive config directory
  help                      Show this help

Examples:
  hermes-transfer.py pack
  hermes-transfer.py pack --output ~/my-backup.revive --exclude-sensitive
  hermes-transfer.py apply ~/my-backup.revive
  hermes-transfer.py deploy user@hostname --key ~/.ssh/id_ed25519
  hermes-transfer.py target add home-server user@hostname
  hermes-transfer.py target deploy home-server
  hermes-transfer.py target sync home-server
  """
    )

    subparsers = parser.add_subparsers(dest="command")

    # pack
    p = subparsers.add_parser("pack", help="打包 Hermes 資料成 .revive 可攜套件")
    p.add_argument("-o", "--output", help="輸出路徑（預設: ~/.hermes/revive/cache/）")
    p.add_argument("--exclude-sensitive", action="store_true",
                   help="排除 .env 和 auth.json 等敏感檔案")
    p.set_defaults(func=cmd_pack)

    # apply
    p = subparsers.add_parser("apply", help="本機還原 .revive 套件")
    p.add_argument("package", help=".revive 套件路徑")
    p.add_argument("--exclude-sensitive", action="store_true",
                   help="套件內含敏感檔案時跳過")
    p.add_argument("--dry-run", action="store_true",
                   help="模擬還原，不實際寫入檔案")
    p.set_defaults(func=cmd_apply)

    # deploy
    p = subparsers.add_parser("deploy", help="一鍵 SSH 部署到遠端主機")
    p.add_argument("host", help="user@hostname")
    p.add_argument("-k", "--key", help="SSH 私鑰路徑")
    p.add_argument("-p", "--port", type=int, default=22, help="SSH 埠號")
    p.add_argument("--package", help="指定 .revive 套件（預設自動打包）")
    p.add_argument("--no-install", action="store_true",
                   help="跳過 Hermes 安裝（假設已存在）")
    p.add_argument("--exclude-sensitive", action="store_true",
                   help="打包時排除敏感檔案")
    p.set_defaults(func=cmd_deploy)

    # target
    p = subparsers.add_parser("target", help="管理目標機")
    target_sub = p.add_subparsers(dest="target_subcommand")

    tp = target_sub.add_parser("add", help="新增目標機")
    tp.add_argument("name", help="目標名稱")
    tp.add_argument("host", help="user@hostname")
    tp.add_argument("-k", "--key", help="SSH 私鑰路徑")
    tp.add_argument("-p", "--port", type=int, default=22, help="SSH 埠號")
    tp.set_defaults(func=cmd_target_add)

    tp = target_sub.add_parser("list", help="列出所有目標機")
    tp.set_defaults(func=cmd_target_list)

    tp = target_sub.add_parser("deploy", help="部署到已註冊目標")
    tp.add_argument("name", help="目標名稱")
    tp.add_argument("--package", help="指定 .revive 套件")
    tp.add_argument("--no-install", action="store_true",
                    help="跳過 Hermes 安裝")
    tp.add_argument("--include-sensitive", action="store_true",
                    help="包含敏感檔案")
    tp.set_defaults(func=cmd_target_deploy)

    tp = target_sub.add_parser("sync", help="增量同步到目標")
    tp.add_argument("name", help="目標名稱")
    tp.set_defaults(func=cmd_target_sync)

    tp = target_sub.add_parser("status", help="檢查目標狀態")
    tp.add_argument("name", help="目標名稱")
    tp.set_defaults(func=cmd_target_status)

    tp = target_sub.add_parser("remove", help="移除目標")
    tp.add_argument("name", help="目標名稱")
    tp.set_defaults(func=cmd_target_remove)

    tp = target_sub.add_parser("rename", help="重新命名目標")
    tp.add_argument("name", help="舊名稱")
    tp.add_argument("new_name", help="新名稱")
    tp.set_defaults(func=cmd_target_rename)

    # init
    p = subparsers.add_parser("init", help="初始化回生目錄結構")
    p.set_defaults(func=cmd_init)

    # help
    p = subparsers.add_parser("help", help="顯示說明")
    p.set_defaults(func=lambda a: parser.print_help())

    # gui
    p = subparsers.add_parser("gui", help="啟動 Web GUI (Gradio)")
    p.add_argument("-p", "--port", type=int, default=7860, help="GUI 埠號 (預設: 7860)")
    p.add_argument("--public", action="store_true",
                   help="監聽 0.0.0.0 允許遠端存取")
    p.set_defaults(func=cmd_gui)

    args = parser.parse_args()

    if not hasattr(args, "func"):
        parser.print_help()
        return

    # 確保目錄存在
    _ensure_dirs()

    # 執行
    args.func(args)


if __name__ == "__main__":
    main()

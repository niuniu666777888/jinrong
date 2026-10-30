#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
================================================================================
backup.py —— 数据库与知识库文档备份
================================================================================
三个要点：

  ① 用 sqlite3 的在线备份 API（conn.backup），不是直接拷文件。
     直接 copy app.db 会漏掉 WAL 里还没落盘的事务，恢复出来是坏库——
     这是备份方案里最常见、也最难发现的问题。

  ② 备份和文档一起打包成一个 zip，恢复时不会出现"数据库有了但文档没了"。

  ③ 保留最近 N 份，自动清理旧的；不清理的话磁盘迟早被备份撑满。

用法：
    python backup.py                 # 备份到 backups/，保留 7 份
    python backup.py --keep 14       # 保留 14 份
    python backup.py --restore backups/backup_20261030_120000.zip
================================================================================
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_FILE = BASE / "webapp" / "app.db"
DOCS_DIR = BASE / "webapp" / "docs"
BACKUP_DIR = BASE / "backups"


def create_backup(keep: int = 7) -> Path:
    """生成一份 zip 备份，返回文件路径。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    tmp_db = BACKUP_DIR / f".tmp_{stamp}.db"
    archive = BACKUP_DIR / f"backup_{stamp}.zip"

    # ① 在线备份数据库（包含 WAL 中未 checkpoint 的事务）
    if DB_FILE.exists():
        src = sqlite3.connect(str(DB_FILE))
        dst = sqlite3.connect(str(tmp_db))
        try:
            src.backup(dst)          # 原子快照，不锁业务库
        finally:
            dst.close()
            src.close()
    else:
        print(f"[警告] 数据库不存在：{DB_FILE}，只备份文档")

    # ② 打包
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        if tmp_db.exists():
            z.write(tmp_db, "app.db")
            tmp_db.unlink()
        if DOCS_DIR.exists():
            for f in DOCS_DIR.glob("*"):
                if f.is_file():
                    z.write(f, f"docs/{f.name}")

    # ③ 轮转
    old = sorted(BACKUP_DIR.glob("backup_*.zip"))[:-keep] if keep > 0 else []
    for f in old:
        f.unlink()

    return archive


def restore(archive: Path) -> None:
    """
    从备份恢复。

    恢复前先把当前的库另存一份（.before_restore），
    否则"恢复错了"就变成第二次事故——这种回滚失败的事故很常见。
    """
    if not archive.exists():
        raise SystemExit(f"备份文件不存在：{archive}")

    if DB_FILE.exists():
        keep = DB_FILE.with_suffix(".db.before_restore")
        shutil.copy2(DB_FILE, keep)
        print(f"[提示] 当前库已另存：{keep.name}")

    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        if "app.db" in names:
            z.extract("app.db", BACKUP_DIR)
            shutil.copy2(BACKUP_DIR / "app.db", DB_FILE)
            (BACKUP_DIR / "app.db").unlink()
        DOCS_DIR.mkdir(parents=True, exist_ok=True)
        for n in names:
            if n.startswith("docs/"):
                z.extract(n, BACKUP_DIR)
                shutil.copy2(BACKUP_DIR / n, DOCS_DIR / Path(n).name)
    shutil.rmtree(BACKUP_DIR / "docs", ignore_errors=True)

    print(f"[完成] 已从 {archive.name} 恢复，请重启服务并抽查检索结果")


def main() -> None:
    parser = argparse.ArgumentParser(description="支付对账系统备份工具")
    parser.add_argument("--keep", type=int, default=7, help="保留最近几份（默认 7）")
    parser.add_argument("--restore", type=str, default="", help="从指定 zip 恢复")
    args = parser.parse_args()

    if args.restore:
        restore(Path(args.restore))
        return

    path = create_backup(args.keep)
    size_mb = path.stat().st_size / 1024 / 1024
    print(f"[完成] {path}（{size_mb:.2f} MB）")
    print(f"[提示] 建议每天定时执行一次；恢复演练每季度做一次")


if __name__ == "__main__":
    main()

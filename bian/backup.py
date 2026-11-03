"""数据库与设置备份（start.sh backup 和每天自动备份共用）。保留最近 keep 份。"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time


def backup(db_path: str, dest_dir: str, secrets_path: str | None = None, keep: int = 7) -> str:
    os.makedirs(dest_dir, mode=0o700, exist_ok=True)
    os.chmod(dest_dir, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = os.path.join(dest_dir, f"bian-{stamp}.db")
    src, dst = sqlite3.connect(db_path), sqlite3.connect(target)
    try:
        src.backup(dst)  # SQLite 在线备份：服务运行中也能得到一致的快照
    finally:
        dst.close()
        src.close()
    os.chmod(target, 0o600)
    if secrets_path and os.path.exists(secrets_path):
        sec = os.path.join(dest_dir, f"secrets-{stamp}.json")
        shutil.copyfile(secrets_path, sec)
        os.chmod(sec, 0o600)
    for prefix in ("bian-", "secrets-"):
        files = sorted(f for f in os.listdir(dest_dir) if f.startswith(prefix))
        for old in files[:-keep]:
            os.remove(os.path.join(dest_dir, old))
    return target

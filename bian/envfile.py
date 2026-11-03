"""读取 .env（KEY=VALUE 每行一个）。不覆盖已存在的环境变量。

规则刻意保持简单，避免密码里的特殊字符被误解析：
- 空行和以 # 开头的行忽略；值里的 # 不当作注释（授权码、cookie 可能包含 #）。
- 值两端成对的单/双引号会被去掉；不做变量展开。
"""
from __future__ import annotations

import os


def load_env_file(path: str | None = None) -> list[str]:
    path = path or os.environ.get("BIAN_ENV_FILE") or ".env"
    if not os.path.isfile(path):
        return []
    loaded = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if key and key not in os.environ and value != "":
                os.environ[key] = value
                loaded.append(key)
    return loaded

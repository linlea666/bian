"""CSV 导出的公共写法：带 BOM（Excel 直接打开不乱码），并防止 Excel 把单元格内容当公式执行。"""
from __future__ import annotations

import csv
import io


def cell(v) -> str:
    v = "" if v is None else str(v)
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def to_csv(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([cell(x) for x in r])
    return ("﻿" + buf.getvalue()).encode()

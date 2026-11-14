"""关注币安聪明钱（公开实盘）：查询主页、关注 / 取消关注。

关注后监控线程每轮读取他的当前持仓，只提醒交易动态（开仓 / 加仓 / 减仓 / 平仓 / 反手）；
他的仓位不是你的钱，不做亏损档位、强平等风险提醒，也不计入总权益、仓位回合和统计。
邮件复用“按领航员单独设置交易动作邮件”（leader_actions），关注时可以直接设为“发邮件”。
"""
from __future__ import annotations

import json
import time

from .api import smart_money

MAX_FOLLOW = 10  # 每个人每轮 1–2 个私有请求，用你的网页凭证；人数设上限，避免请求太密


class SmartError(ValueError):
    """给用户看的错误（中文）。"""


def list_traders(store) -> list[dict]:
    out = []
    for tid, name, added, prof in store.db.execute("SELECT id, name, added, profile FROM smart_traders ORDER BY added"):
        p = json.loads(prof) if prof else {}
        out.append({**p, "id": tid, "name": name, "added": added, "url": smart_money.PROFILE_URL.format(tid)})
    return out


def lookup(client, text: str) -> dict:
    try:
        tid = smart_money.normalize_id(text)
    except ValueError as e:
        raise SmartError(str(e)) from None
    prof = smart_money.profile(client, tid)
    prof["followed"] = False
    return prof


def follow(store, settings, client, text: str, email: bool = True) -> dict:
    try:
        tid = smart_money.normalize_id(text)
    except ValueError as e:
        raise SmartError(str(e)) from None
    if store.db.execute("SELECT 1 FROM smart_traders WHERE id=?", (tid,)).fetchone():
        raise SmartError("已经关注过这个聪明钱了")
    if store.db.execute("SELECT COUNT(*) FROM smart_traders").fetchone()[0] >= MAX_FOLLOW:
        raise SmartError(f"最多关注 {MAX_FOLLOW} 个聪明钱（每人每轮都要用你的网页凭证请求一次），请先取消关注不需要的")
    prof = smart_money.profile(client, tid)
    if not prof["sharing_position"]:
        raise SmartError(f"{prof['name']} 没有公开当前持仓，无法跟踪他的开平仓动态")
    store.db.execute("INSERT INTO smart_traders (id, name, added, profile) VALUES (?,?,?,?)",
                     (tid, prof["name"], time.time(), json.dumps(prof, ensure_ascii=False)))
    store.db.commit()
    if email:
        la = dict(settings.get()["notify"]["leader_actions"])
        la[tid] = True
        settings.update({"notify": {"leader_actions": la}})
    store.log_event("smart.followed", f"关注聪明钱：{prof['name']}" + ("（开平仓发邮件）" if email else ""),
                    leader_id=tid)
    return {"id": tid, "name": prof["name"], "email": email}


def unfollow(store, settings, tid: str) -> dict:
    row = store.db.execute("SELECT name FROM smart_traders WHERE id=?", (tid,)).fetchone()
    if not row:
        raise SmartError("没有关注这个聪明钱")
    store.db.execute("DELETE FROM smart_traders WHERE id=?", (tid,))
    store.db.commit()
    la = dict(settings.get()["notify"]["leader_actions"])
    if la.pop(tid, None) is not None:
        settings.update({"notify": {"leader_actions": la}})
    store.log_event("smart.unfollowed", f"取消关注聪明钱：{row[0]}", leader_id=tid)
    return {"id": tid, "name": row[0]}

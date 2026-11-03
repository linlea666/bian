"""第三期：止损档位推荐、分批止盈阶梯（回测 + R13 提醒）、发现领航员。"""
import json

import pytest

from bian import backtest, recommend
from bian.app import App
from bian.backtest import Costs, Path, ladder_search, simulate_ladder
from bian.config import AppConfig
from bian.models import ClosedPosition, Position
from bian.rules import RuleConfig, evaluate
from bian.store import Store
from bian.sync import ensure_leader, upsert_leader_history
from tests.test_web import web  # noqa: F401

NOFEE = Costs(fee_rate=0, stop_slippage=0)


def cp(i, final_price, lev=10):
    return ClosedPosition("L", str(i), "BTCUSDT", "LONG", i * 1000, i * 1000 + 500, 100, final_price, 1, 1, lev,
                          final_price - 100, (final_price / 100 - 1) * lev)


def test_simulate_ladder_takes_partial_profits():
    # 浮盈冲到 +60%，最后回落到 +10% 平仓：+30% 平 1/3、+60% 平 1/3，剩下 1/3 按 +10%
    p = Path(cp(1, 101), [(0, 0.3, 0.2, 0), (0.1, 0.6, 0.4, 0.2)])
    r = simulate_ladder(p, [(0.3, 1 / 3), (0.6, 1 / 3)], NOFEE)
    assert r.baseline == pytest.approx(0.1) and r.result == pytest.approx(0.1 + 0.2 + 0.1 / 3)
    never = simulate_ladder(Path(cp(2, 99), [(-0.2, 0.1, -0.1, 0)]), [(0.3, 0.5)], NOFEE)
    assert never.result == never.baseline and never.reason == "leader_close"


def test_ladder_search_recommends_only_when_out_of_sample_better():
    # 回吐型：每笔都冲到 +50% 再回落到 +5%
    paths = [Path(cp(i, 100.5), [(0, 0.5, 0.3, 0)]) for i in range(40)]
    out = ladder_search(paths, 0.5, 0.5, NOFEE)
    assert out["recommended"] and out["recommended"]["gain"] > 0 and out["oos"]["test_total"] > out["oos"]["test_baseline"]
    # 趋势型：冲到 +50% 后继续涨到 +100% 平仓 → 止盈反而少赚，不推荐
    trend = [Path(cp(i, 110), [(0, 0.5, 0.4, 0), (0.4, 1.0, 1.0, 0.4)]) for i in range(40)]
    assert ladder_search(trend, 0.5, 0.5, NOFEE)["recommended"] is None
    assert backtest.ladder_candidates(0.33, 0.62)[0] == [(0.35, 1 / 3), (0.6, 1 / 3)]


def lpos(roi, qty=1.0):
    mark = 100 * (1 + roi / 10)
    return Position("L", "BTCUSDT", "LONG", qty, 100, mark, 10, (mark - 100) * qty, "copy")


def test_r13_each_level_once_per_version():
    cfg = RuleConfig().merged({"tp_ladder": [[0.6, 1 / 3], [0.3, 1 / 3]], "min_alert_u": 0})
    assert cfg.tp_ladder == ((0.3, 1 / 3), (0.6, 1 / 3))
    st, al = evaluate(lpos(0.1), None, cfg, 0)
    seq = []
    for t, roi in enumerate((0.35, 0.4, 0.2, 0.35, 0.7, 0.8), 1):
        st, al = evaluate(lpos(roi), st, cfg, t)
        seq.append([a.rule_id for a in al if a.rule_id == "R13"])
    assert seq == [["R13"], [], [], [], ["R13"], []]
    st, al = evaluate(lpos(0.8, qty=2.0), st, cfg, 10)  # 加仓 → 新版本：已在最高级之上，只提醒一次最高级
    assert [a.rule_id for a in al] == ["R07", "R13"] and "第 2/2 级" in al[1].title
    st, al = evaluate(lpos(0.85, qty=2.0), st, cfg, 11)
    assert [a.rule_id for a in al] == []


def test_tp_ladder_validation():
    with pytest.raises(ValueError):
        RuleConfig().merged({"tp_ladder": [[0.3, 0.7], [0.6, 0.7]]})  # 合计超过 100%
    with pytest.raises(ValueError):
        RuleConfig().merged({"tp_ladder": [[-0.1, 0.5]]})
    assert RuleConfig().merged({"tp_ladder": []}).tp_ladder == ()


def seed_history(store, lid="123", n=40):
    rows = []
    for i in range(n):
        win = i % 3 != 0
        rows.append({"positionId": str(i), "symbol": "BTCUSDT", "side": "Long", "opened": i * 10_000,
                     "closed": i * 10_000 + 5000, "avgCost": 100, "avgClosePrice": 102 if win else 97,
                     "maxOpenInterest": 1, "closedVolume": 1, "leverage": "10", "closingPnl": 2 if win else -3,
                     "roi": "0.2" if win else "-0.3", "status": "All Closed", "updateTime": i * 10_000 + 5000})
    upsert_leader_history(store, lid, rows)
    for i in range(n):  # 盈利单最深浮亏 -5%…-80%，亏损单 -40%…-150%
        mae = -(0.05 + (i % 16) * 0.05) if i % 3 != 0 else -(0.4 + (i % 12) * 0.1)
        store.db.execute("UPDATE leader_history SET mae=?, mfe=0.5, path_interval='1m' WHERE lead_id=? AND position_id=?",
                         (mae, lid, str(i)))
    data = {"profile": {"n": n}, "backtest": {"baseline": {"total": 1.0}, "stop_only": [
        {"label": "止损-30%", "total": 0.5, "winners_cut": 9}, {"label": "止损-70%", "total": 1.4, "winners_cut": 2}]},
        "ladder": {"recommended": {"levels": [[0.3, 0.5]], "label": "+30% 平 50%，其余跟随", "gain": 2.0},
                   "candidates": []}, "mdd_90d": 25.0}
    store.db.execute("INSERT OR REPLACE INTO leader_profiles VALUES (?, 1, ?, ?)", (lid, n, json.dumps(data)))
    store.db.commit()


def test_recommend_and_adopt(tmp_path):
    app = App(AppConfig(db_path="bian.db"), data_dir=str(tmp_path))
    store = Store(app.db_path)
    ensure_leader(store, "123", "甲", "copy")
    r = recommend.recommend(store, app.settings, "123")
    assert not r["ok"] and "还没有分析结果" in r["reason"]
    seed_history(store, n=20)
    assert "样本不足" in recommend.recommend(store, app.settings, "123")["reason"]
    seed_history(store, n=40)
    r = recommend.recommend(store, app.settings, "123")
    assert r["ok"] and r["loss_tiers"] == sorted(r["loss_tiers"]) and len(set(r["loss_tiers"])) == len(r["loss_tiers"])
    assert all(20 <= t <= 400 and t % 10 == 0 for t in r["loss_tiers"])
    assert r["tier_notes"][0]["winners_deeper"] <= 0.3  # P75：大约四分之一的盈利单会碰到第 1 档
    assert r["hard_stop"]["label"] == "止损-70%" and r["hard_stop"]["better"]
    assert r["total_stop_pct"] == 0.3 and r["ladder"]["levels"] == [[0.3, 0.5]]
    out = recommend.adopt(store, app.settings, "123", "stops")
    assert "亏损档位" in out["message"]
    lr = app.settings.get()["leader_rules"]["123"]
    assert lr["loss_tiers"] == r["loss_tiers"] and lr["loss_tiers_mode"] == "roi" and lr["recommend_total_stop_pct"] == 0.3
    recommend.adopt(store, app.settings, "123", "ladder")
    assert app.settings.rules_config("123").tp_ladder == ((0.3, 0.5),)
    assert app.settings.rules_config("999").tp_ladder == ()  # 只对这个领航员生效
    recommend.adopt(store, app.settings, "123", "clear_ladder")
    assert app.settings.rules_config("123").tp_ladder == ()
    with pytest.raises(ValueError):
        recommend.adopt(store, app.settings, "123", "nope")


def test_web_detail_adopt_and_discover(web, monkeypatch):  # noqa: F811
    from bian.models import Leader
    app, c = web
    assert c.login("Passw0rd123")[0] == 200
    store = Store(app.db_path)
    ensure_leader(store, "123", "甲", "watch")
    seed_history(store, n=40)
    s, d = c.call("GET", "/api/leaders/detail?id=123")
    assert s == 200 and d["recommend"]["ok"]
    s, d = c.call("POST", "/api/leaders/adopt", {"id": "123", "what": "ladder"})
    assert s == 200 and "已采用" in d["message"]
    calls = []

    def fake_board(client, time_range="30D", sort_by="ROI", page=1, page_size=20, nickname=""):
        calls.append(page)
        return 2, [Leader(portfolio_id=f"{page}23", nickname=f"榜{page}", roi=1.5, pnl=100, mdd=12.0, win_rate=0.6,
                          aum=1000, copier_pnl=50, current_copy_count=10, max_copy_count=100, raw={"sharpRatio": "2"})]

    monkeypatch.setattr("bian.web.server.leaders_api.leaderboard", fake_board)
    s, d = c.call("GET", "/api/discover?range=90D&sort=MDD")
    assert s == 200 and [x["id"] for x in d["items"]] == ["123"] and d["items"][0]["kind"] == "watch"
    s, d = c.call("GET", "/api/discover?range=90D&sort=MDD")
    assert calls == [1]  # 不足一页就不再翻；10 分钟内用缓存

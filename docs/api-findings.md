# 币安跟单接口实测结论

测试时间：2026-11-02。环境：云容器（美国出口 IP），无登录凭证。复测命令：`bian probe --out docs/api-findings-latest.md`。

## 公开接口（无需登录）

| 数据 | 接口（均为 `https://www.binance.com` 下） | 结果 | 关键字段 |
|---|---|---|---|
| 领航员榜单 | POST `/bapi/futures/v1/friendly/future/copy-trade/home-page/query-list` | ✅ 约 9170 位 | leadPortfolioId, nickname, roi, pnl, mdd, winRate, aum, copierPnl, currentCopyCount |
| 领航员详情 | GET `/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio/detail?portfolioId=` | ✅ | marginBalance, aumAmount, copierPnl, profitSharingRate, **positionShow**；copyPortfolioId 不带登录为 null |
| 已平仓历史 | POST `.../lead-portfolio/position-history` `{pageNumber,pageSize≤100,portfolioId}` | ✅ 全量可翻页 | opened, closed, avgCost, avgClosePrice, maxOpenInterest, closedVolume, leverage, closingPnl, roi, side, isolated, status |
| 当前持仓 | GET `.../lead-data/positions?portfolioId=` | ⚠️ 只有 positionShow=true 的领航员有数据（AUM 前 40 名里 6 位） | symbol, positionAmount（BOTH 模式负数=空）, entryPrice, markPrice, unrealizedProfit, leverage, breakEvenPrice, notionalValue；返回全部合约，需过滤数量≠0 |
| 标记价 | GET `/fapi/v1/premiumIndex` | ✅ 945 个合约 | markPrice, time, lastFundingRate |
| K 线 | GET `/fapi/v1/klines` | ✅ 单次最多 1500 根 | OHLC |

注意：`fapi.binance.com` 在该地区返回 **HTTP 451**，而 `www.binance.com/fapi/...` 可以访问，所以本项目统一走 www 域名。

## 私有接口（需 `p20t` cookie + `csrftoken` header，参考 nofx-1 `copytrade/binance_provider.go`）

| 数据 | 接口 | 状态 |
|---|---|---|
| 凭证校验 | GET `/bapi/accounts/v1/private/account/get-user-base-info` | 待用户凭证验证 |
| 我的所有跟单关系 | GET `/bapi/futures/v1/private/future/copy-trade/copy-portfolio/detail-list?ongoing=true` | 待验证（nofx-1 在用：marginBalance、netCopyAmount、unrealizedPnl、copyMode、isPaused） |
| 我对某领航员的 copyPortfolioId | GET 详情接口 + 登录态 | 待验证 |
| 我的跟单仓位 | POST `/bapi/futures/v6/private/future/user-data/user-position` `{copyTradeType:"COPY", portfolioId:<copyPortfolioId>}` | 待验证（预期含 liquidationPrice、positionInitialMargin） |
| 我的跟单成交 / 历史仓位 | POST `.../copy-portfolio/trade-history`、`.../copy-portfolio/position-history` `{copyTradeType, portfolioId, page, rows}` | 待验证 |

凭证失效错误码：`100001005`（未登录）、`100002002`（鉴权失败）。`11012030` 表示领航员已关闭带单，或者把 copyPortfolioId 当成 leadPortfolioId 用了。

## 能做 / 不能做

- ✅ 不登录也能做领航员画像与回测（完整的已平仓历史 + K 线）。
- ✅ 公开持仓的领航员可以实时监控（轮询，约 20 秒延迟）。
- ⚠️ 隐藏持仓的领航员只能通过“我的跟单仓位”间接看到，需要登录凭证，且必须已在币安跟单该领航员。
- ❌ 没找到修改原生跟单仓位止损/平仓的接口（官方 API 也不支持），所以系统只提醒，由你在币安里操作。
- ❌ 公开历史没有逐笔加减仓，MAE/MFE 和回测只能近似（报告中已注明）。

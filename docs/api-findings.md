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

## 私有接口（需 `p20t` cookie + `csrftoken` header）—— 2026-11-02 已用真实账号验证

取凭证：浏览器 Network 面板中，请求被 Service Worker 代发（状态显示 “from service worker”）时不显示 Cookie，需要从 Application → Cookies 里取 `p20t`，或对普通 bapi 请求使用 “Copy as cURL”。

| 数据 | 接口 | 实测 |
|---|---|---|
| 凭证校验 | GET `/bapi/accounts/v1/private/account/get-user-base-info` | ✅ |
| 我的所有跟单关系 | GET `/bapi/futures/v1/private/future/copy-trade/copy-portfolio/detail-list?ongoing=true` | ✅ 字段：leadPortfolioId, copyPortfolioId, nickname, copyMode, marginBalance（我的跟单权益）, netCopyAmount（净投入）, unrealizedPnl, realizedPnl, netProfitAmount, profitSharingRate, profitSharedAmount, unProfitSharedAmount, isPaused, leadStatus, startDate，以及**原生总止损** totalStopLossUsdt（止损额）/ totalStopLossAmount（触发权益）/ totalStopLossMargin（设置时的权益）/ disposalStrategy（SELL_ALL / SELF_MANAGING） |
| 我的跟单仓位 | POST `/bapi/futures/v6/private/future/user-data/user-position` `{copyTradeType:"COPY", portfolioId:<copyPortfolioId>}` | ✅ **是我自己的仓位**（数量约为领航员的权益比例）。字段：symbol, positionSide, positionAmount, entryPrice, markPrice, breakEvenPrice, unrealizedProfit, notionalValue, positionInitialMargin, initialMargin, maintMargin, liquidationPrice, updateTime。**没有 leverage**（用 notional/positionInitialMargin 反推）。未公开持仓的领航员也能这样看到 |
| 我的逐笔成交 | POST `.../copy-portfolio/trade-history` `{copyTradeType, portfolioId, pageNumber, pageSize}` | ✅ time, symbol, side, positionSide, price, qty, quantity(U), fee（负数=支出）, realizedProfit。分页参数必须用 `pageNumber/pageSize`，用 `page/rows` 会被忽略、固定返回 10 条 |
| 我的历史仓位 | POST `.../copy-portfolio/position-history`（参数同上） | ✅ 含 leverage、avgCost/avgClosePrice、maxOpenInterest/closedVolume、status（All Closed / Partially Closed） |

实测要点：

- 延迟约 280ms/次，标记价每几秒更新，20 秒轮询足够。
- **强平价不可靠**：全仓 + 双向持仓下经常是 0，或者明显不合理（例如现价 514、强平价 739467）。只有当强平价在不利方向、且距离标记价 <50% 时才使用。
- **领航员普遍双向持仓**（同一币种多空并存），按单边 ROI 报警会误报，必须按币种合并看净盈亏。
- **跟单比例失真**：小额跟单会被最小下单量取整（例如 BTC 0.001），导致个别币种超配好几倍，另一些币种低配或漏单。
- 刚开始跟单时同步建立的仓位，短时间内不出现在成交和历史记录中。
- 凭证失效错误码：`100001005`（未登录）、`100002002`（鉴权失败）。`11012030` 表示领航员已关闭带单，或把 copyPortfolioId 当成 leadPortfolioId 用了。

## 能做 / 不能做

- ✅ 不登录也能做领航员画像与回测（完整的已平仓历史 + K 线）。
- ✅ 公开持仓的领航员可以实时监控（轮询，约 20 秒延迟）。
- ✅ 有凭证时，所有跟单关系（包括未公开持仓的领航员）都能通过“我的跟单仓位”实时监控，并能读取原生总止损设置。
- ❌ 没找到修改原生跟单仓位止损/平仓的接口（官方 API 也不支持），所以系统只提醒，由你在币安里操作。
- ❌ 公开历史没有逐笔加减仓，MAE/MFE 和回测只能近似（报告中已注明）。

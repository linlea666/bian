# bian — 币安合约跟单风控助手

目标：跟单币安领航员时，**领航员亏损时少亏，盈利时尽量跟上**。

本工具**只读、不下单**：它分析领航员、回测止损规则、监控仓位并提醒你，所有操作仍由你在币安 App 或网页上完成。

| 命令 | 作用 | 需要登录凭证？ |
|---|---|---|
| `bian leaders` | 领航员榜单（ROI、回撤、胜率、跟单者盈亏） | 否 |
| `bian profile <ID>` | 领航员画像：胜率、盈亏比、扛单率、利润回吐、最大浮亏等 | 否 |
| `bian backtest <ID>` | 画像 + 止损/保本/回撤止盈规则回测（含样本外验证），告诉你该领航员适不适合加止损、加多少 | 否 |
| `bian monitor` | 实时监控持仓，按规则提醒（控制台 / Telegram） | 公开持仓的领航员不需要；隐藏持仓的需要 |
| `bian copies` | 我的跟单关系、权益和仓位（含强平价） | 需要 |
| `bian probe` | 检测各接口是否可用 | 私有部分需要 |
| `bian alerts` | 查看提醒记录（含发送失败的） | 否 |

`<ID>` 是领航员主页 URL 中 `lead-details/` 后面的数字。

## 安装

运行时只用 Python 3.11+ 标准库，无第三方依赖。

```bash
git clone https://github.com/linlea666/bian && cd bian
```

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## 推荐用法

1. **先选人、再定参数**：用 `bian backtest <ID> --out reports/xxx.md` 查看每位领航员的画像和回测。
   - 只用硬止损的方案（报告里的“方案一”）可以直接在币安跟单设置里配置。
   - 保本、回撤止盈币安原生跟单不支持，由 monitor 提醒你手动操作。
   - 只采用**样本外也有效**的参数。如果回测显示任何止损都显著降低收益，说明该领航员靠“扛单/暴利单”赚钱，应该通过控制投入金额来控制风险，而不是加止损。
2. **配置监控**：`cp config.example.toml config.toml`，填入领航员 ID 和阈值（可以按领航员单独覆盖）。
3. **运行**：`bian monitor -c config.toml`（建议部署在自己的服务器上 7×24 运行，见下文）。

## 凭证（可选，只放在环境变量中）

| 变量 | 用途 |
|---|---|
| `BINANCE_P20T` | 币安网页登录 cookie `p20t`：浏览器登录 binance.com → 开发者工具 → Application → Cookies |
| `BINANCE_CSRFTOKEN` | 请求头 `csrftoken`：开发者工具 → Network → 任意 bapi 请求的请求头 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 推送：找 @BotFather 创建机器人，向机器人发一条消息后，调用 getUpdates 获取 chat id |

⚠️ `p20t` 等同于你的登录会话。不要发给任何人，不要写进配置文件或提交到 git。它通常 7–30 天过期，过期时 monitor 会发紧急提醒，按上面的步骤重新获取即可。

## 提醒规则

| 规则 | 触发条件（默认值，可改） | 级别 |
|---|---|---|
| R00 | 新开仓 / 启动时的持仓汇总 | 信息 |
| R01 / R02 / R03 | ROI ≤ -20% / -35% / 计划硬止损线 -40%（R03 持续触发时每 30 分钟重复提醒） | 警告 / 紧急 / 紧急 |
| R04 | 浮盈达到 +20% → 建议把止损上移到保本价 | 信息 |
| R05 | 浮盈从最高点回撤 ≥15 个点 → 建议部分止盈或上移止损 | 警告 |
| R10 | 曾经盈利的单子回到保本线以下 | 紧急 |
| R07 | 领航员加仓：均价变化，提醒复查止损（跟单 TP/SL 只对设置后的新仓有效） | 警告 |
| R09 | 标记价距离强平价 ≤5%（仅“我的跟单仓位”能拿到强平价） | 紧急 |
| R06 | 组合预估浮亏超过预算 | 紧急 |
| R08 | 数据失联或无法读取持仓（这时不会显示“一切正常”） | 警告 |
| R11 | 仓位已平 | 信息 |

去重和升级：同一仓位版本、同一规则只提醒一次；回升超过阈值 5 个点后重新武装；加仓后视为新版本，重新评估。持仓列表突然变空时，需要连续确认 2 次才判定为平仓。状态存在 SQLite 中，重启后不会重复提醒。

## 7×24 部署（systemd 示例）

```ini
# /etc/systemd/system/bian.service
[Service]
WorkingDirectory=/opt/bian
EnvironmentFile=/opt/bian/.env   # 放 BINANCE_P20T 等，chmod 600
ExecStart=/opt/bian/.venv/bin/bian monitor -c /opt/bian/config.toml
Restart=always
[Install]
WantedBy=multi-user.target
```

服务器需要能访问 `www.binance.com`。如果服务器在受限地区，`fapi.binance.com` 会返回 451，本工具已统一改走 `www.binance.com/fapi`。

## 精度与限制

- 回测基于公开已平仓历史 + K 线近似重建：没有逐笔加减仓数据，K 线内部按“先不利后有利”的保守顺序处理。只计手续费和止损滑点，不计分润和资金费。
- 领航员的 ROI ≠ 你的 ROI：你的开仓价、滑点和分润都不同；配置了凭证时，监控以你真实的跟单仓位为准。
- 轮询约 20 秒一次，不是交易所端的止损。真正的硬止损仍需你在币安里设置。

更多内容：[接口实测结论](docs/api-findings.md) · [PRD 评审](docs/prd-review.md) · [回测报告示例](docs/examples/backtest-leader-a.md)

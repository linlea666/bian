/* bian 风控作战室 —— 单页应用（原生 JS，无需构建） */
"use strict";

// gen：路由代次。每次切换页面 +1；异步请求返回后代次不一致就丢弃结果，定时器也只属于创建它的那一代
const S = { me: null, settings: null, page: "", timer: null, gen: 0, events: [], alerts: [], evFilter: null };
const stale = (gen) => gen !== S.gen;
const root = document.getElementById("root");

/* ---------------- 术语解释（小白模式） ---------------- */
const HELP = {
  roi: "收益率（ROI）= 浮动盈亏 ÷ 这个仓位占用的保证金。和币安仓位页显示的一致。全仓模式下可以低于 -100%。",
  tier: "亏损档位：仓位亏损达到你设置的百分比时提醒一次。例如 50/100/150，表示亏到 -50%、-100%、-150% 时各提醒一次，不会反复轰炸。",
  drawdown: "回撤：这个跟单的权益从最高点下跌了多少。例如最高 300U、现在 270U，回撤就是 10%。",
  native_stop: "跟单总止损：币安自带的功能。这个跟单累计亏到设定金额时，由交易所自动全部平仓。这是目前唯一能真正“自动少亏”的手段。",
  equity: "权益：这个跟单当前值多少钱（含浮动盈亏）。",
  invested: "净投入：你往这个跟单里投了多少本金（追加减去赎回）。",
  unrealized: "浮动盈亏：还没平仓的仓位现在赚或亏多少。",
  mae: "持仓过程中最深浮亏：这个仓位从开到现在，最多亏到过多少。",
  mfe: "持仓过程中最高浮盈：这个仓位最多赚到过多少。如果很高但最后没拿住，就是“利润回吐”。",
  hedge: "多空对冲：同一个币同时有多单和空单。系统按两边合并后的净盈亏判断风险，避免单边误报。",
  min_u: "金额门槛：亏损或盈利金额低于这个值（U）时不提醒，避免小仓位刷屏。",
  mode: "按 ROI：档位直接对应币安仓位页的收益率。按价格波动：档位是价格反向走了多少 %，系统自动乘以杠杆，高杠杆仓位不会太早触发。",
  cooldown: "冷却：同一个仓位两条亏损提醒之间至少间隔多久（跌到更高一档时不受限制）。",
  breakeven: "保本提醒：浮盈达到这个比例时，提醒你把止损上移到开仓价附近，保住不亏。",
  trail: "回撤止盈提醒：浮盈从最高点回落这么多个百分点时，提醒你部分止盈。",
  cred: "币安网页登录凭证（p20t + csrftoken），用来读取你的跟单仓位。通常 7–30 天会失效，失效后系统会发邮件提醒你更新。",
  early: "提前离场：你的仓位平掉了，但领航员同一个仓位当时还没平（手动平仓、你设的止损止盈单、总止损都算）。系统会继续盯着领航员，等他平仓后算出你这次离场是“挽回了”还是“白止损”。",
  verdict: "止损挽回：领航员后来亏得更多；白止损：领航员扛回来了或亏得更少；避免回吐：你盈利离场后领航员利润回吐了；少赚：你盈利离场后领航员继续涨。金额 = 你的实际结果 − 跟到底的结果（含手续费）。",
  realized: "已实现净盈亏：平仓赚亏 + 手续费，来自你的逐笔成交记录（包含跟单开始以来的历史）。",
  equity_change: "权益变化：（权益 − 净投入）的变化，包含浮动盈亏，已扣除追加 / 赎回。安装后才开始记录。",
  carry: "扛单率：最终盈利的单子里，途中浮亏曾超过 -30% 的比例。越高说明领航员越爱扛单，设太紧的止损容易被误杀。",
  giveback: "利润回吐率：浮盈曾经超过 20% 的单子，最后回吐了一半以上的比例。越高越适合自己分批止盈。",
  email_events: "只有勾选的事件才发邮件，其余只在后台记录（每日摘要里会汇总条数）。同一事件当天只发一次，明显恶化才再发；紧急的立即发，其余按汇总间隔合并成一封。凭证失效邮件无论如何都会发。",
  rec_tiers: "推荐的亏损档位：取他历史上最后赚钱的单子，持仓过程中最深浮亏的 75% / 90% / 97% 分位。意思是大多数最后赚钱的单子不会跌到这么深，提醒响了说明这一单比他平时扛得更深。",
  rec_stop: "跟单总止损比例：他近 90 天最大回撤 × 1.2。正常波动不会触发，真失控时由币安自动全部平仓兜底。采用后，作战室里“建议总止损金额”按这个比例计算，需要你在币安自己设置。",
  profit_guard: "盈利保护：浮盈达到一定比例后，把止损挪到保本价；再从最高浮盈回落若干个点时止盈。推荐的方案来自回测全组合：用前 70% 历史选出，在后 30% 和全部历史上都比原始跟单好才推荐。",
  ladder: "分批止盈阶梯：按他盈利单通常能涨到的浮盈（中位数 / 75% 分位）分批止盈，和“不止盈”对比。只有用前 70% 的历史选出、在后 30% 也更好的方案才推荐。采用后浮盈到位时提醒你在币安手动止盈（默认不发邮件）。",
  principal: "利润翻倍取回本金：跟单权益达到净投入的这个倍数时（例如 2 倍 = 利润翻倍），提醒你在币安把本金取出来，剩下的都是利润，继续跑也不会再亏本。每个整数倍（2 倍、3 倍…）只提醒一次；本金取出后不再提醒。",
  exchange: "交易所账户：用只读 API Key 读取你自己在币安 / OKX 的 U 本位永续持仓。只读、永远不下单；和跟单共用风控参数（可在“按领航员单独设置”里给某个账户单独设）、提醒、邮件、作战室和统计。",
  margin_ratio: "维持保证金率 = 维持保证金 ÷ 账户权益。到 100% 交易所会强平。全仓时它比单个仓位的强平价更能反映整个账户的爆仓风险：默认 50% 提醒、80% 紧急。",
  no_stop: "止损单检查：每分钟读一次你在交易所挂的止损单。仓位开仓 / 加仓 2 分钟后还没挂止损单（或只覆盖了一部分数量），提醒一次。不想要可以关掉。",
  ended: "已停止跟单：你在币安已经不再跟这个领航员。数据默认一直保留；可以导出备份，或删除他的全部数据（删除前会自动备份一份到服务器）。",
};
const help = (k) => HELP[k] ? `<span class="help" data-help="${k}" tabindex="0" role="button" aria-label="说明">?</span>` : "";

/* ---------------- 工具 ---------------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x, d = 2) => (x == null || isNaN(x) ? "-" : Number(x).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d }));
// 先按显示精度取整再判断正负，避免 -0.001 显示成红色的“-0.00”
const rnd = (x, d) => Number(Number(x).toFixed(d)) || 0;
const signed = (x, d = 2) => { if (x == null || isNaN(x)) return "-"; const v = rnd(x, d); return (v > 0 ? "+" : "") + num(v, d); };
const tone = (v) => (v > 0 ? "up" : v < 0 ? "down" : "");
const U = (x, d = 2) => (x == null ? "-" : `<span class="num ${tone(rnd(x, d))}">${signed(x, d)}U</span>`);
const pct = (x, d = 1) => (x == null ? "-" : `<span class="num ${tone(rnd(x * 100, d))}">${signed(x * 100, d)}%</span>`);
const side = (s) => ({ LONG: "多", SHORT: "空", HEDGE: "多空对冲" }[s] || s);
function dt(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function ago(sec) {
  if (sec == null) return "从未";
  if (sec < 60) return `${Math.round(sec)} 秒`;
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟`;
  if (sec < 86400) return `${(sec / 3600).toFixed(1)} 小时`;
  return `${(sec / 86400).toFixed(1)} 天`;
}
function toast(text) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = text;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

async function api(method, path, body) {
  const opt = { method, headers: { "content-type": "application/json" }, credentials: "same-origin" };
  if (S.me && S.me.csrf && method !== "GET") opt.headers["X-CSRF-Token"] = S.me.csrf;
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* 非 JSON */ }
  if (r.status === 401 && path !== "/api/login") { S.me = null; renderLogin(); throw new Error("请先登录"); }
  if (!r.ok) throw new Error(data.error || `请求失败（${r.status}）`);
  return data;
}

/* ---------------- 帮助气泡 ---------------- */
document.addEventListener("click", (e) => {
  document.querySelectorAll(".tip").forEach((t) => t.remove());
  const h = e.target.closest("[data-help]");
  if (!h) return;
  e.preventDefault();
  const tip = document.createElement("div");
  tip.className = "tip";
  tip.textContent = HELP[h.dataset.help];
  document.body.appendChild(tip);
  const r = h.getBoundingClientRect();
  const left = Math.min(window.scrollX + r.left, window.scrollX + document.documentElement.clientWidth - tip.offsetWidth - 8);
  tip.style.left = `${Math.max(8, left)}px`;
  tip.style.top = `${window.scrollY + r.bottom + 6}px`;
});

/* ---------------- 登录 / 改密码 ---------------- */
function renderLogin(msg) {
  clearInterval(S.timer);
  root.innerHTML = `
  <div class="center"><form class="card login" id="loginForm">
    <div class="logo"><span class="mark">B</span>bian 风控作战室</div>
    <p class="muted small">币安合约跟单风控助手 · 只读提醒，不会替你下单</p>
    <label class="f"><span>用户名</span><input name="username" value="admin" autocomplete="username"></label>
    <label class="f"><span>密码</span><input name="password" type="password" autocomplete="current-password" autofocus></label>
    <button class="btn primary" style="width:100%">登录</button>
    <div id="loginMsg">${msg ? `<div class="msg err">${esc(msg)}</div>` : ""}</div>
    <p class="hint">忘记密码：在服务器上执行 <code>cd /opt/bian && ./start.sh password</code></p>
  </form></div>`;
  document.getElementById("loginForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("POST", "/api/login", { username: f.get("username"), password: f.get("password") });
      await boot();
    } catch (err) {
      document.getElementById("loginMsg").innerHTML = `<div class="msg err">${esc(err.message)}</div>`;
    }
  };
}

function renderChangePassword(forced) {
  const html = `
  <form class="card ${forced ? "login" : ""}" id="pwForm">
    <h2>${forced ? "请先修改初始密码" : "修改登录密码"}</h2>
    <p class="muted small">${forced ? "安装时生成的是临时密码，改成你自己记得住的密码后才能继续。" : "修改后其他设备需要重新登录。"}</p>
    <label class="f"><span>原密码</span><input name="old" type="password" autocomplete="current-password"></label>
    <label class="f"><span>新密码</span><input name="new" type="password" autocomplete="new-password"><div class="hint">至少 8 位，同时包含字母和数字</div></label>
    <label class="f"><span>再输入一次</span><input name="new2" type="password" autocomplete="new-password"></label>
    <button class="btn primary">保存新密码</button>
    <div id="pwMsg"></div>
  </form>`;
  if (forced) root.innerHTML = `<div class="center">${html}</div>`;
  else return html;
  bindPassword(true);
}
function bindPassword(forced) {
  const f = document.getElementById("pwForm");
  if (!f) return;
  f.onsubmit = async (e) => {
    e.preventDefault();
    const d = new FormData(f);
    const box = document.getElementById("pwMsg");
    if (d.get("new") !== d.get("new2")) { box.innerHTML = `<div class="msg err">两次输入的新密码不一致</div>`; return; }
    try {
      const r = await api("POST", "/api/password", { old: d.get("old"), new: d.get("new") });
      S.me.csrf = r.csrf;
      S.me.must_change = false;
      if (forced) await boot(); else box.innerHTML = `<div class="msg ok">密码已修改</div>`;
    } catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
}

/* ---------------- 启动 ---------------- */
async function boot() {
  try { S.me = await api("GET", "/api/me"); } catch (e) { return; }
  if (S.me.must_change) return renderChangePassword(true);
  S.settings = await api("GET", "/api/settings");
  if (!S.me.onboarding_done) return renderWizard(0);
  // 只改 hash 时交给 hashchange 触发路由；hash 已存在才直接路由（避免同时跑两个 route）
  if (!location.hash) location.hash = "#/war";
  else route();
}
window.addEventListener("hashchange", () => S.me && S.me.onboarding_done !== false && route());

/* ---------------- 首次引导 ---------------- */
async function renderWizard(step) {
  S.settings = await api("GET", "/api/settings");
  const steps = ["币安凭证", "邮件提醒", "风控预设", "完成"];
  const bar = steps.map((_, i) => `<i class="${i <= step ? "on" : ""}"></i>`).join("");
  let body = "";
  if (step === 0) body = `<h2>第 1 步：连接你的币安跟单账户</h2>
      <p class="muted">系统用币安网页登录凭证读取你的跟单仓位（只读，不能下单、不能提现）。${help("cred")}</p>${credForm()}`;
  if (step === 1) body = `<h2>第 2 步：设置邮件提醒</h2>
      <p class="muted">亏损到档位、凭证失效时发邮件到你的手机邮箱。推荐 163 邮箱。</p>${emailForm()}`;
  if (step === 2) body = `<h2>第 3 步：选择风控风格</h2>
      <p class="muted">不知道怎么选就用“均衡”。以后可以在设置里随时改，也可以按领航员单独调整。</p>${presetCards()}`;
  if (step === 3) body = `<h2>全部完成 🎉</h2>
      <p>接下来：</p><ol class="steps">
        <li>作战室首页的“今日待办”会告诉你现在需要处理什么；</li>
        <li>强烈建议在币安给每个跟单都设置“总止损”（作战室里会提示建议金额）；</li>
        <li>凭证大约 7–30 天失效一次，失效时会收到邮件，按邮件步骤更新即可。</li></ol>`;
  root.innerHTML = `<div class="center"><div class="card wizard">
    <div class="logo"><span class="mark">B</span>首次设置 · ${step + 1}/4 ${esc(steps[step])}</div>
    <div class="wsteps">${bar}</div>${body}
    <div class="actions" style="margin-top:16px">
      ${step > 0 ? `<button class="btn" data-w="${step - 1}">上一步</button>` : ""}
      ${step < 3 ? `<button class="btn ghost" data-w="${step + 1}">${step === 2 ? "使用当前设置，下一步" : "跳过，稍后再设"}</button>` : `<button class="btn primary" id="wDone">进入作战室</button>`}
    </div></div></div>`;
  root.querySelectorAll("[data-w]").forEach((b) => (b.onclick = () => renderWizard(Number(b.dataset.w))));
  bindCredForm(() => setTimeout(() => renderWizard(1), 900));
  bindEmailForm(() => setTimeout(() => renderWizard(2), 600));
  bindPresetCards(() => renderWizard(3));
  const done = document.getElementById("wDone");
  if (done) done.onclick = async () => { await api("POST", "/api/onboarding/done", {}); S.me.onboarding_done = true; if (location.hash === "#/war") route(); else location.hash = "#/war"; };
}

/* ---------------- 外壳 ---------------- */
const NAV = [["war", "📊", "作战室"], ["positions", "📈", "持仓"], ["stats", "📅", "统计"], ["leaders", "🧭", "领航员"], ["records", "🗂️", "记录"], ["settings", "⚙️", "设置"]];
function shell(active, title, chips, content) {
  return `<div class="app">
    <aside class="side">
      <div class="logo"><span class="mark">B</span>风控作战室</div>
      <nav class="nav">${NAV.map(([k, i, t]) => `<a href="#/${k}" class="${k === active ? "active" : ""}"><span class="ico">${i}</span>${t}</a>`).join("")}</nav>
      <div class="foot"><button class="btn sm ghost" id="logout">退出登录</button><div class="small muted">v${esc(S.me.version)}</div></div>
    </aside>
    <main class="main">
      <div class="topbar"><h1>${title}</h1><div class="chips">${chips || ""}</div></div>
      ${content}
      <div class="disclaimer">本系统只做提醒和统计，不会自动下单或止损，不构成投资建议。</div>
    </main></div>`;
}
function bindShell() {
  const b = document.getElementById("logout");
  if (b) b.onclick = async () => { await api("POST", "/api/logout", {}); S.me = null; renderLogin(); };
}

async function route() {
  const gen = ++S.gen;
  clearInterval(S.timer);
  S.timer = null;
  document.querySelectorAll(".tip").forEach((t) => t.remove());
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const page = parts[0] || "war";
  S.page = page;
  try {
    if (page === "positions") await pagePositions();
    else if (page === "stats") await pageStats(parts[1]);
    else if (page === "leaders") await (parts[1] ? pageLeader(parts[1]) : pageLeaders());
    else if (page === "episode") await pageEpisode(parts[1]);
    else if (page === "records") await pageRecords(parts[1] || "alerts");
    else if (page === "settings") await pageSettings(parts[1] || "credentials");
    else await pageWar();
  } catch (e) {
    if (S.me && !stale(gen)) root.innerHTML = shell(page, "出错了", "", `<div class="card msg err">${esc(e.message)}</div>`);
  }
  if (stale(gen)) return; // 期间已切到别的页面：不绑定、不创建定时器
  bindShell();
  if (page === "war" || page === "positions") {
    const timer = setInterval(() => {
      if (stale(gen)) { clearInterval(timer); return; }
      if (document.hidden || document.activeElement?.matches("input,textarea,select")) return;
      (page === "war" ? pageWar : pagePositions)().then(() => !stale(gen) && bindShell()).catch(() => {});
    }, 15000);
    S.timer = timer;
  }
}

function statusChips(ov) {
  const c = ov.credentials, p = ov.poll, e = ov.email;
  const cred = { valid: ["green", "凭证有效"], unknown: ["yellow", "凭证验证中"], expired: ["red", "凭证已失效"], missing: ["red", "未设置凭证"] }[c.state] || ["yellow", c.state];
  const data = p.stale ? ["red", `数据 ${p.age_text}未更新`] : ["green", `数据 ${ago(p.age)}前更新`];
  const mail = !e.configured ? ["yellow", "未设置邮箱"] : e.last_failure ? ["red", "邮件发送失败"] : ["green", "邮件正常"];
  return [[...cred, "#/settings/credentials"], [...data, "#/records/events"], [...mail, "#/settings/email"]]
    .map(([l, t, h]) => `<a class="chip" href="${h}"><span class="dot ${l}"></span>${esc(t)}</a>`).join("");
}

/* ---------------- 作战室 ---------------- */
async function pageWar() {
  const gen = S.gen;
  const [ov, eq] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/equity?days=30").catch(() => ({ points: [] }))]);
  const t = ov.totals;
  let banner = "";
  if (ov.credentials.state === "expired") banner = `<div class="banner red">🚨 币安凭证已失效，跟单仓位监控已暂停。<a class="btn sm primary" href="#/settings/credentials">立即更新</a></div>`;
  else if (ov.credentials.state === "missing") banner = `<div class="banner yellow">还没有设置币安凭证，暂时看不到你的跟单仓位。<a class="btn sm primary" href="#/settings/credentials">去设置</a></div>`;
  else if (ov.poll.stale) banner = `<div class="banner red">⚠️ 监控数据已经 ${esc(ov.poll.age_text)}没有更新，页面上的数字可能不是最新的。</div>`;

  const todos = ov.todos.length ? `<div class="todos">${ov.todos.map((x) => `
      <div class="todo ${x.level}">
        <div><div class="t">${x.level === "red" ? "🔴" : "🟡"} ${esc(x.title)}</div><div class="d">${esc(x.desc)}</div></div>
        <div class="acts">${x.link ? `<a class="btn sm ${x.level === "red" ? "primary" : ""}" href="${esc(x.link)}" ${x.link.startsWith("http") ? 'target="_blank" rel="noopener"' : ""}>${esc(x.action || "查看")}</a>` : ""}
          <button class="btn sm ghost" data-done="${esc(x.key)}" data-title="${esc(x.title)}">已处理</button></div>
      </div>`).join("")}</div>`
    : `<div class="empty"><div class="big">✅</div>暂时没有需要处理的事情</div>`;

  const kpi = (label, value, sub, h) => `<div class="card kpi"><div class="label">${label}${h ? help(h) : ""}</div><div class="value num">${value}</div>${sub ? `<div class="sub">${sub}</div>` : ""}</div>`;
  const worst = t.worst_position;
  const al = t.alerts_today || {};
  const kpis = `<div class="grid kpis">
    ${kpi("总权益", `${num(t.equity)}U`, t.exchange_equity ? `跟单 ${num(t.copy_equity, 0)}U · 交易所 ${num(t.exchange_equity, 0)}U`
      : `净投入 ${num(t.invested, 0)}U`, "equity")}
    ${kpi("今日盈亏", t.today_pnl == null ? "-" : U(t.today_pnl), "按权益变化计算（扣除转入转出）")}
    ${kpi("本月盈亏", t.month_pnl == null ? "-" : U(t.month_pnl), "扣除了追加 / 赎回")}
    ${kpi("当前浮动盈亏", U(t.unrealized), `${t.positions} 个持仓`, "unrealized")}
    ${kpi("风险最大的仓位", worst ? U(worst.unrealized_pnl) : "-", worst ? `${esc(worst.leader_name)} · ${esc(worst.symbol)} ${side(worst.side)}` : "暂无")}
    ${kpi("今日提醒", `${(al.urgent || 0) + (al.warn || 0) + (al.info || 0)}`, `紧急 ${al.urgent || 0} · 提醒 ${al.warn || 0}`)}
  </div>`;

  const leaders = ov.accounts.length ? `<div class="grid leaders">${ov.accounts.map((a) => {
    const dd = Math.min(1, a.drawdown / (S.settings.rules.account_dd_severe || 0.2));
    return `<div class="card leader ${a.light}">
      <div class="name"><span class="dot ${a.light}"></span>${esc(a.name)}${a.kind === "exchange" ? ' <span class="badge">交易所账户</span>' : ""}${a.is_paused ? '<span class="badge">已暂停</span>' : ""}</div>
      <dl class="kv">
        <dt>权益${help("equity")}</dt><dd class="num">${num(a.equity)}U</dd>
        <dt>累计盈亏</dt><dd>${U(a.pnl)}</dd>
        <dt>回撤${help("drawdown")}</dt><dd class="num">${num(a.drawdown * 100, 1)}%</dd>
        ${a.kind === "exchange"
          ? `<dt>保证金率${help("margin_ratio")}</dt><dd class="num">${a.margin_ratio != null ? num(a.margin_ratio * 100, 1) + "%" : "-"}</dd>
             <dt>持仓</dt><dd>${a.positions} 个${a.unprotected ? ` · <span class="down">${a.unprotected} 个没挂好止损</span>` : ""}</dd>`
          : `<dt>总止损${help("native_stop")}</dt><dd>${a.native_stop_u ? `<span class="badge green">已设 ${num(a.native_stop_u, 0)}U</span>` : `<span class="badge yellow">未设置 · 建议 ${num(a.recommended_stop_u, 0)}U</span>`}</dd>
             <dt>持仓</dt><dd>${a.positions} 个</dd>`}
      </dl>
      ${a.error ? `<div class="small down">${esc(a.error)}</div>` : ""}
      <div class="bar"><i class="${a.light}" style="width:${Math.round(dd * 100)}%"></i></div>
      <div class="actions"><a class="btn sm" href="#/positions">看持仓</a><a class="btn sm ghost" href="${esc(a.link)}" target="_blank" rel="noopener">${a.kind === "exchange" ? "交易所 ↗" : "币安页面 ↗"}</a></div>
    </div>`;
  }).join("")}</div>`
    : `<div class="card empty">${ov.credentials.state === "valid" ? "暂时没有进行中的跟单" : "设置币安凭证后，这里会显示你的每个跟单"}；
       也可以在 <a href="#/settings/exchanges">设置 → 交易所账户</a> 接入你自己的币安 / OKX 合约账户</div>`;

  if (stale(gen)) return;
  root.innerHTML = shell("war", "作战室", statusChips(ov), `
    ${banner}
    <div class="section card"><div class="card-h"><h2>今日待办</h2><span class="muted small">按紧急程度排序</span></div>${todos}</div>
    <div class="section">${kpis}</div>
    <div class="section"><div class="card-h" style="margin-top:6px"><h2>我的跟单</h2><span class="muted small">🔴 现在就要处理 · 🟡 需要留意 · 🟢 正常</span></div>${leaders}</div>
    <div class="section card"><div class="card-h"><h2>跟单累计盈亏曲线（30 天）</h2></div>${chart(eq.points)}</div>`);
  root.querySelectorAll("[data-done]").forEach((b) => (b.onclick = async () => {
    await api("POST", "/api/todos/done", { key: b.dataset.done, title: b.dataset.title });
    toast("已标记为已处理；情况变化时会重新提醒");
    pageWar().then(bindShell);
  }));
}

function chart(points) {
  if (!points || points.length < 2) return `<div class="empty">运行一段时间后会显示曲线（每 5 分钟记录一个点）</div>`;
  const W = 800, H = 180, P = 28;
  const xs = points.map((p) => p.ts), ys = points.map((p) => p.pnl);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys, 0), y1 = Math.max(...ys, 0);
  if (y1 - y0 < 1e-9) { y0 -= 1; y1 += 1; }
  const X = (x) => P + ((x - x0) / (x1 - x0 || 1)) * (W - P * 2);
  const Y = (y) => H - P - ((y - y0) / (y1 - y0)) * (H - P * 2);
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.pnl).toFixed(1)}`).join("");
  const last = ys[ys.length - 1];
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="累计盈亏曲线">
    <line class="zero" x1="${P}" x2="${W - P}" y1="${Y(0)}" y2="${Y(0)}"/>
    <path class="ln" d="${d}"/>
    <text x="${P}" y="14">${dt(x0)}</text><text x="${W - P}" y="14" text-anchor="end">${dt(x1)} · ${signed(last)}U</text>
    <text x="${P}" y="${H - 6}">最低 ${signed(Math.min(...ys))}U</text><text x="${W - P}" y="${H - 6}" text-anchor="end">最高 ${signed(Math.max(...ys))}U</text>
  </svg>`;
}

/* ---------------- 持仓 ---------------- */
async function pagePositions() {
  const gen = S.gen;
  const [ov, pv] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/positions")]);
  const sorted = pv.positions.slice().sort((a, b) => (b.tier || 0) - (a.tier || 0) || (a.unrealized_pnl - b.unrealized_pnl));
  const mineList = sorted.filter((p) => p.source === "copy"), exList = sorted.filter((p) => p.source === "exchange");
  const publicList = sorted.filter((p) => p.source === "public");
  // 止损单（交易所账户）：没挂 / 触发价 + 覆盖比例 + 触发时大约盈亏
  const stopCell = (p) => {
    if (p.stop_price == null || p.side === "HEDGE") return '<span class="muted">-</span>';
    if (!p.stop_price) return '<span class="badge red">没挂</span>';
    // 触发时的盈亏只算止损单覆盖的那部分数量（没覆盖的部分不会被平掉）
    const dir = p.side === "LONG" ? 1 : -1, part = p.stop_cover < 0.99, at = (p.stop_price - p.entry_price) * p.qty * Math.min(p.stop_cover || 0, 1) * dir;
    return `<span class="num">${num(p.stop_price, 4)}</span>${part ? ` <span class="badge yellow">只覆盖 ${num(p.stop_cover * 100, 0)}%</span>` : ""}
      <div class="small muted">触发${part ? "时这部分" : ""}约 ${signed(at)}U</div>`;
  };
  const tierBadge = (t) => (t ? `<span class="badge ${t >= 2 ? "red" : "yellow"}">第 ${t} 档</span>` : `<span class="badge">正常</span>`);
  const rows = (ps, withStop) => ps.map((p) => {
    const ep = p.side !== "HEDGE" && p.episodes && p.episodes[0];
    const mine = p.source !== "public";
    const held = p.first_seen ? ago(Date.now() / 1000 - p.first_seen) : "-";
    const main = `<tr>
      <td data-l="领航员">${esc(p.leader_name)}</td>
      <td data-l="币种"><b>${esc(p.symbol)}</b> ${side(p.side)}${p.side === "HEDGE" ? help("hedge") : ""} <span class="muted">${num(p.leverage, 0)}x</span></td>
      <td data-l="保证金" class="r num">${num(p.margin)}U</td>
      <td data-l="浮动盈亏" class="r">${mine ? U(p.unrealized_pnl) : '<span class="muted">领航员仓位</span>'}</td>
      <td data-l="ROI" class="r">${pct(p.roi)}</td>
      <td data-l="亏损档位">${tierBadge(p.tier)}</td>
      <td data-l="最深浮亏" class="r">${ep ? `${pct(ep.mae_roi)} <span class="muted small">${ep.mae_u != null ? signed(ep.mae_u) + "U" : ""}</span>` : pct(p.trough_roi)}</td>
      <td data-l="最高浮盈" class="r">${ep ? `${pct(ep.mfe_roi)} <span class="muted small">${ep.mfe_u != null ? signed(ep.mfe_u) + "U" : ""}</span>` : pct(p.peak_roi)}</td>
      <td data-l="持仓时长" class="r">${held}</td>
      ${withStop ? `<td data-l="止损单">${stopCell(p)}</td>` : ""}
      <td data-l="">${(p.episodes || []).length ? `<a class="btn sm" href="#/episode/${p.episodes[0].id}">时间线</a> ` : ""}<a class="btn sm ghost" href="${esc(p.link)}" target="_blank" rel="noopener">${p.source === "exchange" ? "交易所" : "币安"} ↗</a></td></tr>`;
    const legs = (p.legs || []).map((l) => `<tr class="sub"><td data-l="">└ ${side(l.side)}单</td><td data-l="开仓价">${num(l.entry_price, 4)}</td>
      <td data-l="保证金" class="r">${num(l.margin)}U</td><td data-l="浮动盈亏" class="r">${U(l.unrealized_pnl)}</td><td data-l="ROI" class="r">${pct(l.roi)}</td>
      <td colspan="${withStop ? 6 : 5}" class="hide-m"></td></tr>`).join("");
    return main + legs;
  }).join("");
  const table = (ps, emptyText, withStop) => ps.length ? `<div class="tbl-wrap"><table class="cards"><thead><tr>
      <th>${withStop ? "账户" : "领航员"}</th><th>币种 / 方向</th><th class="r">保证金</th><th class="r">浮动盈亏</th><th class="r">ROI${help("roi")}</th>
      <th>亏损档位${help("tier")}</th><th class="r">最深浮亏${help("mae")}</th><th class="r">最高浮盈${help("mfe")}</th><th class="r">持仓时长</th>
      ${withStop ? `<th>止损单${help("no_stop")}</th>` : ""}<th></th></tr></thead>
      <tbody>${rows(ps, withStop)}</tbody></table></div>`
    : `<div class="empty">${emptyText}</div>`;
  if (stale(gen)) return;
  root.innerHTML = shell("positions", "持仓", statusChips(ov), `
    <div class="card"><div class="card-h"><h2>我的跟单仓位（${mineList.length}）</h2><span class="muted small">每 15 秒自动刷新 · 数据时间 ${dt(pv.updated)}</span></div>
    ${table(mineList, ov.credentials.state === "valid" ? "当前没有持仓" : "设置币安凭证后显示你的跟单仓位")}
    <p class="hint">同一个币同时有多单和空单时合并为一行“多空对冲”，下面列出两条腿。亏损档位按合并后的净亏损计算。</p></div>
    ${exList.length ? `<div class="card"><div class="card-h"><h2>交易所账户持仓（${exList.length}）</h2><span class="muted small">你自己在币安 / OKX 交易的仓位</span></div>${table(exList, "", true)}</div>` : ""}
    ${publicList.length ? `<div class="card"><div class="card-h"><h2>观察中的领航员公开持仓（${publicList.length}）</h2><span class="muted small">领航员本人的仓位，不是你的钱</span></div>${table(publicList)}</div>` : ""}`);
}

/* ---------------- 记录（提醒 / 日志 / 仓位回合） ---------------- */
const EV_LABEL = { position: "仓位", alert: "提醒", credential: "凭证", notify: "邮件", settings: "设置", data: "数据", leader: "跟单", account: "交易所账户", service: "服务", auth: "登录", todo: "待办", web: "后台" };
const evLabel = (t) => EV_LABEL[String(t).split(".")[0]] || t;
const EV_TYPES = [["", "全部类型"], ["position.", "仓位变化"], ["alert.", "提醒"], ["credential.", "凭证"], ["notify.", "邮件投递"],
  ["settings.", "设置修改"], ["data.", "数据中断/恢复"], ["leader.", "跟单增减"], ["account.", "交易所账户"], ["service.", "服务"], ["auth.", "登录"]];
async function pageRecords(tab) {
  const gen = S.gen;
  const ov = await api("GET", "/api/overview");
  const tabs = [["alerts", "提醒记录"], ["exits", "离场判定"], ["events", "事件日志"], ["episodes", "仓位回合"]]
    .map(([k, t]) => `<a href="#/records/${k}" class="${k === tab ? "on" : ""}">${t}</a>`).join("");
  let body = "";
  if (tab === "alerts") {
    const { alerts } = await api("GET", "/api/alerts?limit=100");
    body = alerts.length ? `<div class="log">${alerts.map((a) => `<div class="ev">
        <div class="tm">${dt(a.ts)}<br><span class="badge ${a.severity === "urgent" ? "red" : a.severity === "warn" ? "yellow" : ""}">${{ urgent: "紧急", warn: "提醒", info: "信息" }[a.severity] || a.severity}</span></div>
        <div><b>${esc(a.title)}</b>
          ${deliveryLine(a)}
          <details><summary class="small muted">查看完整内容</summary><pre>${esc(a.message)}</pre></details></div></div>`).join("")}</div>`
      : `<div class="empty">还没有提醒</div>`;
  } else if (tab === "exits") {
    const { exits } = await api("GET", "/api/exits?limit=300");
    body = exitsTable(exits, true) + `<p class="hint">离场类型由系统对比你的成交记录和领航员的仓位自动判断${help("early")}。判断错了可以在“类型”里改；结论说明${help("verdict")}。</p>`;
  } else if (tab === "events") {
    body = `<div class="filters">
        <select id="evType">${EV_TYPES.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select id="evLevel"><option value="">全部级别</option><option value="urgent">紧急</option><option value="warn">提醒</option><option value="info">信息</option></select>
        <input id="evQ" placeholder="搜索（币种、领航员…）" style="max-width:220px">
        <button class="btn" id="evGo">筛选</button>
        <a class="btn ghost" id="evCsv" download>导出 CSV</a><a class="btn ghost" id="evJson" download>导出 JSON</a></div>
      <div class="log" id="evList"></div><div class="actions"><button class="btn" id="evMore">加载更多</button></div>`;
  } else {
    const { episodes } = await api("GET", "/api/episodes?limit=200");
    body = episodes.length ? `<div class="tbl-wrap"><table class="cards"><thead><tr><th>开仓</th><th>领航员</th><th>币种</th><th class="r">最深浮亏${help("mae")}</th>
        <th class="r">最高浮盈${help("mfe")}</th><th class="r">最后</th><th>加/减仓</th><th>最高档</th><th>状态</th><th>标注</th><th></th></tr></thead><tbody>
      ${episodes.map((e) => `<tr><td data-l="开仓">${dt(e.opened_ts)}</td><td data-l="领航员">${esc(e.leader_name)}</td>
        <td data-l="币种"><b>${esc(e.symbol)}</b> ${side(e.side)}</td>
        <td data-l="最深浮亏" class="r">${pct(e.mae_roi)}${e.mae_u != null ? ` <span class="small muted">${signed(e.mae_u)}U</span>` : ""}</td>
        <td data-l="最高浮盈" class="r">${pct(e.mfe_roi)}${e.mfe_u != null ? ` <span class="small muted">${signed(e.mfe_u)}U</span>` : ""}</td>
        <td data-l="最后" class="r">${pct(e.last_roi)}</td><td data-l="加/减仓">${e.adds}/${e.reduces}</td>
        <td data-l="最高档">${e.max_tier ? `第 ${e.max_tier} 档` : "-"}</td>
        <td data-l="状态">${e.status === "open" ? '<span class="badge green">持仓中</span>' : `${esc(e.exit_type || "已平仓")} ${dt(e.closed_ts)}`}</td>
        <td data-l="标注">${(e.labels || []).map((l) => `<span class="badge ${/回吐|盈转亏|超过/.test(l) ? "yellow" : ""}">${esc(l)}</span>`).join(" ")}</td>
        <td data-l=""><a class="btn sm" href="#/episode/${e.id}">时间线</a></td></tr>`).join("")}</tbody></table></div>
      <p class="hint">“利润回吐”：最高浮盈 ≥30% 但最后不到一半；“盈转亏”：最高浮盈 ≥30% 却亏损出场。这些数据会用于之后的分批止盈建议。</p>`
      : `<div class="empty">仓位回合从安装后开始记录，有持仓后这里会出现</div>`;
  }
  if (stale(gen)) return;
  root.innerHTML = shell("records", "记录", statusChips(ov), `<div class="card"><div class="tabs">${tabs}</div>${body}</div>`);
  if (tab === "events") bindEvents();
  if (tab === "exits") bindExitsTable(() => pageRecords("exits").then(bindShell));
}
function bindEvents() {
  document.getElementById("evGo").onclick = () => loadEvents(true);
  document.getElementById("evQ").onkeydown = (e) => e.key === "Enter" && loadEvents(true);
  document.getElementById("evMore").onclick = () => loadEvents(false);
  const pending = S.evFilter; // 从“仓位回合 → 时间线”跳过来时带的筛选，只用一次
  S.evFilter = null;
  loadEvents(true, pending || undefined);
}
async function loadEvents(reset, extra) {
  const list = document.getElementById("evList");
  if (!list) return;
  if (reset) S.events = [];
  if (reset) S.evExtra = extra || null;
  const q = new URLSearchParams({ limit: 100, type: val("evType"), level: val("evLevel"), q: val("evQ"), ...(S.evExtra || {}) });
  const ex = new URLSearchParams({ type: val("evType"), level: val("evLevel"), q: val("evQ"), ...(S.evExtra || {}) });
  const csv = document.getElementById("evCsv"), js = document.getElementById("evJson");
  if (csv) csv.href = "/api/events/export?format=csv&" + ex;
  if (js) js.href = "/api/events/export?format=json&" + ex;
  if (!reset && S.events.length) q.set("before", S.events[S.events.length - 1].id);
  const { events } = await api("GET", "/api/events?" + q);
  S.events = S.events.concat(events);
  list.innerHTML = S.events.length ? S.events.map((e) => `<div class="ev"><div class="tm">${dt(e.ts)}<br><span class="badge ${e.level === "urgent" ? "red" : e.level === "warn" ? "yellow" : ""}" title="${esc(e.type)}">${esc(evLabel(e.type))}</span></div>
      <div>${esc(e.title)}${e.detail && Object.keys(e.detail).length ? `<details><summary class="small muted">详细数据</summary><pre>${esc(JSON.stringify(e.detail, null, 2))}</pre></details>` : ""}</div></div>`).join("")
    : `<div class="empty">没有符合条件的记录</div>`;
  document.getElementById("evMore").disabled = events.length < 100;
}
const val = (id) => (document.getElementById(id) || {}).value || "";

/* ---------------- 设置 ---------------- */
async function pageSettings(tab) {
  const gen = S.gen;
  const [ov, st, ex] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/settings"),
    tab === "exchanges" ? api("GET", "/api/accounts") : null]);
  S.settings = st;
  const tabs = [["credentials", "币安凭证"], ["exchanges", "交易所账户"], ["email", "邮件提醒"], ["rules", "风控参数"], ["account", "账号与系统"]]
    .map(([k, t]) => `<a href="#/settings/${k}" class="${k === tab ? "on" : ""}">${t}</a>`).join("");
  let body = "";
  if (tab === "credentials") body = credStatus(st) + credForm();
  if (tab === "email") body = emailForm();
  if (tab === "exchanges") body = exchangesTab(ex);
  if (tab === "rules") body = rulesForm(st);
  if (tab === "account") body = renderChangePassword(false) + maintForm(st) + `<div class="card"><h3>系统</h3>
      <p class="muted small">版本 v${esc(S.me.version)}。更新：在服务器执行 <code>cd /opt/bian && ./start.sh update</code>（几秒钟，数据和设置都会保留）。</p>
      <p class="muted small">查看运行日志：<code>./start.sh logs</code>；自检：<code>./start.sh doctor</code>。</p></div>`;
  if (stale(gen)) return;
  root.innerHTML = shell("settings", "设置", statusChips(ov), `<div class="card"><div class="tabs">${tabs}</div>${body}</div>`);
  if (tab === "credentials") bindCredForm(() => pageSettings("credentials").then(bindShell));
  if (tab === "email") bindEmailForm();
  if (tab === "exchanges") bindExchanges(ex);
  if (tab === "rules") bindRulesForm();
  if (tab === "account") { bindPassword(false); bindMaintForm(); }
}

/* ---------- 交易所账户（API Key 只读）：和跟单共用风控参数、提醒和邮件 ---------- */
function exchangesTab(d) {
  const list = d.accounts || [], rt = d.runtime || {};
  const card = (a) => {
    const r = rt[a.id] || {};
    const st = a.removed ? '<span class="badge">已移除（数据保留）</span>' : !a.enabled ? '<span class="badge">已停用</span>'
      : (a.last_error || r.error) ? '<span class="badge red">读取失败</span>' : '<span class="badge green">监控中</span>';
    return `<div class="card ex-card"><div class="card-h"><div><b>${esc(a.exchange_name)} · ${esc(a.name)}</b> ${st}</div>
        <span class="muted small num">Key ${esc(a.key_hint || "-")}</span></div>
      ${(a.last_error || r.error) ? `<div class="msg err">${esc(r.error || a.last_error)}</div>` : ""}
      ${a.removed ? "" : `<dl class="kv ex-kv"><dt>账户权益</dt><dd class="num">${r.equity != null ? num(r.equity) + "U" : "-"}</dd>
        <dt>可用</dt><dd class="num">${r.available != null ? num(r.available) + "U" : "-"}</dd>
        <dt>维持保证金率 ${help("margin_ratio")}</dt><dd class="num">${r.margin_ratio != null ? num(r.margin_ratio * 100, 1) + "%" : "-"}</dd>
        <dt>持仓 / 没挂好止损</dt><dd>${r.positions ?? "-"} / ${r.unprotected ? `<span class="down">${r.unprotected}</span>` : (r.positions != null ? 0 : "-")}</dd>
        <dt>最近成功</dt><dd>${dt(a.last_ok)}</dd></dl>`}
      <div class="actions" data-acc="${esc(a.id)}">
        ${a.removed ? `<a class="btn sm" href="/api/accounts/export?id=${esc(a.id)}">导出数据</a><button class="btn sm ghost" data-act="delete">删除数据</button>`
        : `<button class="btn sm" data-act="test">测试连接</button><button class="btn sm" data-act="rename">改名</button>
          <button class="btn sm" data-act="keys">更新密钥</button>
          <button class="btn sm" data-act="toggle">${a.enabled ? "停用" : "启用"}</button>
          <label class="chk sm-chk"><input type="checkbox" data-act="stops" ${a.stop_check ? "checked" : ""}> 检查止损单 ${help("no_stop")}</label>
          <a class="btn sm" href="/api/accounts/export?id=${esc(a.id)}">导出数据</a>
          <button class="btn sm ghost" data-act="remove">移除</button>`}</div>
      <div class="ex-msg"></div></div>`;
  };
  return `<div class="card-h"><div><h3 style="margin:0">交易所账户 ${help("exchange")}</h3>
      <p class="muted small" style="margin:4px 0 0">接入你自己在币安 / OKX 交易的 U 本位永续持仓：亏损档位、强平、保本、回撤止盈、邮件、作战室、统计全部和跟单共用。</p></div>
      <button class="btn primary" id="exAddBtn">添加账户</button></div>
    <form id="exAdd" class="card" hidden>
      <h3>添加交易所账户</h3>
      <ol class="steps small">
        <li>在交易所 <b>API 管理</b> 里新建一个 API Key，<b>只勾选“读取”</b>（不要勾交易、提现、划转——本系统永远不下单，有这些权限的 key 会被拒绝）；</li>
        <li><b>绑定服务器 IP</b>（在服务器执行 <code>curl -4 ifconfig.me</code> 可以看到）：key 即使泄露，别处也用不了；</li>
        <li>OKX 还要填创建 key 时自己设置的 <b>Passphrase</b>。</li>
      </ol>
      <div class="row">
        <label class="f"><span>交易所</span><select name="exchange"><option value="binance">币安 Binance（USDT-M 永续）</option><option value="okx">OKX（USDT 永续）</option></select></label>
        <label class="f"><span>名称</span><input name="name" placeholder="例如 主账户 / 小号" maxlength="30"></label>
      </div>
      <div class="row">
        <label class="f"><span>API Key</span><input name="api_key" autocomplete="off" spellcheck="false"></label>
        <label class="f"><span>Secret Key</span><input name="secret" type="password" autocomplete="new-password"></label>
        <label class="f ex-pass" hidden><span>Passphrase</span><input name="passphrase" type="password" autocomplete="new-password"></label>
      </div>
      <div class="actions"><button class="btn" type="button" id="exTest">测试连接</button><button class="btn primary">验证并添加</button></div>
      <div id="exAddMsg"></div>
      <p class="hint">密钥只保存在服务器的 data/secrets.json（权限 600），网页和日志里只显示打码后的内容。</p>
    </form>
    ${list.length ? list.map(card).join("") : `<div class="empty">还没有交易所账户。点“添加账户”，用一个只读 API Key 接入</div>`}`;
}
function testResult(r) {
  return `<div class="msg ok">✓ 连接正常：只读 key${r.permissions.ip_restricted ? "，已绑定 IP" : "，<b>没有绑定 IP（建议绑定服务器 IP）</b>"}；
    权益 ${num(r.equity)}U，可用 ${num(r.available)}U${r.margin_ratio != null ? `，维持保证金率 ${num(r.margin_ratio * 100, 1)}%` : ""}；
    持仓 ${r.positions} 个，止损单 ${r.stops} 张${r.unprotected ? `，<b>${r.unprotected} 个仓位没挂好止损</b>` : ""}。</div>
    ${r.overlap && r.overlap.length ? `<div class="msg">⚠️ 这些仓位和你的跟单仓位同币种同方向：${esc(r.overlap.join("、"))}。如果它们其实就是跟单仓位（OKX 跟单可能这样），会被重复统计，请核对。</div>` : ""}`;
}
function bindExchanges(d) {
  const f = document.getElementById("exAdd");
  document.getElementById("exAddBtn").onclick = () => { f.hidden = !f.hidden; };
  const syncPass = () => { f.querySelector(".ex-pass").hidden = f.elements.exchange.value !== "okx"; };
  f.elements.exchange.onchange = syncPass; syncPass();
  const body = () => ({ exchange: f.elements.exchange.value, name: f.elements.name.value.trim(), api_key: f.elements.api_key.value.trim(),
    secret: f.elements.secret.value.trim(), passphrase: f.elements.passphrase.value.trim() });
  const box = document.getElementById("exAddMsg");
  document.getElementById("exTest").onclick = async () => {
    box.innerHTML = '<div class="msg">测试中…</div>';
    try { box.innerHTML = testResult(await api("POST", "/api/accounts/test", body())); }
    catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
  f.onsubmit = async (e) => {
    e.preventDefault();
    box.innerHTML = '<div class="msg">验证中…</div>';
    try { const r = await api("POST", "/api/accounts", body()); toast(r.message); pageSettings("exchanges").then(bindShell); }
    catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
  root.querySelectorAll("[data-acc]").forEach((bar) => {
    const id = bar.dataset.acc, a = d.accounts.find((x) => x.id === id), msg = bar.parentElement.querySelector(".ex-msg");
    const reload = () => pageSettings("exchanges").then(bindShell);
    const call = async (path, payload, ok) => {
      try { const r = await api("POST", path, payload); if (ok) toast(ok(r)); reload(); return r; }
      catch (err) { msg.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
    };
    bar.querySelectorAll("[data-act]").forEach((el) => {
      const act = el.dataset.act;
      if (act === "stops") { el.onchange = () => call("/api/accounts/update", { id, stop_check: el.checked }, () => el.checked ? "已开启止损单检查" : "已关闭止损单检查"); return; }
      el.onclick = async () => {
        if (act === "test") {
          msg.innerHTML = '<div class="msg">测试中…</div>';
          try { msg.innerHTML = testResult(await api("POST", "/api/accounts/test", { id })); }
          catch (err) { msg.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
        } else if (act === "rename") {
          const name = prompt("新名称", a.name); if (name && name.trim()) call("/api/accounts/update", { id, name: name.trim() }, () => "已改名");
        } else if (act === "keys") {
          const api_key = prompt("新的 API Key（只读）"); if (!api_key) return;
          const secret = prompt("新的 Secret Key"); if (!secret) return;
          const passphrase = a.exchange === "okx" ? prompt("Passphrase") : "";
          msg.innerHTML = '<div class="msg">验证中…</div>';
          call("/api/accounts/update", { id, api_key, secret, passphrase }, () => "密钥已更新");
        } else if (act === "toggle") {
          call("/api/accounts/update", { id, enabled: !a.enabled }, () => a.enabled ? "已停用（不再读取，数据保留）" : "已启用");
        } else if (act === "remove") {
          if (confirm(`移除“${a.exchange_name} · ${a.name}”？\n密钥会删除、停止监控；历史数据保留（统计照常包含），之后也可以导出或删除。`))
            call("/api/accounts/remove", { id, delete_data: false }, () => "已移除，历史数据保留");
        } else if (act === "delete") {
          const typed = prompt(`删除“${a.name}”的全部数据（持仓回合、提醒、资金流水、权益记录）。\n删除前会自动备份到服务器 data/backups。\n\n请输入账户名确认：${a.name}`);
          if (typed != null) call("/api/accounts/remove", { id, delete_data: true, confirm: typed.trim() }, (r) => `已删除，备份：${r.backup}`);
        }
      };
    });
  });
}

function credStatus(st) {
  const c = st.credentials, s = c.status || {};
  const state = !c.has_credentials ? ["red", "未设置"] : { valid: ["green", "有效"], expired: ["red", "已失效"], unknown: ["yellow", "验证中"], missing: ["red", "未设置"] }[s.state] || ["yellow", s.state || "未知"];
  const age = c.updated_at ? (Date.now() / 1000 - c.updated_at) / 86400 : null;
  return `<div class="card" style="margin-bottom:14px"><h3>当前状态 ${help("cred")}</h3><dl class="kv" style="max-width:420px">
    <dt>状态</dt><dd><span class="badge ${state[0]}">${state[1]}</span></dd>
    <dt>凭证</dt><dd class="num">${esc(c.p20t_masked || "-")}</dd>
    <dt>更新时间</dt><dd>${dt(c.updated_at)}${age != null ? `（已使用 ${age.toFixed(1)} 天）` : ""}</dd>
    <dt>最近验证</dt><dd>${dt(s.checked_at)}</dd>
    <dt>进行中的跟单</dt><dd>${s.relations ?? "-"}</dd>
    ${s.expected_life_days ? `<dt>以往平均有效期</dt><dd>${num(s.expected_life_days, 1)} 天</dd>` : ""}
    ${s.last_error ? `<dt>最近错误</dt><dd class="down small">${esc(s.last_error)}</dd>` : ""}
  </dl></div>`;
}
function credForm() {
  return `<form id="credForm">
    <ol class="steps">
      <li>用<b>电脑浏览器</b>登录 <a href="https://www.binance.com" target="_blank" rel="noopener">binance.com</a>；</li>
      <li>按 <b>F12</b> 打开开发者工具 → 切到 <b>Network（网络）</b>，刷新页面；</li>
      <li>在过滤框输入 <code>bapi</code>，右键任意一个请求 → <b>Copy → Copy as cURL</b>；</li>
      <li>把复制的整段内容粘贴到下面，点“验证并保存”。系统只提取 p20t 和 csrftoken，其余内容会丢弃。</li>
    </ol>
    <label class="f"><span>粘贴 cURL</span><textarea name="curl" placeholder="curl 'https://www.binance.com/bapi/...' -H ... -b '...p20t=...' -H 'csrftoken: ...'"></textarea></label>
    <details class="adv"><summary>或者分别填写 p20t 和 csrftoken</summary><div class="row">
      <label class="f"><span>p20t</span><input name="p20t" autocomplete="off" placeholder="web.开头"></label>
      <label class="f"><span>csrftoken</span><input name="csrftoken" autocomplete="off"></label></div></details>
    <div class="actions"><button class="btn primary" id="credBtn">验证并保存</button></div>
    <div id="credMsg"></div>
    <p class="hint">凭证只保存在你的服务器上（权限 600），页面不会再显示完整内容。保存前会先向币安验证，无效的不会保存。</p>
  </form>`;
}
function bindCredForm(after) {
  const f = document.getElementById("credForm");
  if (!f) return;
  f.onsubmit = async (e) => {
    e.preventDefault();
    const d = new FormData(f), box = document.getElementById("credMsg"), btn = document.getElementById("credBtn");
    btn.disabled = true; btn.textContent = "正在向币安验证…";
    try {
      const r = await api("POST", "/api/credentials", { curl: d.get("curl"), p20t: d.get("p20t"), csrftoken: d.get("csrftoken") });
      box.innerHTML = `<div class="msg ok">✓ ${esc(r.message)}</div>`;
      f.reset();
      toast("✓ " + r.message);
      after && after();
    } catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
    btn.disabled = false; btn.textContent = "验证并保存";
  };
}

function emailForm() {
  const n = S.settings.notify;
  return `<form id="emailForm">
    <label class="switch"><input type="checkbox" name="email_enabled" ${n.email_enabled ? "checked" : ""}> 启用邮件提醒</label>
    <div class="row" style="margin-top:12px">
      <label class="f"><span>SMTP 服务器</span><input name="smtp_host" value="${esc(n.smtp_host)}" placeholder="smtp.163.com"></label>
      <label class="f"><span>端口</span><input name="smtp_port" type="number" value="${esc(n.smtp_port)}"><div class="hint">163 / QQ：465</div></label>
      <label class="f"><span>加密方式</span><select name="smtp_security">
        <option value="" ${!n.smtp_security ? "selected" : ""}>自动（465=TLS，其他=STARTTLS）</option>
        <option value="tls" ${n.smtp_security === "tls" ? "selected" : ""}>TLS</option>
        <option value="starttls" ${n.smtp_security === "starttls" ? "selected" : ""}>STARTTLS</option></select></label>
    </div>
    <div class="row">
      <label class="f"><span>发件邮箱（登录账号）</span><input name="smtp_user" value="${esc(n.smtp_user)}" placeholder="yourname@163.com" autocomplete="off"></label>
      <label class="f"><span>授权码</span><input name="smtp_pass" type="password" autocomplete="new-password" placeholder="${S.settings.smtp_pass_set ? "已设置（不修改请留空）" : "邮箱设置里开启 SMTP 后获得"}">
        <div class="hint">不是邮箱登录密码。163：设置 → POP3/SMTP/IMAP → 开启 SMTP 服务</div></label>
      <label class="f"><span>收件邮箱</span><input name="smtp_to" value="${esc(n.smtp_to)}" placeholder="不填则发给发件邮箱"><div class="hint">多个用逗号分隔</div></label>
    </div>
    <fieldset><legend>哪些事件发邮件 ${help("email_events")}</legend>
      <div class="chips-row">快速选择：<button type="button" class="btn sm" data-evp="key">只发关键的（推荐）</button>
        <button type="button" class="btn sm" data-evp="urgent">只发紧急的</button><button type="button" class="btn sm" data-evp="all">全部</button></div>
      <div class="ev-grid">${(S.settings.email_event_groups || []).map((g) => `<label class="chk"><input type="checkbox" name="ev_${g.key}" ${(n.email_events || []).includes(g.key) ? "checked" : ""}> ${esc(g.label)}</label>`).join("")}
        <label class="chk muted"><input type="checkbox" checked disabled> 币安凭证失效（始终发送）</label></div>
      <div class="hint">没勾选的只在后台“记录 → 提醒记录”里保存，并在每日摘要里汇总条数。同一事件当天只发一次，明显恶化（例如亏损跌到下一档、回撤再加深 5 个点）才会再发。</div>
    </fieldset>
    ${leaderActionsBox(n)}
    <div class="row">
      <label class="f"><span>非紧急提醒汇总间隔（分钟）</span><input name="email_batch_minutes" type="number" min="0" max="240" value="${esc(n.email_batch_minutes)}"><div class="hint">攒够这么久合并成一封；紧急的立即发并顺带捎上。0 = 每次立即发</div></label>
      <label class="f"><span>每小时最多几封</span><input name="email_max_per_hour" type="number" min="1" max="120" value="${esc(n.email_max_per_hour)}"><div class="hint">兜底上限，超出的合并到下一封</div></label>
    </div>
    <div class="actions"><button class="btn primary" type="submit">保存</button><button class="btn" type="button" id="emailTest">发送测试邮件</button></div>
    <div id="emailMsg"></div>
    <p class="hint">阿里云等云服务器默认封禁 25 端口，请用 465 或 587。测试邮件使用表单里的内容，不会保存。</p>
  </form>`;
}
/* 按领航员单独设置交易动作（开仓 / 加仓 / 减仓 / 平仓）邮件：优先于上面的全局勾选 */
function leaderActionsBox(n) {
  const la = n.leader_actions || {};
  const names = Object.fromEntries((S.settings.leaders || []).map((l) => [l.id, l.name]));
  const ids = [...new Set([...(S.settings.leaders || []).map((l) => l.id), ...Object.keys(la)])];
  if (!ids.length) return "";
  const val = (id) => (la[id] === true ? "on" : la[id] === false ? "off" : "");
  return `<fieldset><legend>按领航员单独设置交易动作邮件</legend>
    <div class="hint">开仓 / 加仓 / 减仓 / 平仓这四种提醒：单独设置的领航员以这里为准，其余跟随上面的全局勾选。亏损档位、强平等风险提醒不受影响。</div>
    <div class="ev-grid">${ids.map((id) => `<label class="f"><span>${esc(names[id] || id)}</span><select data-la="${esc(id)}">
      <option value="" ${val(id) === "" ? "selected" : ""}>跟随全局</option><option value="on" ${val(id) === "on" ? "selected" : ""}>发邮件</option>
      <option value="off" ${val(id) === "off" ? "selected" : ""}>不发</option></select></label>`).join("")}</div></fieldset>`;
}
function emailPayload(f) {
  const d = new FormData(f);
  const p = { notify: { email_enabled: d.get("email_enabled") === "on", smtp_host: d.get("smtp_host").trim(), smtp_port: Number(d.get("smtp_port") || 465),
    smtp_security: d.get("smtp_security"), smtp_user: d.get("smtp_user").trim(), smtp_to: d.get("smtp_to").trim(),
    email_events: (S.settings.email_event_groups || []).map((g) => g.key).filter((k) => d.get("ev_" + k) === "on"),
    leader_actions: Object.fromEntries([...f.querySelectorAll("[data-la]")].filter((el) => el.value !== "").map((el) => [el.dataset.la, el.value === "on"])),
    email_batch_minutes: Number(d.get("email_batch_minutes") || 0), email_max_per_hour: Number(d.get("email_max_per_hour") || 6) } };
  if (d.get("smtp_pass")) p.smtp_pass = d.get("smtp_pass");
  return p;
}
function bindEmailForm(after) {
  const f = document.getElementById("emailForm");
  if (!f) return;
  f.querySelectorAll("[data-evp]").forEach((b) => (b.onclick = () => {
    const keys = (S.settings.email_event_presets || {})[b.dataset.evp] || [];
    (S.settings.email_event_groups || []).forEach((g) => { const el = f.elements["ev_" + g.key]; if (el) el.checked = keys.includes(g.key); });
  }));
  const box = () => document.getElementById("emailMsg");
  f.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("PUT", "/api/settings", emailPayload(f));
      S.settings = r.settings;
      box().innerHTML = `<div class="msg ok">✓ 已保存${r.changed.length ? "，下一轮监控生效" : "（没有变化）"}</div>`;
      after && after();
    } catch (err) { box().innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
  document.getElementById("emailTest").onclick = async (e) => {
    const b = e.target;
    b.disabled = true; b.textContent = "发送中…";
    try {
      const p = emailPayload(f);
      const r = await api("POST", "/api/email/test", { ...p.notify, smtp_pass: p.smtp_pass || "" });
      box().innerHTML = `<div class="msg ok">✓ ${esc(r.message)}</div>`;
    } catch (err) { box().innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
    b.disabled = false; b.textContent = "发送测试邮件";
  };
}

const PRESET_DESC = {
  conservative: ["🛡️", "亏损档位 30/60/100%，回撤 8% 就提醒，建议总止损为投入的 15%。提醒多、反应早，适合本金不想有大回撤的人。"],
  balanced: ["⚖️", "亏损档位 50/100/150%，回撤 10% 提醒，建议总止损 20%。大多数人用这个就好。"],
  aggressive: ["🚀", "亏损档位 80/150/250%，回撤 15% 才提醒，建议总止损 30%。提醒少，适合能接受大波动、跟高杠杆领航员的人。"],
};
function presetCards() {
  const cur = S.settings.preset;
  return `<div class="grid presets">${Object.keys(PRESET_DESC).map((k) => `<button type="button" class="preset ${cur === k ? "on" : ""}" data-preset="${k}">
      <h3>${PRESET_DESC[k][0]} ${esc(S.settings.preset_names[k])}${k === "balanced" ? ' <span class="badge green">推荐</span>' : ""}</h3><p>${PRESET_DESC[k][1]}</p></button>`).join("")}</div>
    ${cur === "custom" ? '<p class="hint">当前是自定义参数。点击上面任意一个会用预设覆盖。</p>' : ""}`;
}
function bindPresetCards(after) {
  root.querySelectorAll("[data-preset]").forEach((b) => (b.onclick = async () => {
    const r = await api("POST", "/api/settings/preset", { name: b.dataset.preset });
    S.settings = r.settings;
    toast(`已采用“${S.settings.preset_names[b.dataset.preset]}”预设`);
    if (after) after(); else pageSettings("rules").then(bindShell);
  }));
}

/* 风控参数：type pct=小数按百分比显示；pctnum=本身就是百分比；u=U；min=分钟；num=数字 */
const RULE_GROUPS = [
  ["亏损分档提醒", [
    ["loss_tiers", "亏损档位（%）", "list", "tier", "用逗号分隔，例如 50, 100, 150。每档对每个仓位只提醒一次。"],
    ["loss_tiers_mode", "档位计算方式", "mode", "mode"],
    ["loss_cooldown_minutes", "同一仓位提醒间隔", "min", "cooldown"],
    ["loss_max_per_day", "每个仓位每天最多", "num", null, "条"],
    ["loss_rearm_gap", "回升多少个点后重新启用该档", "num", null, "个百分点"],
    ["loss_top_tier_repeat_hours", "停在最高档时每隔几小时再提醒", "num", null, "小时，0 = 不重复"],
    ["min_alert_u", "金额门槛", "u", "min_u"]]],
  ["盈利保护提醒", [
    ["breakeven_trigger_roi", "浮盈达到多少时建议保本", "pct", "breakeven"],
    ["breakeven_lock_roi", "保本止损锁定的收益", "pct", null, "覆盖手续费，一般 2%"],
    ["trail_trigger_roi", "浮盈达到多少后开始盯回撤", "pct", "trail"],
    ["trail_gap_roi", "从最高浮盈回落多少提醒", "pct", "trail"]]],
  ["跟单账户", [
    ["account_dd_warn", "权益回撤提醒线", "pct", "drawdown"],
    ["account_dd_severe", "权益回撤紧急线", "pct", "drawdown"],
    ["recommend_total_stop_pct", "建议总止损 = 净投入 ×", "pct", "native_stop"],
    ["native_stop_near_pct", "离总止损触发还剩多少时提醒", "pct", null, "占总止损额的比例"],
    ["principal_out_multiple", "权益达到本金几倍时提醒取回本金", "num", "principal", "倍，2 = 利润翻倍；0 = 关闭"],
    ["margin_ratio_warn", "交易所账户维持保证金率提醒线", "pct", "margin_ratio"],
    ["margin_ratio_severe", "交易所账户维持保证金率紧急线", "pct", "margin_ratio"],
    ["liq_distance_pct", "离强平价多近时紧急提醒", "pct"]]],
];
function ruleInput(r, [key, label, type, h, hint], prefix = "r_") {
  const v = r[key];
  let input;
  if (type === "list") input = `<input name="${prefix}${key}" value="${esc((v || []).join(", "))}">`;
  else if (type === "mode") input = `<select name="${prefix}${key}"><option value="roi" ${v === "roi" ? "selected" : ""}>按仓位 ROI（与币安页面一致）</option><option value="price" ${v === "price" ? "selected" : ""}>按价格波动 × 杠杆</option></select>`;
  else if (type === "pct") input = `<input name="${prefix}${key}" type="number" step="0.1" value="${esc(+(v * 100).toFixed(2))}">`;
  else input = `<input name="${prefix}${key}" type="number" step="any" value="${esc(v)}">`;
  const unit = { pct: "%", u: "U", min: "分钟" }[type] || "";
  return `<label class="f"><span>${label}${unit ? `（${unit}）` : ""} ${h ? help(h) : ""}</span>${input}${hint ? `<div class="hint">${hint}</div>` : ""}</label>`;
}
function readRules(f, prefix = "r_", only) {
  const out = {};
  RULE_GROUPS.forEach(([, items]) => items.forEach(([key, , type]) => {
    if (only && !only.includes(key)) return;
    const el = f.elements[prefix + key];
    if (!el || el.value === "") return;
    if (type === "list") out[key] = el.value.split(/[,，\s]+/).filter(Boolean).map(Number);
    else if (type === "mode") out[key] = el.value;
    else if (type === "pct") out[key] = Number(el.value) / 100;
    else out[key] = Number(el.value);
  }));
  return out;
}
const OVERRIDE_KEYS = ["loss_tiers", "loss_tiers_mode", "min_alert_u", "recommend_total_stop_pct",
  "breakeven_trigger_roi", "trail_trigger_roi", "trail_gap_roi", "principal_out_multiple"];
const OV_NAMES = { loss_tiers: "亏损档位", loss_tiers_mode: "档位方式", min_alert_u: "金额门槛", recommend_total_stop_pct: "建议总止损比例", tp_ladder: "分批止盈",
  breakeven_trigger_roi: "保本提醒门槛", trail_trigger_roi: "盯回撤门槛", trail_gap_roi: "回落多少提醒", principal_out_multiple: "取回本金倍数" };
const ladderText = (lv) => (lv || []).map(([r, f]) => `+${Math.round(r * 100)}% 平 ${Math.abs(f - 1 / 3) < 1e-6 ? "1/3" : Math.round(f * 100) + "%"}`).join(" → ");
function fmtOv(k, v) {
  if (k === "tp_ladder") return ladderText(v) || "关闭";
  if (["recommend_total_stop_pct", "breakeven_trigger_roi", "trail_trigger_roi", "trail_gap_roi"].includes(k)) return Math.round(v * 100) + "%";
  if (k === "loss_tiers_mode") return v === "price" ? "按价格波动" : "按 ROI";
  return Array.isArray(v) ? v.join("/") : v;
}
function rulesForm(st) {
  const r = st.rules, m = st.monitor;
  const names = Object.fromEntries((st.leaders || []).map((l) => [l.id, l.name]));
  const overrides = Object.entries(st.leader_rules || {});
  return `<h3>风控风格</h3><p class="muted small">选一个预设就能用；下面的高级参数可以微调（改了之后显示为“自定义”）。</p>
    ${presetCards()}
    <form id="rulesForm" style="margin-top:16px">
      <details class="adv" ${st.preset === "custom" ? "open" : ""}><summary>高级参数</summary>
      ${RULE_GROUPS.map(([title, items]) => `<fieldset><legend>${title}</legend><div class="row">${items.map((it) => ruleInput(r, it)).join("")}</div></fieldset>`).join("")}
      <fieldset><legend>监控</legend><div class="row">
        <label class="f"><span>轮询间隔（秒）</span><input name="poll_seconds" type="number" min="10" max="300" value="${esc(m.poll_seconds)}"></label>
        <label class="f"><span>组合浮亏预算（U）</span><input name="loss_budget_u" type="number" min="0" value="${esc(m.loss_budget_u)}"><div class="hint">所有仓位合计浮亏超过这个数时紧急提醒；0 = 关闭</div></label>
      </div></fieldset>
      <div class="actions"><button class="btn primary">保存参数</button></div></details>
      <div id="rulesMsg"></div>
    </form>
    <h3 style="margin-top:18px">按领航员单独设置</h3>
    <p class="muted small">例如某个领航员杠杆很高，可以单独把亏损档位调大，或改成“按价格波动”。没设置的领航员用上面的全局参数。</p>
    ${overrides.length ? `<div class="tbl-wrap"><table><thead><tr><th>领航员</th><th>单独设置</th><th></th></tr></thead><tbody>${overrides.map(([id, o]) =>
      `<tr><td>${esc(names[id] || id)}</td><td class="small">${Object.entries(o).map(([k, v]) => `${esc(OV_NAMES[k] || k)} = ${esc(fmtOv(k, v))}`).join("；")}</td>
       <td><button class="btn sm" data-unset="${esc(id)}">恢复全局</button></td></tr>`).join("")}</tbody></table></div>` : ""}
    <form id="ovForm" class="card" style="margin-top:10px">
      <label class="f"><span>选择领航员</span><select name="leader">${(st.leaders || []).map((l) => `<option value="${esc(l.id)}">${esc(l.name)}</option>`).join("") || '<option value="">（设置凭证后显示你的跟单）</option>'}</select></label>
      <div class="row">${RULE_GROUPS.flatMap(([, items]) => items).filter((it) => OVERRIDE_KEYS.includes(it[0])).map((it) => ruleInput(r, it, "o_")).join("")}</div>
      <div class="actions"><button class="btn">保存该领航员的设置</button></div><div id="ovMsg"></div>
    </form>`;
}
function bindRulesForm() {
  bindPresetCards();
  const f = document.getElementById("rulesForm");
  f.onsubmit = async (e) => {
    e.preventDefault();
    const box = document.getElementById("rulesMsg");
    try {
      const rules = readRules(f);
      const r = await api("PUT", "/api/settings", { rules, preset: "custom",
        monitor: { poll_seconds: Number(f.elements.poll_seconds.value), loss_budget_u: Number(f.elements.loss_budget_u.value || 0) } });
      S.settings = r.settings;
      await pageSettings("rules");
      bindShell();
      toast(r.changed.length ? `已保存 ${r.changed.length} 项参数，下一轮监控生效` : "参数没有变化");
    } catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
  const of = document.getElementById("ovForm");
  // 选中某个领航员时，表单显示他实际生效的值（全局 + 他的单独设置），避免保存时把已采用的推荐冲回全局值
  const fillOv = () => {
    const id = of.elements.leader.value;
    const v = { ...S.settings.rules, ...((S.settings.leader_rules || {})[id] || {}) };
    RULE_GROUPS.flatMap(([, items]) => items).filter((it) => OVERRIDE_KEYS.includes(it[0])).forEach(([key, , type]) => {
      const el = of.elements["o_" + key];
      if (!el || v[key] == null) return;
      el.value = type === "list" ? v[key].join(", ") : type === "pct" ? +(v[key] * 100).toFixed(2) : v[key];
    });
  };
  of.elements.leader.onchange = fillOv;
  fillOv();
  of.onsubmit = async (e) => {
    e.preventDefault();
    const id = of.elements.leader.value, box = document.getElementById("ovMsg");
    if (!id) return;
    const lr = { ...(S.settings.leader_rules || {}) };
    // 只改表单里的这几项；在领航员页“采用”的其他设置（例如止盈阶梯）保留
    const keep = Object.fromEntries(Object.entries(lr[id] || {}).filter(([k]) => !OVERRIDE_KEYS.includes(k)));
    lr[id] = { ...keep, ...readRules(of, "o_", OVERRIDE_KEYS) };
    try {
      const r = await api("PUT", "/api/settings", { leader_rules: lr });
      S.settings = r.settings;
      toast("已保存该领航员的单独设置");
      pageSettings("rules").then(bindShell);
    } catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
  root.querySelectorAll("[data-unset]").forEach((b) => (b.onclick = async () => {
    const lr = { ...(S.settings.leader_rules || {}) };
    delete lr[b.dataset.unset];
    await api("PUT", "/api/settings", { leader_rules: lr });
    pageSettings("rules").then(bindShell);
  }));
}

/* 提醒的投递状态：有意不发 / 排队等待不是故障，不标红 */
function deliveryLine(a) {
  const e = a.error || "";
  if (a.delivered) return `<div class="small muted">✓ 已送达${e ? " · " + esc(e) : ""}</div>`;
  if (/按设置不发邮件|低于邮件通知级别/.test(e)) return `<div class="small muted">只在后台记录（按设置不发邮件）</div>`;
  if (/等待合并|达到每小时上限|等待重试/.test(e)) return `<div class="small" style="color:var(--yellow, #b7791f)">⏳ ${esc(e.replace(/^email: /, ""))}</div>`;
  return `<div class="small down">✗ 未送达${e ? " · " + esc(e) : ""}</div>`;
}

/* ---------------- 入口 ---------------- */

/* ================= 第二期：统计 / 领航员 / 时间线 / 离场判定 ================= */
const VERDICT_CLS = { "止损挽回": "green", "避免回吐": "green", "比领航员多赚": "green", "白止损": "red", "少赚": "yellow", "比领航员多亏": "red", "未知": "", "跟随": "" };
const EXIT_TYPES = ["跟随平仓", "提前离场", "提前部分离场", "滞后平仓", "跟单结束"];
const day = (ms) => (ms ? dt(ms / 1000) : "-");

function exitsTable(list, withLeader) {
  if (!list.length) return `<div class="empty">还没有离场记录（你的跟单仓位减仓或平仓后会出现）</div>`;
  return `<div class="tbl-wrap"><table class="cards"><thead><tr><th>时间</th>${withLeader ? "<th>领航员</th>" : ""}<th>币种</th>
    <th>类型${help("early")}</th><th class="r">数量</th><th class="r">实际</th><th class="r">跟到底</th><th>结论${help("verdict")}</th><th>备注</th><th></th></tr></thead><tbody>
    ${list.map((x) => `<tr>
      <td data-l="时间">${dt(x.ts)}</td>${withLeader ? `<td data-l="领航员">${esc(x.leader_name)}</td>` : ""}
      <td data-l="币种"><b>${esc(x.symbol)}</b> ${side(x.side)}${x.partial ? ' <span class="badge">部分</span>' : ""}</td>
      <td data-l="类型"><select class="sm-sel" data-exit="${x.id}">${(EXIT_TYPES.includes(x.exit_type) ? [] : [x.exit_type]).concat(EXIT_TYPES)
        .map((t) => `<option ${t === x.exit_type ? "selected" : ""}>${esc(t)}</option>`).join("")}</select>${x.manual ? ' <span class="badge">手动</span>' : ""}</td>
      <td data-l="数量" class="r num">${num(x.qty, 4)}</td>
      <td data-l="实际" class="r">${x.actual_u == null ? '<span class="muted">待同步</span>' : U(x.actual_u)}</td>
      <td data-l="跟到底" class="r">${x.hypo_u == null ? '<span class="muted">-</span>' : U(x.hypo_u)}</td>
      <td data-l="结论">${x.verdict ? `<span class="badge ${VERDICT_CLS[x.verdict] || ""}">${esc(x.verdict)}${x.delta_u != null && x.verdict !== "跟随" ? " " + num(Math.abs(x.delta_u)) + "U" : ""}</span>`
        : x.status === "waiting" ? '<span class="badge yellow">等领航员平仓</span>' : '<span class="muted">判定中</span>'}</td>
      <td data-l="备注" class="small">${esc(x.remarks || "")}</td>
      <td data-l="">${x.episode_id ? `<a class="btn sm" href="#/episode/${x.episode_id}">时间线</a>` : ""}</td></tr>`).join("")}
    </tbody></table></div>`;
}
function bindExitsTable(after) {
  root.querySelectorAll("[data-exit]").forEach((sel) => (sel.onchange = async () => {
    try {
      await api("POST", "/api/exits/manual", { id: Number(sel.dataset.exit), exit_type: sel.value });
      toast("已修改离场类型；提前类型会在领航员平仓后重新结算");
      after && after();
    } catch (e) { toast(e.message); }
  }));
}

/* ---------- 小图表（内联 SVG，不依赖外部库） ---------- */
function bars(items, valueKey, labelKey, h = 140) {
  if (!items.length) return `<div class="empty">暂无数据</div>`;
  const W = 800, P = 24, max = Math.max(...items.map((i) => Math.abs(i[valueKey] || 0)), 1e-9);
  const bw = (W - P * 2) / items.length, mid = h / 2;
  return `<svg class="chart" viewBox="0 0 ${W} ${h}" preserveAspectRatio="none">
    <line class="zero" x1="${P}" x2="${W - P}" y1="${mid}" y2="${mid}"/>
    ${items.map((it, i) => { const v = it[valueKey] || 0, bh = (Math.abs(v) / max) * (mid - 14);
      return `<rect class="${v >= 0 ? "bar-up" : "bar-dn"}" x="${P + i * bw + bw * 0.15}" y="${v >= 0 ? mid - bh : mid}" width="${bw * 0.7}" height="${Math.max(bh, 0.5)}"><title>${esc(it[labelKey])}：${signed(v)}</title></rect>`; }).join("")}
    <text x="${P}" y="12">${esc(items[0][labelKey])}</text><text x="${W - P}" y="12" text-anchor="end">${esc(items[items.length - 1][labelKey])}</text></svg>`;
}

/* ---------- 统计页 ---------- */
async function pageStats(monthArg) {
  const gen = S.gen;
  const now = new Date();
  const month = /^\d{4}-\d{1,2}$/.test(monthArg || "") ? monthArg : `${now.getFullYear()}-${now.getMonth() + 1}`;
  const src = S.statSource || "all";
  const [ov, st] = await Promise.all([api("GET", "/api/overview"),
    api("GET", `/api/stats?month=${month}&source=${encodeURIComponent(src)}`).catch((e) => { S.statSource = "all"; throw e; })]);
  if (stale(gen)) return;
  const mode = S.calMode || "net";
  const srcSel = st.sources.length > 2 ? `<label class="sm-sel">来源 <select id="statSrc">${st.sources.map((x) =>
    `<option value="${esc(x.id)}" ${x.id === src ? "selected" : ""}>${esc(x.name)}</option>`).join("")}</select></label>` : "";
  const c = st.calendar;
  const [y, m] = [c.year, c.month];
  const prev = m === 1 ? `${y - 1}-12` : `${y}-${m - 1}`, next = m === 12 ? `${y + 1}-1` : `${y}-${m + 1}`;
  const vals = c.days.map((d) => (mode === "net" ? d.net : d.equity_change)).filter((v) => v != null);
  const maxAbs = Math.max(...vals.map(Math.abs), 1e-9);
  const lead = (c.first_weekday + 7) % 7; // Python: 周一=0
  const cells = Array(lead).fill('<div class="cal-cell empty"></div>').concat(c.days.map((d) => {
    const v = mode === "net" ? d.net : d.equity_change;
    const a = v == null ? 0 : Math.min(1, Math.abs(v) / maxAbs);
    const cls = v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
    return `<div class="cal-cell ${cls}" style="--a:${(0.12 + a * 0.6).toFixed(2)}" title="${d.date}${d.trades ? ` · 平仓 ${d.trades} 笔 · 手续费 ${signed(d.fee)}U` : ""}${d.equity_change != null ? ` · 权益变化 ${signed(d.equity_change)}U` : ""}">
      <span class="d">${Number(d.date.slice(8))}</span><span class="v num">${v == null ? "" : signed(v, Math.abs(v) >= 100 ? 0 : 1)}</span></div>`;
  })).join("");
  const P = st.periods;
  const pc = (k, t) => `<div class="card kpi"><div class="label">${t}</div><div class="value num">${U(P[k].net)}</div><div class="sub">平仓 ${P[k].trades} 笔 · 手续费 ${signed(P[k].fee)}U${P[k].funding ? ` · 资金费 ${signed(P[k].funding)}U` : ""}</div></div>`;
  const co = st.composition, ex = st.exits, exm = st.exits_month, lo = st.losses;
  const comp = (o, t) => `<dl class="kv"><dt>${t} 已实现盈亏</dt><dd>${U(o.realized)}</dd><dt>手续费</dt><dd>${U(o.fee)}</dd>
      <dt>资金费</dt><dd>${U(o.funding || 0)}</dd><dt>分润</dt><dd>${U(o.profit_shared)}</dd>
      <dt><b>净额</b></dt><dd><b>${U(o.realized + o.fee + (o.funding || 0) + o.profit_shared)}</b></dd></dl>`;
  const exCard = (e, t) => `<dl class="kv"><dt>${t}跟随平仓</dt><dd>${e.follow} 次</dd><dt>提前离场${help("early")}</dt><dd>${e.early} 次${e.waiting ? `（另有 ${e.waiting} 笔等领航员平仓）` : ""}</dd>
      <dt>止损挽回</dt><dd>${e.saved_n} 次 ${U(e.saved_u)}</dd><dt>白止损</dt><dd>${e.wasted_n} 次 ${U(-e.wasted_u)}</dd>
      <dt>避免回吐</dt><dd>${e.avoid_giveback_n} 次 ${U(e.avoid_giveback_u)}</dd><dt>少赚</dt><dd>${e.missed_n} 次 ${U(-e.missed_u)}</dd>
      <dt><b>提前离场净效果</b>${help("verdict")}</dt><dd><b>${U(e.net_u)}</b></dd></dl>`;
  root.innerHTML = shell("stats", "统计", statusChips(ov), `
    ${srcSel ? `<div class="stat-src">${srcSel}<span class="muted small">跟单的资金费币安接口没有；交易所账户的数据来自资金流水（近 3 个月）</span></div>` : ""}
    <div class="grid kpis">${pc("today", "今日")}${pc("yesterday", "昨日")}${pc("week", "本周")}${pc("month", "本月")}${pc("d30", "近 30 天")}${pc("all", "全部")}</div>
    <p class="hint">以上为已实现净盈亏${help("realized")}。已同步 ${st.sync.fills} 笔成交${st.sync.since_ms ? `，最早 ${day(st.sync.since_ms)}` : "（首次同步需要几分钟，凭证有效时自动进行）"}。</p>
    <div class="section card"><div class="card-h"><h2>盈亏日历 · ${y} 年 ${m} 月</h2>
      <div class="actions"><a class="btn sm" href="#/stats/${prev}">‹ 上月</a><a class="btn sm" href="#/stats/${next}">下月 ›</a>
      <button class="btn sm ${mode === "net" ? "primary" : ""}" data-cal="net">已实现${help("realized")}</button><button class="btn sm ${mode === "eq" ? "primary" : ""}" data-cal="eq">权益变化${help("equity_change")}</button></div></div>
      <div class="cal-head">${["一", "二", "三", "四", "五", "六", "日"].map((w) => `<div>${w}</div>`).join("")}</div>
      <div class="cal">${cells}</div>
      <p class="hint">本月已实现 ${U(c.total_net)}，盈利 ${c.win_days} 天、亏损 ${c.loss_days} 天。颜色越深金额越大。</p></div>
    <div class="section grid two">
      <div class="card"><h3>盈亏构成</h3>${comp(co.month, "本月")}<hr class="sep">${comp(co.all, "全部")}
        <p class="hint">当前浮动盈亏 ${U(co.unrealized)}（未计入上面）。跟单的资金费接口没有数据；交易所账户的资金费已计入。</p></div>
      <div class="card"><h3>止损 / 提前离场效果</h3>${exCard(exm, "本月 ")}<hr class="sep">${exCard(ex, "全部 ")}
        <p class="hint">每次离场的明细在“记录 → 离场判定”。</p></div>
    </div>
    <div class="section grid two">
      <div class="card"><h3>最大的 10 笔亏损</h3>${lo.worst.length ? `<table><tbody>${lo.worst.map((w) => `<tr><td>${esc(w.leader_name)}</td><td><b>${esc(w.symbol)}</b> ${side(w.side)} ${num(w.leverage, 0)}x</td><td class="small muted">${day(w.closed_ms)}</td><td class="r">${U(w.pnl)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">没有亏损单</div>`}
        <p class="hint">共 ${lo.closed_n} 笔已平仓；最长连续亏损 ${lo.max_loss_streak_days} 天。</p></div>
      <div class="card"><h3>单笔盈亏分布（U）</h3>${bars(lo.distribution, "count", "range")}
        <div class="small muted dist">${lo.distribution.map((d) => `<span>${esc(d.range)}: ${d.count}</span>`).join("")}</div></div>
    </div>
    <div class="section card"><h3>利润回吐（我的仓位）${help("giveback")}</h3>
      ${st.giveback.length ? `<div class="tbl-wrap"><table class="cards"><thead><tr><th>领航员</th><th class="r">已平仓回合</th><th class="r">利润回吐</th><th class="r">盈转亏</th><th class="r">未分批止盈</th><th class="r">少拿的利润</th></tr></thead><tbody>
        ${st.giveback.map((g) => `<tr><td data-l="领航员"><a href="#/leaders/${esc(g.lead_id)}">${esc(g.name)}</a></td><td data-l="已平仓回合" class="r">${g.closed}</td><td data-l="利润回吐" class="r">${g.giveback}</td><td data-l="盈转亏" class="r">${g.profit_to_loss}</td><td data-l="未分批止盈" class="r">${g.no_partial_tp}</td><td data-l="少拿的利润" class="r">${U(-g.lost_u)}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty">仓位回合从安装后开始记录，平仓后这里会出现</div>`}
      <p class="hint">“少拿的利润” = 持仓过程最高浮盈 − 最后结果。经常回吐的领航员，适合你自己分批止盈。</p></div>`);
  root.querySelectorAll("[data-cal]").forEach((b) => (b.onclick = (e) => { if (e.target.closest("[data-help]")) return; S.calMode = b.dataset.cal; pageStats(month).then(bindShell); }));
  const ss = document.getElementById("statSrc");
  if (ss) ss.onchange = () => { S.statSource = ss.value; pageStats(month).then(bindShell); };
}

/* ---------- 领航员 ---------- */
const RANK_COLS = [
  ["name", "领航员"], ["equity", "权益"], ["pnl", "累计盈亏"], ["month_net", "本月已实现"], ["drawdown", "回撤"],
  ["win_rate", "我的胜率"], ["payoff", "盈亏比"], ["exits_net_u", "提前离场净效果"], ["carry_rate", "扛单率"], ["giveback_rate", "回吐率"], ["alerts", "提醒数"],
];
async function pageLeaders() {
  if (S.leadersTab === "discover") return pageDiscover();
  const gen = S.gen;
  const [ov, { leaders }] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/leaders")]);
  if (stale(gen)) return;
  const key = S.rankKey || "pnl", dir = S.rankDir || -1;
  const endedN = leaders.filter((r) => r.kind === "ended").length;
  const sorted = leaders.filter((r) => !(S.hideEnded && r.kind === "ended")).sort((a, b) => {
    if ((a.kind === "ended") !== (b.kind === "ended")) return a.kind === "ended" ? 1 : -1;  // 已停止跟单的排在最后
    const x = a[key], y = b[key];
    if (x == null) return 1; if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * dir;
  });
  const job = (j) => !j ? "" : j.state === "done" ? "" : j.state === "error" ? '<span class="badge red">分析出错</span>'
    : `<span class="badge yellow">分析中 ${j.total ? `${j.progress}/${j.total}` : ""}</span>`;
  const dev = (d) => !d ? "" : (d.missing || d.over || d.under) ? `<span class="badge yellow" title="超配 ${d.over} · 低配 ${d.under} · 漏单 ${d.missing}">偏差 ${d.over + d.under + d.missing}</span>` : "";
  root.innerHTML = shell("leaders", "领航员", statusChips(ov), `${leaderTabs("rank")}
    <div class="card"><div class="card-h"><h2>领航员排行</h2><span class="muted small">点列头排序
      ${endedN ? ` · <a href="javascript:void 0" id="toggleEnded">${S.hideEnded ? `显示已停止跟单的 ${endedN} 位` : "隐藏已停止跟单的"}</a>` : ""}</span></div>
    ${sorted.length ? `<div class="tbl-wrap"><table class="cards rank"><thead><tr>${RANK_COLS.map(([k, t]) => `<th class="${k === "name" ? "" : "r"} sort" data-k="${k}">${t}${k === "carry_rate" ? help("carry") : k === "giveback_rate" ? help("giveback") : k === "exits_net_u" ? help("verdict") : ""}${key === k ? (dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead><tbody>
      ${sorted.map((r) => `<tr class="click" data-go="${esc(r.lead_id)}">
        <td data-l="领航员"><b>${esc(r.name)}</b> ${KIND_BADGE[r.kind] || ""} ${job(r.job)} ${dev(r.deviation)}<div class="tags">${(r.tags || []).slice(0, 4).map((t) => `<span class="badge">${esc(t)}</span>`).join("")}</div></td>
        <td data-l="权益" class="r num">${r.equity == null ? "-" : num(r.equity) + "U"}</td>
        <td data-l="累计盈亏" class="r">${r.pnl == null ? "-" : U(r.pnl)}</td>
        <td data-l="本月已实现" class="r">${U(r.month_net)}</td>
        <td data-l="回撤" class="r">${r.drawdown == null ? "-" : num(r.drawdown * 100, 1) + "%"}</td>
        <td data-l="我的胜率" class="r">${r.win_rate == null ? "-" : num(r.win_rate * 100, 0) + "%"} <span class="small muted">${r.closed || 0} 笔</span></td>
        <td data-l="盈亏比" class="r">${r.payoff == null ? "-" : num(r.payoff)}</td>
        <td data-l="提前离场净效果" class="r">${r.early_n ? U(r.exits_net_u) + ` <span class="small muted">${r.early_n} 次</span>` : "-"}</td>
        <td data-l="扛单率" class="r">${r.carry_rate == null ? "-" : num(r.carry_rate * 100, 0) + "%"}</td>
        <td data-l="回吐率" class="r">${r.giveback_rate == null ? "-" : num(r.giveback_rate * 100, 0) + "%"}</td>
        <td data-l="提醒数" class="r">${r.alerts}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">设置币安凭证后，你跟单的领航员会自动出现在这里，并在后台分析历史</div>`}</div>
    <form class="card" id="watchForm"><h3>分析其他领航员</h3>
      <p class="muted small">想跟一个新领航员？输入他主页网址里 lead-details/ 后面那串数字，系统会抓取他的历史并做风格画像和回测（只读，不影响你的账户）。</p>
      <div class="row"><label class="f"><span>领航员 ID</span><input name="id" inputmode="numeric" placeholder="例如 4708220152086930177"></label>
      <label class="f"><span>备注名（可选）</span><input name="name"></label></div>
      <button class="btn">开始分析</button><div id="watchMsg"></div></form>`);
  bindLeaderTabs();
  const te = document.getElementById("toggleEnded");
  if (te) te.onclick = () => { S.hideEnded = !S.hideEnded; pageLeaders().then(bindShell); };
  root.querySelectorAll("th.sort").forEach((th) => (th.onclick = (e) => {
    if (e.target.closest("[data-help]")) return;
    S.rankDir = S.rankKey === th.dataset.k ? -(S.rankDir || -1) : -1; S.rankKey = th.dataset.k; pageLeaders().then(bindShell);
  }));
  root.querySelectorAll("tr[data-go]").forEach((tr) => (tr.onclick = (e) => { if (!e.target.closest("[data-help]")) location.hash = "#/leaders/" + tr.dataset.go; }));
  document.getElementById("watchForm").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target), box = document.getElementById("watchMsg");
    try { const r = await api("POST", "/api/leaders/analyze", { id: f.get("id").trim(), name: f.get("name").trim() }); box.innerHTML = `<div class="msg ok">${esc(r.message)}</div>`; setTimeout(() => pageLeaders().then(bindShell), 1200); }
    catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
}

const KIND_BADGE = { watch: '<span class="badge">观察</span>', ended: '<span class="badge">已停止跟单</span>' };
function leaderTabs(cur) {
  return `<div class="tabs">${[["rank", "排行"], ["discover", "发现领航员"]].map(([k, t]) =>
    `<button class="tab ${cur === k ? "on" : ""}" data-ltab="${k}">${t}</button>`).join("")}</div>`;
}
function bindLeaderTabs() {
  root.querySelectorAll("[data-ltab]").forEach((b) => (b.onclick = () => { S.leadersTab = b.dataset.ltab; pageLeaders().then(bindShell); }));
}

/* ---------- 发现领航员（币安榜单，只读）---------- */
const DISC = { range: "30D", sort: "ROI", minRoi: "", maxMdd: "50", copierWin: true, hideFull: true, minAum: "" };
async function pageDiscover() {
  const gen = S.gen;
  const f = S.disc || (S.disc = { ...DISC });
  const [ov, d] = await Promise.all([api("GET", "/api/overview"),
    api("GET", `/api/discover?range=${f.range}&sort=${f.sort}`).catch((e) => ({ error: e.message, items: [] }))]);
  if (stale(gen)) return;
  const list = d.items.filter((x) => (f.minRoi === "" || x.roi >= Number(f.minRoi)) && (f.maxMdd === "" || x.mdd <= Number(f.maxMdd))
    && (!f.copierWin || x.copier_pnl > 0) && (!f.hideFull || !x.max_copiers || x.copiers < x.max_copiers)
    && (f.minAum === "" || x.aum >= Number(f.minAum)));
  const sel = (name, opts, v) => `<select name="${name}">${opts.map(([k, t]) => `<option value="${k}" ${k === v ? "selected" : ""}>${t}</option>`).join("")}</select>`;
  root.innerHTML = shell("leaders", "领航员", statusChips(ov), `${leaderTabs("discover")}
    <form class="card" id="discForm"><div class="card-h"><h2>发现领航员</h2><span class="muted small">数据来自币安跟单榜单（前 90 名），10 分钟更新一次</span></div>
      <div class="row">
        <label class="f"><span>时间范围</span>${sel("range", [["7D", "7 天"], ["30D", "30 天"], ["90D", "90 天"], ["180D", "180 天"]], f.range)}</label>
        <label class="f"><span>榜单排序</span>${sel("sort", [["ROI", "收益率"], ["PNL", "收益额"], ["COPIER_PNL", "跟单者盈亏"], ["MDD", "最大回撤"], ["WIN_RATE", "胜率"], ["AUM", "规模"]], f.sort)}</label>
        <label class="f"><span>收益率至少（%）</span><input name="minRoi" type="number" value="${esc(f.minRoi)}"></label>
        <label class="f"><span>最大回撤不超过（%）</span><input name="maxMdd" type="number" value="${esc(f.maxMdd)}"></label>
        <label class="f"><span>规模至少（U）</span><input name="minAum" type="number" value="${esc(f.minAum)}"></label>
      </div>
      <label class="chk"><input type="checkbox" name="copierWin" ${f.copierWin ? "checked" : ""}> 只看跟单者整体赚钱的（领航员赚钱不代表跟单者赚钱）</label>
      <label class="chk"><input type="checkbox" name="hideFull" ${f.hideFull ? "checked" : ""}> 隐藏已满员的</label>
      <div class="actions"><button class="btn">筛选</button></div></form>
    <div class="card"><div class="card-h"><h3>符合条件 ${list.length} 位</h3><span class="muted small">点“加入观察”后台会分析他的历史风格和回测（只读）</span></div>
    ${d.error ? `<div class="msg err">${esc(d.error)}</div>` : list.length ? `<div class="tbl-wrap"><table class="cards"><thead><tr><th>领航员</th><th class="r">收益率</th><th class="r">最大回撤</th><th class="r">胜率</th>
      <th class="r">跟单者盈亏</th><th class="r">规模</th><th class="r">跟单人数</th><th></th></tr></thead><tbody>
      ${list.map((x) => `<tr><td data-l="领航员"><b>${esc(x.name)}</b></td><td data-l="收益率" class="r">${pct(x.roi / 100, 0)}</td>
        <td data-l="最大回撤" class="r num">${num(x.mdd, 1)}%</td><td data-l="胜率" class="r num">${num(x.win_rate, 0)}%</td>
        <td data-l="跟单者盈亏" class="r">${U(x.copier_pnl, 0)}</td><td data-l="规模" class="r num">${num(x.aum, 0)}U</td>
        <td data-l="跟单人数" class="r num">${x.copiers}/${x.max_copiers || "-"}</td>
        <td data-l="">${x.kind ? `<a class="btn sm" href="#/leaders/${esc(x.id)}">查看${x.kind === "copy" ? "（跟单中）" : ""}</a>`
          : `<button class="btn sm" data-watch="${esc(x.id)}" data-name="${esc(x.name)}">加入观察</button>`}
          <a class="btn sm ghost" href="https://www.binance.com/zh-CN/copy-trading/lead-details/${esc(x.id)}" target="_blank" rel="noopener">币安 ↗</a></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">没有符合条件的领航员，放宽一下筛选条件试试</div>`}</div>`);
  bindLeaderTabs();
  document.getElementById("discForm").onsubmit = (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    S.disc = { range: fd.get("range"), sort: fd.get("sort"), minRoi: fd.get("minRoi"), maxMdd: fd.get("maxMdd"), minAum: fd.get("minAum"),
      copierWin: fd.get("copierWin") === "on", hideFull: fd.get("hideFull") === "on" };
    pageLeaders().then(bindShell);
  };
  root.querySelectorAll("[data-watch]").forEach((b) => (b.onclick = async () => {
    try { const r = await api("POST", "/api/leaders/analyze", { id: b.dataset.watch, name: b.dataset.name }); toast(r.message); pageLeaders().then(bindShell); }
    catch (err) { toast(err.message); }
  }));
}

function scatter(points) {
  if (!points.length) return `<div class="empty">历史分析完成后显示</div>`;
  const W = 800, H = 260, P = 32;
  const xs = points.map((p) => p.mae), ys = points.map((p) => p.mfe);
  const x0 = Math.min(...xs, -0.1), y1 = Math.max(...ys, 0.1);
  const X = (v) => P + ((v - x0) / (0 - x0)) * (W - P * 2), Y = (v) => H - P - (v / y1) * (H - P * 2);
  return `<svg class="chart tall" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line class="zero" x1="${X(0)}" x2="${X(0)}" y1="${P}" y2="${H - P}"/><line class="zero" x1="${P}" x2="${W - P}" y1="${Y(0)}" y2="${Y(0)}"/>
    ${points.map((p) => `<circle class="${p.win ? "pt-win" : "pt-loss"}" cx="${X(p.mae).toFixed(1)}" cy="${Y(p.mfe).toFixed(1)}" r="3.5"><title>最深浮亏 ${signed(p.mae * 100, 0)}% · 最高浮盈 ${signed(p.mfe * 100, 0)}% · ${p.win ? "盈利" : "亏损"}</title></circle>`).join("")}
    <text x="${P}" y="${H - 8}">← 最深浮亏 ${signed(x0 * 100, 0)}%</text><text x="${X(0)}" y="14" text-anchor="end">最高浮盈 ${signed(y1 * 100, 0)}% ↑</text></svg>
    <p class="hint">每个点是一笔历史仓位：越靠左过程中亏得越深，越靠上过程中赚得越多；<span class="up">绿点</span>最终盈利、<span class="down">红点</span>最终亏损。很多绿点在很左边 = 爱扛单，止损太紧会误杀。</p>`;
}
function btRows(list) {
  return `<div class="tbl-wrap"><table><thead><tr><th>方案</th><th class="r">累计收益</th><th class="r">最大回撤</th><th class="r">最差单笔</th><th class="r">误杀盈利单</th></tr></thead><tbody>
    ${list.filter(Boolean).map((b) => `<tr><td>${esc(b.label)}</td><td class="r">${signed(b.total)}</td><td class="r">${num(b.max_dd)}</td><td class="r">${pct(b.worst, 0)}</td><td class="r">${b.winners_cut}</td></tr>`).join("")}</tbody></table></div>`;
}
async function pageLeader(id) {
  const gen = S.gen;
  const [ov, d] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/leaders/detail?id=" + encodeURIComponent(id))]);
  if (stale(gen)) return;
  const pr = d.profile, p = pr ? pr.profile : null, r = d.ranking || {}, j = d.job;
  const jobLine = !j ? "还没有分析" : j.state === "done" ? `已分析 ${pr ? pr.n : 0} 笔历史仓位（${dt(pr && pr.computed_at)}）`
    : j.state === "error" ? `分析出错：${esc(j.message)}` : `分析中：${esc(j.message || "")} ${j.total ? `${j.progress}/${j.total}` : ""}`;
  const metric = (t, v, h) => `<div class="card kpi"><div class="label">${t}${h ? help(h) : ""}</div><div class="value num">${v}</div></div>`;
  const bt = pr && pr.backtest;
  // 只观察、从没跟单过的领航员，不显示“我的”结果
  const mine = d.kind === "copy" || d.my_positions.length > 0 || d.exits.length > 0;
  root.innerHTML = shell("leaders", esc(d.name), statusChips(ov), `
    <div class="card"><div class="card-h"><div><a href="#/leaders" class="small">‹ 领航员排行</a> ${KIND_BADGE[d.kind] || '<span class="badge green">跟单中</span>'}${d.kind === "ended" ? help("ended") : ""}</div>
      <div class="actions"><button class="btn sm" id="reAnalyze">${d.kind === "ended" ? "转为观察并分析" : "重新分析"}</button>
        <a class="btn sm" href="/api/leaders/export?id=${esc(d.lead_id)}">导出数据</a>
        ${d.kind !== "copy" ? '<button class="btn sm ghost" id="delLeader">删除数据</button>' : ""}
        <a class="btn sm ghost" href="${esc(d.link)}" target="_blank" rel="noopener">币安主页 ↗</a></div></div>
      ${d.kind === "ended" ? `<p class="small">你在 ${dt(d.ended_ts)} 停止跟单这个领航员。数据会一直保留（不再每天同步他的历史）；可以导出备份，或删除他的全部数据。</p>` : ""}
      <p class="muted small">${jobLine}</p>
      ${p && p.tags && p.tags.length ? `<div class="tags">${p.tags.map((t) => `<span class="badge yellow">${esc(t)}</span>`).join(" ")}</div>` : ""}</div>
    ${mine && r.lead_id ? `<h3 class="section">我在他身上的结果</h3><div class="grid kpis">
      ${metric("累计盈亏", r.pnl == null ? "-" : U(r.pnl))}${metric("本月已实现", U(r.month_net))}${metric("回撤", r.drawdown == null ? "-" : num(r.drawdown * 100, 1) + "%", "drawdown")}
      ${metric("我的胜率", r.win_rate == null ? "-" : num(r.win_rate * 100, 0) + "%")}${metric("盈亏比", r.payoff == null ? "-" : num(r.payoff))}${metric("提前离场净效果", d.exits_summary.early ? U(d.exits_summary.net_u) : "-", "verdict")}</div>` : ""}
    ${p ? `<h3 class="section">领航员风格画像（他自己的历史）</h3><div class="grid kpis">
      ${metric("胜率", num(p.win_rate * 100, 0) + "%")}${metric("盈亏比", num(p.payoff))}${metric("扛单率", p.carry_rate == null ? "-" : num(p.carry_rate * 100, 0) + "%", "carry")}
      ${metric("利润回吐率", p.giveback_rate == null ? "-" : num(p.giveback_rate * 100, 0) + "%", "giveback")}${metric("杠杆中位", num(p.leverage_median, 0) + "x")}${metric("持仓时长中位", num(p.duration_median_h, 1) + "h")}
      ${metric("最深浮亏中位", p.mae_median == null ? "-" : pct(p.mae_median), "mae")}${metric("最差 10% 浮亏", p.mae_p10 == null ? "-" : pct(p.mae_p10))}
      ${metric("盈利单浮盈中位", p.winner_mfe_p50 == null ? "-" : pct(p.winner_mfe_p50), "mfe")}${metric("盈利单浮盈 P75", p.winner_mfe_p75 == null ? "-" : pct(p.winner_mfe_p75))}
      ${metric("最大连亏", p.max_loss_streak + " 笔")}${metric("前 5 大亏损占比", num(p.top5_loss_share * 100, 0) + "%")}</div>
      ${!p.sample_ok ? '<p class="hint">⚠️ 样本少于 30 笔，以上结论仅供参考。</p>' : ""}
      <div class="section card"><h3>每笔仓位：过程最深浮亏 vs 最高浮盈</h3>${scatter(d.mae_mfe)}</div>` : ""}
    ${recCard(d.recommend)}
    ${bt ? `<div class="section grid two"><div class="card"><h3>只设硬止损（币安里能直接设置）</h3>${btRows([bt.baseline, ...bt.stop_only])}</div>
      <div class="card"><h3>全部组合前 10</h3>${btRows([bt.baseline, ...bt.ranked])}
      <p class="hint">${bt.oos.train_best ? `样本外检验：前 ${bt.oos.n_train} 笔选出“${esc(bt.oos.train_best.label)}”，在后 ${bt.oos.n_test} 笔上 ${signed(bt.oos.test_best.total)}（原始跟单 ${signed(bt.oos.test_baseline.total)}）。` : "样本不足，未做样本外检验。"}
      收益单位 = 每笔投入 1 份保证金的倍数。历史表现不代表未来。</p></div></div>` : ""}
    ${!mine ? `<div class="section card"><div class="empty">你没有跟单这个领航员，这里只显示他自己的历史风格。跟单后会出现你在他身上的盈亏、离场记录和历史仓位。</div></div>` : `<div class="section card"><h3>我的离场记录</h3>${exitsTable(d.exits, false)}</div>
    <div class="section card"><h3>我的历史仓位（最近 50 笔）</h3>${d.my_positions.length ? `<div class="tbl-wrap"><table class="cards"><thead><tr><th>开仓</th><th>平仓</th><th>币种</th><th class="r">开仓价</th><th class="r">平仓价</th><th class="r">盈亏</th><th>状态</th></tr></thead><tbody>
      ${d.my_positions.map((m) => `<tr><td data-l="开仓">${day(m.opened_ms)}</td><td data-l="平仓">${day(m.closed_ms)}</td><td data-l="币种"><b>${esc(m.symbol)}</b> ${side(m.side)} ${num(m.leverage, 0)}x</td>
        <td data-l="开仓价" class="r num">${num(m.avg_cost, 4)}</td><td data-l="平仓价" class="r num">${num(m.avg_close, 4)}</td><td data-l="盈亏" class="r">${U(m.pnl)}</td><td data-l="状态" class="small">${esc(m.status)}</td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty">还没有同步到你的历史仓位</div>`}</div>`}`);
  document.getElementById("reAnalyze").onclick = async () => { const r2 = await api("POST", "/api/leaders/analyze", { id: d.lead_id, name: d.name }); toast(r2.message); pageLeader(id).then(bindShell); };
  const del = document.getElementById("delLeader");
  if (del) del.onclick = async () => {
    const typed = prompt(`删除“${d.name}”的全部数据（成交、历史仓位、离场判定、仓位回合、提醒、画像和单独设置）。\n删除前会自动备份到服务器 data/backups；统计页的历史盈亏也会去掉他的部分。\n\n请输入领航员名字确认：${d.name}`);
    if (typed == null) return;
    try { const r2 = await api("POST", "/api/leaders/delete", { id: d.lead_id, confirm: typed.trim() }); toast(r2.message); location.hash = "#/leaders"; }
    catch (err) { toast(err.message); }
  };
  root.querySelectorAll("[data-adopt]").forEach((b) => (b.onclick = async () => {
    try { const r2 = await api("POST", "/api/leaders/adopt", { id: d.lead_id, what: b.dataset.adopt }); toast(r2.message); pageLeader(id).then(bindShell); }
    catch (err) { toast(err.message); }
  }));
  bindExitsTable(() => pageLeader(id).then(bindShell));
}

/* ---------- 推荐（第三期）：点“采用”才写入该领航员的单独设置 ---------- */
const OUTDATED = "这份分析是旧版本生成的（还没有止盈回测和最大回撤），系统会在后台自动重新分析；也可以点上面的“重新分析”。";
function recCard(r) {
  if (!r) return "";
  const cur = r.current || {};
  const curTiers = (cur.loss_tiers || []).map((t) => `-${t}%`).join(" / ") + (cur.loss_tiers_mode === "price" ? "（按价格波动）" : "");
  if (!r.ok) return `<div class="section card"><h3>止损 / 止盈推荐</h3><div class="empty">${esc(r.reason || "暂无推荐")}</div></div>`;
  const last = (r.tier_notes || []).length - 1;
  const notes = (r.tier_notes || []).map((n, i) => `<li><b>-${n.tier}%</b>：他最后赚钱的单子里约 ${num(n.winners_deeper * 100, 0)}% 曾跌到这里${n.losers_deeper != null ? `；亏钱的单子约 ${num(n.losers_deeper * 100, 0)}% 跌到这里` : ""}${i === last ? "。<b>到这一档考虑先减半</b>，之后看离场判定的结论再调整" : ""}</li>`).join("");
  const hs = r.hard_stop, pg = r.profit_guard;
  const lad = r.ladder, curLad = cur.tp_ladder || [];
  // 一句话结论：单仓硬止损要不要设、提醒档位、盈利保护、总止损兜底
  const verdict = [!hs ? "" : hs.better ? `单仓硬止损可参考“${hs.label}”` : "不设单仓硬止损（回测都不如不设）",
    `用亏损提醒（${r.loss_tiers.map((t) => `-${t}%`).join(" / ")}）`,
    pg && pg.adoptable ? `盈利保护（${pg.label}）` : "",
    `跟单总止损兜底${r.total_stop_pct ? `（净投入 × ${num(r.total_stop_pct * 100, 0)}%）` : ""}`].filter(Boolean);
  return `<div class="section card"><h3>这个领航员怎么设</h3><p><b>${esc(verdict.join(" + "))}</b></p>
      <p class="muted small">亏损提醒只提醒、不平仓，到档位时由你决定；总止损在币安该跟单的设置里开启，是唯一由交易所自动执行的保护。</p></div>
    <div class="section grid two">
    <div class="card"><h3>亏损提醒档位（只提醒，不平仓） ${help("rec_tiers")}</h3>
      <p>推荐亏损档位：<b>${r.loss_tiers.map((t) => `-${t}%`).join(" / ")}</b> <span class="muted small">（当前：${esc(curTiers)}）</span></p>
      <ul class="rec-list small">${notes}</ul>
      ${r.total_stop_pct ? `<p>跟单总止损：<b>净投入 × ${num(r.total_stop_pct * 100, 0)}%</b> ${help("rec_stop")} <span class="muted small">他近 90 天最大回撤 ${num(r.mdd_90d, 1)}%；当前 ${num((cur.recommend_total_stop_pct || 0) * 100, 0)}%</span></p>`
        : `<p class="muted small">${r.outdated ? OUTDATED : "查不到他的近 90 天最大回撤（不在币安公开榜单上），总止损比例不做推荐。"}</p>`}
      ${hs ? `<p class="small">硬止损参考（回测）：${esc(hs.label)}，累计 ${signed(hs.total)}（不设止损 ${signed(hs.baseline_total)}），会误杀 ${hs.winners_cut} 笔盈利单${hs.better ? "" : "；<b>不如不设</b>，这个领航员更适合只用提醒 + 总止损兜底"}。</p>` : ""}
      ${r.my_note ? `<p class="msg">${esc(r.my_note)}</p>` : ""}
      <div class="actions">${String(cur.loss_tiers) === String(r.loss_tiers) && cur.loss_tiers_mode !== "price" && (!r.total_stop_pct || Math.abs(cur.recommend_total_stop_pct - r.total_stop_pct) < 1e-6)
        ? '<span class="badge green">✓ 已采用</span>' : `<button class="btn primary sm" data-adopt="stops">采用档位${r.total_stop_pct ? "和总止损比例" : ""}</button>`}</div>
      <p class="hint">基于 ${r.n_paths} 笔算出浮亏的历史仓位。只对这个领航员生效，可在 设置 → 风控参数 → 按领航员单独设置 里修改或恢复全局。</p></div>
    <div class="card"><h3>盈利保护 ${help("profit_guard")}</h3>
      ${profitGuard(pg, cur, r.outdated)}
      <h3 style="margin-top:16px">分批止盈阶梯 ${help("ladder")}</h3>
      ${curLad.length ? `<p>当前已开启：<b>${esc(ladderText(curLad))}</b>，其余跟随。浮盈到位时提醒你在币安手动止盈。</p>` : ""}
      ${lad ? `<p>推荐：<b>${esc(lad.label)}</b></p><p class="small">回测累计 ${signed(lad.total)}，不止盈 ${signed(lad.baseline)}（多 ${signed(lad.gain)}）；会少赚的大单 ${lad.winners_trimmed} 笔。用前 70% 历史选出、后 30% 验证仍更好。</p>
        <div class="actions"><button class="btn primary sm" data-adopt="ladder">采用止盈阶梯</button></div>`
        : `<p class="muted small">${r.outdated ? OUTDATED : `暂不推荐：${(r.ladder_candidates || []).length ? "候选方案在样本外没有比“不止盈”更好，这个领航员更适合跟到底。" : "历史仓位还不够做止盈回测（至少需要 10 笔算出浮盈的仓位，且样本外检验需要 30 笔以上）。"}`}</p>`}
      ${(r.ladder_candidates || []).length ? `<div class="tbl-wrap"><table><thead><tr><th>候选方案</th><th class="r">累计</th><th class="r">比不止盈</th></tr></thead><tbody>
        ${r.ladder_candidates.map((c) => `<tr><td class="small">${esc(c.label)}</td><td class="r num">${signed(c.total)}</td><td class="r"><span class="num ${tone(rnd(c.gain, 2))}">${signed(c.gain)}</span></td></tr>`).join("")}</tbody></table></div>` : ""}
      ${curLad.length ? '<div class="actions"><button class="btn sm ghost" data-adopt="clear_ladder">关闭止盈提醒</button></div>' : ""}
      <p class="hint">收益单位 = 每笔投入 1 份保证金的倍数。止盈提醒默认不发邮件（设置 → 邮件提醒 里可以勾选“分批止盈到位”）。</p></div></div>`;
}

const near = (a, b) => a != null && b != null && Math.abs(a - b) < 1e-6;
const guardAdopted = (p, cur) => (p.be_trigger == null || near(p.be_trigger, cur.breakeven_trigger_roi))
  && (p.trail_gap == null || (near(p.trail_gap, cur.trail_gap_roi) && near(p.trail_trigger, cur.trail_trigger_roi)));
function profitGuard(pg, cur, outdated) {
  const now = `当前：浮盈 ${num(cur.breakeven_trigger_roi * 100, 0)}% 建议保本；浮盈 ≥${num(cur.trail_trigger_roi * 100, 0)}% 后回落 ${num(cur.trail_gap_roi * 100, 0)} 个点提醒止盈`;
  if (!pg) return `<p class="muted small">${outdated ? OUTDATED : "回测里样本外最好的方案不含保本 / 回撤止盈，这个领航员不需要额外的盈利保护。"}</p><p class="muted small">${now}</p>`;
  const cmp = `全部历史累计 ${signed(pg.total)}（原始跟单 ${signed(pg.baseline_total)}），最大回撤 ${num(pg.max_dd)}（原始 ${num(pg.baseline_dd)}），会误杀 ${pg.winners_cut} 笔盈利单；
    前 ${pg.n_train} 笔选出、后 ${pg.n_test} 笔检验 ${signed(pg.test_total)}（原始 ${signed(pg.test_baseline)}）。`;
  return `<p>${pg.adoptable ? "推荐" : "回测最好"}：<b>${esc(pg.label)}</b></p><p class="small">${cmp}</p>
    ${pg.adoptable ? `<div class="actions">${guardAdopted(pg.params, cur) ? '<span class="badge green">✓ 已采用</span>' : '<button class="btn primary sm" data-adopt="profit">采用盈利保护</button>'}</div>
      <p class="hint">采用后，浮盈到位时提醒你在币安把止损挪到保本、回落时手动止盈（币安跟单仓位不支持移动止盈）。这两类提醒默认不发邮件，可在 设置 → 邮件提醒 勾选“浮盈达标建议保本”“浮盈回撤”。</p>`
      : `<p class="muted small">${pg.has_stop ? "这个方案带单仓硬止损，和“亏损只提醒”的建议冲突，不提供一键采用。" : "样本外没有比原始跟单更好，暂不推荐。"}</p>`}
    <p class="muted small">${now}</p>`;
}

/* ---------- 仓位时间线 ---------- */
function timeline(points, events) {
  if (points.length < 2) return `<div class="empty">持仓时间太短，还没有足够的采样点（每 5 分钟一个）</div>`;
  const W = 800, H = 220, P = 30;
  const xs = points.map((p) => p.ts), ys = points.map((p) => p.roi);
  const x0 = Math.min(...xs), x1 = Math.max(...xs, ...events.map((e) => e.ts).filter((t) => t <= Math.max(...xs) + 3600));
  let y0 = Math.min(...ys, 0), y1 = Math.max(...ys, 0); if (y1 - y0 < 1e-9) { y0 -= 0.1; y1 += 0.1; }
  const X = (t) => P + ((t - x0) / (x1 - x0 || 1)) * (W - P * 2), Y = (v) => H - P - ((v - y0) / (y1 - y0)) * (H - P * 2);
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(p.ts).toFixed(1)},${Y(p.roi).toFixed(1)}`).join("");
  const marks = events.filter((e) => e.ts >= x0 && e.ts <= x1).map((e) => {
    const cls = e.type.startsWith("alert") ? "mk-alert" : e.type.startsWith("exit") || e.type === "position.close" ? "mk-exit" : "mk-pos";
    return `<line class="${cls}" x1="${X(e.ts)}" x2="${X(e.ts)}" y1="${P}" y2="${H - P}"><title>${dt(e.ts)} ${esc(e.title)}</title></line>`;
  }).join("");
  return `<svg class="chart tall" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <line class="zero" x1="${P}" x2="${W - P}" y1="${Y(0)}" y2="${Y(0)}"/>${marks}<path class="ln" d="${d}"/>
    <text x="${P}" y="14">${dt(x0)}</text><text x="${W - P}" y="14" text-anchor="end">${dt(x1)}</text>
    <text x="${P}" y="${H - 8}">ROI 最低 ${signed(Math.min(...ys) * 100, 1)}%</text><text x="${W - P}" y="${H - 8}" text-anchor="end">最高 ${signed(Math.max(...ys) * 100, 1)}%</text></svg>
    <p class="hint">折线是这个仓位的 ROI；竖线是事件：<span class="mk-pos-t">蓝 = 开仓 / 加减仓</span>、<span class="mk-alert-t">黄 = 提醒</span>、<span class="mk-exit-t">红 = 离场</span>。鼠标移上去看详情。</p>`;
}
async function pageEpisode(id) {
  const gen = S.gen;
  const [ov, d] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/episode?id=" + encodeURIComponent(id))]);
  if (stale(gen)) return;
  const e = d.episode, lp = d.leader_position;
  root.innerHTML = shell("records", `${esc(e.leader_name)} · ${esc(e.symbol)} ${side(e.side)}`, statusChips(ov), `
    <div class="card"><div class="card-h"><a href="#/records/episodes" class="small">‹ 仓位回合</a>
      <div class="actions"><a class="btn sm ghost" href="${esc(d.link)}" target="_blank" rel="noopener">币安 ↗</a></div></div>
      <div class="tags">${e.status === "open" ? '<span class="badge green">持仓中</span>' : `<span class="badge">${esc(e.exit_type || "已平仓")}</span>`}
        ${(e.labels || []).map((l) => `<span class="badge yellow">${esc(l)}</span>`).join(" ")}</div>
      <dl class="kv kv-wide"><dt>开仓</dt><dd>${dt(e.opened_ts)} @ ${num(e.first_entry, 4)}</dd><dt>${e.status === "open" ? "现在" : "平仓"}</dt><dd>${e.status === "open" ? dt(e.last_seen) : dt(e.closed_ts)} · ROI ${pct(e.last_roi)}${e.last_u != null ? " · " + U(e.last_u) : ""}</dd>
        <dt>最深浮亏${help("mae")}</dt><dd>${pct(e.mae_roi)}${e.mae_u != null ? " · " + U(e.mae_u) : ""}（${dt(e.mae_ts)}）</dd><dt>最高浮盈${help("mfe")}</dt><dd>${pct(e.mfe_roi)}${e.mfe_u != null ? " · " + U(e.mfe_u) : ""}（${dt(e.mfe_ts)}）</dd>
        <dt>杠杆 / 最大数量</dt><dd>${num(e.leverage, 0)}x / ${num(e.max_qty, 4)}</dd><dt>加仓 / 减仓</dt><dd>${e.adds} / ${e.reduces}（盈利中减仓 ${e.reduces_in_profit}）</dd>
        <dt>最高亏损档位</dt><dd>${e.max_tier ? "第 " + e.max_tier + " 档" : "-"} · 提醒 ${e.alerts} 次</dd>
        <dt>领航员对应仓位</dt><dd>${lp ? `${day(lp.opened)} 开 · ${lp.closed_ms ? `${day(lp.closed_ms)} 平 @ ${num(lp.avg_close, 4)}` : "还没平"}` : "还没找到（领航员平仓后会出现）"}</dd></dl></div>
    <div class="section card"><h3>ROI 走势</h3>${timeline(d.points, d.events)}</div>
    <div class="section card"><h3>离场判定</h3>${exitsTable(d.exits, false)}</div>
    <div class="section card"><div class="card-h"><h3>事件</h3><a class="btn sm" id="allEv">在事件日志里查看</a></div>
      ${d.events.length ? `<div class="log">${d.events.map((x) => `<div class="ev"><div class="tm">${dt(x.ts)}<br><span class="badge ${x.level === "urgent" ? "red" : x.level === "warn" ? "yellow" : ""}">${esc(evLabel(x.type))}</span></div><div>${esc(x.title)}</div></div>`).join("")}</div>` : `<div class="empty">没有事件</div>`}</div>`);
  document.getElementById("allEv").onclick = () => { S.evFilter = { episode: String(e.id) }; location.hash = "#/records/events"; };
  bindExitsTable(() => pageEpisode(id).then(bindShell));
}

/* ---------- 设置：每日摘要 / 自动备份 ---------- */
function maintForm(st) {
  const m = st.maintenance;
  return `<form class="card" id="maintForm"><h3>每日摘要与自动备份</h3>
    <div class="row">
      <label class="f"><span><label class="switch"><input type="checkbox" name="digest_enabled" ${m.digest_enabled ? "checked" : ""}> 每日摘要邮件</label></span>
        <input name="digest_time" type="time" value="${esc(m.digest_time)}"><div class="hint">昨天的盈亏、各领航员表现、风险仓位、待办，每天一封</div></label>
      <label class="f"><span><label class="switch"><input type="checkbox" name="backup_enabled" ${m.backup_enabled ? "checked" : ""}> 每天自动备份</label></span>
        <input name="backup_time" type="time" value="${esc(m.backup_time)}"><div class="hint">数据库和设置，保留最近 7 份（/opt/bian/data/backups）</div></label>
    </div><button class="btn primary">保存</button><div id="maintMsg"></div></form>`;
}
function bindMaintForm() {
  const f = document.getElementById("maintForm");
  if (!f) return;
  f.onsubmit = async (e) => {
    e.preventDefault();
    const d = new FormData(f), box = document.getElementById("maintMsg");
    try {
      await api("PUT", "/api/settings", { maintenance: { digest_enabled: d.get("digest_enabled") === "on", digest_time: d.get("digest_time"),
        backup_enabled: d.get("backup_enabled") === "on", backup_time: d.get("backup_time") } });
      box.innerHTML = `<div class="msg ok">✓ 已保存</div>`;
    } catch (err) { box.innerHTML = `<div class="msg err">${esc(err.message)}</div>`; }
  };
}

boot();

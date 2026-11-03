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
  severity: "邮件级别：信息（开仓、启动汇总等）、提醒（第 1 档亏损、回撤等）、紧急（更高档亏损、凭证失效等）。凭证失效邮件无论如何都会发。",
};
const help = (k) => HELP[k] ? `<span class="help" data-help="${k}" tabindex="0" role="button" aria-label="说明">?</span>` : "";

/* ---------------- 工具 ---------------- */
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (x, d = 2) => (x == null || isNaN(x) ? "-" : Number(x).toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d }));
const signed = (x, d = 2) => (x == null || isNaN(x) ? "-" : (x > 0 ? "+" : "") + num(x, d));
const U = (x, d = 2) => (x == null ? "-" : `<span class="num ${x > 0 ? "up" : x < 0 ? "down" : ""}">${signed(x, d)}U</span>`);
const pct = (x, d = 1) => (x == null ? "-" : `<span class="num ${x > 0 ? "up" : x < 0 ? "down" : ""}">${signed(x * 100, d)}%</span>`);
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
const NAV = [["war", "📊", "作战室"], ["positions", "📈", "持仓"], ["records", "🗂️", "记录"], ["settings", "⚙️", "设置"]];
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
    ${kpi("总权益", `${num(t.equity)}U`, `净投入 ${num(t.invested, 0)}U`, "equity")}
    ${kpi("今日盈亏", t.today_pnl == null ? "-" : U(t.today_pnl), "按跟单权益变化计算")}
    ${kpi("本月盈亏", t.month_pnl == null ? "-" : U(t.month_pnl), "扣除了追加 / 赎回")}
    ${kpi("当前浮动盈亏", U(t.unrealized), `${t.positions} 个持仓`, "unrealized")}
    ${kpi("风险最大的仓位", worst ? U(worst.unrealized_pnl) : "-", worst ? `${esc(worst.leader_name)} · ${esc(worst.symbol)} ${side(worst.side)}` : "暂无")}
    ${kpi("今日提醒", `${(al.urgent || 0) + (al.warn || 0) + (al.info || 0)}`, `紧急 ${al.urgent || 0} · 提醒 ${al.warn || 0}`)}
  </div>`;

  const leaders = ov.accounts.length ? `<div class="grid leaders">${ov.accounts.map((a) => {
    const dd = Math.min(1, a.drawdown / (S.settings.rules.account_dd_severe || 0.2));
    return `<div class="card leader ${a.light}">
      <div class="name"><span class="dot ${a.light}"></span>${esc(a.name)}${a.is_paused ? '<span class="badge">已暂停</span>' : ""}</div>
      <dl class="kv">
        <dt>权益${help("equity")}</dt><dd class="num">${num(a.equity)}U</dd>
        <dt>累计盈亏</dt><dd>${U(a.pnl)}</dd>
        <dt>回撤${help("drawdown")}</dt><dd class="num">${num(a.drawdown * 100, 1)}%</dd>
        <dt>总止损${help("native_stop")}</dt><dd>${a.native_stop_u ? `<span class="badge green">已设 ${num(a.native_stop_u, 0)}U</span>` : `<span class="badge yellow">未设置 · 建议 ${num(a.recommended_stop_u, 0)}U</span>`}</dd>
        <dt>持仓</dt><dd>${a.positions} 个</dd>
      </dl>
      <div class="bar"><i class="${a.light}" style="width:${Math.round(dd * 100)}%"></i></div>
      <div class="actions"><a class="btn sm" href="#/positions">看持仓</a><a class="btn sm ghost" href="${esc(a.link)}" target="_blank" rel="noopener">币安页面 ↗</a></div>
    </div>`;
  }).join("")}</div>`
    : `<div class="card empty">${ov.credentials.state === "valid" ? "暂时没有进行中的跟单" : "设置币安凭证后，这里会显示你的每个跟单"}</div>`;

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
  const mineList = sorted.filter((p) => p.source === "copy"), publicList = sorted.filter((p) => p.source !== "copy");
  const tierBadge = (t) => (t ? `<span class="badge ${t >= 2 ? "red" : "yellow"}">第 ${t} 档</span>` : `<span class="badge">正常</span>`);
  const rows = (ps) => ps.map((p) => {
    const ep = p.side !== "HEDGE" && p.episodes && p.episodes[0];
    const mine = p.source === "copy";
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
      <td data-l=""><a class="btn sm" href="${esc(p.link)}" target="_blank" rel="noopener">币安 ↗</a></td></tr>`;
    const legs = (p.legs || []).map((l) => `<tr class="sub"><td data-l="">└ ${side(l.side)}单</td><td data-l="开仓价">${num(l.entry_price, 4)}</td>
      <td data-l="保证金" class="r">${num(l.margin)}U</td><td data-l="浮动盈亏" class="r">${U(l.unrealized_pnl)}</td><td data-l="ROI" class="r">${pct(l.roi)}</td>
      <td colspan="5" class="hide-m"></td></tr>`).join("");
    return main + legs;
  }).join("");
  const table = (ps, emptyText) => ps.length ? `<div class="tbl-wrap"><table class="cards"><thead><tr>
      <th>领航员</th><th>币种 / 方向</th><th class="r">保证金</th><th class="r">浮动盈亏</th><th class="r">ROI${help("roi")}</th>
      <th>亏损档位${help("tier")}</th><th class="r">最深浮亏${help("mae")}</th><th class="r">最高浮盈${help("mfe")}</th><th class="r">持仓时长</th><th></th></tr></thead>
      <tbody>${rows(ps)}</tbody></table></div>`
    : `<div class="empty">${emptyText}</div>`;
  if (stale(gen)) return;
  root.innerHTML = shell("positions", "持仓", statusChips(ov), `
    <div class="card"><div class="card-h"><h2>我的跟单仓位（${mineList.length}）</h2><span class="muted small">每 15 秒自动刷新 · 数据时间 ${dt(pv.updated)}</span></div>
    ${table(mineList, ov.credentials.state === "valid" ? "当前没有持仓" : "设置币安凭证后显示你的跟单仓位")}
    <p class="hint">同一个币同时有多单和空单时合并为一行“多空对冲”，下面列出两条腿。亏损档位按合并后的净亏损计算。</p></div>
    ${publicList.length ? `<div class="card"><div class="card-h"><h2>观察中的领航员公开持仓（${publicList.length}）</h2><span class="muted small">领航员本人的仓位，不是你的钱</span></div>${table(publicList)}</div>` : ""}`);
}

/* ---------------- 记录（提醒 / 日志 / 仓位回合） ---------------- */
const EV_LABEL = { position: "仓位", alert: "提醒", credential: "凭证", notify: "邮件", settings: "设置", data: "数据", leader: "跟单", service: "服务", auth: "登录", todo: "待办", web: "后台" };
const evLabel = (t) => EV_LABEL[String(t).split(".")[0]] || t;
const EV_TYPES = [["", "全部类型"], ["position.", "仓位变化"], ["alert.", "提醒"], ["credential.", "凭证"], ["notify.", "邮件投递"],
  ["settings.", "设置修改"], ["data.", "数据中断/恢复"], ["leader.", "跟单增减"], ["service.", "服务"], ["auth.", "登录"]];
async function pageRecords(tab) {
  const gen = S.gen;
  const ov = await api("GET", "/api/overview");
  const tabs = [["alerts", "提醒记录"], ["events", "事件日志"], ["episodes", "仓位回合"]]
    .map(([k, t]) => `<a href="#/records/${k}" class="${k === tab ? "on" : ""}">${t}</a>`).join("");
  let body = "";
  if (tab === "alerts") {
    const { alerts } = await api("GET", "/api/alerts?limit=100");
    body = alerts.length ? `<div class="log">${alerts.map((a) => `<div class="ev">
        <div class="tm">${dt(a.ts)}<br><span class="badge ${a.severity === "urgent" ? "red" : a.severity === "warn" ? "yellow" : ""}">${{ urgent: "紧急", warn: "提醒", info: "信息" }[a.severity] || a.severity}</span></div>
        <div><b>${esc(a.title)}</b>
          <div class="small ${a.delivered ? "muted" : "down"}">${a.delivered ? "✓ 已送达" : "✗ 未送达"}${a.error ? " · " + esc(a.error) : ""}</div>
          <details><summary class="small muted">查看完整内容</summary><pre>${esc(a.message)}</pre></details></div></div>`).join("")}</div>`
      : `<div class="empty">还没有提醒</div>`;
  } else if (tab === "events") {
    body = `<div class="filters">
        <select id="evType">${EV_TYPES.map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select id="evLevel"><option value="">全部级别</option><option value="urgent">紧急</option><option value="warn">提醒</option><option value="info">信息</option></select>
        <input id="evQ" placeholder="搜索（币种、领航员…）" style="max-width:220px">
        <button class="btn" id="evGo">筛选</button></div>
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
        <td data-l=""><button class="btn sm" data-ep="${e.id}">时间线</button></td></tr>`).join("")}</tbody></table></div>
      <p class="hint">“利润回吐”：最高浮盈 ≥30% 但最后不到一半；“盈转亏”：最高浮盈 ≥30% 却亏损出场。这些数据会用于之后的分批止盈建议。</p>`
      : `<div class="empty">仓位回合从安装后开始记录，有持仓后这里会出现</div>`;
  }
  if (stale(gen)) return;
  root.innerHTML = shell("records", "记录", statusChips(ov), `<div class="card"><div class="tabs">${tabs}</div>${body}</div>`);
  if (tab === "events") bindEvents();
  root.querySelectorAll("[data-ep]").forEach((b) => (b.onclick = () => { S.evFilter = { episode: b.dataset.ep }; location.hash = "#/records/events"; }));
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
  const q = new URLSearchParams({ limit: 100, type: val("evType"), level: val("evLevel"), q: val("evQ"), ...(extra || {}) });
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
  const [ov, st] = await Promise.all([api("GET", "/api/overview"), api("GET", "/api/settings")]);
  S.settings = st;
  const tabs = [["credentials", "币安凭证"], ["email", "邮件提醒"], ["rules", "风控参数"], ["account", "账号与系统"]]
    .map(([k, t]) => `<a href="#/settings/${k}" class="${k === tab ? "on" : ""}">${t}</a>`).join("");
  let body = "";
  if (tab === "credentials") body = credStatus(st) + credForm();
  if (tab === "email") body = emailForm();
  if (tab === "rules") body = rulesForm(st);
  if (tab === "account") body = renderChangePassword(false) + `<div class="card"><h3>系统</h3>
      <p class="muted small">版本 v${esc(S.me.version)}。更新：在服务器执行 <code>cd /opt/bian && ./start.sh update</code>（几秒钟，数据和设置都会保留）。</p>
      <p class="muted small">查看运行日志：<code>./start.sh logs</code>；自检：<code>./start.sh doctor</code>。</p></div>`;
  if (stale(gen)) return;
  root.innerHTML = shell("settings", "设置", statusChips(ov), `<div class="card"><div class="tabs">${tabs}</div>${body}</div>`);
  if (tab === "credentials") bindCredForm(() => pageSettings("credentials").then(bindShell));
  if (tab === "email") bindEmailForm();
  if (tab === "rules") bindRulesForm();
  if (tab === "account") bindPassword(false);
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
    <div class="row">
      <label class="f"><span>发送哪些提醒 ${help("severity")}</span><select name="email_min_severity">
        <option value="warn" ${n.email_min_severity === "warn" ? "selected" : ""}>提醒和紧急（推荐）</option>
        <option value="urgent" ${n.email_min_severity === "urgent" ? "selected" : ""}>只发紧急</option>
        <option value="info" ${n.email_min_severity === "info" ? "selected" : ""}>全部（包括开仓等信息）</option></select></label>
      <label class="f"><span>每小时最多几封</span><input name="email_max_per_hour" type="number" min="1" max="120" value="${esc(n.email_max_per_hour)}"><div class="hint">同一轮的多条提醒会合并成一封</div></label>
    </div>
    <div class="actions"><button class="btn primary" type="submit">保存</button><button class="btn" type="button" id="emailTest">发送测试邮件</button></div>
    <div id="emailMsg"></div>
    <p class="hint">阿里云等云服务器默认封禁 25 端口，请用 465 或 587。测试邮件使用表单里的内容，不会保存。</p>
  </form>`;
}
function emailPayload(f) {
  const d = new FormData(f);
  const p = { notify: { email_enabled: d.get("email_enabled") === "on", smtp_host: d.get("smtp_host").trim(), smtp_port: Number(d.get("smtp_port") || 465),
    smtp_security: d.get("smtp_security"), smtp_user: d.get("smtp_user").trim(), smtp_to: d.get("smtp_to").trim(),
    email_min_severity: d.get("email_min_severity"), email_max_per_hour: Number(d.get("email_max_per_hour") || 12) } };
  if (d.get("smtp_pass")) p.smtp_pass = d.get("smtp_pass");
  return p;
}
function bindEmailForm(after) {
  const f = document.getElementById("emailForm");
  if (!f) return;
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
const OVERRIDE_KEYS = ["loss_tiers", "loss_tiers_mode", "min_alert_u", "recommend_total_stop_pct"];
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
      `<tr><td>${esc(names[id] || id)}</td><td class="small">${Object.entries(o).map(([k, v]) => `${esc(k)} = ${esc(Array.isArray(v) ? v.join("/") : v)}`).join("；")}</td>
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
  of.onsubmit = async (e) => {
    e.preventDefault();
    const id = of.elements.leader.value, box = document.getElementById("ovMsg");
    if (!id) return;
    const lr = { ...(S.settings.leader_rules || {}) };
    lr[id] = readRules(of, "o_", OVERRIDE_KEYS);
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

/* ---------------- 入口 ---------------- */
boot();

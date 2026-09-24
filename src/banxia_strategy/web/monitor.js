const byId = (id) => document.getElementById(id);
let latest = null;
let selected = null;
let signature = "";
let inFlight = false;
let connected = false;
let serverOffset = 0;
let lastPageSync = 0;

function write(id, value) { byId(id).textContent = value ?? "—"; }
function numeric(value) { return value !== null && value !== undefined && Number.isFinite(Number(value)); }
function price(value) { return numeric(value) ? Number(value).toFixed(2) : "—"; }
function percent(value) { return numeric(value) ? `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(2)}%` : "—"; }
function clock(value) {
  return value ? new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value)) : "—";
}
function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}
function changeClass(value) { return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : ""; }
function originLabel(stock) {
  return stock.origin === "supplement"
    ? `盘中补充${stock.plan.eligible === false ? " · 仅观察" : ""}`
    : `日报入选${numeric(stock.score) ? ` · ${stock.score}分` : ""}`;
}

function renderRows(data) {
  const rows = byId("watch-rows");
  rows.replaceChildren();
  if (!data.stocks.length) {
    const row = element("tr");
    const cell = element("td", data.error || "等待后台首次采集，行情到达后自动显示。", "waiting-cell");
    cell.colSpan = 6;
    row.append(cell);
    rows.append(row);
    return;
  }
  for (const stock of data.stocks) {
    const row = element("tr");
    row.classList.toggle("selected", stock.code === selected);
    const identity = element("td");
    const button = element("button", undefined, "stock-select");
    button.type = "button";
    button.setAttribute("aria-pressed", String(stock.code === selected));
    button.setAttribute("aria-label", `查看${stock.name}分时与执行细则`);
    button.append(
      element("strong", stock.name), element("small", stock.code),
      element("small", originLabel(stock)),
    );
    button.addEventListener("click", () => {
      selected = stock.code;
      renderRows(latest);
      renderDetail(latest.stocks.find((item) => item.code === selected));
      byId("stock-detail").scrollIntoView({ behavior: "smooth", block: "nearest" });
      byId("watch-rows").querySelectorAll("button")[latest.stocks.findIndex((item) => item.code === selected)]?.focus({ preventScroll: true });
    });
    identity.append(button);
    const q = stock.quote;
    const decision = element("td");
    const label = element("span", stock.advice.label, "advice-label");
    label.dataset.tone = stock.advice.tone;
    decision.append(label, element("span", stock.advice.reason, "advice-reason"));
    row.append(
      identity, element("td", price(q.price), `quote-value ${changeClass(q.change_pct)}`),
      element("td", percent(q.change_pct), `change-value ${changeClass(q.change_pct)}`),
      element("td", price(q.open), "quote-small"), element("td", clock(q.quote_time), "quote-small"), decision,
    );
    rows.append(row);
  }
}

function svgNode(tag, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderChart(stock) {
  const root = byId("chart");
  root.replaceChildren();
  const candles = stock.quote.candles || [];
  if (!candles.length) {
    root.append(element("div", "暂无可核验的当日分钟成交数据", "chart-empty"));
    return;
  }
  const reference = stock.plan.previous_close;
  const values = candles.map((candle) => candle.price).concat(reference);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const padding = Math.max((high - low) * 0.2, reference * 0.003);
  const yMin = low - padding;
  const yMax = high + padding;
  const width = 560;
  const y = (value) => 12 + (yMax - value) / (yMax - yMin) * 128;
  const x = (stamp) => {
    const time = new Date(stamp);
    // API 时间始终带 +08:00，按上海时区计算交易分钟。
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(time).split(":").map(Number);
    const minutes = parts[0] * 60 + parts[1];
    const tradingMinute = minutes <= 690 ? minutes - 570 : 120 + minutes - 780;
    return 8 + Math.max(0, Math.min(240, tradingMinute)) / 240 * 490;
  };
  const svg = svgNode("svg", { viewBox: `0 0 ${width} 170`, role: "img", "aria-label": `${stock.name}当日分钟收盘价走势，虚线为昨收${price(reference)}元` });
  for (const value of [low, high]) {
    svg.append(svgNode("line", { x1: 8, x2: 500, y1: y(value), y2: y(value), stroke: "#ddd7cb" }));
    svg.append(svgNode("text", { x: 508, y: y(value) + 3, fill: "#68716b", "font-size": 10 }, price(value)));
  }
  svg.append(svgNode("line", { x1: 8, x2: 500, y1: y(reference), y2: y(reference), stroke: "#8c958d", "stroke-dasharray": "4 4" }));
  const points = candles.map((candle) => `${x(candle.time)},${y(candle.price)}`).join(" ");
  const color = candles.at(-1).price >= reference ? "#a33c32" : "#116149";
  svg.append(svgNode("polyline", { points, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round" }));
  const last = candles.at(-1);
  svg.append(svgNode("circle", { cx: x(last.time), cy: y(last.price), r: 3, fill: color }));
  for (const [offset, text] of [[8, "09:30"], [228, "11:30 / 13:00"], [466, "15:00"]]) {
    svg.append(svgNode("text", { x: offset, y: 164, fill: "#68716b", "font-size": 10 }, text));
  }
  root.append(svg);
}

function renderDetail(stock) {
  byId("stock-detail").hidden = !stock;
  if (!stock) return;
  const q = stock.quote;
  const p = stock.plan;
  write("detail-name", stock.name);
  write("detail-code", `${stock.code} · 分时与执行细则`);
  write("detail-industry", stock.industry);
  write("detail-advice", stock.advice.label);
  byId("detail-advice").dataset.tone = stock.advice.tone;
  write("detail-reason", stock.advice.reason);
  write("detail-provenance", `${originLabel(stock)} · 昨收日期 ${stock.reference_date ?? "—"} · 计划交易日 ${stock.plan_date ?? "—"}`);
  write("detail-eligibility", p.eligibility_reason);
  byId("detail-eligibility").dataset.tone = p.eligible === false ? "risk" : "muted";
  write("detail-high", price(q.high));
  write("detail-low", price(q.low));
  write("detail-amount", numeric(q.amount) ? `${(q.amount / 100000000).toFixed(2)}亿` : "—");
  write("detail-volume", numeric(q.minute_volume_ratio) ? `${q.minute_volume_ratio.toFixed(2)}×` : "样本不足");
  write("detail-close", `${price(p.previous_close)}元`);
  write("detail-auction", `${price(p.auction_low)}～${price(p.auction_high)}元`);
  write("detail-limit", `${price(p.limit_price)}元`);
  write("detail-position", numeric(p.position_limit_pct) ? `${p.position_limit_pct}%（上限，非目标）` : "—");
  write("detail-entry", p.entry_trigger);
  write("detail-cancel", p.invalidation);
  write("bar-time", `分钟线时间 ${clock(q.bar_time)}`);
  renderChart(stock);
}

function renderEvents(events) {
  const root = byId("events");
  root.replaceChildren();
  if (!events.length) root.append(element("li", "尚无判断变化", "empty-event"));
  for (const event of events) {
    const row = element("li");
    const time = element("time", clock(event.time));
    time.dateTime = event.time;
    const label = element("span", event.label);
    label.dataset.tone = event.tone;
    row.append(time, element("strong", event.name), label);
    root.append(row);
  }
}

function render(data) {
  latest = data;
  selected = data.stocks.some((stock) => stock.code === selected) ? selected : data.stocks[0]?.code;
  write("phase", data.phase_label);
  write("collected", clock(data.collected_at));
  write("plan-date", data.plan_date);
  const watchlist = data.watchlist || data.stocks;
  write("watch-count", String(data.requested_codes?.length ?? watchlist.length).padStart(2, "0"));
  write("scope", watchlist.map((stock) => stock.name).join(" / "));
  write("watch-heading", `${data.requested_codes?.length ?? watchlist.length}只股票观察清单`);
  write("revision", String(data.revision));
  const fault = data.error || (data.delayed ? "后台采集已延迟，暂停入场判断。" : "");
  const today = data.server_time.slice(0, 10);
  const expired = (data.plan_date && data.plan_date !== today) ||
    data.stocks.some((stock) => stock.plan_date && stock.plan_date !== today);
  const banner = fault || (expired ? "部分股票计划已过期，已暂停其入场判断；请更新对应日报或补充清单。" : "") ||
    (data.log_error ? "行情已更新，但本地日志写入失败。" : "");
  byId("monitor-error").hidden = !banner;
  write("monitor-error", banner);
  write("connection", fault ? "采集异常 · 自动重试" : data.collected_at ? "后台采集中" : "等待首次采集");
  byId("connection").dataset.error = String(Boolean(fault));
  renderRows(data);
  renderDetail(data.stocks.find((stock) => stock.code === selected));
  renderEvents(data.events);
  if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    byId("collected").animate([{ background: "#dfece5" }, { background: "transparent" }], { duration: 700 });
  }
}

function markDisconnected(message) {
  connected = false;
  signature = "";
  if (latest) {
    for (const stock of latest.stocks) {
      stock.advice = {
        state: "disconnected", label: "连接中断 · 暂停判断",
        reason: "等待连接恢复，不使用旧判断入场。", tone: "risk",
      };
      stock.quote.fresh = false;
    }
  }
  byId("monitor-error").hidden = false;
  write("monitor-error", `${message}；页面每5秒重连，旧报价仅供核对。`);
  write("connection", "连接中断 · 暂停判断");
  byId("connection").dataset.error = "true";
  for (const label of document.querySelectorAll(".advice-label, #detail-advice")) {
    label.textContent = "连接中断 · 暂停判断";
    label.dataset.tone = "risk";
  }
  for (const reason of document.querySelectorAll(".advice-reason, #detail-reason")) {
    reason.textContent = "等待连接恢复，不使用旧判断入场。";
  }
}

async function sync() {
  if (inFlight) return;
  inFlight = true;
  byId("sync-button").disabled = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch("/api/monitor", { cache: "no-store", signal: controller.signal });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`);
    if (!Array.isArray(data.stocks)) throw new Error("监控返回格式不正确");
    serverOffset = new Date(data.server_time).valueOf() - Date.now();
    lastPageSync = Date.now();
    connected = true;
    const nextSignature = JSON.stringify([
      data.revision, data.delayed, data.phase, data.error,
      data.stocks.map((stock) => stock.advice.state),
    ]);
    if (signature !== nextSignature) {
      render(data);
      signature = nextSignature;
    } else {
      latest = data;
    }
  } catch (error) {
    markDisconnected(error.name === "AbortError" ? "请求超时" : error.message);
  } finally {
    clearTimeout(timeout);
    inFlight = false;
    byId("sync-button").disabled = false;
  }
}

setInterval(() => {
  if (connected && Date.now() - lastPageSync > 15000) markDisconnected("页面同步超时");
  if (!connected) { write("countdown", "正在重连监控服务"); return; }
  const remaining = latest?.next_poll_at ? Math.ceil((new Date(latest.next_poll_at).valueOf() - Date.now() - serverOffset) / 1000) : null;
  write("countdown", remaining === null ? "首次采集中" : remaining <= 0 ? "正在等待本轮采集结果" : `下次行情采集 ${remaining}秒 · 页面每5秒同步`);
}, 1000);
setInterval(sync, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });
byId("sync-button").addEventListener("click", sync);
sync();

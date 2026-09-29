const byId = (id) => document.getElementById(id);
const reportDateInput = byId("report-date");
const refreshButton = byId("refresh-button");
const sourceStatus = document.querySelector(".source-status");
let latest = null;
let selected = null;
let connected = false;
let inFlight = false;
let refreshing = false;
let serverOffset = 0;
let lastPageSync = 0;
let eventSource = null;
let reconnectTimer = null;
let selectedPeriod = "minute";
let chartRenderVersion = 0;
let displayedChartKey = null;
const klineCache = new Map();
const periodLabels = {
  minute: "分时",
  day: "日K",
  week: "周K",
  month: "月K",
  year: "年K",
};

function write(id, value) { byId(id).textContent = value ?? "—"; }
function numeric(value) { return value !== null && value !== undefined && Number.isFinite(Number(value)); }
function price(value) { return numeric(value) ? Number(value).toFixed(2) : "—"; }
function percent(value) { return numeric(value) ? `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(2)}%` : "—"; }
function dateLabel(value) { return value ? String(value).replaceAll("-", ".") : "—"; }
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
function monitorURL(path) {
  const url = new URL(path, location.origin);
  if (reportDateInput.value) {
    url.searchParams.set("trade_date", reportDateInput.value);
  }
  return window.strategyURL(`${url.pathname}${url.search}`);
}
function changeClass(value) { return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : ""; }
function toneFor(state, irreversible) {
  if (irreversible || ["expired", "stale", "unavailable", "reject_open", "reject_low", "outside_open", "window_closed"].includes(state)) return "risk";
  if (["sealed", "at_limit", "near_limit"].includes(state)) return "focus";
  return "muted";
}
function originLabel(stock) {
  return stock.origin === "supplement"
    ? `盘中补充${stock.plan.eligible === false ? " · 仅观察" : ""}`
    : `日报入选${numeric(stock.score) ? ` · ${stock.score}分` : ""}`;
}
function normalize(snapshot, eventPayload) {
  const events = (eventPayload?.items || []).map((item) => ({
    time: item.occurred_at,
    name: snapshot.stocks.find((stock) => stock.symbol === item.symbol)?.name || item.symbol,
    label: item.reason || item.state,
    tone: toneFor(item.state, false),
  }));
  const stocks = snapshot.stocks.map((stock) => {
    const feature = stock.feature || {};
    const plan = {
      ...(stock.plan || {}),
      previous_close: stock.plan?.previous_close ?? stock.previous_close,
      position_limit_pct: stock.plan?.position_limit_pct,
      entry_trigger: stock.plan?.entry_trigger,
      invalidation: stock.plan?.invalidation,
      eligible: stock.eligible,
      eligibility_reason: stock.eligibility_reason,
    };
    return {
      code: stock.symbol,
      name: stock.name,
      industry: stock.industry,
      score: stock.score,
      origin: stock.origin,
      reference_date: stock.reference_date,
      plan_date: stock.plan_date,
      plan,
      quote: {
        price: stock.price,
        open: stock.open,
        high: stock.high,
        low: stock.low,
        previous_close: stock.previous_close,
        change_pct: stock.change_pct,
        open_change_pct: stock.open_change_pct,
        amount: stock.amount_cny,
        volume: stock.volume,
        quote_time: stock.source_time,
        bar_time: stock.candles?.at(-1)?.time,
        candles: stock.candles || [],
        minute_volume_ratio: feature.minute_volume_ratio,
        fresh: snapshot.data_status.state === "fresh",
      },
      advice: {
        ...stock.decision,
        tone: toneFor(stock.decision.state, stock.decision.irreversible),
      },
    };
  });
  return {
    ...snapshot,
    collected_at: stocks.map((stock) => stock.quote.quote_time).filter(Boolean).sort().at(-1),
    delayed: snapshot.data_status.state !== "fresh",
    error: snapshot.data_status.reason === "non_trading_day_fallback"
      ? `所选日期为休市日，展示最近交易日 ${snapshot.trade_date} 的当日实盘数据。`
      : snapshot.data_status.state === "unavailable"
        ? "实时投影尚未就绪，暂停入场判断。"
        : null,
    plan_date: snapshot.trade_date,
    revision: stocks.map((stock) => stock.quote.quote_time || "").sort().at(-1) || "—",
    requested_codes: stocks.map((stock) => stock.code),
    stocks,
    events,
  };
}

function renderRows(data) {
  const list = byId("watch-list");
  list.replaceChildren();
  if (!data.stocks.length) {
    list.append(element("p", data.error || "等待行情投影。", "waiting-cell"));
    return;
  }
  for (const stock of data.stocks) {
    const button = element("button", undefined, "watch-stock");
    button.type = "button";
    button.role = "option";
    button.classList.toggle("selected", stock.code === selected);
    button.setAttribute("aria-selected", String(stock.code === selected));
    button.setAttribute("aria-label", `查看${stock.name}分时与执行细则`);

    const identity = element("div", undefined, "watch-stock-identity");
    identity.append(
      element("strong", stock.name),
      element("small", stock.code),
      element("small", originLabel(stock)),
    );
    const advice = element("span", stock.advice.label, "watch-advice");
    advice.dataset.tone = stock.advice.tone;
    const heading = element("div", undefined, "watch-stock-head");
    heading.append(identity, advice);

    const q = stock.quote;
    const quoteGrid = element("dl", undefined, "watch-quote-grid");
    for (const [label, value, className] of [
      ["最新价", price(q.price), changeClass(q.change_pct)],
      ["涨跌幅", percent(q.change_pct), changeClass(q.change_pct)],
      ["今日开盘", price(q.open), ""],
      ["盘口时间", clock(q.quote_time), ""],
    ]) {
      const metric = element("div");
      metric.append(element("dt", label), element("dd", value, className));
      quoteGrid.append(metric);
    }
    button.append(heading, quoteGrid, element("p", stock.advice.reason, "watch-reason"));
    button.addEventListener("click", () => {
      selected = stock.code;
      renderRows(latest);
      renderDetail(latest.stocks.find((item) => item.code === selected));
    });
    list.append(button);
  }
}

function svgNode(tag, attrs, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  if (text !== undefined) node.textContent = text;
  return node;
}
function renderIntradayChart(stock) {
  const root = byId("chart");
  root.replaceChildren();
  root.dataset.period = "minute";
  write("chart-status", "当日分钟走势");
  write("chart-primary-label", "当日分钟收盘价");
  write("chart-reference-label", "虚线：昨收基准");
  const candles = stock.quote.candles || [];
  const reference = stock.plan.previous_close;
  if (!candles.length || !numeric(reference)) {
    root.append(element("div", "暂无可核验的当日分钟成交数据", "chart-empty"));
    write("bar-time", "分钟线时间 —");
    return;
  }
  const values = candles.map((candle) => Number(candle.price)).concat(Number(reference));
  const low = Math.min(...values);
  const high = Math.max(...values);
  const padding = Math.max((high - low) * 0.2, reference * 0.003);
  const yMin = low - padding;
  const yMax = high + padding;
  const y = (value) => 12 + (yMax - value) / (yMax - yMin) * 128;
  const x = (stamp) => {
    const parts = new Intl.DateTimeFormat("en-GB", {
      timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(new Date(stamp)).split(":").map(Number);
    const minutes = parts[0] * 60 + parts[1];
    const tradingMinute = minutes <= 690 ? minutes - 570 : 120 + minutes - 780;
    return 8 + Math.max(0, Math.min(240, tradingMinute)) / 240 * 490;
  };
  const svg = svgNode("svg", { viewBox: "0 0 560 170", role: "img", "aria-label": `${stock.name}当日分钟走势` });
  for (const value of [low, high]) {
    svg.append(svgNode("line", { x1: 8, x2: 500, y1: y(value), y2: y(value), stroke: "#ddd7cb" }));
    svg.append(svgNode("text", { x: 508, y: y(value) + 3, fill: "#68716b", "font-size": 10 }, price(value)));
  }
  svg.append(svgNode("line", { x1: 8, x2: 500, y1: y(reference), y2: y(reference), stroke: "#8c958d", "stroke-dasharray": "4 4" }));
  const color = candles.at(-1).price >= reference ? "#a33c32" : "#116149";
  svg.append(svgNode("polyline", {
    points: candles.map((candle) => `${x(candle.time)},${y(candle.price)}`).join(" "),
    fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round",
  }));
  root.append(svg);
}

function showKlineTooltip(event, candle, tooltip, root) {
  tooltip.replaceChildren(
    element("strong", dateLabel(candle.date)),
    element("span", `开 ${price(candle.open)}　高 ${price(candle.high)}`),
    element("span", `低 ${price(candle.low)}　收 ${price(candle.close)}`),
    element("span", `量 ${numeric(candle.volume) ? Number(candle.volume).toLocaleString("zh-CN") : "—"}`),
  );
  const bounds = root.getBoundingClientRect();
  const x = Math.max(72, Math.min(bounds.width - 72, event.clientX - bounds.left));
  const y = Math.max(8, event.clientY - bounds.top - 82);
  tooltip.style.left = `${x}px`;
  tooltip.style.top = `${y}px`;
  tooltip.hidden = false;
}

function renderKlineChart(stock, payload) {
  const root = byId("chart");
  root.replaceChildren();
  root.dataset.period = payload.period;
  const candles = (payload.items || []).filter((item) => (
    numeric(item.open) && numeric(item.high) && numeric(item.low) && numeric(item.close)
  ));
  write("chart-status", `${periodLabels[payload.period]} · ${payload.source}`);
  write("chart-primary-label", "红柱上涨 · 绿柱下跌");
  write("chart-reference-label", "下方柱状图：成交量");
  if (!candles.length) {
    root.append(element("div", `暂无${periodLabels[payload.period]}数据`, "chart-empty"));
    write("bar-time", `截至 ${dateLabel(payload.trade_date)}`);
    return;
  }

  const width = 640;
  const plotLeft = 10;
  const plotRight = 580;
  const priceTop = 12;
  const priceBottom = 190;
  const volumeTop = 207;
  const volumeBottom = 250;
  const lows = candles.map((item) => Number(item.low));
  const highs = candles.map((item) => Number(item.high));
  const rawLow = Math.min(...lows);
  const rawHigh = Math.max(...highs);
  const padding = Math.max((rawHigh - rawLow) * 0.06, rawHigh * 0.002);
  const yMin = rawLow - padding;
  const yMax = rawHigh + padding;
  const priceY = (value) => priceTop + (yMax - Number(value)) / (yMax - yMin) * (priceBottom - priceTop);
  const maxVolume = Math.max(...candles.map((item) => Number(item.volume) || 0), 1);
  const band = (plotRight - plotLeft) / candles.length;
  const bodyWidth = Math.max(1, Math.min(8, band * 0.62));
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} 272`,
    role: "img",
    "aria-label": `${stock.name}${periodLabels[payload.period]}，共${candles.length}根`,
  });

  for (const value of [yMax, (yMax + yMin) / 2, yMin]) {
    const lineY = priceY(value);
    svg.append(
      svgNode("line", {
        x1: plotLeft, x2: plotRight, y1: lineY, y2: lineY,
        stroke: "#ddd7cb", "stroke-width": 1,
      }),
      svgNode("text", {
        x: plotRight + 8, y: lineY + 3, fill: "#68716b", "font-size": 10,
      }, price(value)),
    );
  }

  const tooltip = element("div", undefined, "kline-tooltip");
  tooltip.hidden = true;
  candles.forEach((candle, index) => {
    const x = plotLeft + band * (index + 0.5);
    const openY = priceY(candle.open);
    const closeY = priceY(candle.close);
    const rising = Number(candle.close) >= Number(candle.open);
    const color = rising ? "#a33c32" : "#116149";
    const group = svgNode("g", {});
    group.append(
      svgNode("title", {}, `${candle.date} 开${price(candle.open)} 高${price(candle.high)} 低${price(candle.low)} 收${price(candle.close)}`),
      svgNode("line", {
        x1: x, x2: x, y1: priceY(candle.high), y2: priceY(candle.low),
        stroke: color, "stroke-width": 1,
      }),
      svgNode("rect", {
        x: x - bodyWidth / 2,
        y: Math.min(openY, closeY),
        width: bodyWidth,
        height: Math.max(1, Math.abs(closeY - openY)),
        fill: color,
      }),
      svgNode("rect", {
        x: x - bodyWidth / 2,
        y: volumeBottom - (Number(candle.volume) || 0) / maxVolume * (volumeBottom - volumeTop),
        width: bodyWidth,
        height: (Number(candle.volume) || 0) / maxVolume * (volumeBottom - volumeTop),
        fill: color,
        opacity: 0.55,
      }),
    );
    const hit = svgNode("rect", {
      x: plotLeft + band * index,
      y: priceTop,
      width: Math.max(1, band),
      height: volumeBottom - priceTop,
      fill: "transparent",
    });
    hit.addEventListener("pointerenter", (event) => showKlineTooltip(event, candle, tooltip, root));
    hit.addEventListener("pointermove", (event) => showKlineTooltip(event, candle, tooltip, root));
    hit.addEventListener("pointerleave", () => { tooltip.hidden = true; });
    group.append(hit);
    svg.append(group);
  });

  const tickIndexes = [...new Set([0, Math.floor((candles.length - 1) / 2), candles.length - 1])];
  for (const index of tickIndexes) {
    const x = plotLeft + band * (index + 0.5);
    svg.append(svgNode("text", {
      x,
      y: 268,
      fill: "#68716b",
      "font-size": 10,
      "text-anchor": index === 0 ? "start" : index === candles.length - 1 ? "end" : "middle",
    }, candles[index].date.slice(2)));
  }
  root.append(svg, tooltip);
  write(
    "bar-time",
    `${candles.length}根 · ${dateLabel(candles[0].date)} 至 ${dateLabel(candles.at(-1).date)}`,
  );
}

function updatePeriodButtons() {
  document.querySelectorAll("[data-chart-period]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.chartPeriod === selectedPeriod));
  });
}

async function renderSelectedChart(stock) {
  updatePeriodButtons();
  if (selectedPeriod === "minute") {
    displayedChartKey = null;
    renderIntradayChart(stock);
    return;
  }

  const key = `${latest.plan_id}:${latest.plan_date}:${stock.code}:${selectedPeriod}`;
  if (displayedChartKey === key) return;
  const version = ++chartRenderVersion;
  write("chart-status", `正在读取${periodLabels[selectedPeriod]} · mootdx`);
  write("chart-primary-label", "历史行情加载中");
  write("chart-reference-label", "数据源：mootdx");
  write("bar-time", `截至 ${dateLabel(latest.plan_date)}`);
  byId("chart").dataset.period = selectedPeriod;
  byId("chart").replaceChildren(
    element("div", `正在读取${stock.name}${periodLabels[selectedPeriod]}数据`, "chart-empty"),
  );
  let request = klineCache.get(key);
  if (!request) {
    request = fetchJson(
      `/api/v1/monitor/kline/${encodeURIComponent(stock.code)}?period=${encodeURIComponent(selectedPeriod)}&trade_date=${encodeURIComponent(latest.plan_date)}`,
    );
    klineCache.set(key, request);
  }
  try {
    const payload = await request;
    if (version !== chartRenderVersion || selected !== stock.code || selectedPeriod !== payload.period) return;
    displayedChartKey = key;
    renderKlineChart(stock, payload);
  } catch (error) {
    klineCache.delete(key);
    if (version !== chartRenderVersion) return;
    displayedChartKey = null;
    write("chart-status", `${periodLabels[selectedPeriod]}读取失败`);
    byId("chart").replaceChildren(element("div", error.message, "chart-empty"));
  }
}

function renderDetail(stock) {
  byId("stock-detail").hidden = !stock;
  if (!stock) return;
  const q = stock.quote;
  const p = stock.plan;
  write("detail-name", stock.name);
  write("detail-code", `${stock.code} · 行情与执行细则`);
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
  write("detail-volume", numeric(q.minute_volume_ratio) ? `${Number(q.minute_volume_ratio).toFixed(2)}×` : "样本不足");
  write("detail-close", `${price(p.previous_close)}元`);
  write("detail-auction", `${price(p.auction_low)}～${price(p.auction_high)}元`);
  write("detail-limit", `${price(p.limit_price)}元`);
  write("detail-position", numeric(p.position_limit_pct) ? `${p.position_limit_pct}%（上限，非目标）` : "—");
  write("detail-entry", p.entry_trigger);
  write("detail-cancel", p.invalidation);
  renderSelectedChart(stock);
}
function renderEvents(events) {
  const root = byId("events");
  root.replaceChildren();
  if (!events.length) root.append(element("li", "尚无判断变化", "empty-event"));
  for (const event of events) {
    const row = element("li");
    const stamp = element("time", clock(event.time));
    stamp.dateTime = event.time;
    const label = element("span", event.label);
    label.dataset.tone = event.tone;
    row.append(stamp, element("strong", event.name), label);
    root.append(row);
  }
}
function render(data) {
  if (latest && (
    latest.plan_id !== data.plan_id
    || latest.plan_date !== data.plan_date
  )) {
    klineCache.clear();
    displayedChartKey = null;
    chartRenderVersion += 1;
  }
  latest = data;
  selected = data.stocks.some((stock) => stock.code === selected) ? selected : data.stocks[0]?.code;
  write("monitor-date", dateLabel(data.requested_date || data.plan_date));
  write("phase", data.phase_label);
  write("collected", clock(data.collected_at));
  write("plan-date", data.plan_date);
  write("watch-count", String(data.stocks.length).padStart(2, "0"));
  write("scope", data.stocks.map((stock) => stock.name).join(" / "));
  write("watch-heading", `${data.stocks.length}只股票观察清单`);
  write("revision", clock(data.revision));
  byId("monitor-error").hidden = !data.error;
  write("monitor-error", data.error);
  write("connection", data.error ? "数据不可用 · 暂停判断" : "事件流已连接");
  byId("connection").dataset.error = String(Boolean(data.error));
  sourceStatus.className = `source-status ${data.error ? "error" : "ready"}`;
  reportDateInput.value = data.requested_date || data.plan_date;
  renderRows(data);
  renderDetail(data.stocks.find((stock) => stock.code === selected));
  renderEvents(data.events);
}
function markDisconnected(message) {
  connected = false;
  sourceStatus.className = "source-status error";
  byId("monitor-error").hidden = false;
  write("monitor-error", `${message}；页面将自动重连，旧报价仅供核对。`);
  write("connection", "连接中断 · 暂停判断");
  byId("connection").dataset.error = "true";
}
async function sync({ userInitiated = false } = {}) {
  await window.strategyReady;
  if (inFlight || refreshing) return;
  const requestedDate = reportDateInput.value;
  inFlight = true;
  refreshButton.disabled = true;
  if (userInitiated) {
    reportDateInput.disabled = true;
    refreshButton.textContent = "加载中…";
    write("connection", "正在读取策略与日期");
    sourceStatus.className = "source-status";
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const [snapshotResponse, eventsResponse] = await Promise.all([
      fetch(monitorURL("/api/v1/monitor"), { cache: "no-store", signal: controller.signal }),
      fetch(monitorURL("/api/v1/monitor/events?limit=40"), { cache: "no-store", signal: controller.signal }),
    ]);
    const snapshot = await snapshotResponse.json();
    const events = await eventsResponse.json();
    if (!snapshotResponse.ok) throw new Error(snapshot.error?.message || `请求失败 ${snapshotResponse.status}`);
    if (!eventsResponse.ok) throw new Error(events.error?.message || `事件请求失败 ${eventsResponse.status}`);
    if (requestedDate !== reportDateInput.value) return;
    const data = normalize(snapshot, events);
    serverOffset = new Date(data.server_time).valueOf() - Date.now();
    lastPageSync = Date.now();
    connected = true;
    render(data);
  } catch (error) {
    markDisconnected(error.name === "AbortError" ? "请求超时" : error.message);
  } finally {
    clearTimeout(timeout);
    inFlight = false;
    refreshButton.disabled = (
      latest?.data_status?.reason === "non_trading_day_fallback"
    );
    reportDateInput.disabled = false;
    refreshButton.textContent = "刷新";
    if (requestedDate !== reportDateInput.value) {
      queueMicrotask(() => sync({ userInitiated: true }));
    }
  }
}
function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}
async function fetchJson(path, options = {}) {
  const response = await fetch(window.strategyURL(path), { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `请求失败（${response.status}）`);
  return payload;
}
async function refreshSelectedDay() {
  await window.strategyReady;
  if (inFlight || refreshing) return;
  const requestedDate = reportDateInput.value;
  if (!requestedDate) {
    markDisconnected("请先选择交易日");
    return;
  }
  if (latest?.data_status?.reason === "non_trading_day_fallback") return;
  refreshing = true;
  refreshButton.disabled = true;
  reportDateInput.disabled = true;
  refreshButton.textContent = "生成中…";
  write("connection", "正在按最新策略重新生成");
  sourceStatus.className = "source-status";
  try {
    const job = await fetchJson(
      `/api/v1/monitor/${encodeURIComponent(requestedDate)}/refresh`,
      { method: "POST" },
    );
    for (let attempt = 0; attempt < 300; attempt += 1) {
      const status = await fetchJson(
        `/api/v1/report-jobs/${encodeURIComponent(job.job_id)}`,
      );
      if (status.status === "succeeded") {
        refreshing = false;
        await sync();
        return;
      }
      if (status.status === "failed" || status.status === "cancelled") {
        throw new Error(status.error || "当日实盘生成失败");
      }
      await wait(1000);
    }
    throw new Error("当日实盘生成超时，请稍后重试");
  } catch (error) {
    sourceStatus.className = "source-status error";
    write("connection", `刷新失败：${error.message}`);
  } finally {
    refreshing = false;
    refreshButton.disabled = false;
    reportDateInput.disabled = false;
    refreshButton.textContent = "刷新";
  }
}
async function connectStream() {
  await window.strategyReady;
  if (eventSource) eventSource.close();
  eventSource = new EventSource(monitorURL("/api/v1/monitor/stream"));
  eventSource.onopen = () => {
    connected = true;
    write("connection", "事件流已连接");
    byId("connection").dataset.error = "false";
    sourceStatus.className = "source-status ready";
  };
  for (const name of ["snapshot", "decision.changed", "data.degraded"]) {
    eventSource.addEventListener(name, sync);
  }
  eventSource.onerror = () => {
    eventSource.close();
    markDisconnected("实时事件流中断");
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectStream, 2000);
  };
}

setInterval(() => {
  if (connected && Date.now() - lastPageSync > 15000) sync();
  const today = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
  const age = latest?.collected_at
    ? Math.max(0, Math.floor((Date.now() + serverOffset - new Date(latest.collected_at).valueOf()) / 1000))
    : null;
  write(
    "countdown",
    latest?.plan_date && latest.plan_date < today
      ? "历史收盘快照 · mootdx 已验证"
      : age === null
        ? "等待首次行情"
        : `行情年龄 ${age}秒 · SSE 实时推送 · 5秒轮询兜底`,
  );
}, 1000);
setInterval(sync, 5000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) sync(); });
reportDateInput.addEventListener("change", () => {
  if (!reportDateInput.value) return;
  const params = new URLSearchParams(location.search);
  params.set("trade_date", reportDateInput.value);
  history.replaceState(null, "", `${location.pathname}?${params}`);
  selected = null;
  sync({ userInitiated: true });
  connectStream();
});
refreshButton.addEventListener("click", refreshSelectedDay);
document.querySelectorAll("[data-chart-period]").forEach((button) => {
  button.addEventListener("click", () => {
    if (selectedPeriod === button.dataset.chartPeriod) return;
    selectedPeriod = button.dataset.chartPeriod;
    displayedChartKey = null;
    chartRenderVersion += 1;
    const stock = latest?.stocks.find((item) => item.code === selected);
    if (stock) renderSelectedChart(stock);
  });
});

async function initialize() {
  await window.strategyReady;
  try {
    await window.initializeTradingDateControl(
      reportDateInput,
      { defaultKey: "monitor" },
    );
    await sync();
    connectStream();
  } catch (error) {
    markDisconnected(error.message);
  }
}

initialize();

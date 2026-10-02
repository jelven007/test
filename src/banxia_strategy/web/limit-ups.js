const byId = (id) => document.getElementById(id);
const pageSize = 100;
let offset = 0;
let total = 0;
let loading = false;
let requestNumber = 0;
let activeRequest = null;
let sortBy = "trade_date";
let sortDirection = "desc";
const stockPageSize = 10;
let stockOffset = 0;
let stockTotal = 0;
let monthlyCounts = [];
let monthlyMetric = "limit_up_count";

const boardLabels = {
  main: "沪深主板",
  gem: "创业板",
  star: "科创板",
};

const sortLabels = {
  trade_date: "日期",
  symbol: "股票",
  board: "大盘",
  industry: "所属行业",
  close: "收盘价",
  total_market_cap_cny: "总市值",
  float_market_cap_cny: "流通市值",
  turnover_pct: "换手率",
  amplitude_pct: "振幅",
  open_change_pct: "开盘涨幅",
  change_pct: "当天涨幅",
  return_5d_pct: "5 日涨幅",
  amount_cny: "成交额",
  consecutive_limit_days: "连板数",
};

const numericFilters = [
  ["min-total-cap", "min_total_market_cap_cny", 100000000],
  ["max-total-cap", "max_total_market_cap_cny", 100000000],
  ["min-float-cap", "min_float_market_cap_cny", 100000000],
  ["max-float-cap", "max_float_market_cap_cny", 100000000],
  ["min-turnover", "min_turnover_pct", 1],
  ["max-turnover", "max_turnover_pct", 1],
  ["min-amplitude", "min_amplitude_pct", 1],
  ["max-amplitude", "max_amplitude_pct", 1],
  ["min-open-change", "min_open_change_pct", 1],
  ["max-open-change", "max_open_change_pct", 1],
  ["min-change", "min_change_pct", 1],
  ["max-change", "max_change_pct", 1],
  ["min-return-5d", "min_return_5d_pct", 1],
  ["max-return-5d", "max_return_5d_pct", 1],
];

function numeric(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

function formatNumber(value, digits = 2) {
  return numeric(value) ? Number(value).toFixed(digits) : "—";
}

function formatPercent(value) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function formatAmount(value) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)} 亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)} 万`;
  return number.toLocaleString("zh-CN");
}

function tone(value) {
  return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "";
}

function cell(text, className = "") {
  const node = document.createElement("td");
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function fetchJson(url, signal) {
  const response = await fetch(url, { cache: "no-store", signal });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error?.message || payload.detail || `请求失败 ${response.status}`);
  }
  return payload;
}

function buildParameters() {
  const parameters = new URLSearchParams({
    start_date: byId("start-date").value || "2025-01-01",
    end_date: byId("end-date").value,
    q: byId("stock-query").value.trim(),
    board: byId("board").value,
    industry: byId("industry").value,
    include_unverified: String(byId("include-unverified").checked),
    sort: sortBy,
    direction: sortDirection,
    limit: String(pageSize),
    offset: String(offset),
    include_statistics: "true",
    stock_limit: String(stockPageSize),
    stock_offset: String(stockOffset),
  });
  if (!parameters.get("end_date")) parameters.delete("end_date");
  for (const [id, name, multiplier] of numericFilters) {
    const value = byId(id).value.trim();
    if (value !== "") parameters.set(name, String(Number(value) * multiplier));
  }
  return parameters;
}

function visibleParameters(parameters) {
  const visible = new URLSearchParams(parameters);
  visible.delete("limit");
  visible.delete("include_statistics");
  visible.delete("stock_limit");
  if (visible.get("stock_offset") === "0") visible.delete("stock_offset");
  if (monthlyMetric === "stock_count") visible.set("monthly_metric", monthlyMetric);
  if (!visible.get("q")) visible.delete("q");
  if (visible.get("board") === "all") visible.delete("board");
  if (!visible.get("industry")) visible.delete("industry");
  if (visible.get("offset") === "0") visible.delete("offset");
  if (visible.get("sort") === "trade_date") visible.delete("sort");
  if (visible.get("direction") === "desc" && !visible.has("sort")) {
    visible.delete("direction");
  }
  if (visible.get("include_unverified") === "false") visible.delete("include_unverified");
  return visible;
}

function updateBrowserUrl(parameters) {
  const query = visibleParameters(parameters).toString();
  history.replaceState(null, "", query ? `${location.pathname}?${query}` : location.pathname);
}

function updateSortHeaders() {
  document.querySelectorAll(".sort-button").forEach((button) => {
    const key = button.dataset.sort;
    const active = key === sortBy;
    const direction = active ? sortDirection : null;
    const header = button.closest("th");
    const indicator = button.querySelector("span");
    header.setAttribute(
      "aria-sort",
      direction === "asc" ? "ascending" : direction === "desc" ? "descending" : "none",
    );
    indicator.textContent = direction === "asc" ? "↑" : direction === "desc" ? "↓" : "↕";
    button.title = active
      ? `${sortLabels[key]}当前${direction === "asc" ? "升序" : "降序"}`
      : `按${sortLabels[key]}排序`;
  });
}

function renderRows(items) {
  const root = byId("limit-up-rows");
  root.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const empty = cell("没有符合当前条件的涨停记录", "waiting-cell");
    empty.colSpan = 15;
    row.append(empty);
    root.append(row);
    return;
  }
  for (const item of items) {
    const row = document.createElement("tr");
    const identity = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "limit-up-identity";
    const name = document.createElement("a");
    name.href = `/stocks/${encodeURIComponent(item.symbol)}`;
    name.textContent = item.name;
    const code = document.createElement("code");
    code.textContent = item.symbol;
    wrap.append(name, code);
    identity.append(wrap);
    row.append(
      cell(item.trade_date, "date-cell"),
      identity,
      cell(boardLabels[item.board] || item.board, "board-cell"),
      cell(item.industry, "industry-cell"),
      cell(formatNumber(item.close), "numeric-cell"),
      cell(formatAmount(item.total_market_cap_cny), "numeric-cell"),
      cell(formatAmount(item.float_market_cap_cny), "numeric-cell"),
      cell(formatPercent(item.turnover_pct), "numeric-cell"),
      cell(formatPercent(item.amplitude_pct), "numeric-cell"),
      cell(formatPercent(item.open_change_pct), `numeric-cell ${tone(item.open_change_pct)}`),
      cell(formatPercent(item.change_pct), `numeric-cell ${tone(item.change_pct)}`),
      cell(formatPercent(item.return_5d_pct), `numeric-cell ${tone(item.return_5d_pct)}`),
      cell(formatAmount(item.amount_cny), "numeric-cell"),
      cell(`${item.consecutive_limit_days} 板`, "numeric-cell streak-cell"),
      cell([
        item.capital_basis === "xdxr_capital_history" ? "历史股本" : "股本估算",
        item.limit_rule_basis === "unverified_5pct_candidate" ? "5% 待核实" : "",
      ].filter(Boolean).join(" · "), "basis-cell"),
    );
    root.append(row);
  }
}

function updateSummary(payload) {
  byId("summary-total").textContent = payload.total.toLocaleString("zh-CN");
  byId("summary-days").textContent = payload.trading_days.toLocaleString("zh-CN");
  byId("summary-stocks").textContent = payload.stock_count.toLocaleString("zh-CN");
  byId("summary-range").textContent = payload.first_date && payload.last_date
    ? `${payload.first_date} 至 ${payload.last_date}`
    : "—";
}

function svgNode(tag, attributes = {}, text = "") {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, value);
  if (text) node.textContent = text;
  return node;
}

function renderMonthlyChart() {
  const root = byId("monthly-chart");
  root.replaceChildren();
  document.querySelectorAll("[data-monthly-metric]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.monthlyMetric === monthlyMetric));
  });
  const unit = monthlyMetric === "stock_count" ? "只" : "次";
  const label = monthlyMetric === "stock_count" ? "去重股票数" : "涨停次数";
  root.setAttribute("aria-label", `月度${label}曲线`);
  const available = monthlyCounts.filter((item) => numeric(item[monthlyMetric]));
  if (!available.length) {
    root.append(svgNode("text", { x: 380, y: 130, "text-anchor": "middle" }, "当前范围暂无月度统计"));
    byId("monthly-insight").textContent = "暂无统计";
    byId("monthly-detail").textContent = "有采集覆盖的月份才会显示统计，缺失月份不按零计数。";
    return;
  }
  const peak = available.reduce((best, item) => item[monthlyMetric] > best[monthlyMetric] ? item : best);
  byId("monthly-insight").textContent = peak[monthlyMetric] > 0
    ? `${available.length} 个月 · 最高 ${peak.month} / ${peak[monthlyMetric].toLocaleString("zh-CN")} ${unit}`
    : `${available.length} 个月 · 没有符合条件的涨停记录`;
  byId("monthly-detail").textContent =
    `统计至 ${available[available.length - 1].last_date || "已采集日期"}；首尾月按筛选日期截取。悬停或聚焦数据点查看统计。`;
  root.append(svgNode("title", {}, `按当前筛选条件汇总的月度${label}`));
  const maximum = Math.max(4, ...available.map((item) => item[monthlyMetric]));
  const magnitude = 10 ** Math.floor(Math.log10(maximum / 4));
  const step = Math.ceil(maximum / 4 / magnitude) * magnitude;
  const ceiling = step * 4;
  const left = 52, right = 738, top = 18, bottom = 232;
  const x = (index) => monthlyCounts.length === 1
    ? (left + right) / 2
    : left + index / (monthlyCounts.length - 1) * (right - left);
  const y = (value) => bottom - value / ceiling * (bottom - top);
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = tick * step;
    root.append(
      svgNode("line", { x1: left, x2: right, y1: y(value), y2: y(value), class: "chart-grid" }),
      svgNode("text", { x: left - 10, y: y(value) + 4, "text-anchor": "end" }, value.toLocaleString("zh-CN")),
    );
  }
  root.append(svgNode("text", { x: 12, y: 12 }, unit));
  const segments = [];
  let segment = [];
  monthlyCounts.forEach((item, index) => {
    if (numeric(item[monthlyMetric])) segment.push([x(index), y(item[monthlyMetric])]);
    else if (segment.length) { segments.push(segment); segment = []; }
  });
  if (segment.length) segments.push(segment);
  for (const points of segments) {
    const line = points.map(([px, py], index) => `${index ? "L" : "M"}${px},${py}`).join(" ");
    root.append(
      svgNode("path", { class: "chart-area", d: `${line} L${points.at(-1)[0]},${bottom} L${points[0][0]},${bottom} Z` }),
      svgNode("path", { class: "chart-line", d: line }),
    );
  }
  const labelEvery = Math.max(1, Math.ceil(monthlyCounts.length / 7));
  monthlyCounts.forEach((item, index) => {
    const px = x(index);
    if (index === 0 || index === monthlyCounts.length - 1 || (
      index % labelEvery === 0 && monthlyCounts.length - 1 - index >= labelEvery * 0.75
    )) {
      root.append(svgNode("text", { x: px, y: 260, "text-anchor": "middle" }, item.month));
    }
    if (!numeric(item[monthlyMetric])) return;
    const description = `${item.month} · 涨停 ${item.limit_up_count.toLocaleString("zh-CN")} 次 · ${
      item.stock_count.toLocaleString("zh-CN")} 只股票 · 覆盖 ${item.covered_sessions} 个交易日${
      item.partial_sessions ? ` · ${item.partial_sessions} 日待补齐` : ""}`;
    const group = svgNode("g", { class: "chart-datum", tabindex: "0", role: "img", "aria-label": description });
    group.append(
      svgNode("title", {}, description),
      svgNode("circle", { cx: px, cy: y(item[monthlyMetric]), r: 3.5, class: "chart-point" }),
      svgNode("circle", { cx: px, cy: y(item[monthlyMetric]), r: 12, class: "chart-hit" }),
    );
    const show = () => { byId("monthly-detail").textContent = description; };
    group.addEventListener("pointerenter", show);
    group.addEventListener("focus", show);
    group.addEventListener("click", show);
    root.append(group);
  });
}

function renderStockCounts(payload) {
  const root = byId("stock-count-rows");
  root.replaceChildren();
  stockTotal = payload.total;
  byId("stock-count-summary").textContent =
    `${stockTotal.toLocaleString("zh-CN")} 只股票 · 次数降序 · 点击股票查看月度分布及明细`;
  if (!payload.items.length) {
    const row = document.createElement("tr");
    const empty = cell("没有符合条件的股票", "waiting-cell");
    empty.colSpan = 4;
    row.append(empty);
    root.append(row);
  }
  payload.items.forEach((item, index) => {
    const row = document.createElement("tr");
    const identity = document.createElement("td");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "stock-count-name";
    button.textContent = item.name;
    button.title = `查看 ${item.name} 的涨停分布和明细`;
    button.disabled = loading;
    button.addEventListener("click", () => {
      byId("stock-query").value = item.symbol;
      offset = stockOffset = 0;
      loadHistory();
    });
    const meta = document.createElement("span");
    meta.className = "stock-count-meta";
    meta.textContent = `${item.symbol} · ${item.industry}`;
    identity.append(button, meta);
    const count = cell(`${item.limit_up_count} 次`, "numeric-cell stock-count-value");
    count.title = `首次 ${item.first_date}，最近 ${item.last_date}`;
    row.append(cell(String(stockOffset + index + 1)), identity, count, cell(item.last_date, "date-cell"));
    root.append(row);
  });
}

function updatePaging() {
  const page = Math.floor(offset / pageSize) + 1;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  byId("page-label").textContent = `第 ${page} / ${pages} 页`;
  byId("previous-page").disabled = loading || offset === 0;
  byId("next-page").disabled = loading || offset + pageSize >= total;
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + pageSize, total);
  byId("result-range").textContent = `显示第 ${start}–${end} 条，共 ${total.toLocaleString("zh-CN")} 条`;
  byId("stock-page-label").textContent =
    `第 ${Math.floor(stockOffset / stockPageSize) + 1} / ${Math.max(1, Math.ceil(stockTotal / stockPageSize))} 页`;
  byId("stock-previous-page").disabled = loading || stockOffset === 0;
  byId("stock-next-page").disabled = loading || stockOffset + stockPageSize >= stockTotal;
  document.querySelector(".limit-up-analytics").setAttribute("aria-busy", String(loading));
  document.querySelectorAll(".stock-count-name").forEach((button) => { button.disabled = loading; });
}

async function loadHistory() {
  const currentRequest = ++requestNumber;
  activeRequest?.abort();
  activeRequest = new AbortController();
  loading = true;
  updatePaging();
  byId("limit-up-status").textContent = "正在查询涨停记录…";
  try {
    const parameters = buildParameters();
    const payload = await fetchJson(`/api/v1/limit-up-history?${parameters}`, activeRequest.signal);
    if (currentRequest !== requestNumber) return;
    total = payload.total;
    renderRows(payload.items);
    updateSummary(payload);
    monthlyCounts = payload.monthly_counts || [];
    renderMonthlyChart();
    renderStockCounts(payload.stock_counts || { items: [], total: 0 });
    updateBrowserUrl(parameters);
    byId("limit-up-status").textContent =
      `已读取 ${payload.items.length} 条 · 数据源 ${payload.data_source} · ${sortLabels[sortBy]}${sortDirection === "asc" ? "升序" : "降序"}`;
  } catch (error) {
    if (currentRequest !== requestNumber || error.name === "AbortError") return;
    total = 0;
    renderRows([]);
    updateSummary({ total: 0, trading_days: 0, stock_count: 0 });
    monthlyCounts = [];
    renderMonthlyChart();
    renderStockCounts({ items: [], total: 0 });
    byId("monthly-insight").textContent = "统计暂不可用";
    byId("limit-up-status").textContent = error.message;
  } finally {
    if (currentRequest === requestNumber) {
      loading = false;
      updatePaging();
      updateSortHeaders();
    }
  }
}

function restoreFilters() {
  const parameters = new URLSearchParams(location.search);
  const today = new Date();
  const localToday = new Date(today.getTime() - today.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 10);
  byId("start-date").value = parameters.get("start_date") || "2025-01-01";
  byId("end-date").value = parameters.get("end_date") || localToday;
  byId("stock-query").value = parameters.get("q") || "";
  byId("board").value = parameters.get("board") || "all";
  byId("include-unverified").checked = parameters.get("include_unverified") === "true";
  sortBy = Object.hasOwn(sortLabels, parameters.get("sort"))
    ? parameters.get("sort")
    : "trade_date";
  sortDirection = parameters.get("direction") === "asc" ? "asc" : "desc";
  const requestedOffset = Number(parameters.get("offset"));
  offset = Number.isInteger(requestedOffset) && requestedOffset >= 0
    ? requestedOffset
    : 0;
  const requestedStockOffset = Number(parameters.get("stock_offset"));
  stockOffset = Number.isInteger(requestedStockOffset) && requestedStockOffset >= 0
    ? requestedStockOffset : 0;
  monthlyMetric = parameters.get("monthly_metric") === "stock_count" ? "stock_count" : "limit_up_count";
  for (const [id, name, multiplier] of numericFilters) {
    byId(id).value = parameters.has(name) ? Number(parameters.get(name)) / multiplier : "";
  }
  return parameters.get("industry") || "";
}

async function loadOptions(requestedIndustry) {
  const payload = await fetchJson("/api/v1/limit-up-history/options");
  const select = byId("industry");
  for (const item of payload.industries) {
    const option = document.createElement("option");
    option.value = item.name;
    option.textContent = `${item.name} (${item.count})`;
    select.append(option);
  }
  select.value = [...select.options].some((option) => option.value === requestedIndustry)
    ? requestedIndustry
    : "";
  byId("coverage-range").textContent = payload.coverage_first_date && payload.coverage_last_date
    ? `${payload.coverage_first_date} 至 ${payload.coverage_last_date} · ${payload.covered_sessions} 个交易日${
      payload.partial_sessions ? ` · ${payload.partial_sessions} 日待补齐` : ""}`
    : "暂无数据";
  const labels = { running: "同步中", succeeded: "完成", partial: "部分完成", failed: "失败" };
  const sync = payload.latest_sync;
  const verified = sync && ["succeeded", "partial"].includes(sync.status);
  byId("sync-time").textContent = sync
    ? `${labels[sync.status] || sync.status} · ${
      sync.finished_at
        ? new Date(sync.finished_at).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" })
        : "采集中"} · ${
      verified ? `缺失 ${sync.missing_symbols?.length || 0} 只` : "完整性待核对"}${
      sync.excluded_symbols?.length ? ` · 排除未上市 ${sync.excluded_symbols.length} 只` : ""}`
    : "尚未同步";
}

byId("limit-up-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  offset = stockOffset = 0;
  loadHistory();
});

byId("reset-filters").addEventListener("click", () => {
  byId("limit-up-filters").reset();
  byId("start-date").value = "2025-01-01";
  const today = new Date();
  byId("end-date").value = new Date(today.getTime() - today.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 10);
  sortBy = "trade_date";
  sortDirection = "desc";
  offset = stockOffset = 0;
  monthlyMetric = "limit_up_count";
  loadHistory();
});

byId("previous-page").addEventListener("click", () => {
  offset = Math.max(0, offset - pageSize);
  loadHistory();
});

byId("next-page").addEventListener("click", () => {
  if (offset + pageSize >= total) return;
  offset += pageSize;
  loadHistory();
});

byId("stock-previous-page").addEventListener("click", () => {
  stockOffset = Math.max(0, stockOffset - stockPageSize);
  loadHistory();
});

byId("stock-next-page").addEventListener("click", () => {
  if (stockOffset + stockPageSize >= stockTotal) return;
  stockOffset += stockPageSize;
  loadHistory();
});

document.querySelectorAll("[data-monthly-metric]").forEach((button) => {
  button.addEventListener("click", () => {
    monthlyMetric = button.dataset.monthlyMetric;
    renderMonthlyChart();
    const parameters = new URLSearchParams(location.search);
    parameters.delete("monthly_metric");
    if (monthlyMetric === "stock_count") parameters.set("monthly_metric", monthlyMetric);
    history.replaceState(null, "", `${location.pathname}?${parameters}`);
  });
});

document.querySelectorAll(".sort-button").forEach((button) => {
  button.addEventListener("click", () => {
    const key = button.dataset.sort;
    if (sortBy === key) {
      sortDirection = sortDirection === "asc" ? "desc" : "asc";
    } else {
      sortBy = key;
      sortDirection = key === "symbol" || key === "industry" || key === "board"
        ? "asc"
        : "desc";
    }
    offset = 0;
    loadHistory();
  });
});

const requestedIndustry = restoreFilters();
document.querySelectorAll(".metric-filter-grid fieldset").forEach((fieldset) => {
  const label = fieldset.querySelector("legend").textContent;
  fieldset.querySelectorAll("input").forEach((input, index) => {
    input.setAttribute("aria-label", `${label}${index === 0 ? "下限" : "上限"}`);
  });
});
updateSortHeaders();
loadOptions(requestedIndustry)
  .catch((error) => {
    byId("limit-up-status").textContent = error.message;
  })
  .finally(loadHistory);

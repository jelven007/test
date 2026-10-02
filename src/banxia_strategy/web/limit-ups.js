const byId = (id) => document.getElementById(id);
const pageSize = 100;
let offset = 0;
let total = 0;
let loading = false;
let requestNumber = 0;
let activeRequest = null;
let sortBy = "trade_date";
let sortDirection = "desc";

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

function updatePaging() {
  const page = Math.floor(offset / pageSize) + 1;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  byId("page-label").textContent = `第 ${page} / ${pages} 页`;
  byId("previous-page").disabled = loading || offset === 0;
  byId("next-page").disabled = loading || offset + pageSize >= total;
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + pageSize, total);
  byId("result-range").textContent = `显示第 ${start}–${end} 条，共 ${total.toLocaleString("zh-CN")} 条`;
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
    updateBrowserUrl(parameters);
    byId("limit-up-status").textContent =
      `已读取 ${payload.items.length} 条 · 数据源 ${payload.data_source} · ${sortLabels[sortBy]}${sortDirection === "asc" ? "升序" : "降序"}`;
  } catch (error) {
    if (currentRequest !== requestNumber || error.name === "AbortError") return;
    total = 0;
    renderRows([]);
    updateSummary({ total: 0, trading_days: 0, stock_count: 0 });
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
  offset = 0;
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
  offset = 0;
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

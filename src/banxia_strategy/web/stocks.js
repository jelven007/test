const byId = (id) => document.getElementById(id);
const pageSize = 50;
let offset = 0;
let total = 0;
let loading = false;
let savingWatchlist = false;
let requestedBlock = "";
let sortBy = "";
let sortDirection = "asc";
let currentItems = [];
let currentListPath = "/";
const selectedSymbols = new Set();

const boardLabels = {
  main: "沪深主板",
  gem: "创业板",
  star: "科创板",
};

const sortLabels = {
  symbol: "股票",
  board: "大盘",
  price: "最新价",
  change_pct: "涨跌幅",
  open: "今开",
  high: "最高",
  low: "最低",
  volume: "成交量",
  amount: "成交额",
  quote_time: "行情时间",
};

function numeric(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

function price(value) {
  return numeric(value) ? Number(value).toFixed(2) : "—";
}

function percent(value) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function compact(value) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
  return number.toLocaleString("zh-CN");
}

function tone(value) {
  return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "";
}

function quoteTime(quote) {
  const value = quote.servertime || quote.source_time || quote.collected_at;
  if (!value) return "—";
  const match = String(value).match(/T(\d{2}:\d{2}:\d{2})/);
  return match ? match[1] : value;
}

function cell(text, className) {
  const node = document.createElement("td");
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || `请求失败 ${response.status}`);
  return payload;
}

function queryParameters() {
  const parameters = new URLSearchParams({
    q: byId("stock-query").value.trim(),
    board: byId("stock-board").value,
    block: byId("stock-block").value,
    watchlist: byId("stock-scope").value,
    limit: String(pageSize),
    offset: String(offset),
  });
  if (sortBy) {
    parameters.set("sort", sortBy);
    parameters.set("direction", sortDirection);
  }
  return parameters;
}

function visibleQueryParameters(parameters) {
  const visibleParameters = new URLSearchParams(parameters);
  visibleParameters.delete("limit");
  if (!visibleParameters.get("q")) visibleParameters.delete("q");
  if (visibleParameters.get("board") === "all") visibleParameters.delete("board");
  if (!visibleParameters.get("block")) visibleParameters.delete("block");
  if (visibleParameters.get("watchlist") === "all") visibleParameters.delete("watchlist");
  if (visibleParameters.get("offset") === "0") visibleParameters.delete("offset");
  if (!visibleParameters.get("sort")) visibleParameters.delete("direction");
  return visibleParameters;
}

function listPath(parameters) {
  const query = visibleQueryParameters(parameters).toString();
  return query ? `${location.pathname}?${query}` : location.pathname;
}

function stockPath(symbol, suffix = "") {
  const parameters = new URLSearchParams({ return_to: currentListPath });
  return `/stocks/${encodeURIComponent(symbol)}${suffix}?${parameters}`;
}

function updateBrowserUrl(parameters) {
  currentListPath = listPath(parameters);
  history.replaceState(null, "", currentListPath);
}

function updateSortHeaders() {
  document.querySelectorAll(".sort-button").forEach((button) => {
    const key = button.dataset.sort;
    const active = key === sortBy;
    const header = button.closest("th");
    const indicator = button.querySelector("span");
    const label = sortLabels[key];
    const nextDirection = active && sortDirection === "asc" ? "desc" : "asc";
    header.setAttribute(
      "aria-sort",
      active
        ? sortDirection === "asc" ? "ascending" : "descending"
        : "none",
    );
    indicator.textContent = active
      ? sortDirection === "asc" ? "↑" : "↓"
      : "↕";
    button.setAttribute(
      "aria-label",
      active
        ? `${label}当前${sortDirection === "asc" ? "升序" : "降序"}，切换为${nextDirection === "asc" ? "升序" : "降序"}`
        : `按${label}升序排列`,
    );
    button.title = button.getAttribute("aria-label");
  });
}

function renderRows(items) {
  const root = byId("stock-rows");
  root.replaceChildren();
  currentItems = items;
  if (!items.length) {
    const row = document.createElement("tr");
    const empty = cell("没有符合条件的股票");
    empty.colSpan = 12;
    empty.className = "waiting-cell";
    row.append(empty);
    root.append(row);
    updateSelectionControls();
    return;
  }

  for (const item of items) {
    const quote = item.quote || {};
    const row = document.createElement("tr");
    if (item.watchlisted) {
      selectedSymbols.delete(item.symbol);
      row.classList.add("is-watchlisted");
    }
    const selection = document.createElement("td");
    selection.className = "selection-column";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "stock-checkbox";
    checkbox.dataset.symbol = item.symbol;
    checkbox.checked = selectedSymbols.has(item.symbol);
    checkbox.disabled = item.watchlisted;
    checkbox.setAttribute(
      "aria-label",
      item.watchlisted
        ? `${item.name}已在自选股`
        : `选择${item.name}`,
    );
    selection.append(checkbox);
    const identity = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "stock-identity";
    const name = document.createElement("a");
    name.href = stockPath(item.symbol);
    name.textContent = item.name;
    const code = document.createElement("a");
    code.href = name.href;
    const codeText = document.createElement("code");
    codeText.textContent = item.symbol;
    code.append(codeText);
    wrap.append(name, code);
    identity.append(wrap);

    const actions = document.createElement("td");
    const actionWrap = document.createElement("div");
    actionWrap.className = "stock-actions";
    for (const [label, href] of [
      ["详情", stockPath(item.symbol)],
      ["历史行情", stockPath(item.symbol, "/history")],
    ]) {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = label;
      actionWrap.append(link);
    }
    if (item.watchlisted) {
      const badge = document.createElement("span");
      badge.className = "watchlist-state";
      badge.textContent = "已自选";
      actionWrap.prepend(badge);
    }
    actions.append(actionWrap);

    row.append(
      selection,
      identity,
      cell(`${boardLabels[item.board]} · ${item.market.toUpperCase()}`, "board-label"),
      cell(price(quote.price), `quote-number ${tone(quote.change_pct)}`),
      cell(percent(quote.change_pct), `quote-number ${tone(quote.change_pct)}`),
      cell(price(quote.open), "quote-number"),
      cell(price(quote.high), "quote-number"),
      cell(price(quote.low), "quote-number"),
      cell(compact(quote.volume ?? quote.vol), "quote-number"),
      cell(compact(quote.amount), "quote-number"),
      cell(quoteTime(quote), "quote-number"),
      actions,
    );
    root.append(row);
  }
  updateSelectionControls();
}

function updateSelectionControls() {
  const eligible = currentItems.filter((item) => !item.watchlisted);
  const selectedOnPage = eligible.filter((item) => selectedSymbols.has(item.symbol));
  const selectPage = byId("select-page");
  selectPage.checked = eligible.length > 0 && selectedOnPage.length === eligible.length;
  selectPage.indeterminate = selectedOnPage.length > 0 && selectedOnPage.length < eligible.length;
  selectPage.disabled = loading || eligible.length === 0;
  byId("selected-count").textContent = `已选 ${selectedSymbols.size} 只`;
  byId("add-watchlist").disabled = loading || savingWatchlist || selectedSymbols.size === 0;
}

function updatePaging() {
  const page = Math.floor(offset / pageSize) + 1;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  byId("page-label").textContent = `第 ${page} / ${pages} 页`;
  byId("previous-page").disabled = offset === 0 || loading;
  byId("next-page").disabled = offset + pageSize >= total || loading;
  const start = total ? offset + 1 : 0;
  const end = Math.min(offset + pageSize, total);
  byId("stock-range").textContent = `显示第 ${start}–${end} 只股票`;
}

async function loadStocks() {
  if (loading) return;
  loading = true;
  updatePaging();
  byId("stock-status").textContent = "正在读取股票目录与当前页实时行情…";
  updateSelectionControls();
  try {
    const parameters = queryParameters();
    currentListPath = listPath(parameters);
    const payload = await fetchJson(`/api/v1/stocks?${parameters}`);
    total = payload.total;
    renderRows(payload.items);
    byId("stock-total").textContent = total.toLocaleString("zh-CN");
    const times = payload.items
      .map((item) => item.quote?.servertime)
      .filter(Boolean)
      .sort();
    byId("market-time").textContent = times.length
      ? `盘口时间 ${times.at(-1)}`
      : "当前页暂无实时报价";
    const sorting = sortBy
      ? ` · ${sortLabels[sortBy]}${sortDirection === "asc" ? "升序" : "降序"}`
      : "";
    const planScope = payload.plan_scope?.trade_date
      ? ` · 次日计划执行日 ${payload.plan_scope.trade_date}`
      : "";
    byId("stock-status").textContent = `已读取 ${payload.items.length} 只股票 · ${payload.source}${planScope}${sorting}`;
    updateBrowserUrl(parameters);
  } catch (error) {
    byId("stock-status").textContent = error.message;
    renderRows([]);
  } finally {
    loading = false;
    updatePaging();
    updateSelectionControls();
  }
}

function restoreFilters() {
  const parameters = new URLSearchParams(location.search);
  byId("stock-query").value = parameters.get("q") || "";
  byId("stock-board").value = parameters.get("board") || "all";
  const requestedScope = parameters.get("watchlist") || "all";
  byId("stock-scope").value = ["all", "only", "plan"].includes(requestedScope)
    ? requestedScope
    : "all";
  requestedBlock = parameters.get("block") || "";
  const requestedSort = parameters.get("sort") || "";
  sortBy = Object.hasOwn(sortLabels, requestedSort) ? requestedSort : "";
  sortDirection = parameters.get("direction") === "desc" ? "desc" : "asc";
  const requestedOffset = Number(parameters.get("offset"));
  offset = Number.isInteger(requestedOffset) && requestedOffset >= 0
    ? requestedOffset
    : 0;
}

async function loadBlocks() {
  const select = byId("stock-block");
  const payload = await fetchJson("/api/v1/stock-blocks");
  for (const item of payload.items) {
    const option = document.createElement("option");
    option.value = item.blockname;
    option.textContent = item.blockname;
    select.append(option);
  }
  select.value = [...select.options].some((option) => option.value === requestedBlock)
    ? requestedBlock
    : "";
}

function disableBlockFilter() {
  const select = byId("stock-block");
  select.options[0].textContent = "板块加载失败";
  select.disabled = true;
}

async function addSelectedToWatchlist() {
  if (!selectedSymbols.size || savingWatchlist) return;
  savingWatchlist = true;
  updateSelectionControls();
  byId("stock-status").textContent = `正在添加 ${selectedSymbols.size} 只股票到自选…`;
  try {
    const payload = await fetchJson("/api/v1/watchlist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbols: [...selectedSymbols] }),
    });
    selectedSymbols.clear();
    await loadStocks();
    byId("stock-status").textContent = payload.added
      ? `已添加 ${payload.added} 只股票到自选，共 ${payload.count} 只`
      : `所选股票已在自选中，共 ${payload.count} 只`;
  } catch (error) {
    byId("stock-status").textContent = error.message;
  } finally {
    savingWatchlist = false;
    updateSelectionControls();
  }
}

byId("stock-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  offset = 0;
  loadStocks();
});

byId("stock-reset").addEventListener("click", () => {
  byId("stock-query").value = "";
  byId("stock-board").value = "all";
  byId("stock-block").value = "";
  byId("stock-scope").value = "all";
  sortBy = "";
  sortDirection = "asc";
  offset = 0;
  updateSortHeaders();
  loadStocks();
});

document.querySelectorAll(".sort-button").forEach((button) => {
  button.addEventListener("click", () => {
    if (loading) return;
    const key = button.dataset.sort;
    sortDirection = sortBy === key && sortDirection === "asc" ? "desc" : "asc";
    sortBy = key;
    offset = 0;
    updateSortHeaders();
    loadStocks();
  });
});

byId("previous-page").addEventListener("click", () => {
  offset = Math.max(0, offset - pageSize);
  loadStocks();
});

byId("next-page").addEventListener("click", () => {
  if (offset + pageSize < total) {
    offset += pageSize;
    loadStocks();
  }
});

byId("select-page").addEventListener("change", (event) => {
  for (const item of currentItems) {
    if (item.watchlisted) continue;
    if (event.target.checked) selectedSymbols.add(item.symbol);
    else selectedSymbols.delete(item.symbol);
  }
  renderRows(currentItems);
});

byId("stock-rows").addEventListener("change", (event) => {
  if (!event.target.classList.contains("stock-checkbox")) return;
  const symbol = event.target.dataset.symbol;
  if (event.target.checked) selectedSymbols.add(symbol);
  else selectedSymbols.delete(symbol);
  updateSelectionControls();
});

byId("add-watchlist").addEventListener("click", addSelectedToWatchlist);

async function initialize() {
  restoreFilters();
  updateSortHeaders();
  if (requestedBlock) {
    await loadBlocks().catch(disableBlockFilter);
    loadStocks();
    return;
  }
  await loadStocks();
  loadBlocks().catch(disableBlockFilter);
}

initialize();

const byId = (id) => document.getElementById(id);
let pageSize = 50;
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
let marketCapPayload = null;
let marketCapBoard = "sh_main";
let marketCapGranularity = "month";

const boardLabels = {
  main: "沪深主板",
  gem: "创业板",
  star: "科创板",
};

const marketCapBoards = {
  sh_main: { name: "沪市主板", color: "#126044" },
  sz_main: { name: "深市主板", color: "#315a78" },
  sh_star: { name: "沪创业板", color: "#a66c16" },
  sz_gem: { name: "深科创板", color: "#a33c32" },
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

function trillion(value) {
  return numeric(value) ? `${(Number(value) / 1_000_000_000_000).toFixed(2)} 万亿` : "—";
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

function svgNode(tag, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, value);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderMarketCapRows() {
  const root = byId("market-cap-rows");
  root.replaceChildren();
  for (const board of marketCapPayload?.boards || []) {
    const definition = marketCapBoards[board.code];
    if (!definition) continue;
    const row = document.createElement("div");
    row.className = "market-cap-table-row";
    row.setAttribute("role", "row");

    const boardCell = document.createElement("div");
    boardCell.className = "market-cap-board-cell";
    boardCell.setAttribute("role", "cell");
    const button = document.createElement("button");
    button.type = "button";
    button.className = "market-cap-board-button";
    button.dataset.board = board.code;
    button.setAttribute("aria-pressed", String(board.code === marketCapBoard));
    const name = document.createElement("strong");
    name.textContent = definition.name;
    const count = document.createElement("small");
    count.textContent = `${Number(board.stock_count).toLocaleString("zh-CN")} 只`;
    button.append(name, count);
    boardCell.append(button);

    const total = document.createElement("div");
    total.className = "market-cap-value total";
    total.setAttribute("role", "cell");
    total.textContent = trillion(board.total_market_cap_cny);
    const floating = document.createElement("div");
    floating.className = "market-cap-value floating";
    floating.setAttribute("role", "cell");
    floating.textContent = trillion(board.float_market_cap_cny);
    row.append(boardCell, total, floating);
    root.append(row);
  }
}

function showMarketCapTooltip(event, item, tooltip, root) {
  tooltip.replaceChildren();
  for (const text of [
    item.trade_date,
    `总市值 ${trillion(item.total_market_cap_cny)}`,
    `流通市值 ${trillion(item.float_market_cap_cny)}`,
    `覆盖 ${Number(item.stock_count).toLocaleString("zh-CN")} 只`,
  ]) {
    const line = document.createElement("span");
    line.textContent = text;
    tooltip.append(line);
  }
  const bounds = root.getBoundingClientRect();
  tooltip.style.left = `${Math.max(90, Math.min(bounds.width - 90, event.clientX - bounds.left))}px`;
  tooltip.style.top = `${Math.max(8, event.clientY - bounds.top - 90)}px`;
  tooltip.hidden = false;
}

function renderMarketCapChart() {
  const root = byId("market-cap-chart");
  root.replaceChildren();
  const board = marketCapBoards[marketCapBoard];
  const items = marketCapPayload?.series?.[marketCapBoard] || [];
  byId("market-cap-chart-title").textContent = `${board.name}历史趋势`;
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "market-cap-chart-empty";
    empty.textContent = "暂无板块市值历史";
    root.append(empty);
    return;
  }

  const values = items.flatMap((item) => [
    Number(item.total_market_cap_cny),
    Number(item.float_market_cap_cny),
  ]);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const span = Math.max(maximum - minimum, maximum * 0.02, 1);
  const floor = Math.max(0, minimum - span * 0.08);
  const ceiling = maximum + span * 0.08;
  const width = 1200;
  const height = 320;
  const left = 34;
  const right = 1100;
  const top = 24;
  const bottom = 274;
  const x = (index) => left + index / Math.max(1, items.length - 1) * (right - left);
  const y = (value) => top + (ceiling - value) / (ceiling - floor) * (bottom - top);
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    "aria-hidden": "true",
  });

  for (const value of [ceiling, (ceiling + floor) / 2, floor]) {
    const position = y(value);
    svg.append(
      svgNode("line", {
        x1: left,
        x2: right,
        y1: position,
        y2: position,
        stroke: "#d7dcd8",
      }),
      svgNode("text", {
        x: right + 14,
        y: position + 4,
        fill: "#66716b",
        "font-size": 11,
      }, `${(value / 1_000_000_000_000).toFixed(2)}万亿`),
    );
  }
  for (const [field, color] of [
    ["total_market_cap_cny", "#126044"],
    ["float_market_cap_cny", "#315a78"],
  ]) {
    const points = items
      .map((item, index) => `${x(index)},${y(Number(item[field]))}`)
      .join(" ");
    svg.append(
      svgNode("polyline", {
        points,
        fill: "none",
        stroke: color,
        "stroke-width": 2.5,
        "stroke-linejoin": "round",
        "stroke-linecap": "round",
        "data-cap-line": field,
      }),
    );
  }
  for (const index of [0, Math.floor((items.length - 1) / 2), items.length - 1]) {
    svg.append(svgNode("text", {
      x: x(index),
      y: 309,
      fill: "#66716b",
      "font-size": 10,
      "text-anchor": index === 0 ? "start" : index === items.length - 1 ? "end" : "middle",
    }, items[index].trade_date));
  }

  const tooltip = document.createElement("div");
  tooltip.className = "market-cap-tooltip";
  tooltip.hidden = true;
  const crosshair = svgNode("line", {
    y1: top,
    y2: bottom,
    stroke: "#17201c",
    "stroke-width": 1,
    "stroke-dasharray": "3 3",
    visibility: "hidden",
  });
  const totalPoint = svgNode("circle", {
    r: 4,
    fill: "#ffffff",
    stroke: "#126044",
    "stroke-width": 2,
    visibility: "hidden",
  });
  const floatPoint = svgNode("circle", {
    r: 4,
    fill: "#ffffff",
    stroke: "#315a78",
    "stroke-width": 2,
    visibility: "hidden",
  });
  const hit = svgNode("rect", {
    x: left,
    y: top,
    width: right - left,
    height: bottom - top,
    fill: "transparent",
  });
  hit.addEventListener("pointermove", (event) => {
    const bounds = root.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
    const index = Math.round(ratio * (items.length - 1));
    const positionX = x(index);
    crosshair.setAttribute("x1", positionX);
    crosshair.setAttribute("x2", positionX);
    crosshair.setAttribute("visibility", "visible");
    totalPoint.setAttribute("cx", positionX);
    totalPoint.setAttribute("cy", y(Number(items[index].total_market_cap_cny)));
    totalPoint.setAttribute("visibility", "visible");
    floatPoint.setAttribute("cx", positionX);
    floatPoint.setAttribute("cy", y(Number(items[index].float_market_cap_cny)));
    floatPoint.setAttribute("visibility", "visible");
    showMarketCapTooltip(event, items[index], tooltip, root);
  });
  hit.addEventListener("pointerleave", () => {
    tooltip.hidden = true;
    crosshair.setAttribute("visibility", "hidden");
    totalPoint.setAttribute("visibility", "hidden");
    floatPoint.setAttribute("visibility", "hidden");
  });
  svg.append(crosshair, totalPoint, floatPoint, hit);
  root.append(svg, tooltip);

  const first = items[0];
  const latest = items.at(-1);
  const change = Number(first.total_market_cap_cny)
    ? (Number(latest.total_market_cap_cny) / Number(first.total_market_cap_cny) - 1) * 100
    : null;
  byId("market-cap-chart-summary").textContent = `${first.trade_date} 至 ${latest.trade_date} · 总 ${trillion(latest.total_market_cap_cny)} · 流通 ${trillion(latest.float_market_cap_cny)} · ${percent(change)}`;
}

function updateMarketCapControls() {
  document.querySelectorAll("[data-cap-granularity]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.capGranularity === marketCapGranularity));
  });
}

async function loadMarketCapDashboard() {
  byId("market-cap-as-of").textContent = "正在读取板块市值…";
  try {
    marketCapPayload = await fetchJson(
      `/api/v1/market-cap-dashboard?granularity=${encodeURIComponent(marketCapGranularity)}`,
    );
    byId("market-cap-as-of").textContent = `截至 ${marketCapPayload.end_date || "—"} · 单位：万亿元`;
    renderMarketCapRows();
    renderMarketCapChart();
  } catch (error) {
    byId("market-cap-as-of").textContent = error.message;
    byId("market-cap-rows").replaceChildren();
    renderMarketCapChart();
  }
  updateMarketCapControls();
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
    watchlist: byId("stock-plan-only").checked
      ? "plan"
      : byId("stock-scope").value,
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
  if (pageSize !== 50) visibleParameters.set("page_size", String(pageSize));
  else visibleParameters.delete("page_size");
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
  byId("stock-scope").value = requestedScope === "only" ? "only" : "all";
  byId("stock-plan-only").checked = requestedScope === "plan";
  requestedBlock = parameters.get("block") || "";
  const requestedSort = parameters.get("sort") || "";
  sortBy = Object.hasOwn(sortLabels, requestedSort) ? requestedSort : "";
  sortDirection = parameters.get("direction") === "desc" ? "desc" : "asc";
  const requestedOffset = Number(parameters.get("offset"));
  const requestedPageSize = Number(parameters.get("page_size"));
  pageSize = [50, 100, 200].includes(requestedPageSize)
    ? requestedPageSize
    : 50;
  byId("stock-page-size").value = String(pageSize);
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
  byId("stock-plan-only").checked = false;
  pageSize = 50;
  byId("stock-page-size").value = "50";
  sortBy = "";
  sortDirection = "asc";
  offset = 0;
  updateSortHeaders();
  loadStocks();
});

byId("stock-scope").addEventListener("change", () => {
  byId("stock-plan-only").checked = false;
});

byId("stock-plan-only").addEventListener("change", (event) => {
  if (event.target.checked) byId("stock-scope").value = "all";
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

byId("stock-page-size").addEventListener("change", (event) => {
  pageSize = Number(event.target.value);
  offset = 0;
  loadStocks();
});

byId("market-cap-rows").addEventListener("click", (event) => {
  const button = event.target.closest("[data-board]");
  if (!button) return;
  marketCapBoard = button.dataset.board;
  renderMarketCapRows();
  renderMarketCapChart();
});

document.querySelectorAll("[data-cap-granularity]").forEach((button) => {
  button.addEventListener("click", () => {
    marketCapGranularity = button.dataset.capGranularity;
    updateMarketCapControls();
    loadMarketCapDashboard();
  });
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
  loadMarketCapDashboard();
  if (requestedBlock) {
    await loadBlocks().catch(disableBlockFilter);
    loadStocks();
    return;
  }
  await loadStocks();
  loadBlocks().catch(disableBlockFilter);
}

initialize();

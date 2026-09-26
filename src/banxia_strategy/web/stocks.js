const byId = (id) => document.getElementById(id);
const pageSize = 50;
let offset = 0;
let total = 0;
let loading = false;

const boardLabels = {
  main: "沪深主板",
  gem: "创业板",
  star: "科创板",
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

function cell(text, className) {
  const node = document.createElement("td");
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || `请求失败 ${response.status}`);
  return payload;
}

function queryParameters() {
  const parameters = new URLSearchParams({
    q: byId("stock-query").value.trim(),
    board: byId("stock-board").value,
    market: byId("stock-market").value,
    limit: String(pageSize),
    offset: String(offset),
  });
  return parameters;
}

function updateBrowserUrl(parameters) {
  const visibleParameters = new URLSearchParams(parameters);
  visibleParameters.delete("limit");
  if (!visibleParameters.get("q")) visibleParameters.delete("q");
  if (visibleParameters.get("board") === "all") visibleParameters.delete("board");
  if (visibleParameters.get("market") === "all") visibleParameters.delete("market");
  if (visibleParameters.get("offset") === "0") visibleParameters.delete("offset");
  const query = visibleParameters.toString();
  history.replaceState(null, "", query ? `${location.pathname}?${query}` : location.pathname);
}

function renderRows(items) {
  const root = byId("stock-rows");
  root.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const empty = cell("没有符合条件的股票");
    empty.colSpan = 10;
    empty.className = "waiting-cell";
    row.append(empty);
    root.append(row);
    return;
  }

  for (const item of items) {
    const quote = item.quote || {};
    const row = document.createElement("tr");
    const identity = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "stock-identity";
    const name = document.createElement("a");
    name.href = `/stocks/${encodeURIComponent(item.symbol)}`;
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
      ["详情", `/stocks/${item.symbol}`],
      ["历史行情", `/stocks/${item.symbol}/history`],
    ]) {
      const link = document.createElement("a");
      link.href = href;
      link.textContent = label;
      actionWrap.append(link);
    }
    actions.append(actionWrap);

    row.append(
      identity,
      cell(`${boardLabels[item.board]} · ${item.market.toUpperCase()}`, "board-label"),
      cell(price(quote.price), `quote-number ${tone(quote.change_pct)}`),
      cell(percent(quote.change_pct), `quote-number ${tone(quote.change_pct)}`),
      cell(price(quote.open), "quote-number"),
      cell(`${price(quote.high)} / ${price(quote.low)}`, "quote-number"),
      cell(compact(quote.volume ?? quote.vol), "quote-number"),
      cell(compact(quote.amount), "quote-number"),
      cell(quote.servertime || "—", "quote-number"),
      actions,
    );
    root.append(row);
  }
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
  try {
    const parameters = queryParameters();
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
    byId("stock-status").textContent = `已读取 ${payload.items.length} 只股票 · ${payload.source}`;
    updateBrowserUrl(parameters);
  } catch (error) {
    byId("stock-status").textContent = error.message;
    renderRows([]);
  } finally {
    loading = false;
    updatePaging();
  }
}

function restoreFilters() {
  const parameters = new URLSearchParams(location.search);
  byId("stock-query").value = parameters.get("q") || "";
  byId("stock-board").value = parameters.get("board") || "all";
  byId("stock-market").value = parameters.get("market") || "all";
  const requestedOffset = Number(parameters.get("offset"));
  offset = Number.isInteger(requestedOffset) && requestedOffset >= 0
    ? requestedOffset
    : 0;
}

byId("stock-filters").addEventListener("submit", (event) => {
  event.preventDefault();
  offset = 0;
  loadStocks();
});

byId("stock-reset").addEventListener("click", () => {
  byId("stock-query").value = "";
  byId("stock-board").value = "all";
  byId("stock-market").value = "all";
  offset = 0;
  loadStocks();
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

restoreFilters();
loadStocks();

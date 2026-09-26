const byId = (id) => document.getElementById(id);
const symbol = location.pathname.split("/").filter(Boolean)[1] || "";
const pageSize = 100;
let period = new URLSearchParams(location.search).get("period") || "day";
let items = [];
let page = 0;
let loading = false;

const periodLabels = {
  minute: "分时",
  day: "日 K",
  week: "周 K",
  month: "月 K",
  year: "年 K",
};

function numeric(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

function price(value) {
  return numeric(value) ? Number(value).toFixed(2) : "—";
}

function compact(value) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(1)}万`;
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function percent(value) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function tone(value) {
  return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "";
}

function svgNode(tag, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) {
    node.setAttribute(key, value);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || `请求失败 ${response.status}`);
  return payload;
}

function showTooltip(event, item, tooltip, root) {
  tooltip.replaceChildren();
  for (const text of [
    item.time.replace("T", " ").slice(0, period === "minute" ? 16 : 10),
    `开 ${price(item.open)}  高 ${price(item.high)}`,
    `低 ${price(item.low)}  收 ${price(item.close)}`,
    `量 ${compact(item.volume)}  额 ${compact(item.amount)}`,
  ]) {
    const line = document.createElement("span");
    line.textContent = text;
    tooltip.append(line);
  }
  const bounds = root.getBoundingClientRect();
  tooltip.style.left = `${Math.max(90, Math.min(bounds.width - 90, event.clientX - bounds.left))}px`;
  tooltip.style.top = `${Math.max(8, event.clientY - bounds.top - 92)}px`;
  tooltip.hidden = false;
}

function renderChart(allItems) {
  const root = byId("history-chart");
  root.replaceChildren();
  const chartItems = allItems.slice(-180).filter((item) => (
    numeric(item.open)
    && numeric(item.high)
    && numeric(item.low)
    && numeric(item.close)
  ));
  if (!chartItems.length) {
    const empty = document.createElement("div");
    empty.className = "history-chart-empty";
    empty.textContent = `暂无${periodLabels[period]}行情`;
    root.append(empty);
    return;
  }

  const width = 1200;
  const height = 420;
  const left = 18;
  const right = 1110;
  const priceTop = 18;
  const priceBottom = 310;
  const volumeTop = 328;
  const volumeBottom = 386;
  const lows = chartItems.map((item) => Number(item.low));
  const highs = chartItems.map((item) => Number(item.high));
  const rawLow = Math.min(...lows);
  const rawHigh = Math.max(...highs);
  const padding = Math.max((rawHigh - rawLow) * 0.06, rawHigh * 0.002);
  const minimum = rawLow - padding;
  const maximum = rawHigh + padding;
  const priceY = (value) => priceTop
    + (maximum - Number(value)) / (maximum - minimum)
    * (priceBottom - priceTop);
  const maxVolume = Math.max(...chartItems.map((item) => Number(item.volume) || 0), 1);
  const band = (right - left) / chartItems.length;
  const bodyWidth = Math.max(1, Math.min(7, band * 0.65));
  const svg = svgNode("svg", {
    viewBox: `0 0 ${width} ${height}`,
    role: "img",
    "aria-label": `${periodLabels[period]}历史行情图，共${chartItems.length}根`,
  });

  for (const value of [maximum, (maximum + minimum) / 2, minimum]) {
    const y = priceY(value);
    svg.append(
      svgNode("line", {
        x1: left,
        x2: right,
        y1: y,
        y2: y,
        stroke: "#d7dcd8",
      }),
      svgNode("text", {
        x: right + 12,
        y: y + 4,
        fill: "#66716b",
        "font-size": 11,
      }, price(value)),
    );
  }

  const tooltip = document.createElement("div");
  tooltip.className = "history-tooltip";
  tooltip.hidden = true;
  chartItems.forEach((item, index) => {
    const x = left + band * (index + 0.5);
    const openY = priceY(item.open);
    const closeY = priceY(item.close);
    const rising = Number(item.close) >= Number(item.open);
    const color = rising ? "#a33c32" : "#126044";
    const group = svgNode("g", {});
    group.append(
      svgNode("line", {
        x1: x,
        x2: x,
        y1: priceY(item.high),
        y2: priceY(item.low),
        stroke: color,
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
        y: volumeBottom - (Number(item.volume) || 0) / maxVolume * (volumeBottom - volumeTop),
        width: bodyWidth,
        height: (Number(item.volume) || 0) / maxVolume * (volumeBottom - volumeTop),
        fill: color,
        opacity: .55,
      }),
    );
    const hit = svgNode("rect", {
      x: left + band * index,
      y: priceTop,
      width: Math.max(1, band),
      height: volumeBottom - priceTop,
      fill: "transparent",
    });
    hit.addEventListener("pointerenter", (event) => showTooltip(event, item, tooltip, root));
    hit.addEventListener("pointermove", (event) => showTooltip(event, item, tooltip, root));
    hit.addEventListener("pointerleave", () => { tooltip.hidden = true; });
    group.append(hit);
    svg.append(group);
  });

  for (const index of [0, Math.floor((chartItems.length - 1) / 2), chartItems.length - 1]) {
    const item = chartItems[index];
    svg.append(svgNode("text", {
      x: left + band * (index + 0.5),
      y: 410,
      fill: "#66716b",
      "font-size": 10,
      "text-anchor": index === 0 ? "start" : index === chartItems.length - 1 ? "end" : "middle",
    }, item.time.slice(period === "minute" ? 11 : 2, period === "minute" ? 16 : 10)));
  }
  root.append(svg, tooltip);
}

function renderSummary() {
  if (!items.length) {
    for (const id of ["range-label", "latest-close", "range-change", "range-high", "range-low", "range-amount"]) {
      byId(id).textContent = "—";
    }
    return;
  }
  const first = items[0];
  const last = items.at(-1);
  const change = numeric(first.close) && numeric(last.close)
    ? (Number(last.close) / Number(first.close) - 1) * 100
    : null;
  const amounts = items.map((item) => Number(item.amount) || 0);
  byId("range-label").textContent = `${first.date} 至 ${last.date}`;
  byId("latest-close").textContent = price(last.close);
  byId("range-change").textContent = percent(change);
  byId("range-change").className = tone(change);
  byId("range-high").textContent = price(Math.max(...items.map((item) => Number(item.high))));
  byId("range-low").textContent = price(Math.min(...items.map((item) => Number(item.low))));
  byId("range-amount").textContent = amounts.some(Boolean)
    ? compact(amounts.reduce((sum, value) => sum + value, 0))
    : "—";
}

function renderRows() {
  const root = byId("history-rows");
  root.replaceChildren();
  const descending = [...items].reverse();
  const visible = descending.slice(page * pageSize, (page + 1) * pageSize);
  if (!visible.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.className = "waiting-cell";
    cell.textContent = "暂无历史行情";
    row.append(cell);
    root.append(row);
  }
  let previousClose = null;
  const changes = new Map();
  for (const item of items) {
    changes.set(
      item.time,
      numeric(previousClose) && numeric(item.close)
        ? (Number(item.close) / Number(previousClose) - 1) * 100
        : null,
    );
    previousClose = item.close;
  }
  for (const item of visible) {
    const row = document.createElement("tr");
    const change = changes.get(item.time);
    const values = [
      item.time.replace("T", " ").slice(0, period === "minute" ? 16 : 10),
      price(item.open),
      price(item.high),
      price(item.low),
      price(item.close),
      percent(change),
      compact(item.volume),
      compact(item.amount),
    ];
    values.forEach((value, index) => {
      const cell = document.createElement("td");
      cell.textContent = value;
      if (index === 5) cell.className = tone(change);
      row.append(cell);
    });
    root.append(row);
  }
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  byId("history-page").textContent = `第 ${page + 1} / ${pages} 页`;
  byId("history-previous").disabled = page === 0;
  byId("history-next").disabled = page + 1 >= pages;
}

function updatePeriodControls() {
  document.querySelectorAll("[data-period]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.period === period));
  });
  byId("minute-date-control").hidden = period !== "minute";
}

async function loadHistory(refresh = false) {
  if (loading) return;
  loading = true;
  byId("history-refresh").disabled = true;
  byId("history-status").textContent = `正在读取${periodLabels[period]}行情…`;
  updatePeriodControls();
  const parameters = new URLSearchParams({
    period,
    limit: "20000",
  });
  if (period === "minute") {
    parameters.set("trade_date", byId("minute-date").value);
  }
  if (refresh) parameters.set("refresh", "true");
  try {
    const payload = await fetchJson(
      `/api/v1/stocks/${encodeURIComponent(symbol)}/history?${parameters}`,
    );
    items = payload.items || [];
    page = 0;
    byId("history-name").textContent = payload.name;
    byId("history-code").textContent = payload.symbol;
    byId("history-storage").textContent = payload.served_by === "clickhouse"
      ? "本地历史库"
      : "mootdx 已回填本地";
    byId("history-status").textContent = `${periodLabels[period]} · ${items.length.toLocaleString("zh-CN")} 条`;
    byId("history-count").textContent = `图示最近 ${Math.min(items.length, 180)} 条 · 明细保留全部 ${items.length.toLocaleString("zh-CN")} 条`;
    document.title = `${payload.name}历史行情 · 一进二`;
    renderSummary();
    renderChart(items);
    renderRows();
    const locationParameters = new URLSearchParams({ period });
    if (period === "minute") {
      locationParameters.set("trade_date", byId("minute-date").value);
    }
    history.replaceState(null, "", `${location.pathname}?${locationParameters}`);
  } catch (error) {
    items = [];
    byId("history-status").textContent = error.message;
    renderSummary();
    renderChart([]);
    renderRows();
  } finally {
    loading = false;
    byId("history-refresh").disabled = false;
  }
}

async function initialize() {
  if (!/^\d{6}$/.test(symbol)) {
    byId("history-status").textContent = "股票代码无效";
    return;
  }
  byId("history-code").textContent = symbol;
  byId("detail-link").href = `/stocks/${symbol}`;
  try {
    const [directory, calendar] = await Promise.all([
      fetchJson(`/api/v1/stocks?q=${encodeURIComponent(symbol)}&limit=1`),
      fetchJson("/api/v1/trading-calendar"),
    ]);
    const stock = directory.items.find((item) => item.symbol === symbol);
    byId("history-name").textContent = stock?.name || symbol;
    const dateSelect = byId("minute-date");
    for (const value of [...calendar.sessions].reverse()) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      dateSelect.append(option);
    }
    const requestedDate = new URLSearchParams(location.search).get("trade_date");
    dateSelect.value = calendar.sessions.includes(requestedDate)
      ? requestedDate
      : calendar.defaults.monitor;
  } catch (error) {
    byId("history-status").textContent = error.message;
    return;
  }
  if (!Object.hasOwn(periodLabels, period)) period = "day";
  loadHistory();
}

document.querySelectorAll("[data-period]").forEach((button) => {
  button.addEventListener("click", () => {
    period = button.dataset.period;
    loadHistory();
  });
});

byId("minute-date").addEventListener("change", () => loadHistory());
byId("history-refresh").addEventListener("click", () => loadHistory(true));
byId("history-previous").addEventListener("click", () => {
  page = Math.max(0, page - 1);
  renderRows();
});
byId("history-next").addEventListener("click", () => {
  if ((page + 1) * pageSize < items.length) {
    page += 1;
    renderRows();
  }
});

initialize();

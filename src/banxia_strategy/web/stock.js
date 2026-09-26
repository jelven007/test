const byId = (id) => document.getElementById(id);
const symbol = location.pathname.split("/").filter(Boolean)[1] || "";
let activeSection = "";

const boardLabels = {
  main: "沪深主板",
  gem: "创业板",
  star: "科创板",
};

const financeLabels = {
  market: "市场代码",
  code: "证券代码",
  liutongguben: "流通股本",
  province: "省份代码",
  industry: "行业代码",
  updated_date: "更新日期",
  ipo_date: "上市日期",
  zongguben: "总股本",
  guojiagu: "国家股",
  faqirenfarengu: "发起人法人股",
  farengu: "法人股",
  bgu: "B 股",
  hgu: "H 股",
  zhigonggu: "职工股",
  zongzichan: "总资产",
  liudongzichan: "流动资产",
  gudingzichan: "固定资产",
  wuxingzichan: "无形资产",
  gudongrenshu: "股东人数",
  liudongfuzhai: "流动负债",
  changqifuzhai: "长期负债",
  zibengongjijin: "资本公积金",
  jingzichan: "净资产",
  zhuyingshouru: "主营收入",
  zhuyinglirun: "主营利润",
  yingshouzhangkuan: "应收账款",
  yingyelirun: "营业利润",
  touzishouyu: "投资收益",
  jingyingxianjinliu: "经营现金流",
  zongxianjinliu: "总现金流",
  cunhuo: "存货",
  lirunzonghe: "利润总额",
  shuihoulirun: "税后利润",
  jinglirun: "净利润",
  weifenpeilirun: "未分配利润",
  meigujingzichan: "每股净资产",
  baoliu2: "保留字段",
};

function numeric(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

function price(value) {
  return numeric(value) ? Number(value).toFixed(2) : "—";
}

function compact(value) {
  if (!numeric(value)) return value ?? "—";
  const number = Number(value);
  if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(2)}亿`;
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(2)}万`;
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 4 });
}

function percent(value) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function tone(value) {
  return Number(value) > 0 ? "up" : Number(value) < 0 ? "down" : "";
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || `请求失败 ${response.status}`);
  return payload;
}

function renderOrderBook(quote) {
  const root = byId("order-book");
  root.replaceChildren();
  for (const index of [5, 4, 3, 2, 1]) {
    const label = document.createElement("dt");
    label.textContent = `卖 ${index}`;
    const value = document.createElement("dd");
    value.textContent = price(quote[`ask${index}`]);
    value.className = "down";
    const volume = document.createElement("dd");
    volume.textContent = compact(quote[`ask_vol${index}`]);
    root.append(label, value, volume);
  }
  for (const index of [1, 2, 3, 4, 5]) {
    const label = document.createElement("dt");
    label.textContent = `买 ${index}`;
    const value = document.createElement("dd");
    value.textContent = price(quote[`bid${index}`]);
    value.className = "up";
    const volume = document.createElement("dd");
    volume.textContent = compact(quote[`bid_vol${index}`]);
    root.append(label, value, volume);
  }
}

function renderFinancialMetrics(finance, quote) {
  const root = byId("financial-metrics");
  root.replaceChildren();
  const current = Number(quote.price);
  const metrics = [
    ["总市值", numeric(current) ? compact(current * Number(finance.zongguben || 0)) : "—"],
    ["流通市值", numeric(current) ? compact(current * Number(finance.liutongguben || 0)) : "—"],
    ["总资产", compact(finance.zongzichan)],
    ["主营收入", compact(finance.zhuyingshouru)],
    ["净利润", compact(finance.jinglirun)],
    ["股东人数", compact(finance.gudongrenshu)],
  ];
  for (const [label, value] of metrics) {
    const item = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    item.append(term, detail);
    root.append(item);
  }
}

function renderFinanceFields(finance) {
  const root = byId("finance-fields");
  root.replaceChildren();
  const entries = Object.entries(finance);
  for (let index = 0; index < entries.length; index += 2) {
    const row = document.createElement("tr");
    for (const entry of entries.slice(index, index + 2)) {
      const key = document.createElement("td");
      key.textContent = `${financeLabels[entry[0]] || entry[0]} (${entry[0]})`;
      const value = document.createElement("td");
      value.textContent = compact(entry[1]);
      row.append(key, value);
    }
    if (row.children.length === 2) {
      row.append(document.createElement("td"), document.createElement("td"));
    }
    root.append(row);
  }
}

function renderActions(items) {
  const root = byId("action-rows");
  root.replaceChildren();
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 8;
    cell.className = "waiting-cell";
    cell.textContent = "暂无除权除息记录";
    row.append(cell);
    root.append(row);
    return;
  }
  const values = [...items].reverse();
  for (const item of values) {
    const row = document.createElement("tr");
    const actionDate = [item.year, item.month, item.day]
      .map((value, index) => String(value).padStart(index ? 2 : 4, "0"))
      .join("-");
    for (const value of [
      actionDate,
      item.name || item.category,
      compact(item.fenhong),
      compact(item.songzhuangu),
      compact(item.peigu),
      compact(item.peigujia),
      compact(item.qianzongguben),
      compact(item.houzongguben),
    ]) {
      const cell = document.createElement("td");
      cell.textContent = value;
      row.append(cell);
    }
    root.append(row);
  }
}

async function selectCompanySection(section) {
  if (!section || section === activeSection) return;
  activeSection = section;
  byId("company-section-title").textContent = section;
  byId("company-section-meta").textContent = "正在读取 mootdx F10…";
  byId("company-content").textContent = "正在读取公司资料…";
  document.querySelectorAll("#company-sections button").forEach((button) => {
    button.setAttribute("aria-current", String(button.dataset.section === section));
  });
  try {
    const parameters = new URLSearchParams({ section });
    const payload = await fetchJson(
      `/api/v1/stocks/${encodeURIComponent(symbol)}/company?${parameters}`,
    );
    if (activeSection !== section) return;
    byId("company-content").textContent = payload.content || "该栏目暂无内容。";
    byId("company-section-meta").textContent = `${payload.source} F10 · ${section}`;
  } catch (error) {
    byId("company-content").textContent = error.message;
    byId("company-section-meta").textContent = "读取失败";
  }
}

function renderSections(sections) {
  const root = byId("company-sections");
  root.replaceChildren();
  for (const item of sections) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.section = item.name;
    button.textContent = item.name;
    button.title = `${item.name} · ${compact(item.length)} 字节`;
    button.addEventListener("click", () => selectCompanySection(item.name));
    root.append(button);
  }
  const initial = sections.find((item) => item.name === "公司概况") || sections[0];
  if (initial) selectCompanySection(initial.name);
}

function render(payload) {
  const quote = payload.quote || {};
  const finance = payload.finance || {};
  document.title = `${payload.name} ${payload.symbol} · 一进二`;
  byId("stock-code").textContent = payload.symbol;
  byId("stock-name").textContent = payload.name;
  byId("stock-market-label").textContent = `${boardLabels[payload.board]} · ${payload.market.toUpperCase()}`;
  byId("history-link").href = `/stocks/${payload.symbol}/history`;
  byId("quote-price").textContent = price(quote.price);
  byId("quote-price").className = tone(quote.change_pct);
  byId("quote-change").textContent = percent(quote.change_pct);
  byId("quote-change").className = tone(quote.change_pct);
  byId("quote-close").textContent = price(quote.previous_close);
  byId("quote-open").textContent = price(quote.open);
  byId("quote-high").textContent = price(quote.high);
  byId("quote-low").textContent = price(quote.low);
  byId("quote-volume").textContent = compact(quote.volume ?? quote.vol);
  byId("quote-amount").textContent = compact(quote.amount);
  byId("quote-buy-volume").textContent = compact(quote.b_vol);
  byId("quote-sell-volume").textContent = compact(quote.s_vol);
  byId("finance-date").textContent = finance.updated_date
    ? `财务更新 ${finance.updated_date}`
    : "mootdx 财务快照";
  renderOrderBook(quote);
  renderFinancialMetrics(finance, quote);
  renderFinanceFields(finance);
  renderActions(payload.corporate_actions || []);
  renderSections(payload.sections || []);
  byId("detail-status").textContent = `盘口时间 ${quote.servertime || "—"} · ${payload.source}`;
}

async function load() {
  if (!/^\d{6}$/.test(symbol)) {
    byId("detail-status").textContent = "股票代码无效";
    return;
  }
  try {
    render(await fetchJson(`/api/v1/stocks/${encodeURIComponent(symbol)}`));
  } catch (error) {
    byId("detail-status").textContent = error.message;
  }
}

load();

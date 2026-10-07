const byId = (id) => document.getElementById(id);

const campaignCatalog = {
  "first-board-positive-v1": {
    label: "一进二 V1",
    tagline: "按冻结的一进二弱转强规则，累计 30 笔新增样本。",
    rules: [
      { phase: "D1", title: "首板筛选", detail: "换手率 3%–15%，首封不晚于 10:30。" },
      { phase: "D2", title: "确认入场", detail: "开盘 0.5%–5%，首分钟涨幅 ≥ 6.5%，09:43 前出现触板成交窗口。" },
      { phase: "D3", title: "隔日退出", detail: "达到 +5.1% 止盈，否则 14:55 强制退出。" },
    ],
  },
  "fusion-l7-v1": {
    label: "Fusion L7 V1",
    tagline: "半夏+炒股养家融合 L2+L4+L6 筛选 · L5 暂停 · 遵守 T+1，D3 执行 Layer 7 退出。",
    rules: [
      { phase: "D1", title: "L2+L4 筛选", detail: "全市场扫描，排除 ST/*ST，D1 涨幅 ≥ 3%，Rule A：末 30 分钟 r_last30 < -0.5% 且 close_loc < 0.75。" },
      { phase: "D2", title: "L6 跳空闸门", detail: "开盘跳空 ∈ [-1%, +4%] 才入场，09:31 开盘价成交，单票 20% 仓位，并发 ≤ 5。" },
      { phase: "D3", title: "L7 动态退出", detail: "D2 不卖；D3 首次触及 TP +5.0% 或 SL -2.5% 时退出，未触发则 14:55 发出强退指令，以 14:56 分钟价代理。" },
    ],
  },
};
const defaultCampaignCode = "first-board-positive-v1";
let currentCampaignCode = defaultCampaignCode;

const tradeStatus = {
  entry_data_missing: { label: "入场数据缺失", tone: "waiting", group: "missing" },
  rejected: { label: "未成交", tone: "neutral", group: "rejected" },
  open: { label: "持仓中", tone: "focus", group: "open" },
  exit_data_missing: { label: "退出数据缺失", tone: "waiting", group: "missing" },
  closed: { label: "已结算", tone: "neutral", group: "settled" },
  exit_unfilled: { label: "退出未成交", tone: "negative", group: "settled" },
};

const campaignStatus = {
  running: { label: "运行中", tone: "focus" },
  completed: { label: "已完成", tone: "focus" },
  stopped: { label: "已停止", tone: "risk" },
  paused: { label: "已暂停", tone: "waiting" },
};

let trades = [];
let loading = false;

function numeric(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value));
}

function formatPercent(value, signed = true) {
  if (!numeric(value)) return "—";
  const number = Number(value);
  return `${signed && number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function formatPrice(value) {
  return numeric(value) ? Number(value).toFixed(2) : "—";
}

function formatTime(value) {
  return value ? String(value).slice(0, 5) : "—";
}

function formatDate(value) {
  return value || "—";
}

function cell(text, className = "") {
  const node = document.createElement("td");
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function errorMessage(payload, status) {
  return payload?.error?.message || payload?.detail || `请求失败 ${status}`;
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  let payload;
  try {
    payload = await response.json();
  } catch (_error) {
    throw new Error(`接口返回格式异常 ${response.status}`);
  }
  if (!response.ok) throw new Error(errorMessage(payload, response.status));
  return payload;
}

function setMetric(id, value, toneValue = null) {
  const node = byId(id);
  node.textContent = value;
  node.classList.remove("is-positive", "is-negative");
  if (numeric(toneValue)) {
    if (Number(toneValue) > 0) node.classList.add("is-positive");
    if (Number(toneValue) < 0) node.classList.add("is-negative");
  }
}

function renderSummary(summary) {
  const target = Number(summary.target_sample_count) || 30;
  const completed = Number(summary.completed_sample_count) || 0;
  const remaining = Number(summary.remaining_sample_count) || 0;
  const progress = Math.min(100, Math.max(0, 100 * completed / target));
  const state = campaignStatus[summary.status] || {
    label: summary.status || "未知状态",
    tone: "neutral",
  };

  byId("campaign-code").textContent = summary.code || "—";
  byId("campaign-start").textContent = formatDate(summary.started_on);
  byId("campaign-state").textContent = state.label;
  byId("campaign-state").dataset.tone = state.tone;
  byId("progress-completed").textContent = completed;
  byId("progress-target").textContent = target;
  byId("progress-caption").textContent = completed >= target
    ? `样本目标已完成${summary.completed_at ? ` · ${summary.completed_at}` : ""}`
    : completed
      ? `已完成 ${progress.toFixed(0)}%，仍需 ${remaining} 笔有效结算`
      : "等待第一笔新增样本";

  const progressNode = byId("sample-progress");
  progressNode.style.setProperty("--progress", `${progress}%`);
  progressNode.setAttribute("aria-valuemax", String(target));
  progressNode.setAttribute("aria-valuenow", String(completed));

  setMetric("metric-completed", `${completed} 笔`);
  setMetric("metric-remaining", `${remaining} 笔`);
  setMetric(
    "metric-positive-rate",
    numeric(summary.positive_rate_pct)
      ? formatPercent(summary.positive_rate_pct, false)
      : "待结算",
  );
  setMetric(
    "metric-wilson",
    numeric(summary.wilson_95_lower_pct) && numeric(summary.wilson_95_upper_pct)
      ? `${Number(summary.wilson_95_lower_pct).toFixed(1)}–${Number(summary.wilson_95_upper_pct).toFixed(1)}%`
      : "待结算",
  );
  setMetric(
    "metric-average",
    formatPercent(summary.average_net_return_pct),
    summary.average_net_return_pct,
  );
  setMetric(
    "metric-worst",
    formatPercent(summary.worst_net_return_pct),
    summary.worst_net_return_pct,
  );
  byId("counter-open").textContent = Number(summary.open_trade_count) || 0;
  byId("counter-missing").textContent = Number(summary.missing_data_count) || 0;
  byId("counter-rejected").textContent = Number(summary.rejected_count) || 0;
}

function statusFor(item) {
  const base = tradeStatus[item.status] || {
    label: item.status || "未知",
    tone: "neutral",
    group: "other",
  };
  if (item.status === "closed" && item.positive === true) {
    return { ...base, label: "正收益", tone: "positive" };
  }
  if (item.status === "closed" && item.positive === false) {
    return { ...base, label: "负收益", tone: "negative" };
  }
  return base;
}

function priceDetail(time, price) {
  if (!numeric(price)) return "—";
  return `${formatTime(time)} · ¥${formatPrice(price)}`;
}

function resultReason(item) {
  if (item.rejection_reason) return item.rejection_reason;
  if (item.exit_reason) return item.exit_reason;
  if (item.status === "open") return "等待隔日退出";
  if (item.status === "entry_data_missing") return "无法取得满足校验要求的入场行情";
  if (item.status === "exit_data_missing") return "等待补齐隔日退出行情";
  return "—";
}

function renderTrades() {
  const filter = byId("trade-status-filter").value;
  const visible = trades.filter((item) => (
    filter === "all" || statusFor(item).group === filter
  ));
  const root = byId("paper-trade-rows");
  root.replaceChildren();

  byId("ledger-count").textContent = filter === "all"
    ? `共 ${trades.length} 笔记录`
    : `显示 ${visible.length} / ${trades.length} 笔`;

  if (!visible.length) {
    const row = document.createElement("tr");
    const empty = cell(
      trades.length ? "当前筛选下没有记录" : "尚无新增样本，工作日收盘后自动更新",
      "table-empty",
    );
    empty.colSpan = 8;
    row.append(empty);
    root.append(row);
    return;
  }

  for (const item of visible) {
    const row = document.createElement("tr");
    const identity = document.createElement("td");
    const identityWrap = document.createElement("div");
    identityWrap.className = "trade-identity";
    const name = document.createElement("a");
    name.href = `/stocks/${encodeURIComponent(item.symbol)}`;
    name.textContent = item.name || item.symbol;
    const code = document.createElement("code");
    code.textContent = [item.symbol, item.industry].filter(Boolean).join(" · ");
    identityWrap.append(name, code);
    identity.append(identityWrap);

    const status = statusFor(item);
    const statusCell = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = "status-badge trade-status";
    badge.dataset.tone = status.tone;
    badge.textContent = status.label;
    statusCell.append(badge);

    const entry = cell(
      priceDetail(item.entry_time, item.entry_price),
      "trade-price",
    );
    const target = cell(
      numeric(item.target_price) ? `¥${formatPrice(item.target_price)}` : "—",
      "trade-price",
    );
    const exit = cell(
      priceDetail(item.exit_time, item.exit_price),
      "trade-price",
    );
    const netReturn = cell(
      formatPercent(item.net_return_pct),
      "trade-return",
    );
    if (numeric(item.net_return_pct)) {
      netReturn.classList.add(Number(item.net_return_pct) > 0 ? "is-positive" : "is-negative");
    }

    row.append(
      cell(formatDate(item.entry_date), "trade-date"),
      identity,
      statusCell,
      entry,
      target,
      exit,
      netReturn,
      cell(resultReason(item), "trade-detail"),
    );
    root.append(row);
  }
}

async function loadPaperTrading() {
  if (loading) return;
  loading = true;
  const refresh = byId("refresh-paper");
  refresh.disabled = true;
  byId("paper-status").textContent = "正在读取模拟盘进度…";
  byId("paper-status").classList.remove("is-error");
  byId("paper-error").hidden = true;

  const code = encodeURIComponent(currentCampaignCode);
  try {
    const [summary, tradePayload] = await Promise.all([
      fetchJson(`/api/v1/paper-trading?campaign=${code}`),
      fetchJson(
        `/api/v1/paper-trading/trades?limit=200&offset=0&campaign=${code}`,
      ),
    ]);
    trades = Array.isArray(tradePayload.items) ? tradePayload.items : [];
    renderSummary(summary);
    renderTrades();
    const refreshed = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
    byId("last-refreshed").textContent = refreshed;
    byId("paper-status").textContent = summary.completed_sample_count
      ? `已读取 ${summary.completed_sample_count} 笔有效结算`
      : "活动运行正常，当前尚无有效结算样本";
  } catch (error) {
    byId("paper-status").textContent = "模拟盘数据读取失败";
    byId("paper-status").classList.add("is-error");
    byId("paper-error").textContent = error.message;
    byId("paper-error").hidden = false;
  } finally {
    loading = false;
    refresh.disabled = false;
  }
}

function renderRules() {
  const list = byId("execution-rules-list");
  if (!list) return;
  const definition = campaignCatalog[currentCampaignCode];
  list.replaceChildren();
  for (const rule of definition.rules) {
    const li = document.createElement("li");
    const phase = document.createElement("span");
    phase.textContent = rule.phase;
    const title = document.createElement("strong");
    title.textContent = rule.title;
    const detail = document.createElement("p");
    detail.textContent = rule.detail;
    li.append(phase, title, detail);
    list.append(li);
  }
}

function selectCampaign(code) {
  if (!(code in campaignCatalog) || code === currentCampaignCode) return;
  currentCampaignCode = code;
  const definition = campaignCatalog[code];
  byId("campaign-tagline").textContent = definition.tagline;
  for (const tab of document.querySelectorAll(".campaign-tab")) {
    const active = tab.dataset.campaign === code;
    tab.setAttribute("aria-selected", String(active));
  }
  trades = [];
  renderRules();
  loadPaperTrading();
}

renderRules();
for (const tab of document.querySelectorAll(".campaign-tab")) {
  tab.addEventListener("click", () => selectCampaign(tab.dataset.campaign));
}
byId("trade-status-filter").addEventListener("change", renderTrades);
byId("refresh-paper").addEventListener("click", loadPaperTrading);
loadPaperTrading();

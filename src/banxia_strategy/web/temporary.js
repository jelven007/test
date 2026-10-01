"use strict";

const pageMode = document.body.dataset.temporaryPage;
const dateControl = document.querySelector("#report-date");
const refreshButton = document.querySelector("#refresh-button");
const sourceStatus = document.querySelector(".source-status");
const errorBox = document.querySelector("#temporary-error");
let latestPayload = null;
let selectedStrategyId = null;
let loading = false;

function byId(id) {
  return document.getElementById(id);
}

function node(tag, text, className) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  if (className) item.className = className;
  return item;
}

function write(id, value) {
  const target = byId(id);
  if (target) target.textContent = value ?? "—";
}

function dateLabel(value) {
  return value ? String(value).replaceAll("-", ".") : "—";
}

function clock(value) {
  if (!value) return "—";
  const stamp = new Date(value);
  if (Number.isNaN(stamp.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(stamp);
}

function price(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "—";
}

function percent(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function amount(value) {
  if (value === null || value === undefined || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number >= 100_000_000) return `${(number / 100_000_000).toFixed(2)}亿`;
  if (number >= 10_000) return `${(number / 10_000).toFixed(0)}万`;
  return number.toLocaleString("zh-CN");
}

function showError(message) {
  errorBox.hidden = !message;
  errorBox.textContent = message || "";
  sourceStatus.className = `source-status ${message ? "error" : "ready"}`;
  write("connection", message ? "临时数据读取失败" : "十策略数据已同步");
}

async function fetchJson(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(
      payload.error?.message || payload.detail || `请求失败（${response.status}）`,
    );
  }
  return payload;
}

function candidateTable(candidates, mode) {
  const wrap = node("div", undefined, "candidate-table-wrap");
  const table = node("table", undefined, "candidate-table");
  const head = node("thead");
  const header = node("tr");
  const labels = mode === "plan"
    ? ["顺位", "股票", "评分", "昨收", "题材", "入场条件", "放弃条件"]
    : ["顺位", "股票", "最新 / 涨幅", "开盘 / 成交额", "盘口时间", "实时判断"];
  const widths = mode === "plan"
    ? ["7%", "14%", "8%", "9%", "13%", "24%", "25%"]
    : ["7%", "16%", "15%", "16%", "13%", "33%"];
  labels.forEach((label, index) => {
    const cell = node("th", label);
    cell.style.width = widths[index];
    header.append(cell);
  });
  head.append(header);
  const body = node("tbody");
  candidates.forEach((candidate, index) => {
    const row = node("tr");
    row.append(node("td", String(candidate.rank || index + 1).padStart(2, "0"), "rank-cell"));
    const identity = node("td", undefined, "stock-cell");
    identity.append(
      node("strong", candidate.name || "—"),
      node("small", candidate.symbol || candidate.code || "—"),
    );
    row.append(identity);
    if (mode === "plan") {
      row.append(
        node("td", Number(candidate.score || 0).toFixed(1), "score-cell"),
        node("td", price(candidate.latest_price), "price-cell"),
        node("td", candidate.industry || "未分类"),
        node("td", candidate.entry_trigger || "等待计划规则", "condition-cell"),
        node("td", candidate.invalidation || "等待计划规则", "condition-cell"),
      );
    } else {
      const quote = node("td", undefined, "quote-cell");
      const quotePrice = node("strong", price(candidate.price));
      const quoteChange = node("small", percent(candidate.change_pct));
      quoteChange.className = Number(candidate.change_pct) > 0
        ? "up"
        : Number(candidate.change_pct) < 0 ? "down" : "";
      quote.append(quotePrice, quoteChange);
      const opening = node("td", undefined, "quote-cell");
      opening.append(
        node("strong", price(candidate.open)),
        node("small", amount(candidate.amount_cny)),
      );
      const decision = candidate.decision || {};
      const decisionCell = node("td", undefined, "decision-cell");
      const risky = decision.irreversible
        || ["stale", "unavailable", "reject_open", "reject_low", "outside_open", "window_closed"].includes(decision.state);
      decisionCell.dataset.tone = risky
        ? "risk"
        : ["sealed", "at_limit", "near_limit", "triggered"].includes(decision.state)
          ? "focus" : "muted";
      decisionCell.append(
        node("strong", decision.label || "等待判断"),
        node("small", decision.reason || "尚无实时策略判断"),
      );
      row.append(
        quote,
        opening,
        node("td", clock(candidate.source_time), "quote-cell"),
        decisionCell,
      );
    }
    body.append(row);
  });
  table.append(head, body);
  wrap.append(table);
  return wrap;
}

function emptyState(title, detail) {
  const empty = node("div", undefined, "empty-strategy");
  empty.append(node("strong", title), node("span", detail));
  return empty;
}

function renderPlan(payload) {
  latestPayload = payload;
  write("display-date", dateLabel(payload.requested_date));
  write("strategy-count", `${payload.strategy_count} 条`);
  write("ready-count", `${payload.ready_count} / ${payload.strategy_count}`);
  write("candidate-count", `${payload.candidate_count} 只`);
  write("execution-date", dateLabel(payload.plan_date));
  write(
    "scope",
    payload.resolved_from_non_trading_day
      ? `所选日期为休市日，展示 ${payload.trade_date} 收盘后生成的十策略计划。`
      : `${payload.ready_count} 条策略已生成，共 ${payload.candidate_count} 只候选。`,
  );

  const jump = byId("strategy-jump");
  const list = byId("strategy-plans");
  jump.replaceChildren();
  list.replaceChildren();
  payload.strategies.forEach((strategy, index) => {
    const anchor = `temporary-strategy-${index + 1}`;
    const link = node("a", `${String(index + 1).padStart(2, "0")} ${strategy.name}`);
    link.href = `#${anchor}`;
    jump.append(link);

    const section = node("section", undefined, "strategy-plan");
    section.id = anchor;
    const header = node("header", undefined, "strategy-plan-header");
    const title = node("div", undefined, "strategy-plan-title");
    title.append(
      node("span", String(index + 1).padStart(2, "0"), "strategy-number"),
      node("h2", strategy.name),
    );
    const meta = node("div", undefined, "strategy-plan-meta");
    meta.append(
      node("span", strategy.archetype.stage),
      node("span", `证据 ${strategy.archetype.evidence_level}`),
      node("span", `${strategy.candidate_count} 只候选`),
    );
    header.append(title, meta);
    section.append(
      header,
      node("p", strategy.archetype.description, "strategy-plan-copy"),
    );
    if (strategy.status !== "ready") {
      section.append(emptyState("计划尚未生成", "点击顶部“生成十份”创建所选交易日的十策略清单。"));
    } else if (!strategy.report.candidates.length) {
      section.append(emptyState("本策略无合格候选", "当日样本未通过该策略的静态筛选和评分门槛。"));
    } else {
      section.append(candidateTable(strategy.report.candidates, "plan"));
    }
    list.append(section);
  });
  showError(null);
}

function renderStrategyTabs(strategies) {
  const tabs = byId("strategy-tabs");
  tabs.replaceChildren();
  const available = strategies.some((item) => item.strategy_id === selectedStrategyId);
  if (!available) selectedStrategyId = strategies[0]?.strategy_id || null;
  strategies.forEach((strategy, index) => {
    const button = node("button", undefined, "strategy-tab");
    button.type = "button";
    button.role = "option";
    button.setAttribute("aria-selected", String(strategy.strategy_id === selectedStrategyId));
    button.append(
      node("b", String(index + 1).padStart(2, "0")),
      node("span", strategy.name),
      node("em", strategy.status === "ready" ? strategy.candidate_count : "待生成"),
    );
    button.addEventListener("click", () => {
      selectedStrategyId = strategy.strategy_id;
      renderMonitor(latestPayload);
    });
    tabs.append(button);
  });
}

function renderMonitorPanel(strategy) {
  const panel = byId("monitor-panel");
  panel.replaceChildren();
  if (!strategy) {
    panel.append(emptyState("暂无初始策略", "策略目录尚未完成初始化。"));
    return;
  }
  const header = node("header", undefined, "monitor-strategy-header");
  const copy = node("div");
  copy.append(
    node("h2", strategy.name),
    node("p", strategy.archetype.description),
  );
  const state = node("div", undefined, "monitor-state");
  state.append(
    node("strong", strategy.status === "ready" ? `${strategy.candidate_count} 只观察` : "计划待生成"),
    node("small", `阶段 ${strategy.archetype.stage} · 证据 ${strategy.archetype.evidence_level}`),
  );
  header.append(copy, state);
  panel.append(header);
  if (strategy.status !== "ready") {
    panel.append(emptyState("没有对应实盘计划", strategy.message || "请生成所选交易日的十策略计划。"));
    return;
  }
  const stocks = strategy.snapshot?.stocks || [];
  if (!stocks.length) {
    panel.append(emptyState("本策略当日空仓观察", "对应次日计划没有合格候选。"));
    return;
  }
  panel.append(candidateTable(stocks, "monitor"));
}

function renderMonitor(payload) {
  latestPayload = payload;
  write("display-date", dateLabel(payload.requested_date));
  write("phase-label", payload.phase_label);
  write("ready-count", `${payload.ready_count} / ${payload.strategy_count}`);
  write("candidate-count", `${payload.candidate_count} 只`);
  write("sync-time", clock(payload.server_time));
  write("strategy-count", `${payload.strategy_count} 条`);
  write(
    "scope",
    payload.resolved_from_non_trading_day
      ? `所选日期为休市日，展示最近交易日 ${payload.trade_date} 的十策略实盘。`
      : `${payload.ready_count} 条策略已有对应计划，当前观察 ${payload.candidate_count} 只股票。`,
  );
  renderStrategyTabs(payload.strategies);
  renderMonitorPanel(
    payload.strategies.find((item) => item.strategy_id === selectedStrategyId),
  );
  showError(null);
}

async function load() {
  if (loading || !dateControl.value) return;
  loading = true;
  refreshButton.disabled = true;
  const requestedDate = dateControl.value;
  try {
    const endpoint = pageMode === "plan"
      ? "/api/v1/temporary-plans"
      : "/api/v1/temporary-monitor";
    const payload = await fetchJson(
      `${endpoint}?trade_date=${encodeURIComponent(requestedDate)}`,
    );
    if (requestedDate !== dateControl.value) return;
    if (pageMode === "plan") renderPlan(payload);
    else renderMonitor(payload);
  } catch (error) {
    showError(error.message);
  } finally {
    loading = false;
    refreshButton.disabled = false;
  }
}

function wait(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function refreshAll() {
  if (loading || !dateControl.value) return;
  loading = true;
  refreshButton.disabled = true;
  dateControl.disabled = true;
  refreshButton.textContent = "生成中…";
  sourceStatus.className = "source-status";
  write("connection", "十个策略任务正在执行");
  try {
    const endpoint = pageMode === "plan"
      ? `/api/v1/temporary-plans/${encodeURIComponent(dateControl.value)}/refresh`
      : `/api/v1/temporary-monitor/${encodeURIComponent(dateControl.value)}/refresh`;
    const batch = await fetchJson(endpoint, { method: "POST" });
    const pending = new Set(batch.jobs.map((job) => job.job_id));
    for (let attempt = 0; attempt < 600 && pending.size; attempt += 1) {
      const statuses = await Promise.all(
        [...pending].map((jobId) => fetchJson(`/api/v1/report-jobs/${encodeURIComponent(jobId)}`)),
      );
      statuses.forEach((status) => {
        if (status.status === "failed" || status.status === "cancelled") {
          throw new Error(status.error || "部分临时策略生成失败");
        }
        if (status.status === "succeeded") pending.delete(status.job_id);
      });
      if (pending.size) await wait(1000);
    }
    if (pending.size) throw new Error("十策略生成超时，请稍后查看任务结果");
    loading = false;
    await load();
  } catch (error) {
    showError(error.message);
  } finally {
    loading = false;
    refreshButton.disabled = false;
    dateControl.disabled = false;
    refreshButton.textContent = "生成十份";
  }
}

async function initialize() {
  await window.strategyReady;
  try {
    await window.initializeTradingDateControl(dateControl, {
      defaultKey: pageMode === "plan" ? "next_plan" : "monitor",
    });
    await load();
    if (pageMode === "monitor") {
      window.setInterval(load, 3000);
    }
  } catch (error) {
    showError(error.message);
  }
}

dateControl.addEventListener("change", load);
refreshButton.addEventListener("click", refreshAll);
initialize();

const byId = (id) => document.getElementById(id);
const percent = (value) => value == null ? "—" : `${Number(value).toFixed(2)}%`;
const text = (id, value) => { byId(id).textContent = value ?? "—"; };
let study = null;
let period = "days";
let selectedDay = null;
let requestSequence = 0;
let parameterNames = {};

function node(tag, value, className) {
  const element = document.createElement(tag);
  if (value != null) element.textContent = value;
  if (className) element.className = className;
  return element;
}
function cell(row, value, className) {
  const element = node("td", value, className);
  row.append(element);
  return element;
}
async function get(path) {
  const response = await fetch(path, { cache: "no-store" });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error?.message || `读取失败（${response.status}）`);
  return payload;
}
function summaryOf(item) { return item.summary || item; }
function keyOf(item) { return item.plan_date || item.period; }

function renderChart(original, optimized) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 1100 230");
  const plot = (tag, attrs) => {
    const el = document.createElementNS(svgNS, tag);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    svg.append(el);
    return el;
  };
  [0, 25, 50, 75, 100].forEach((value) => {
    const y = 190 - value * 1.6;
    plot("line", { x1: 40, x2: 1080, y1: y, y2: y, stroke: "#ded9cd", "stroke-width": 1 });
    plot("text", { x: 0, y: y + 4, fill: "#817f73", "font-size": 11 }).textContent = String(value);
  });
  const x = (index) => 60 + index * (1000 / Math.max(original.length - 1, 1));
  [original, optimized].forEach((items, group) => {
    let points = [];
    const draw = () => {
      if (points.length > 1) plot("polyline", { points: points.join(" "), fill: "none", stroke: group ? "#17664b" : "#aaa18a", "stroke-width": group ? 2.5 : 1.5 });
      points = [];
    };
    items.forEach((item, index) => {
      const value = summaryOf(item).accuracy_pct;
      if (value == null) { draw(); return; }
      const y = 190 - value * 1.6;
      points.push(`${x(index)},${y}`);
      const circle = plot("circle", { cx: x(index), cy: y, r: group ? 3.2 : 2.5, fill: group ? "#17664b" : "#aaa18a" });
      const title = document.createElementNS(svgNS, "title");
      title.textContent = `${keyOf(item)} ${group ? "优化候选" : "原策略"} ${percent(value)}`;
      circle.append(title);
    });
    draw();
  });
  original.forEach((item, index) => {
    if (index % Math.max(1, Math.ceil(original.length / 8)) && index !== original.length - 1) return;
    plot("text", { x: x(index), y: 220, fill: "#817f73", "text-anchor": "middle", "font-size": 11 }).textContent = keyOf(item).replace("2026-", "");
  });
  byId("accuracy-chart").replaceChildren(svg);
}

function renderPeriods() {
  const original = study.baseline[period];
  const optimized = study.optimized[period];
  const rows = original.map((old, index) => {
    const current = optimized[index];
    const a = summaryOf(old), b = summaryOf(current);
    const row = node("tr", null, period === "days" && old.reference_date === selectedDay ? "selected" : "");
    const first = cell(row);
    if (period === "days") {
      const button = node("button", keyOf(old), "day-button");
      button.type = "button";
      button.addEventListener("click", () => { selectedDay = old.reference_date; renderPeriods(); renderDay(); });
      first.append(button);
    } else first.textContent = keyOf(old);
    [a, b].forEach((item) => {
      const count = cell(row, `${item.hit_count} / ${item.observed_count}`);
      if (item.pending_count) count.append(node("small", `${item.pending_count} 只待验证`));
      if (item.missing_count) count.append(node("small", `${item.missing_count} 只数据缺失`));
      cell(row, percent(item.accuracy_pct));
    });
    const delta = a.accuracy_pct == null || b.accuracy_pct == null ? null : b.accuracy_pct - a.accuracy_pct;
    cell(row, delta == null ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(2)} pp`, delta > 0 ? "up" : delta < 0 ? "down" : "");
    return row;
  });
  byId("period-rows").replaceChildren(...rows);
  text("period-caption", period === "days" ? "按计划执行日统计，点击日期查看候选明细" : "总命中数 ÷ 总已验证候选数；不是每日百分比的简单平均");
  renderChart(original, optimized);
}

function renderDay() {
  const variant = byId("variant-select").value;
  const day = study[variant].days.find((item) => item.reference_date === selectedDay);
  text("day-title", `${day.plan_date} · 候选与实际行情`);
  const rows = day.outcomes.map((outcome, index) => {
    const row = node("tr");
    const candidate = day.report.candidates[index];
    cell(row, `${outcome.name} · ${outcome.score}`).append(node("small", outcome.symbol));
    cell(row, outcome.industry);
    cell(row, outcome.status === "observed" ? ["open", "high", "low", "close"].map((key) => outcome[key].toFixed(2)).join(" / ") : outcome.status === "pending" ? "待次日收盘验证" : "行情缺失");
    cell(row, percent(outcome.open_change_pct));
    cell(row, outcome.auction_qualified == null ? "—" : outcome.auction_qualified ? "合格" : "不合格");
    cell(row, outcome.status === "observed" ? `${outcome.touched_limit_up ? "触板" : "未触板"} / ${outcome.closed_limit_up ? "收盘封板" : "未封住"}` : "—", outcome.closed_limit_up ? "up" : "");
    const details = node("details");
    details.append(node("summary", "查看执行条件"), node("p", candidate.entry_trigger), node("p", candidate.invalidation), node("p", candidate.exit_plan), node("p", outcome.reason));
    cell(row).append(details);
    return row;
  });
  if (!rows.length) { const row = node("tr"); const td = cell(row, "当日无合格候选，准确率无样本。"); td.colSpan = 7; rows.push(row); }
  byId("candidate-rows").replaceChildren(...rows);
}

function renderStudy() {
  const base = study.baseline, fresh = study.optimized, strategy = study.strategy;
  text("range-label", `${study.start_date} — ${study.end_date}`);
  text("verdict-title", strategy.status === "holdout_improved" ? "留出区间出现改善，继续观察。" : "研究候选已保存，仍需新增样本验证。");
  text("verdict-detail", strategy.conclusion);
  text("revision-label", `版本 ${strategy.revision.slice(0, 12)}`);
  const metrics = [
    ["原策略 · 全区间", percent(base.summary.accuracy_pct), `${base.summary.hit_count} / ${base.summary.observed_count} 个已验证候选`],
    ["优化候选 · 全区间", percent(fresh.summary.accuracy_pct), `含训练样本 · ${fresh.summary.pending_count} 个待验证`],
    ["原策略 → 新策略 · 留出区间", `${percent(base.holdout.accuracy_pct)} → ${percent(fresh.holdout.accuracy_pct)}`, `${study.protocol.split.holdout.length} 个计划日 · ${study.protocol.holdout_reused ? "复用样本，探索性结果" : "按时间留出"}`],
    ["竞价合格后 · 原策略 → 新策略", `${percent(base.summary.auction_accuracy_pct)} → ${percent(fresh.summary.auction_accuracy_pct)}`, "日线开盘价近似 · 非成交成功率"],
  ];
  byId("research-metrics").replaceChildren(...metrics.map(([label, value, note]) => {
    const div = node("div", null, "research-metric");
    div.append(node("span", label), node("strong", value), node("small", note));
    return div;
  }));
  text("metric-definition", `${study.protocol.metric.formula}。无候选和未验证日期不记作成功。${study.protocol.metric.execution}`);
  byId("parameter-rows").replaceChildren(...Object.entries(strategy.changes).map(([key, change]) => {
    const row = node("tr");
    cell(row, parameterNames[key] || key); cell(row, String(change.before)); cell(row, String(change.after));
    return row;
  }));
  text("full-config", JSON.stringify(strategy.config, null, 2));
  text("strategy-note", `选自 ${strategy.selected_trial}，完整参数单独存储。当前执行策略保持独立，可下载研究候选进行后续验证。`);
  byId("split-rows").replaceChildren(...["train", "validation", "holdout"].map((key, index) => {
    const days = study.protocol.split[key];
    const div = node("div", null, "split-row");
    div.append(node("strong", ["训练", "验证", "留出"][index]), node("span", `${days[0]} — ${days.at(-1)} · ${days.length} 日`));
    return div;
  }));
  text("protocol-note", `${study.protocol.selection} 目标分：${study.protocol.objective}。${study.protocol.sample_constraint}`);
  text("reuse-note", study.protocol.holdout_reused ? `扩展自实验 ${study.protocol.prior_run_id.slice(0, 8)}。留出日期已复用，需用后续新增交易日检验。` : study.protocol.holdout_rule);
  byId("trial-rows").replaceChildren(...study.trials.map((trial) => {
    const row = node("tr", null, trial.trial_id === strategy.selected_trial ? "selected" : "");
    cell(row, `${trial.trial_id}${trial.trial_id === strategy.selected_trial ? " · 已选" : ""}`);
    const change = cell(row);
    Object.entries(trial.changes).forEach(([key, value]) => change.append(node("small", `${parameterNames[key] || key}：${value.before} → ${value.after}`)));
    if (!Object.keys(trial.changes).length) change.textContent = "基线参数";
    cell(row, `${trial.training_summary.hit_count} / ${trial.training_summary.observed_count}`);
    cell(row, percent(trial.training_summary.accuracy_pct));
    cell(row, trial.objective.toFixed(2));
    cell(row, trial.validation_summary ? percent(trial.validation_summary.accuracy_pct) : "未入围");
    cell(row, trial.eligible ? "满足" : "不足");
    return row;
  }));
  const quality = study.quality;
  text("coverage-note", `主板代码 ${quality.universe_count} 个，取得日线 ${quality.daily_history_count} 个，缺失 ${quality.missing_history_codes.length} 个。采集时间：${study.input_collected_at}。`);
  byId("quality-list").replaceChildren(...quality.limitations.map((value) => node("li", value)));
  text("snapshot-hash", `输入 SHA-256：${study.input_sha256} · 实验 ${study.run_id}`);
  const root = `/api/v1/research/${encodeURIComponent(study.run_id)}`;
  byId("download-strategy").href = `${root}/strategy`;
  [["download-report", "report.md"], ["download-daily", "baseline-daily.csv"], ["download-archive", "experiment.tar.gz"]].forEach(([id, filename]) => { byId(id).href = `${root}/assets/${filename}`; });
  selectedDay = fresh.days.find((day) => day.summary.observed_count)?.reference_date || fresh.days[0].reference_date;
  renderPeriods();
  renderDay();
}

async function loadStudy(id) {
  const sequence = ++requestSequence;
  text("research-status", "正在读取实验明细…");
  byId("research-status").className = "";
  byId("research-content").hidden = true;
  try {
    const result = await get(`/api/v1/research/${encodeURIComponent(id)}`);
    if (sequence !== requestSequence) return;
    study = result;
    renderStudy();
    byId("research-content").hidden = false;
    text("research-status", `${study.baseline.days.length} 个计划日 · ${study.trials.length} 轮实验 · 全过程已保存`);
  } catch (error) {
    if (sequence !== requestSequence) return;
    text("research-status", error.message);
    byId("research-status").className = "error";
  }
}
async function initialize() {
  byId("reload-research").disabled = true;
  try {
    const payload = await get("/api/v1/research");
    try { const config = await get("/api/v1/strategy-config"); parameterNames = Object.fromEntries(config.fields.map((field) => [field.key, field.label])); } catch (_) { /* parameter keys remain readable */ }
    const selector = byId("run-select");
    selector.replaceChildren(...payload.items.map((item) => {
      const option = node("option", `${item.start_date} — ${item.end_date} · ${item.run_id.slice(0, 8)}`);
      option.value = item.run_id;
      return option;
    }));
    selector.disabled = !payload.items.length;
    byId("research-empty").hidden = !!payload.items.length;
    if (payload.items.length) await loadStudy(payload.items[0].run_id);
    else { text("research-status", "尚无实验记录"); byId("research-content").hidden = true; }
  } catch (error) {
    text("research-status", error.message);
    byId("research-status").className = "error";
  } finally { byId("reload-research").disabled = false; }
}
byId("run-select").addEventListener("change", (event) => loadStudy(event.target.value));
byId("reload-research").addEventListener("click", initialize);
byId("variant-select").addEventListener("change", () => { if (study) renderDay(); });
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
  period = button.dataset.period;
  document.querySelectorAll("[data-period]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  if (study) renderPeriods();
}));
initialize();

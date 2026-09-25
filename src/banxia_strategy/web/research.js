const byId = (id) => document.getElementById(id);
const percent = (value) => value == null ? "—" : `${Number(value).toFixed(2)}%`;
const text = (id, value) => { byId(id).textContent = value ?? "—"; };
let comparison = null;
let period = "month";
let selectedDay = null;
let requestSequence = 0;

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

function successRate(summary) {
  return summary?.execution_success_rate_pct ?? summary?.buyable_rate_pct ?? null;
}

function periodMap(strategy) {
  return new Map(strategy.periods[period].map((item) => [item.period, item]));
}

function renderChart(strategies, keys) {
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 1100 230");
  const plot = (tag, attrs) => {
    const element = document.createElementNS(svgNS, tag);
    Object.entries(attrs).forEach(([key, value]) => element.setAttribute(key, value));
    svg.append(element);
    return element;
  };
  [0, 25, 50, 75, 100].forEach((value) => {
    const y = 190 - value * 1.6;
    plot("line", { x1: 40, x2: 1080, y1: y, y2: y, stroke: "#ded9cd", "stroke-width": 1 });
    plot("text", { x: 0, y: y + 4, fill: "#817f73", "font-size": 11 }).textContent = String(value);
  });
  const x = (index) => 60 + index * (1000 / Math.max(keys.length - 1, 1));
  const colors = ["#aaa18a", "#17664b"];
  strategies.forEach((strategy, strategyIndex) => {
    const values = periodMap(strategy);
    let points = [];
    const draw = () => {
      if (points.length > 1) {
        plot("polyline", {
          points: points.join(" "),
          fill: "none",
          stroke: colors[strategyIndex],
          "stroke-width": strategyIndex ? 2.5 : 1.5,
        });
      }
      points = [];
    };
    keys.forEach((key, index) => {
      const summary = values.get(key);
      const value = successRate(summary);
      if (value == null) {
        draw();
        return;
      }
      const y = 190 - value * 1.6;
      points.push(`${x(index)},${y}`);
      const circle = plot("circle", {
        cx: x(index),
        cy: y,
        r: strategyIndex ? 3.2 : 2.5,
        fill: colors[strategyIndex],
      });
      const title = document.createElementNS(svgNS, "title");
      title.textContent = `${key} ${strategy.strategy_name} ${percent(value)}`;
      circle.append(title);
    });
    draw();
  });
  keys.forEach((key, index) => {
    if (index % Math.max(1, Math.ceil(keys.length / 8)) && index !== keys.length - 1) return;
    plot("text", {
      x: x(index),
      y: 220,
      fill: "#817f73",
      "text-anchor": "middle",
      "font-size": 11,
    }).textContent = key.replace(/^20\d{2}-/, "");
  });
  byId("accuracy-chart").replaceChildren(svg);
}

function renderPeriods() {
  const strategies = comparison.strategies.slice(0, 2);
  const maps = strategies.map(periodMap);
  const keys = [...new Set(strategies.flatMap((strategy) => strategy.periods[period].map((item) => item.period)))].sort();
  const rows = keys.map((key) => {
    const summaries = maps.map((items) => items.get(key) || {});
    const row = node("tr", null, period === "day" && key === selectedDay ? "selected" : "");
    const first = cell(row);
    if (period === "day") {
      const button = node("button", key, "day-button");
      button.type = "button";
      button.addEventListener("click", () => {
        selectedDay = key;
        renderPeriods();
        renderDay();
      });
      first.append(button);
    } else {
      first.textContent = key;
    }
    summaries.forEach((summary) => {
      const sample = cell(row, `${summary.buyable_count ?? 0} / ${summary.verified_count ?? 0}`);
      if (summary.missing_count) sample.append(node("small", `${summary.missing_count} 只分钟线缺失`));
      cell(row, percent(successRate(summary)));
      cell(row, percent(summary.buyable_close_rate_pct));
    });
    const rates = summaries.map(successRate);
    const delta = rates.some((value) => value == null) ? null : rates[1] - rates[0];
    cell(
      row,
      delta == null ? "—" : `${delta > 0 ? "+" : ""}${delta.toFixed(2)} pp`,
      delta > 0 ? "up" : delta < 0 ? "down" : "",
    );
    return row;
  });
  byId("period-rows").replaceChildren(...rows);
  text(
    "period-caption",
    period === "day"
      ? "按计划执行日统计，点击日期查看严格可买股票"
      : "周期成功率按候选数加权汇总，不是每日百分比的简单平均",
  );
  renderChart(strategies, keys);
}

function renderDay() {
  const strategy = comparison.strategies[Number(byId("variant-select").value) || 0];
  const day = strategy.days.find((item) => item.trade_date === selectedDay);
  text("day-title", `${selectedDay} · 严格可买股票`);
  const buyable = (day?.stocks || []).filter((stock) => stock.buyable);
  const rows = buyable.map((stock) => {
    const row = node("tr");
    cell(row, stock.name).append(node("small", stock.symbol));
    cell(row, stock.industry).append(node("small", `评分 ${stock.score ?? "—"}`));
    cell(row, stock.first_touch_time || "—");
    cell(row, stock.buy_window_time || "—");
    cell(row, stock.confirmation_time || "—");
    cell(row, stock.break_count_before_cutoff ?? "—");
    cell(row, stock.closed_limit_up ? "收盘封板" : "收盘未封板", stock.closed_limit_up ? "up" : "down");
    return row;
  });
  if (!rows.length) {
    const row = node("tr");
    const message = day
      ? `该策略当日 ${day.summary.verified_count} 只候选中无严格可买标的`
      : "该策略当日无执行计划";
    const empty = cell(row, message);
    empty.colSpan = 7;
    rows.push(row);
  }
  byId("candidate-rows").replaceChildren(...rows);
}

function renderComparison() {
  const strategies = comparison.strategies.slice(0, 2);
  if (strategies.length !== 2) throw new Error("当前策略数量不是 2，无法生成对比");
  const summaries = strategies.map((strategy) => strategy.summary);
  const rates = summaries.map(successRate);
  const delta = rates[1] - rates[0];
  const winner = delta === 0 ? "两个策略严格可买成功率相同。" : `${strategies[delta > 0 ? 1 : 0].strategy_name}严格可买成功率更高 ${Math.abs(delta).toFixed(2)} 个百分点。`;

  text("comparison-range", `${comparison.start} — ${comparison.end}`);
  text("verdict-detail", `${winner} 统计仅使用分钟线完整的计划候选。`);
  text("generated-at", `生成于 ${new Date(comparison.generated_at).toLocaleString("zh-CN", { hour12: false })}`);
  text("legend-first", strategies[0].strategy_name);
  text("legend-second", strategies[1].strategy_name);
  text("first-sample-head", `${strategies[0].strategy_name} 可买 / 样本`);
  text("second-sample-head", `${strategies[1].strategy_name} 可买 / 样本`);

  const metrics = [
    [strategies[0].strategy_name, percent(rates[0]), `${summaries[0].buyable_count} / ${summaries[0].verified_count} 只严格可买`],
    [strategies[1].strategy_name, percent(rates[1]), `${summaries[1].buyable_count} / ${summaries[1].verified_count} 只严格可买`],
    ["成功率差异", `${delta > 0 ? "+" : ""}${delta.toFixed(2)} pp`, `以 ${strategies[0].strategy_name} 为基准`],
    ["分钟线覆盖", `${summaries.reduce((sum, item) => sum + item.verified_count, 0)} 只`, `缺失 ${summaries.reduce((sum, item) => sum + item.missing_count, 0)} 只`],
  ];
  byId("research-metrics").replaceChildren(...metrics.map(([label, value, note]) => {
    const item = node("div", null, "research-metric");
    item.append(node("span", label), node("strong", value), node("small", note));
    return item;
  }));
  text("metric-definition", `${comparison.metric.formula}。${comparison.metric.buyable_formula}作为买入后质量指标。`);
  byId("buyable-rules").replaceChildren(...comparison.metric.strict_buyable.map((rule) => node("li", rule)));
  text("quality-note", comparison.metric.limitation);

  const selector = byId("variant-select");
  selector.replaceChildren(...strategies.map((strategy, index) => {
    const option = node("option", strategy.strategy_name);
    option.value = String(index);
    return option;
  }));
  const days = strategies.flatMap((strategy) => strategy.days.map((day) => day.trade_date));
  selectedDay = days.sort().at(-1);
  renderPeriods();
  renderDay();
}

async function initialize() {
  const sequence = ++requestSequence;
  byId("reload-research").disabled = true;
  byId("research-status").className = "ui-status";
  byId("research-content").hidden = true;
  byId("research-empty").hidden = true;
  text("research-status", "正在读取实盘成功率对比…");
  try {
    const payload = await get("/api/v1/research/comparison");
    if (sequence !== requestSequence) return;
    comparison = payload;
    renderComparison();
    byId("research-content").hidden = false;
    text("research-status", `${comparison.strategies.length} 个策略 · ${comparison.start} 至 ${comparison.end} · 数据已完成统计`);
  } catch (error) {
    if (sequence !== requestSequence) return;
    text("research-status", error.message);
    byId("research-status").className = "ui-status is-error";
    byId("research-empty").hidden = false;
  } finally {
    byId("reload-research").disabled = false;
  }
}

byId("reload-research").addEventListener("click", initialize);
byId("variant-select").addEventListener("change", renderDay);
document.querySelectorAll("[data-period]").forEach((button) => button.addEventListener("click", () => {
  period = button.dataset.period;
  document.querySelectorAll("[data-period]").forEach((item) => item.setAttribute("aria-pressed", String(item === button)));
  if (comparison) renderPeriods();
}));
initialize();

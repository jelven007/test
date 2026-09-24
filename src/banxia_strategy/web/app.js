const dashboard = document.querySelector("#dashboard");
const dashboardTemplate = document.querySelector("#dashboard-template");
const candidateTemplate = document.querySelector("#candidate-template");
const reportDateInput = document.querySelector("#report-date");
const refreshButton = document.querySelector("#refresh-button");
const sourceStatus = document.querySelector(".source-status");
const sourceLabel = document.querySelector("#source-label");

let activeDate = null;

function field(root, name) {
  return root.querySelector(`[data-field="${name}"]`);
}

function setText(root, name, value) {
  const target = field(root, name);
  if (target) target.textContent = value ?? "—";
}

function formatDate(value) {
  if (!value) return "待交易日历更新";
  const [year, month, day] = value.split("-");
  return `${year}.${month}.${day}`;
}

function formatTimestamp(value) {
  if (!value) return "生成时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Shanghai",
  }).format(date);
}

function formatAmount(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number >= 100_000_000) return `${(number / 100_000_000).toFixed(2)} 亿`;
  if (number >= 10_000) return `${(number / 10_000).toFixed(0)} 万`;
  return number.toLocaleString("zh-CN");
}

function formatPrice(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(2) : "—";
}

function splitClauses(value) {
  if (!value) return [];
  return String(value)
    .split(/[；、]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function appendList(list, items) {
  list.replaceChildren();
  for (const text of items) {
    const item = document.createElement("li");
    item.textContent = text;
    list.append(item);
  }
}

function extractRange(text, pattern, fallback) {
  const match = String(text || "").match(pattern);
  if (!match) return fallback;
  return [Number(match[1]), Number(match[2])];
}

function priceAt(price, percent) {
  return price * (1 + percent / 100);
}

function regimeDetails(regime) {
  if (regime === "强势接力") {
    return {
      key: "strong",
      guidance: "允许按计划参与，但仍需等待封板确认。",
    };
  }
  if (regime === "退潮防守") {
    return {
      key: "defensive",
      guidance: "降低仓位，条件稍有偏离即放弃。",
    };
  }
  return {
    key: "neutral",
    guidance: "只做前排确认，控制试错次数与总仓位。",
  };
}

function renderCandidate(candidate, index) {
  const fragment = candidateTemplate.content.cloneNode(true);
  const article = fragment.querySelector(".candidate");
  const anchor = `candidate-${candidate.code || index + 1}`;
  article.id = anchor;
  article.style.animationDelay = `${Math.min(index * 70, 350)}ms`;

  setText(fragment, "rank", String(candidate.rank || index + 1).padStart(2, "0"));
  setText(fragment, "name", candidate.name);
  setText(fragment, "code", candidate.code);
  setText(fragment, "strategy", candidate.strategy);
  setText(fragment, "industry", candidate.industry);
  setText(fragment, "score", Number(candidate.score).toFixed(1));
  setText(fragment, "latest-price", `¥ ${formatPrice(candidate.latest_price)}`);
  setText(fragment, "position-limit", candidate.position_limit_pct);

  const entryRange = extractRange(
    candidate.entry_trigger,
    /位于(-?\d+(?:\.\d+)?)%[～~-](-?\d+(?:\.\d+)?)%/,
    [0.5, 5],
  );
  const abandonRange = extractRange(
    candidate.invalidation,
    /低于(-?\d+(?:\.\d+)?)%或高于(-?\d+(?:\.\d+)?)%/,
    [-2, 7],
  );
  const price = Number(candidate.latest_price);
  if (Number.isFinite(price)) {
    setText(
      fragment,
      "auction-range",
      `¥ ${formatPrice(priceAt(price, entryRange[0]))} — ${formatPrice(priceAt(price, entryRange[1]))}`,
    );
    setText(
      fragment,
      "abandon-range",
      `< ¥ ${formatPrice(priceAt(price, abandonRange[0]))} / > ¥ ${formatPrice(priceAt(price, abandonRange[1]))}`,
    );
  }
  setText(fragment, "auction-percent", `${entryRange[0]}% — ${entryRange[1]}%`);
  setText(fragment, "abandon-percent", `< ${abandonRange[0]}% 或 > ${abandonRange[1]}%`);

  const entryParts = String(candidate.entry_trigger || "").split("；");
  setText(fragment, "auction-action", entryParts[0] || "等待竞价数据确认");
  setText(fragment, "entry-action", entryParts.slice(1).join("；") || candidate.entry_trigger);
  appendList(field(fragment, "invalidation-list"), splitClauses(candidate.invalidation));
  appendList(field(fragment, "exit-list"), splitClauses(candidate.exit_plan));
  appendList(field(fragment, "reasons"), candidate.reasons || []);

  setText(fragment, "seal-time", `${candidate.first_seal_time || "—"} / ${candidate.last_seal_time || "—"}`);
  setText(fragment, "break-count", `${candidate.break_count ?? "—"} 次`);
  setText(fragment, "turnover", `${Number(candidate.turnover_pct).toFixed(1)}%`);
  setText(fragment, "seal-ratio", `${(Number(candidate.seal_amount_ratio) * 100).toFixed(1)}%`);
  setText(fragment, "amount", formatAmount(candidate.amount_cny));
  setText(fragment, "float-cap", formatAmount(candidate.float_market_cap_cny));
  setText(fragment, "industry-limit-ups", `${candidate.industry_limit_up_count ?? "—"} 只`);
  setText(fragment, "industry-max-board", `${candidate.industry_max_board ?? "—"} 板`);

  return { fragment, anchor };
}

function renderReport(report) {
  report = {
    ...report,
    as_of: report.trade_date,
    next_session: report.plan_date,
    candidates: (report.candidates || []).map((candidate) => ({
      ...candidate,
      code: candidate.symbol,
    })),
  };
  const fragment = dashboardTemplate.content.cloneNode(true);
  const market = report.market || {};
  const candidates = Array.isArray(report.candidates) ? report.candidates : [];
  const regime = regimeDetails(market.regime);

  setText(fragment, "next-session", formatDate(report.next_session));
  setText(fragment, "as-of", formatDate(report.as_of));
  setText(fragment, "generated-at", formatTimestamp(report.generated_at));
  setText(fragment, "regime", market.regime);
  setText(fragment, "regime-guidance", regime.guidance);
  setText(fragment, "market-score", market.score);
  setText(fragment, "limit-up-count", market.limit_up_count);
  setText(fragment, "broken-count", market.broken_board_data_available ? market.broken_board_count : "—");
  setText(
    fragment,
    "broken-note",
    market.broken_board_data_available ? "当日失败样本" : "数据暂不可用",
  );
  setText(
    fragment,
    "break-rate",
    market.broken_board_data_available && market.break_rate_pct != null
      ? `${market.break_rate_pct}%`
      : "—",
  );
  setText(fragment, "max-board", market.max_board);
  setText(fragment, "candidate-count", candidates.length);
  setText(fragment, "rejected-count", report.rejected_count);
  setText(fragment, "candidate-total", `${candidates.length} 只`);
  setText(fragment, "data-source", report.data_source);
  setText(fragment, "data-sessions", (report.data_sessions || []).join("、"));
  setText(fragment, "disclaimer", report.disclaimer);

  const regimePanel = fragment.querySelector(".regime-panel");
  regimePanel.dataset.regime = regime.key;
  const scoreBar = field(fragment, "score-bar");
  scoreBar.style.width = `${Math.max(0, Math.min(100, Number(market.score) || 0))}%`;

  const candidateList = fragment.querySelector("#candidate-list");
  const candidateNav = fragment.querySelector("#candidate-nav");
  if (!candidates.length) {
    const empty = document.createElement("div");
    empty.className = "empty-candidates";
    const title = document.createElement("strong");
    title.textContent = "今日无合格候选";
    const note = document.createElement("p");
    note.textContent = "评分或组合风控条件未通过，次日策略为空仓观察。";
    empty.append(title, note);
    candidateList.append(empty);
  }

  candidates.forEach((candidate, index) => {
    const { fragment: candidateFragment, anchor } = renderCandidate(candidate, index);
    candidateList.append(candidateFragment);

    const link = document.createElement("a");
    link.href = `#${anchor}`;
    const rank = document.createElement("b");
    rank.textContent = String(candidate.rank || index + 1).padStart(2, "0");
    const name = document.createElement("span");
    name.textContent = candidate.name;
    const score = document.createElement("em");
    score.textContent = Number(candidate.score).toFixed(1);
    link.append(rank, name, score);
    candidateNav.append(link);
  });

  dashboard.replaceChildren(fragment);
  activeDate = report.as_of;
  document.title = `${report.as_of} 次日执行计划 · 一进二`;
  sourceStatus.className = "source-status ready";
  sourceLabel.textContent = `${report.data_source || "数据源"} · ${candidates.length} 只候选`;
}

function renderError(message) {
  const section = document.createElement("section");
  section.className = "error-state";
  const content = document.createElement("div");
  const title = document.createElement("h1");
  title.textContent = "日报暂不可用";
  const detail = document.createElement("p");
  detail.textContent = message;
  content.append(title, detail);
  section.append(content);
  dashboard.replaceChildren(section);
  sourceStatus.className = "source-status error";
  sourceLabel.textContent = "读取失败";
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `请求失败（${response.status}）`);
  return payload;
}

async function loadReport(asOf) {
  activeDate = asOf;
  refreshButton.disabled = true;
  try {
    const path = `/api/v1/reports/${encodeURIComponent(asOf)}`;
    const report = await fetchJson(path);
    const selectedDate = report.trade_date || report.as_of;
    renderReport(report);
    reportDateInput.value = selectedDate;
  } catch (error) {
    renderError(error.message);
  } finally {
    refreshButton.disabled = false;
  }
}

async function initialize() {
  try {
    const payload = await fetchJson("/api/v1/reports");
    if (!payload.items.length) {
      throw new Error("未发现 candidates.json，请先执行 .venv/bin/banxia-strategy run");
    }
    const latestDate = payload.items[0].trade_date;
    reportDateInput.value = latestDate;
    await loadReport(latestDate);
  } catch (error) {
    renderError(error.message);
  }
}

reportDateInput.addEventListener("change", () => {
  if (reportDateInput.value) loadReport(reportDateInput.value);
});
refreshButton.addEventListener("click", () => {
  const requestedDate = reportDateInput.value || activeDate;
  if (requestedDate) loadReport(requestedDate);
});

initialize();

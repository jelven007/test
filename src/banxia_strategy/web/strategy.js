"use strict";

const form = document.querySelector("#strategy-form");
const groups = document.querySelector("#settings-groups");
const nav = document.querySelector("#settings-nav");
const message = document.querySelector("#settings-message");
const errors = document.querySelector("#settings-errors");
const save = document.querySelector("#save-button");
const reload = document.querySelector("#reload-button");
const defaults = document.querySelector("#defaults-button");
const retry = document.querySelector("#retry-button");
let model = null;
let dirtyCount = 0;
let saving = false;
let strategyId = new URLSearchParams(location.search).get("strategy_id");
let strategies = [];
let recordsVisible = true;
let daysBefore = null;
let pendingConfig = null;
const selector = document.querySelector("#strategy-select");
const records = document.querySelector("#strategy-records");
const saveDialog = document.querySelector("#save-strategy-dialog");
const statuses = {ready: "已生成", pending: "待生成 / 待行情", live: "实盘跟踪中", complete: "收盘已验证", missing: "行情不完整", no_plan: "当日无执行计划", no_candidates: "无候选 · 空仓观察"};

function scopedConfig() {
  return `/api/v1/strategy-config${strategyId ? `?strategy_id=${encodeURIComponent(strategyId)}` : ""}`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {cache: "no-store", ...options});
  const payload = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(payload.error?.message || "操作失败");
  return payload;
}

function showRecords(value) {
  recordsVisible = value;
  records.hidden = !value;
  form.hidden = value || !model;
  document.querySelector("#records-tab").setAttribute("aria-pressed", String(value));
  document.querySelector("#parameters-tab").setAttribute("aria-pressed", String(!value));
}

async function loadCatalog() {
  const payload = await api("/api/v1/strategies");
  strategies = payload.items;
  if (!strategies.some(item => item.strategy_id === strategyId)) {
    strategyId = strategies.find(item => item.enabled)?.strategy_id || strategies[0]?.strategy_id;
  }
  selector.replaceChildren(...strategies.map(item => {
    const option = node("option", `${item.name} · ${item.enabled ? "激活" : "未激活"}`);
    option.value = item.strategy_id;
    return option;
  }));
  selector.value = strategyId;
  const item = strategies.find(item => item.strategy_id === strategyId);
  document.querySelector("#strategy-status").textContent = item?.enabled ? "激活" : "未激活";
  document.querySelector("#strategy-status").classList.toggle("active", Boolean(item?.enabled));
  const parent = strategies.find(candidate => candidate.strategy_id === item?.parent_strategy_id);
  document.querySelector("#strategy-lineage").textContent = item?.parent_strategy_id
    ? `来源：${parent?.name || "已删除策略"}` : "初始策略";
  document.querySelector("#toggle-strategy").textContent = item?.enabled ? "取消激活" : "激活";
  const url = new URL(location.href);
  url.searchParams.set("strategy_id", strategyId);
  history.replaceState(null, "", url);
  document.querySelector(".settings-revision a").href = `/?strategy_id=${strategyId}`;
}

async function loadDays(append = false) {
  const selectedId = strategyId;
  const payload = await api(`/api/v1/strategies/${selectedId}/days?limit=30${append && daysBefore ? `&before=${daysBefore}` : ""}`);
  if (selectedId !== strategyId) return;
  const body = document.querySelector("#strategy-days");
  if (!append) body.replaceChildren();
  for (const day of payload.items) {
    const tr = node("tr");
    tr.append(node("td", day.trade_date));
    const plan = node("td");
    if (day.plan_status === "ready") {
      const link = node("a", `${day.plan_date || "待定"} · ${day.candidate_count} 只`);
      link.href = `/?strategy_id=${selectedId}&trade_date=${day.trade_date}`;
      plan.append(link);
    } else plan.textContent = "待生成";
    tr.append(plan, node("td", `${statuses[day.actual_status] || day.actual_status}${day.execution_count != null ? ` · ${day.execution_count} 只` : ""}`));
    const accuracy = day.summary.accuracy_pct;
    tr.append(node("td", accuracy == null ? "—" : `${accuracy.toFixed(2)}% (${day.summary.hit_count}/${day.summary.observed_count})`));
    const actions = node("td");
    const detail = node("button", "查看");
    detail.type = "button";
    detail.addEventListener("click", () => openDay(day.trade_date).catch(showError));
    const refresh = node("button", "生成 / 刷新计划");
    refresh.type = "button";
    const currentStrategy = strategies.find(item => item.strategy_id === selectedId);
    const localDay = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
    const localHour = Number(new Intl.DateTimeFormat("en-GB", {timeZone: "Asia/Shanghai", hour: "2-digit", hourCycle: "h23"}).format(new Date()));
    refresh.disabled = !currentStrategy?.enabled || day.trade_date > localDay || (day.trade_date === localDay && localHour < 15);
    if (!currentStrategy?.enabled) refresh.title = "仅当前激活策略可刷新";
    else if (refresh.disabled) refresh.title = "该交易日收盘后可生成计划";
    refresh.addEventListener("click", async () => {
      refresh.disabled = true;
      try {
        const job = await api(`/api/v1/reports/${day.trade_date}/refresh`, {method: "POST"});
        message.textContent = `已提交 ${day.trade_date} 计划任务，执行时使用当前激活策略。`;
        await watchJob(job.job_id, selectedId);
      } catch (error) { showError(error); }
      finally { refresh.disabled = false; }
    });
    actions.append(detail, refresh);
    tr.append(actions);
    body.append(tr);
  }
  if (!payload.items.length && !append) {
    const tr = node("tr"), td = node("td", "策略已建立，首个交易日记录将在计划生成后显示。");
    td.colSpan = 5; tr.append(td); body.append(tr);
  }
  daysBefore = payload.items.at(-1)?.trade_date;
  document.querySelector("#more-days").hidden = payload.items.length < 30;
}

async function openDay(day) {
  const selectedId = strategyId;
  const item = await api(`/api/v1/strategies/${selectedId}/days/${day}`);
  if (selectedId !== strategyId) return;
  const target = document.querySelector("#day-detail");
  target.replaceChildren(node("h2", `${day} · 当日实盘`),
    node("p", `${statuses[item.actual_status] || item.actual_status}。收盘命中率依据 mootdx 日线验证，入场与成交情况需人工核验。`));
  const table = node("table", undefined, "records-table");
  const head = node("tr");
  ["股票", "开盘", "收盘 / 最新", "结果", "说明"].forEach(text => head.append(node("th", text)));
  table.append(head);
  const outcomes = item.actuals?.outcomes || Object.entries(item.actuals?.stocks || {}).map(([symbol, event]) => ({
    symbol, name: item.execution_plan?.candidates.find(c => c.code === symbol)?.name,
    open: event.rule_inputs?.quote?.open, close: event.rule_inputs?.quote?.price,
    reason: event.reason, state: event.label || event.state,
  }));
  for (const stock of outcomes) {
    const tr = node("tr");
    [`${stock.symbol} ${stock.name || ""}`, stock.open ?? "—", stock.close ?? "—",
      stock.closed_limit_up === true ? "收盘封板" : stock.closed_limit_up === false ? "未封板" : stock.state || "待验证",
      stock.reason || "等待行情"].forEach(text => tr.append(node("td", text)));
    table.append(tr);
  }
  target.append(table);
  const link = node("a", "打开该策略盘中监控 ↗");
  link.href = `/monitor?strategy_id=${selectedId}&trade_date=${day}`;
  target.append(link);
  target.hidden = false;
  target.scrollIntoView({behavior: "smooth", block: "start"});
}

function showError(error) { message.textContent = error.message; }
async function watchJob(id, selectedId) {
  for (let i = 0; i < 300; i++) {
    await new Promise(resolve => setTimeout(resolve, 2000));
    const job = await api(`/api/v1/report-jobs/${id}`);
    if (job.status === "failed") throw new Error(job.error || "计划生成失败");
    if (job.status === "succeeded") {
      if (strategyId === selectedId) { message.textContent = "计划生成完成，交易日记录已更新。"; await loadDays(); }
      return;
    }
  }
  message.textContent = "任务仍在后台执行，可稍后刷新记录查看。";
}

function node(tag, text, className) {
  const result = document.createElement(tag);
  if (text !== undefined) result.textContent = text;
  if (className) result.className = className;
  return result;
}

function display(value, field) {
  return field.kind === "number" ? Number((value / field.scale).toPrecision(12)) : value;
}

function read(field) {
  if (field.kind === "fixed") return model.config[field.key];
  const input = document.getElementById(field.key);
  if (field.kind === "boolean") return input.checked;
  if (field.kind === "number") {
    if (!input.value.trim() || !Number.isFinite(Number(input.value))) return null;
    return Number((Number(input.value) * field.scale).toPrecision(12));
  }
  return input.value.trim();
}

function selectGroup(id, updateHash = true) {
  if (!model?.groups.some((group) => group.id === id)) id = model.groups[0].id;
  groups.querySelectorAll(".settings-group").forEach((group) => { group.hidden = group.id !== id; });
  nav.querySelectorAll("a").forEach((link) => {
    if (link.hash === `#${id}`) link.setAttribute("aria-current", "true");
    else link.removeAttribute("aria-current");
  });
  if (updateHash) history.replaceState(null, "", `#${id}`);
}

function clearErrors() {
  errors.hidden = true;
  errors.textContent = "";
  groups.querySelectorAll("[aria-invalid]").forEach((input) => input.removeAttribute("aria-invalid"));
  groups.querySelectorAll(".setting-error").forEach((element) => { element.textContent = ""; });
}

function markErrors(fields, summary) {
  errors.textContent = summary;
  errors.hidden = false;
  const first = model.fields.find((field) => fields[field.key]);
  for (const [key, text] of Object.entries(fields)) {
    const input = document.getElementById(key);
    if (!input) continue;
    input.setAttribute("aria-invalid", "true");
    const target = document.getElementById(`${key}-error`);
    if (target) target.textContent = text;
  }
  if (first) {
    selectGroup(first.group);
    document.getElementById(first.key)?.focus();
  }
}

function refreshDirty() {
  dirtyCount = 0;
  for (const field of model.fields) {
    const changed = read(field) !== model.config[field.key];
    document.getElementById(`${field.key}-field`).classList.toggle("changed", changed);
    if (changed) dirtyCount += 1;
  }
  document.querySelector("#dirty-state").textContent = dirtyCount ? `${dirtyCount} 项未保存` : "已保存";
  document.querySelector("#change-summary").textContent = dirtyCount
    ? "当前策略不会被覆盖；请保存为一条新策略。"
    : "已有计划沿用各自的参数快照。";
  save.disabled = saving || !dirtyCount;
  reload.disabled = saving || !dirtyCount;
}

function fillValues(values) {
  for (const field of model.fields) {
    if (field.kind === "fixed") continue;
    const input = document.getElementById(field.key);
    if (field.kind === "boolean") input.checked = values[field.key];
    else input.value = display(values[field.key], field);
  }
  clearErrors();
  refreshDirty();
}

function render(payload) {
  model = payload;
  nav.replaceChildren();
  groups.replaceChildren();
  model.groups.forEach((group, index) => {
    const link = node("a");
    link.href = `#${group.id}`;
    link.append(node("span", String(index + 1).padStart(2, "0")), node("b", group.title));
    link.addEventListener("click", (event) => {
      event.preventDefault();
      selectGroup(group.id);
    });
    nav.append(link);
    const section = node("section", undefined, "settings-group");
    section.id = group.id;
    section.setAttribute("aria-labelledby", `${group.id}-title`);
    const title = node("h2", group.title);
    title.id = `${group.id}-title`;
    section.append(title, node("p", group.description));
    const grid = node("div", undefined, "settings-fields");
    model.fields.filter((field) => field.group === group.id).forEach((field) => {
      const container = node("div", undefined, "setting-field");
      container.id = `${field.key}-field`;
      const label = node("label", field.label);
      label.htmlFor = field.key;
      container.append(label);
      if (field.kind === "fixed") {
        const output = node("output", typeof model.config[field.key] === "boolean" ? "已启用 · 固定约束" : model.config[field.key]);
        output.id = field.key;
        container.append(output);
      } else {
        const wrap = node("div", undefined, "setting-input");
        const input = node("input");
        input.id = input.name = field.key;
        input.type = field.kind === "boolean" ? "checkbox" : field.kind === "schedule" ? "text" : field.kind;
        input.required = field.kind !== "boolean";
        input.autocomplete = "off";
        input.setAttribute("aria-describedby", `${field.key}-note ${field.key}-error`);
        if (field.kind === "number") {
          input.min = field.min / field.scale;
          input.max = field.max / field.scale;
          input.step = field.integer ? "1" : "any";
        }
        if (field.kind === "schedule") input.placeholder = "16:30,23:30";
        wrap.append(input, node("span", field.kind === "boolean" ? "启用此条件" : field.unit));
        container.append(wrap);
      }
      const defaultText = field.kind === "fixed" ? "数据源与交易制度保持一致。" :
        `默认 ${field.kind === "boolean" ? (model.defaults[field.key] ? "开启" : "关闭") : `${display(model.defaults[field.key], field)}${field.unit ? ` ${field.unit}` : ""}`}`;
      const note = node("small", `${defaultText}${field.note ? ` · ${field.note}` : ""}`, "setting-note");
      note.id = `${field.key}-note`;
      const error = node("small", "", "setting-error");
      error.id = `${field.key}-error`;
      container.append(note, error);
      grid.append(container);
    });
    section.append(grid);
    groups.append(section);
  });
  document.querySelector("#config-revision").textContent = model.revision.slice(0, 12);
  form.hidden = recordsVisible;
  fillValues(model.config);
  selectGroup(location.hash.slice(1), false);
}

async function load() {
  const selectedId = strategyId;
  retry.hidden = true;
  message.textContent = "正在读取策略参数…";
  try {
    const response = await fetch(scopedConfig(), { cache: "no-store" });
    const payload = await response.json();
    if (selectedId !== strategyId) return;
    if (!response.ok) throw new Error(payload.error?.message || "读取失败");
    render(payload);
    message.textContent = "参数已加载。运行设置自动生效，选股及入场规则用于新生成的计划。";
  } catch (error) {
    message.textContent = `读取失败：${error.message}`;
    retry.hidden = false;
  }
}

form.addEventListener("input", () => {
  clearErrors();
  refreshDirty();
  message.textContent = dirtyCount ? "修改尚未保存。" : "参数与已保存版本一致。";
});
defaults.addEventListener("click", () => {
  fillValues(model.defaults);
  message.textContent = "默认值已填入表单，保存时会创建新策略。";
});
reload.addEventListener("click", () => {
  fillValues(model.config);
  message.textContent = "已撤销本页修改。";
});
retry.addEventListener("click", load);
window.addEventListener("hashchange", () => { if (model) selectGroup(location.hash.slice(1), false); });
window.addEventListener("beforeunload", (event) => {
  if (dirtyCount) { event.preventDefault(); event.returnValue = ""; }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (saving || !dirtyCount) return;
  clearErrors();
  const config = {};
  const invalid = {};
  for (const field of model.fields) {
    const value = read(field);
    config[field.key] = value;
    if (field.kind === "number" && (value === null || value < field.min || value > field.max || (field.integer && !Number.isInteger(value)))) {
      invalid[field.key] = `请输入 ${display(field.min, field)} 至 ${display(field.max, field)} 之间的${field.integer ? "整数" : "数值"}`;
    } else if (["text", "time", "schedule"].includes(field.kind) && !value) {
      invalid[field.key] = "此项不能为空";
    }
  }
  if (Object.keys(invalid).length) {
    markErrors(invalid, "有参数未通过校验，请修正后保存。");
    return;
  }
  pendingConfig = config;
  const current = strategies.find(item => item.strategy_id === strategyId);
  document.querySelector("#new-strategy-name").value = `${current.name} - 新版本`;
  document.querySelector("#activate-new-strategy").checked = false;
  saveDialog.showModal();
});

document.querySelector("#records-tab").onclick = () => showRecords(true);
document.querySelector("#parameters-tab").onclick = () => showRecords(false);
document.querySelector("#reload-days").onclick = () => loadDays().catch(showError);
document.querySelector("#more-days").onclick = () => loadDays(true).catch(showError);
selector.onchange = async () => {
  if (dirtyCount || saving) {
    selector.value = strategyId;
    message.textContent = "请先保存或撤销参数修改，再切换策略。";
    return;
  }
  strategyId = selector.value;
  document.querySelector("#day-detail").hidden = true;
  try { await loadCatalog(); await load(); await loadDays(); } catch (error) { showError(error); }
};

async function mutate(action) {
  if (dirtyCount || saving) { message.textContent = "请先保存或撤销参数修改。"; return; }
  const buttons = document.querySelectorAll(".catalog-buttons button");
  buttons.forEach(button => { button.disabled = true; });
  try {
    const current = strategies.find(item => item.strategy_id === strategyId);
    if (action === "copy") {
      pendingConfig = {...model.config};
      document.querySelector("#new-strategy-name").value = `${current.name} - 副本`;
      document.querySelector("#activate-new-strategy").checked = false;
      saveDialog.showModal();
      return;
    } else if (action === "archive") {
      if (!confirm(`确认删除策略“${current.name}”？历史记录和来源关系会保留。`)) return;
      await api(`/api/v1/strategies/${strategyId}`, {method: "DELETE"});
      strategyId = null;
    } else {
      const payload = {enabled: !current.enabled};
      await api(`/api/v1/strategies/${strategyId}`, {
        method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload),
      });
    }
    await loadCatalog(); await load(); await loadDays();
    document.querySelector("#day-detail").hidden = true;
    message.textContent = action === "archive" ? "策略已删除。" : "策略状态已更新。";
  } catch (error) { showError(error); }
  finally {
    buttons.forEach(button => { button.disabled = false; });
  }
}
["toggle", "copy", "archive"].forEach(action => {
  document.querySelector(`#${action}-strategy`).onclick = () => mutate(action);
});

document.querySelector("#cancel-save-strategy").onclick = () => saveDialog.close();
document.querySelector("#save-strategy-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!pendingConfig || saving) return;
  saving = true;
  try {
    const result = await api("/api/v1/strategies", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name: document.querySelector("#new-strategy-name").value.trim(),
        parent_strategy_id: strategyId,
        config: pendingConfig,
        revision: model.revision,
        activate: document.querySelector("#activate-new-strategy").checked,
      }),
    });
    saveDialog.close();
    pendingConfig = null;
    strategyId = result.strategy_id;
    await loadCatalog(); await load(); await loadDays();
    message.textContent = result.enabled
      ? "新策略已保存并激活，原激活策略已转为未激活。"
      : "新策略已保存为未激活状态。";
  } catch (error) {
    message.textContent = `保存失败：${error.message}`;
  } finally {
    saving = false;
  }
};
(async () => {
  try { await loadCatalog(); await load(); await loadDays(); }
  catch (error) {
    // The file-only legacy server still supports its original parameter editor.
    const health = await fetch("/api/health").then(r => r.ok ? r.json() : null).catch(() => null);
    if (health?.status === "ok") {
      strategyId = null;
      document.querySelector(".catalog-toolbar").hidden = true;
      document.querySelector(".catalog-tabs").hidden = true;
      showRecords(false);
      await load();
      message.textContent = "本地文件模式仅支持默认策略参数；多策略管理请使用 API 服务。";
    } else showError(error);
  }
})();

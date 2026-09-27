const byId = (id) => document.getElementById(id);
let storedItems = [];

function setStatus(message, error = false) {
  const target = byId("settings-status");
  target.textContent = message;
  target.classList.toggle("is-error", error);
}

async function request(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error?.message || `请求失败 ${response.status}`);
  }
  return payload;
}

function field(className, value, maximum, placeholder) {
  const input = document.createElement("input");
  input.className = `ui-control ${className}`;
  input.value = value;
  input.maxLength = maximum;
  input.placeholder = placeholder;
  input.required = className === "board-name";
  return input;
}

function createRow(item = {}, position = 0) {
  const row = document.createElement("div");
  row.className = "board-row";
  row.dataset.id = item.id || "";

  const order = document.createElement("span");
  order.className = "board-order";
  order.textContent = String(position + 1).padStart(2, "0");

  const name = field("board-name", item.name || "", 40, "板块名称");
  name.setAttribute("aria-label", "板块名称");
  const description = field(
    "board-description",
    item.description || "",
    200,
    "备注",
  );
  description.setAttribute("aria-label", "板块备注");

  const activeLabel = document.createElement("label");
  activeLabel.className = "board-active";
  const active = document.createElement("input");
  active.type = "checkbox";
  active.checked = item.active !== false;
  activeLabel.append(active, document.createTextNode("启用"));

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "ui-button danger-button";
  remove.textContent = "×";
  remove.title = "删除新板块";
  remove.setAttribute("aria-label", "删除新板块");
  remove.addEventListener("click", () => {
    row.remove();
    renumber();
    renderEmptyState();
    setStatus("有未保存的修改");
  });
  for (const control of [name, description, active]) {
    control.addEventListener("input", () => setStatus("有未保存的修改"));
    control.addEventListener("change", () => setStatus("有未保存的修改"));
  }
  row.append(order, name, description, activeLabel, remove);
  return row;
}

function renumber() {
  document.querySelectorAll(".board-row").forEach((row, index) => {
    row.querySelector(".board-order").textContent = String(index + 1).padStart(2, "0");
  });
}

function renderEmptyState() {
  const root = byId("board-list");
  const current = root.querySelector(".board-empty");
  const hasRows = Boolean(root.querySelector(".board-row"));
  if (hasRows && current) current.remove();
  if (!hasRows && !current) {
    const empty = document.createElement("p");
    empty.className = "board-empty";
    empty.textContent = "暂无新板块参数";
    root.append(empty);
  }
}

function render(items) {
  const root = byId("board-list");
  root.replaceChildren();
  items.forEach((item, index) => root.append(createRow(item, index)));
  renderEmptyState();
}

function collect() {
  return [...document.querySelectorAll(".board-row")].map((row, index) => ({
    id: row.dataset.id || null,
    name: row.querySelector(".board-name").value.trim(),
    description: row.querySelector(".board-description").value.trim(),
    sort_order: index,
    active: row.querySelector(".board-active input").checked,
  }));
}

async function load() {
  try {
    const payload = await request("/api/v1/settings/new-boards");
    storedItems = payload.items;
    render(storedItems);
    setStatus(`已读取 ${storedItems.length} 个新板块参数`);
  } catch (error) {
    setStatus(error.message, true);
  }
}

byId("add-board").addEventListener("click", () => {
  const root = byId("board-list");
  root.querySelector(".board-empty")?.remove();
  const row = createRow({}, root.querySelectorAll(".board-row").length);
  root.append(row);
  row.querySelector(".board-name").focus();
  setStatus("有未保存的修改");
});

byId("reset-boards").addEventListener("click", () => {
  render(storedItems);
  setStatus("已撤销未保存的修改");
});

byId("save-boards").addEventListener("click", async () => {
  const items = collect();
  if (items.some((item) => !item.name)) {
    setStatus("新板块名称不能为空", true);
    return;
  }
  const button = byId("save-boards");
  button.disabled = true;
  setStatus("正在保存…");
  try {
    const payload = await request("/api/v1/settings/new-boards", {
      method: "PUT",
      body: JSON.stringify({ items }),
    });
    storedItems = payload.items;
    render(storedItems);
    setStatus(`已保存 ${storedItems.length} 个新板块参数`);
  } catch (error) {
    setStatus(error.message, true);
  } finally {
    button.disabled = false;
  }
});

load();

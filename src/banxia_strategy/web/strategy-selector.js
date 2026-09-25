"use strict";
window.strategyURL = function(path) {
  const url = new URL(path, location.origin);
  const params = new URLSearchParams(location.search);
  const strategy = params.get("strategy_id");
  if (strategy) url.searchParams.set("strategy_id", strategy);
  if (url.pathname.startsWith("/api/v1/monitor") && params.get("trade_date")) {
    url.searchParams.set("trade_date", params.get("trade_date"));
  }
  return url.pathname + url.search;
};
window.strategyReady = (async () => {
  const target = document.querySelector("#page-strategy");
  if (!target) return;
  try {
    const response = await fetch("/api/v1/strategies", {cache: "no-store"});
    if (!response.ok) throw new Error("策略列表不可用");
    const payload = await response.json();
    const params = new URLSearchParams(location.search);
    const selected = params.get("strategy_id") || payload.items.find(item => item.enabled)?.strategy_id
      || payload.items[0]?.strategy_id;
    target.replaceChildren(...payload.items.map(item => {
      const option = document.createElement("option");
      option.value = item.strategy_id;
      option.textContent = `${item.name} · ${item.enabled ? "激活" : "未激活"}`;
      return option;
    }));
    target.value = selected;
    if (selected) {
      params.set("strategy_id", selected);
      history.replaceState(null, "", `${location.pathname}?${params}`);
      const scopedNavPaths = new Set(["/", "/monitor"]);
      document.querySelectorAll(".primary-nav a").forEach(link => {
        if (!scopedNavPaths.has(link.pathname)) return;
        link.href = `${link.pathname}?strategy_id=${encodeURIComponent(selected)}`;
      });
    }
    target.onchange = () => {
      params.set("strategy_id", target.value);
      location.href = `${location.pathname}?${params}`;
    };
  } catch (error) {
    const option = document.createElement("option");
    option.textContent = error.message;
    target.replaceChildren(option);
  }
})();

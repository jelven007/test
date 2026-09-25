"use strict";

let calendarRequest;

function fetchTradingCalendar() {
  if (!calendarRequest) {
    calendarRequest = fetch(window.strategyURL("/api/v1/trading-calendar"), {
      cache: "no-store",
    }).then(async (response) => {
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(
          payload.error?.message || `交易日历请求失败（${response.status}）`,
        );
      }
      return payload;
    });
  }
  return calendarRequest;
}

function updateTradeDateInUrl(value) {
  const params = new URLSearchParams(location.search);
  params.set("trade_date", value);
  history.replaceState(null, "", `${location.pathname}?${params}`);
}

function resolveSelectedDate(sessions, requested, fallback) {
  if (sessions.includes(requested)) return requested;
  if (requested && /^\d{4}-\d{2}-\d{2}$/.test(requested)) {
    const previous = [...sessions].reverse().find(
      (session) => session < requested,
    );
    if (previous) return previous;
  }
  return sessions.includes(fallback) ? fallback : sessions.at(-1);
}

window.initializeTradingDateControl = async function(
  control,
  { defaultKey },
) {
  const calendar = await fetchTradingCalendar();
  const maxDate = defaultKey === "next_plan"
    ? calendar.defaults.next_plan
    : calendar.defaults.monitor;
  const sessions = calendar.sessions.filter(
    (session) => !maxDate || session <= maxDate,
  );
  if (!sessions.length) {
    throw new Error("交易日历中没有可选交易日");
  }

  control.replaceChildren(...sessions.map((session) => {
    const option = document.createElement("option");
    option.value = session;
    option.textContent = session;
    return option;
  }));

  const requested = new URLSearchParams(location.search).get("trade_date");
  const selected = resolveSelectedDate(
    sessions,
    requested,
    calendar.defaults[defaultKey],
  );
  control.value = selected;
  control.disabled = false;
  updateTradeDateInUrl(selected);

  control.addEventListener("keydown", (event) => {
    if (
      !["ArrowLeft", "ArrowRight"].includes(event.key)
      || event.altKey
      || event.ctrlKey
      || event.metaKey
    ) {
      return;
    }
    event.preventDefault();
    const offset = event.key === "ArrowLeft" ? -1 : 1;
    const nextIndex = Math.max(
      0,
      Math.min(control.options.length - 1, control.selectedIndex + offset),
    );
    if (nextIndex === control.selectedIndex) return;
    control.selectedIndex = nextIndex;
    control.dispatchEvent(new Event("change", { bubbles: true }));
  });

  return {
    ...calendar,
    value: selected,
  };
};

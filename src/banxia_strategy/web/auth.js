(() => {
  const nativeFetch = window.fetch.bind(window);

  function cookie(name) {
    const prefix = `${encodeURIComponent(name)}=`;
    const part = document.cookie
      .split(";")
      .map((item) => item.trim())
      .find((item) => item.startsWith(prefix));
    return part ? decodeURIComponent(part.slice(prefix.length)) : "";
  }

  function isSameOrigin(input) {
    const value = typeof input === "string" ? input : input.url;
    return new URL(value, location.href).origin === location.origin;
  }

  window.fetch = async (input, init = {}) => {
    const options = { ...init, credentials: init.credentials || "same-origin" };
    const method = String(
      options.method || (typeof input !== "string" && input.method) || "GET",
    ).toUpperCase();
    if (isSameOrigin(input) && !["GET", "HEAD", "OPTIONS"].includes(method)) {
      const headers = new Headers(options.headers || (
        typeof input !== "string" ? input.headers : undefined
      ));
      const csrf = cookie("banxia_csrf");
      if (csrf) headers.set("X-CSRF-Token", csrf);
      options.headers = headers;
    }
    const response = await nativeFetch(input, options);
    if (
      response.status === 401
      && location.pathname !== "/login"
      && isSameOrigin(input)
    ) {
      location.assign(`/login?next=${encodeURIComponent(location.pathname + location.search)}`);
    }
    return response;
  };

  async function mountAccount() {
    const topbar = document.querySelector(".topbar, .monitor-header");
    if (!topbar || location.pathname === "/login") return;
    try {
      const response = await window.fetch("/api/v1/auth/me", {
        cache: "no-store",
      });
      if (!response.ok) return;
      const payload = await response.json();
      if (!payload.authenticated || !payload.user) return;

      const currentSource = topbar.querySelector(
        ".app-source, .settings-source, .research-source",
      );
      if (currentSource) currentSource.hidden = true;

      const account = document.createElement("div");
      account.className = "account-tools";
      const email = document.createElement("span");
      email.textContent = payload.user.email;
      email.title = payload.user.email;
      const logout = document.createElement("button");
      logout.type = "button";
      logout.className = "account-logout";
      logout.textContent = "退出";
      logout.addEventListener("click", async () => {
        logout.disabled = true;
        try {
          await window.fetch("/api/v1/auth/logout", { method: "POST" });
        } finally {
          location.assign("/login");
        }
      });
      account.append(email, logout);
      const accountSlot = topbar.querySelector("[data-account-slot]");
      (accountSlot || topbar).append(account);
    } catch (_error) {
      // Authentication redirects are handled by the shared fetch wrapper.
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountAccount);
  } else {
    mountAccount();
  }
})();

const byId = (id) => document.getElementById(id);
let challengeId = "";
let countdownTimer = null;

function destination() {
  const value = new URLSearchParams(location.search).get("next") || "/";
  return value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

function status(message, error = false) {
  const target = byId("auth-status");
  target.textContent = message;
  target.classList.toggle("error", error);
}

async function request(url, body) {
  const response = await fetch(url, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error?.message || `请求失败 ${response.status}`);
  }
  return payload;
}

function selectTab(name) {
  const login = name === "login";
  byId("login-tab").setAttribute("aria-selected", String(login));
  byId("register-tab").setAttribute("aria-selected", String(!login));
  byId("login-form").hidden = !login;
  byId("register-form").hidden = login;
  status("");
}

function startCountdown(seconds) {
  const button = byId("send-code");
  let remaining = seconds;
  clearInterval(countdownTimer);
  button.disabled = true;
  button.textContent = `${remaining}s`;
  countdownTimer = setInterval(() => {
    remaining -= 1;
    button.textContent = remaining > 0 ? `${remaining}s` : "重新发送";
    if (remaining <= 0) {
      clearInterval(countdownTimer);
      button.disabled = false;
    }
  }, 1000);
}

byId("login-tab").addEventListener("click", () => selectTab("login"));
byId("register-tab").addEventListener("click", () => selectTab("register"));

byId("send-code").addEventListener("click", async () => {
  const button = byId("send-code");
  const email = byId("register-email").value.trim();
  if (!byId("register-email").reportValidity()) return;
  button.disabled = true;
  status("正在发送验证码…");
  try {
    const payload = await request("/api/v1/auth/request-code", { email });
    challengeId = payload.challenge_id;
    status("验证码已发送，请查收邮箱。");
    startCountdown(60);
    byId("register-code").focus();
  } catch (error) {
    button.disabled = false;
    status(error.message, true);
  }
});

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  status("正在登录…");
  try {
    await request("/api/v1/auth/login", {
      email: byId("login-email").value.trim(),
      password: byId("login-password").value,
    });
    location.assign(destination());
  } catch (error) {
    status(error.message, true);
    button.disabled = false;
  }
});

byId("register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!challengeId) {
    status("请先获取邮箱验证码。", true);
    return;
  }
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  status("正在创建账号…");
  try {
    await request("/api/v1/auth/register", {
      challenge_id: challengeId,
      email: byId("register-email").value.trim(),
      code: byId("register-code").value.trim(),
      password: byId("register-password").value,
    });
    location.assign(destination());
  } catch (error) {
    status(error.message, true);
    button.disabled = false;
  }
});

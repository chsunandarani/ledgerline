const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
let token = null, me = null, accounts = [];

function store(k, v) { try { v === null ? sessionStorage.removeItem(k) : sessionStorage.setItem(k, v); } catch (e) {} }
function load(k) { try { return sessionStorage.getItem(k); } catch (e) { return null; } }

// Build DOM with textContent only: names and memos come from users, never innerHTML them.
function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v; else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else n.setAttribute(k, v);
  }
  for (const k of kids.flat()) n.append(k instanceof Node ? k : document.createTextNode(k ?? ""));
  return n;
}

const rupees = p => (p < 0 ? "-" : "") + "Rs " + (Math.abs(p) / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 });

// Rupee string -> integer paise without ever going through a float.
function toPaise(s) {
  s = String(s).trim().replace(/,/g, "");
  const m = /^(\d{1,9})(?:\.(\d{1,2}))?$/.exec(s);
  if (!m) throw new Error("Amount must be rupees with at most 2 decimals, e.g. 1250.50");
  return parseInt(m[1], 10) * 100 + parseInt((m[2] || "0").padEnd(2, "0"), 10);
}

async function api(method, path, body, idemKey) {
  const headers = { "Content-Type": "application/json" };
  if (token) headers.Authorization = "Bearer " + token;
  if (idemKey) headers["Idempotency-Key"] = idemKey;
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  let data = null; try { data = await r.json(); } catch (e) {}
  if (r.status === 401 && path !== "/api/auth/login") { logout(); }
  return { status: r.status, data, replayed: r.headers.get("Idempotent-Replayed") === "true" };
}

function newKey() { return "ui-" + crypto.randomUUID(); }

async function start() {
  const res = await api("GET", "/api/me");
  if (res.status !== 200) return logout();
  me = res.data;
  $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); $("#who").classList.remove("hidden");
  $("#whoName").textContent = me.username; $("#whoRole").textContent = me.role;
  $$("[data-roles]").forEach(n => n.classList.toggle("hidden", !n.dataset.roles.split(" ").includes(me.role)));
  $$("form[data-money]").forEach(f => resetKey(f));
  await refresh();
}

async function refresh() {
  const res = await api("GET", "/api/accounts");
  accounts = res.data || [];
  const t = $("#accounts"); t.replaceChildren(el("tr", {}, el("th", {}, "Id"), el("th", {}, "Name"), el("th", {}, "Owner"),
    el("th", {}, "Status"), el("th", { class: "num" }, "Balance"), el("th", { class: "num" }, "Overdraft limit")));
  for (const a of accounts) {
    t.append(el("tr", { class: "clickable", onclick: () => showStatement(a.id) },
      el("td", {}, String(a.id)), el("td", {}, a.name), el("td", {}, a.owner_username || "system"),
      el("td", {}, a.status), el("td", { class: "num" }, rupees(a.balance)),
      el("td", { class: "num" }, a.kind === "customer" ? rupees(a.overdraft_limit) : "-")));
  }
  const customer = accounts.filter(a => a.kind === "customer");
  $$("select[data-own-accounts]").forEach(s => {
    const keep = s.value;
    s.replaceChildren(...customer.map(a => el("option", { value: a.id }, `#${a.id} ${a.name} (${rupees(a.balance)})`)));
    if (keep) s.value = keep;
  });
}

async function showStatement(id) {
  const res = await api("GET", `/api/accounts/${id}/statement`);
  if (res.status !== 200) return;
  $("#statementBox").classList.remove("hidden");
  $("#statementTitle").textContent = `Statement - #${id} ${res.data.account.name}`;
  const t = $("#statement");
  t.replaceChildren(el("tr", {}, el("th", {}, "When (UTC)"), el("th", {}, "Txn"), el("th", {}, "What"),
    el("th", {}, "By"), el("th", { class: "num" }, "Amount"), el("th", { class: "num" }, "Balance"), el("th", {}, "Memo / reason")));
  for (const e of res.data.entries.slice().reverse()) {
    const amt = e.type === "refused" ? `(${rupees(e.amount)})` : rupees(e.signed_amount);
    t.append(el("tr", {},
      el("td", {}, e.created_at.replace("T", " ").slice(0, 19)), el("td", {}, String(e.transaction_id)),
      el("td", { class: e.type }, `${e.type} - ${e.kind}`), el("td", {}, e.initiated_by),
      el("td", { class: "num " + e.type }, amt), el("td", { class: "num" }, rupees(e.running_balance)),
      el("td", {}, e.refusal_reason ? `refused: ${e.refusal_reason}` : e.memo)));
  }
  $("#statementBox").scrollIntoView({ behavior: "smooth" });
}

// One idempotency key per intended payment. It survives retries and double-clicks and
// is only replaced after the server has given a definite answer.
function resetKey(form) { form.dataset.key = newKey(); $(".key", form).textContent = "Idempotency-Key: " + form.dataset.key; }

function moneyBody(form) {
  const fd = Object.fromEntries(new FormData(form));
  const body = {};
  for (const [k, v] of Object.entries(fd)) {
    if (k === "memo") body.memo = v;
    else if (k === "amount") body.amount = toPaise(v);
    else body[k] = parseInt(v, 10);
  }
  return body;
}

$$("form[data-money]").forEach(form => form.addEventListener("submit", async ev => {
  ev.preventDefault();
  const msg = $(".msg", form), btn = $("button", form);
  let body;
  try { body = moneyBody(form); } catch (e) { msg.className = "msg err"; msg.textContent = e.message; return; }
  btn.disabled = true; msg.className = "msg"; msg.textContent = "Sending...";
  let res;
  try { res = await api("POST", form.dataset.money, body, form.dataset.key); }
  catch (e) {
    // Network failure: we don't know if it went through. Keep the SAME key so a retry is safe.
    msg.className = "msg err"; msg.textContent = "Network problem. Press the button again to retry safely - same key, so it cannot pay twice.";
    btn.disabled = false; return;
  }
  btn.disabled = false;
  const t = res.data && res.data.transaction;
  if (t && t.status === "posted") {
    msg.className = "msg ok"; msg.textContent = `Done. Transaction #${t.id}${res.replayed ? " (this was a repeat - nothing moved again)" : ""}.`;
    resetKey(form); form.reset(); await refresh();
  } else if (t) {
    msg.className = "msg err"; msg.textContent = `Refused: ${t.refusal_reason}. Recorded as transaction #${t.id}.`;
    resetKey(form); await refresh();
  } else if (res.status >= 500 || res.status === 503) {
    msg.className = "msg err"; msg.textContent = ((res.data && res.data.error && res.data.error.message) || "Server error") + " Press again to retry with the same key.";
  } else {
    msg.className = "msg err"; msg.textContent = (res.data && res.data.error && res.data.error.message) || `Error ${res.status}`;
    if (res.status === 409) resetKey(form);
  }
}));

$("#userForm").addEventListener("submit", async ev => {
  ev.preventDefault(); const f = ev.target, msg = $(".msg", f);
  const res = await api("POST", "/api/users", Object.fromEntries(new FormData(f)));
  msg.className = "msg " + (res.status === 201 ? "ok" : "err");
  msg.textContent = res.status === 201 ? `Created user #${res.data.id}` : res.data.error.message;
  if (res.status === 201) f.reset();
});

$("#accountForm").addEventListener("submit", async ev => {
  ev.preventDefault(); const f = ev.target, msg = $(".msg", f);
  const fd = Object.fromEntries(new FormData(f)); const body = { name: fd.name };
  if (me.role === "admin") body.owner_id = parseInt(fd.owner_id, 10);
  const res = await api("POST", "/api/accounts", body);
  msg.className = "msg " + (res.status === 201 ? "ok" : "err");
  msg.textContent = res.status === 201 ? `Opened account #${res.data.id}` : res.data.error.message;
  if (res.status === 201) { f.reset(); refresh(); }
});

$("#patchForm").addEventListener("submit", async ev => {
  ev.preventDefault(); const f = ev.target, msg = $(".msg", f);
  const fd = Object.fromEntries(new FormData(f)); const body = {};
  try {
    if (fd.status) body.status = fd.status;
    if (fd.overdraft_limit) body.overdraft_limit = toPaise(fd.overdraft_limit);
  } catch (e) { msg.className = "msg err"; msg.textContent = e.message; return; }
  const res = await api("PATCH", `/api/accounts/${parseInt(fd.id, 10)}`, body);
  msg.className = "msg " + (res.status === 200 ? "ok" : "err");
  msg.textContent = res.status === 200 ? "Saved." : res.data.error.message;
  refresh();
});

function showJSON(data) { $("#auditOut").replaceChildren(el("pre", {}, JSON.stringify(data, null, 2))); }
$("#verifyBtn").addEventListener("click", async () => showJSON((await api("GET", "/api/audit/verify")).data));
$("#flagsBtn").addEventListener("click", async () => showJSON((await api("GET", "/api/audit/flags")).data));
$("#dupesBtn").addEventListener("click", async () => showJSON((await api("GET", "/api/audit/requests?outcome=replayed")).data));
$("#eventsBtn").addEventListener("click", async () => showJSON((await api("GET", "/api/audit/admin-events")).data));

$("#loginForm").addEventListener("submit", async ev => {
  ev.preventDefault();
  const res = await api("POST", "/api/auth/login", { username: $("#u").value.trim(), password: $("#p").value });
  if (res.status !== 200) { $("#loginMsg").textContent = res.data?.error?.message || "Login failed"; return; }
  token = res.data.token; store("ll_token", token); $("#p").value = ""; start();
});

async function logout() {
  if (token) { try { await fetch("/api/auth/logout", { method: "POST", headers: { Authorization: "Bearer " + token } }); } catch (e) {} }
  token = null; me = null; store("ll_token", null);
  $("#app").classList.add("hidden"); $("#who").classList.add("hidden"); $("#login").classList.remove("hidden");
}
$("#logoutBtn").addEventListener("click", logout);

token = load("ll_token");
if (token) start();
